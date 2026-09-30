"""
Chat bot: local LLM (OpenAI-compatible, tool calling) + Claude Code CLI runner.
One backend (engines, sessions, reminders, scheduled prompts, settings), several front ends: Discord (this file)
and Telegram (llmbot/telegram.py, started when TELEGRAM_BOT_TOKEN is set). Either token alone is enough.

Run:  python -m llmbot        (from the repo root; config in .env there, see .env.example)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import discord
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from discord import app_commands
from dotenv import load_dotenv

from llmbot import __version__
from llmbot import hints as hints_mod
from llmbot import logs as logs_mod
from llmbot import store as store_mod

# =============================================================================
# Config
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent.parent  # the repo root: .env, default workspace
load_dotenv(os.getenv("LLMBOT_ENV_FILE") or BASE_DIR / ".env")
# Runtime state (settings, tasks, reminders, usage, log). Not in git.
DATA_DIR = Path(os.getenv("LLMBOT_DATA_DIR") or BASE_DIR / "data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("llmbot")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _ids(name: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", _env(name)) if x.isdigit()}


DISCORD_TOKEN = _env("DISCORD_TOKEN")
# One or more servers (comma-separated) for instant slash-command sync; empty = global commands (all servers + DMs)
GUILD_IDS = sorted(_ids("GUILD_ID") | _ids("GUILD_IDS"))
ALLOWED_USER_IDS = _ids("ALLOWED_USER_IDS")
OWNER_IDS = _ids("OWNER_IDS") or set(ALLOWED_USER_IDS)  # fallback; empty => Claude Code disabled

# Telegram front end (optional). Telegram has its own user ids, so it has its own allow-list; strangers can find
# any bot by name, so an empty TELEGRAM_ALLOWED_USER_IDS means nobody (the bot then only tells /start its user id).
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_ALLOWED_USER_IDS = _ids("TELEGRAM_ALLOWED_USER_IDS")
TELEGRAM_OWNER_IDS = _ids("TELEGRAM_OWNER_IDS") or set(TELEGRAM_ALLOWED_USER_IDS)


def is_telegram_id(x: int) -> bool:
    """Telegram user/chat ids are < 2^52 (groups negative); Discord ids are snowflakes > 10^16 (since 2015)."""
    return abs(int(x)) < 10 ** 16


# Web chat in the dashboard (llmbot/webchat.py): its chats and its one user have ids -10^17 and below, which no
# Discord id (positive) or Telegram id (abs < 10^16) can be.
WEB_ID_BASE = 10 ** 17
WEB_USER_ID = -WEB_ID_BASE
# Who the dashboard chat talks as: owner (Claude Code, /power: the access key is the PC owner's), user (local
# model only), or off.
DASHBOARD_CHAT = _env("DASHBOARD_CHAT", "owner").lower()


def is_web_id(x: int) -> bool:
    return int(x) <= -WEB_ID_BASE


LLM_URL = _env("LLM_URL", "http://localhost:11434/v1").rstrip("/")
LLM_MODEL = _env("LLM_MODEL", "qwen3.5:9b")
LLM_API_KEY = _env("LLM_API_KEY")
TIMEZONE = _env("TIMEZONE", "UTC")
TZ = ZoneInfo(TIMEZONE)
SEARXNG_URL = _env("SEARXNG_URL").rstrip("/")
SYSTEM_PROMPT = _env("SYSTEM_PROMPT") or (
    "You are a helpful assistant in a chat (Discord or Telegram). Be concise and use Discord-style markdown. "
    "Use web_search for anything current or factual you are unsure about, and fetch_page to read a specific URL. "
    "Cite sources as links when you used the web."
)
# Stays the same between requests so Ollama reuses its cached prompt (system + tools + history); the current time
# goes at the top of each user message instead. Any change in the prefix makes it re-read the whole chat.
TIME_NOTE = "Each user message starts with the local time it was sent, as [Now: ...]."
OLLAMA_URL = _env("OLLAMA_URL", "http://localhost:11434").rstrip("/")  # used by the Claude Code 'ollama' backend
# Start `ollama serve` if nothing answers on this PC's Ollama port when the bot starts. Needed when the bot starts at
# boot (bot_control.ps1 boot): Ollama's own app only starts once someone signs in.
OLLAMA_AUTOSTART = _env("OLLAMA_AUTOSTART", "true").lower() not in ("0", "false", "no", "off")

CLAUDE_BIN = _env("CLAUDE_BIN")
DEFAULT_ENGINE = _env("DEFAULT_ENGINE", "local").lower()
CC_BACKEND = _env("CC_BACKEND", "anthropic").lower()
CC_MODEL = _env("CC_MODEL", "sonnet")
CC_PERMISSION = _env("CC_PERMISSION", "edit").lower()
CLAUDE_TIMEOUT = int(_env("CLAUDE_TIMEOUT", "900") or 900)
# /ask, @mentions and /<BOT_COMMAND> continue the channel's Claude Code session instead of starting a new one
CC_CONTINUE = _env("CC_CONTINUE", "true").lower() in ("1", "true", "yes", "on")
# Extra slash command named after your bot, e.g. BOT_COMMAND=jarvis gives /jarvis <prompt>
BOT_COMMAND = _env("BOT_COMMAND").lower()
# Unload Ollama models the bot used after this many idle seconds (0 = never; leave it to Ollama)
LLM_IDLE_UNLOAD = int(_env("LLM_IDLE_UNLOAD", "600") or 0)

CC_CUSTOM_BASE_URL = _env("CC_CUSTOM_BASE_URL").rstrip("/")
CC_CUSTOM_TOKEN = _env("CC_CUSTOM_TOKEN")
CC_CUSTOM_MODELS = [m.strip() for m in _env("CC_CUSTOM_MODELS").split(",") if m.strip()]

# Plain local engine memory: recent exchanges resent with each message, capped by count and by size so a few long
# answers can't push the prompt past the model's context window (Ollama then silently drops its start).
MAX_HISTORY_TURNS = int(_env("LLM_HISTORY_TURNS", "20") or 20)
HISTORY_MAX_CHARS = int(_env("LLM_HISTORY_CHARS", "40000") or 40000)  # ~11k tokens: a third of a 32k window
MAX_TOOL_ROUNDS = 6
FETCH_MAX_CHARS = 6000
FETCH_MAX_BYTES = 3_000_000
TASK_MIN_INTERVAL = timedelta(minutes=15)
TASK_MAX = 20
PROGRESS_EDIT_EVERY = 2.5
VIEW_TIMEOUT = 24 * 3600

SETTINGS_FILE = DATA_DIR / "settings.json"
TASKS_FILE = DATA_DIR / "tasks.json"

# ---- Cost controls (Claude Code) --------------------------------------------
# Per-job spend cap (Anthropic backend; CLI --max-budget-usd). 0 = no cap.
CC_MAX_BUDGET_USD = float(_env("CC_MAX_BUDGET_USD", "1.0") or 0)
# Daily cap across all jobs (API-equivalent USD, Anthropic backend). New jobs are refused once reached. 0 = no cap.
CC_DAILY_BUDGET_USD = float(_env("CC_DAILY_BUDGET_USD", "5.0") or 0)
# Reasoning effort: low | medium | high | xhigh | max. Empty = CLI default. "low" is cheapest for chat.
CC_EFFORT = _env("CC_EFFORT").lower()
# Load your MCP servers, skills and plugins into Discord jobs. Off by default: they add ~24k tokens to every call.
CC_EXTRAS = _env("CC_EXTRAS", "false").lower() in ("1", "true", "yes", "on")
# Start a fresh Claude Code session when the channel's last one has been idle this long (minutes). Claude Code's
# prompt cache lasts 1 hour; after that, resuming re-sends the whole history at cache-write price. 0 = never reset.
CC_SESSION_IDLE_MINUTES = float(_env("CC_SESSION_IDLE_MINUTES", "60") or 0)
# Suggest compacting once a session's context passes this many tokens (a fresh lean session is ~11k).
# Every message re-reads the whole context, so cost per message grows with it. 0 = never suggest.
CC_COMPACT_HINT_TOKENS = int(_env("CC_COMPACT_HINT_TOKENS", "50000") or 0)
# Default reply style for /ask, /<BOT_COMMAND> and @mentions (changeable per channel in /panel → ⚙️):
#   chat  = plain messages + typing indicator + one small stats line, like a person
#   cards = embeds with live progress, Stop / Follow up / Retry / Full log / Compact buttons
REPLY_STYLE = _env("REPLY_STYLE", "chat").lower()
if REPLY_STYLE not in ("chat", "cards"):
    REPLY_STYLE = "chat"
STYLES = {"chat": ("💬", "Chat", "Plain replies like a person, one small stats line"),
          "cards": ("🗂️", "Cards", "Embeds with live progress and buttons")}

# ---- Voice messages (Discord voice notes -> local Whisper -> normal prompt) --------------------------------
VOICE_ENABLED = _env("VOICE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
WHISPER_MODEL = _env("WHISPER_MODEL", "small")  # tiny | base | small | medium | large-v3 (bigger = better, slower)
WHISPER_DEVICE = _env("WHISPER_DEVICE", "cpu")  # cpu keeps the GPU free for Ollama; "cuda" is faster
WHISPER_COMPUTE = _env("WHISPER_COMPUTE", "int8")  # int8 on CPU; float16 on cuda
WHISPER_LANGUAGE = _env("WHISPER_LANGUAGE").lower() or None  # e.g. "en", "hi"; empty = auto-detect
VOICE_MAX_SECONDS = int(_env("VOICE_MAX_SECONDS", "180") or 180)
# Per channel (changeable in /panel → ⚙️): which voice notes to answer. Voice notes can't @mention anyone.
VOICE_MODES = {"all": ("🎙️", "Voice: answer all", "Every voice note from an allowed user in this channel"),
               "replies": ("↩️", "Voice: replies only", "Only voice notes sent as a reply to the bot"),
               "off": ("🔇", "Voice: off", "Ignore voice notes here")}
VOICE_DEFAULT = _env("VOICE_DEFAULT", "all").lower()
if VOICE_DEFAULT not in VOICE_MODES:
    VOICE_DEFAULT = "all"
LEAN_ARGS = ["--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections"]
USAGE_FILE = DATA_DIR / "usage.json"
# Appended to Claude Code's system prompt. Without it, Claude Code sees itself as a coding tool and may refuse
# general questions ("I can't check live weather") even though WebSearch is available. Static text, so it caches.
CC_SYSTEM_APPEND = _env("CC_SYSTEM_APPEND") or (
    "You are replying to the user through a chat bot (Discord or Telegram), so your final message is posted in "
    "that chat. "
    "You are also a general assistant, not only a coding tool: for anything current or factual (weather, news, "
    "prices, sports, docs), use your web search and web fetch tools instead of saying you lack real-time access, and "
    "cite sources as links. Keep replies concise and use Discord-style markdown, which the bot converts for "
    "Telegram (no wide tables). The user cannot see "
    "files in your working directory: when asked to share or show a file, paste its contents in a code block "
    "(summarise if it is very long). You CAN send files from the working directory as chat attachments: put a "
    "line [[attach: relative/path]] in your final message for each file (up to 9 files, 10 MB each); the bot "
    "uploads them and removes the marker. You CAN set one-time reminders: put a line [[remind: WHEN | TEXT]] in "
    "your final message, where WHEN is like 'in 10 min', 'in 2 hours', '18:30', 'tomorrow 9am' or "
    "'YYYY-MM-DD HH:MM' (the user's local time) and TEXT is what to tell them; the bot pings them in this channel "
    "at that time, removes the marker and shows the confirmed time, so just acknowledge briefly. For repeating "
    "reminders, or anything that must be looked up or done at that time (news, tweets, weather, prices, checks), "
    "schedule yourself instead: [[task: WHEN | PROMPT]] runs PROMPT once at WHEN (same formats as reminders) and "
    "[[task: cron MIN HOUR DAY MONTH WEEKDAY | PROMPT]] runs it repeatedly (5-field cron in the user's timezone, "
    "0=Sunday, at most every 15 minutes). At that time a fresh session of you, read-only unless an owner allows more "
    "(web search/fetch, reading files), runs PROMPT with no memory of this chat and your answer is posted here, pinging the user, so "
    "write PROMPT fully self-contained (names, handles, links, what to report and how briefly). Don't ask for "
    "confirmation first: the bot shows what was scheduled and /tasks cancels it. To delete files from the working directory "
    "(edit or full permission), put a line [[delete: relative/path]] in your final message; the bot deletes them "
    "after uploading any attachments, so 'send it then delete it' works. Facts about your setup: your working "
    "directory is a separate workspace folder, NOT this bot's source code, which you cannot see. The bot silently "
    "ignores users who are not in its allow-list (ALLOWED_USER_IDS for Discord, TELEGRAM_ALLOWED_USER_IDS for "
    "Telegram, in the bot's .env), so that is the likely "
    "reason it doesn't answer someone. Without full permission you cannot run commands or code; if a request needs "
    "that (e.g. truly random numbers, running scripts), do your best and mention that the owner can switch on full "
    "access in /panel -> Settings. Don't repeat these limitations in every message. If you have tools named "
    "set_reminder, schedule_prompt, send_file or delete_file, call those instead of writing the [[...]] lines.")
# Sessions started under a different prompt/flag setup are not resumed: Claude Code keeps a session's original
# system prompt, and a changed prefix means a full (paid) cache rewrite anyway.
CC_SETUP_FINGERPRINT = hashlib.sha256(f"{CC_SYSTEM_APPEND}|{CC_EXTRAS}".encode()).hexdigest()[:12]

# ---- Secret handling ----------------------------------------------------------
# Env vars that look secret are removed from the Claude Code subprocess (except its own ANTHROPIC_/CLAUDE_ ones
# and names listed in CC_ENV_PASSTHROUGH), and their values are redacted from everything posted to Discord.
_SECRET_NAME = re.compile(r"(^|_)(TOKEN|SECRET|PASSWORD|PASSWD|PASS|APIKEY|API_KEY|KEY|PRIVATE_KEY|CREDENTIALS?|AUTH|COOKIE)(_|$)", re.I)
CC_ENV_KEEP_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
CC_ENV_PASSTHROUGH = {x.strip().upper() for x in _env("CC_ENV_PASSTHROUGH").split(",") if x.strip()}
SCRUB_ENV = {"DISCORD_TOKEN", "TELEGRAM_BOT_TOKEN", "LLM_API_KEY", "CC_CUSTOM_TOKEN", "DATABASE_URL"} | {
    k for k in os.environ if _SECRET_NAME.search(k) and not k.upper().startswith(CC_ENV_KEEP_PREFIXES)
    and k.upper() not in CC_ENV_PASSTHROUGH}

_SECRET_VALUES = sorted(
    {v for k, v in os.environ.items() if (_SECRET_NAME.search(k) or k in SCRUB_ENV) and len(v) >= 8}, key=len, reverse=True)
_HOME_VARIANTS = sorted({str(Path.home()), str(Path.home()).replace("\\", "/"), str(Path.home()).replace("\\", "\\\\")},
                        key=len, reverse=True)
_SECRET_PATTERNS = [
    (re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"), "[redacted: discord token]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), "[redacted: telegram token]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), "[redacted: anthropic key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "[redacted: api key]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"), "[redacted: github token]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[redacted: aws key]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[redacted: google key]"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "[redacted: hf token]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[redacted: slack token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted: jwt]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), "[redacted: private key]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[redacted]"),
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", re.I), r"\1[redacted]@"),
    (re.compile(r"(?i)([?&](?:key|api_?key|token|access_token|auth|secret|password|sig)=)[^&\s]+"), r"\1[redacted]"),
    (re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{10,}"), "[redacted: stripe key]"),
    # KEY=value lines (.env files, shell exports). Also matches after a Read line-number prefix ("12\t", "12→")
    # and inside JSON-escaped text ("...\nKEY=value\n..."); the value ends at a newline, a literal \n or a quote.
    (re.compile(r"(?m)((?:^|\\n|\")[ \t]*(?:\d+[ \t]*[\t→:|][ \t]*)?(?:export[ \t]+)?[A-Z0-9_]*"
                r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_KEY|CREDENTIALS?)[A-Z0-9_]*[ \t]*[=:][ \t]*)"
                r"(?:\"[^\"\n]*\"|'[^'\n]*'|\\\"(?:(?!\\\")[^\n])*\\\"|[^\s\"'\\](?:(?!\\n)[^\n\"])*)"), r"\1[redacted]"),
]


def redact(text: Any) -> str:
    """Scrub secrets and the home directory from anything that goes to Discord."""
    s = str(text or "")
    for v in _SECRET_VALUES:
        if v in s:
            s = s.replace(v, "[redacted]")
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    for h in _HOME_VARIANTS:
        s = re.sub(re.escape(h), "~", s, flags=re.I)
    return s


def _parse_workspaces(raw: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, path = (s.strip() for s in part.split("=", 1))
        if name and path:
            p = Path(os.path.expandvars(os.path.expanduser(path)))
            out[name[:50]] = (p if p.is_absolute() else BASE_DIR / p).resolve()
    return out or {"default": (BASE_DIR / "claude_workspace").resolve()}


WORKSPACES = _parse_workspaces(_env("WORKSPACES"))
for _p in WORKSPACES.values():
    _p.mkdir(parents=True, exist_ok=True)

ENGINES = {"local": ("💻", "Local"), "claude": ("🤖", "Claude Code"), "auto": ("✨", "Auto")}
PERMS = {
    "read": ("🔍", "Read-only", "Read/search files in the workspace + web. No shell."),
    "edit": ("✏️", "Edit", "Read-only + edit/create files in the workspace. No shell."),
    "full": ("⚠️", "Full access", "bypassPermissions: any command. Always confirms."),
}
BACKENDS = {
    "anthropic": ("☁️", "Anthropic", "Your normal claude login"),
    "ollama": ("🦙", "Ollama", f"Local models at {OLLAMA_URL}"),
    "custom": ("🔌", "Custom gateway", redact(CC_CUSTOM_BASE_URL) or "not configured (CC_CUSTOM_BASE_URL)"),
}
ANTHROPIC_MODELS = ["sonnet", "opus", "fable", "haiku"]

# File access is path-scoped to the workspace ("./**" = the job's cwd); verified against the CLI that reads and
# writes outside it (absolute paths, ../) are denied. No Bash in read/edit: `cat`, `git diff --no-index` and
# `git log --output` can read or write anywhere. --tools also drops unused tool schemas (fewer tokens per call).
READ_TOOLS = ["Read(./**)", "Glob(./**)", "Grep(./**)", "WebSearch", "WebFetch"]
PERM_ARGS = {
    "read": ["--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--tools", "Read,Glob,Grep,WebSearch,WebFetch", "--allowedTools", *READ_TOOLS],
    "edit": ["--permission-mode", "acceptEdits", "--permission-prompts", "none",
             "--tools", "Read,Glob,Grep,Edit,Write,WebSearch,WebFetch", "--allowedTools", *READ_TOOLS, "Edit(./**)"],
    "full": ["--permission-mode", "bypassPermissions"],
}
# Read/edit jobs can't run commands, but the workspace is theirs to write. Files Claude Code itself loads from there
# would turn one prompt-injected edit (a web page telling it what to write) into commands or standing instructions
# on every later job: .claude/settings*.json hooks, .mcp.json servers, CLAUDE.md. So those files are not writable,
# and project/local settings in the workspace aren't loaded at all (only your user settings).
SANDBOX_ARGS = ["--setting-sources", "user",
                "--disallowedTools", "Edit(./.claude/**)", "Edit(./.mcp.json)", "Edit(./CLAUDE.md)",
                "Edit(./CLAUDE.local.md)", "Edit(./**/CLAUDE.md)"]

if DEFAULT_ENGINE not in ENGINES:
    DEFAULT_ENGINE = "local"
if CC_BACKEND not in BACKENDS:
    CC_BACKEND = "anthropic"
if CC_PERMISSION not in PERMS:
    CC_PERMISSION = "edit"

# Shared runtime state (filled in setup_hook)
http: httpx.AsyncClient = None  # type: ignore[assignment]
CLAUDE_VERSION: str | None = None

# =============================================================================
# Persistence
# =============================================================================


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception:
        log.exception("Could not read %s; starting empty", path.name)
        return default


# Bookkeeping lives in memory and is saved through STORE after each change: JSON files in data/ by default, or
# Postgres when DATABASE_URL is set (llmbot/store.py). main() opens the configured store and loads everything.
DATABASE_URL = _env("DATABASE_URL")


def _num(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        log.warning("%s must be a number; using %g", name, default)
        return default


# Log files (bot.log, and jobs.jsonl without Postgres): see llmbot/logs.py
LOG_MAX_TOTAL_MB = _num("LOG_MAX_TOTAL_MB", 2048)  # everything together, archives included
LOG_FILE_MB = _num("LOG_FILE_MB", 50)  # roll over into an archive at this size
LOG_COMPRESS = _env("LOG_COMPRESS", "true").lower() not in ("0", "false", "no", "off")
LOG_KEEP_DAYS = _num("LOG_KEEP_DAYS", 0)  # 0 = only the size cap
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
logs_mod.configure(LOG_MAX_TOTAL_MB, LOG_FILE_MB, LOG_COMPRESS, LOG_KEEP_DAYS)
HISTORY_FILE = DATA_DIR / "history.json"
# Web dashboard (llmbot/dashboard.py): open http://<this PC's LAN address>:DASHBOARD_PORT from a phone on the same
# network. DASHBOARD_TOKEN is the access key; empty = one is generated and kept in data/dashboard.key.
DASHBOARD_PORT = int(_num("DASHBOARD_PORT", 8765))  # 0 = off
DASHBOARD_HOST = _env("DASHBOARD_HOST", "0.0.0.0")  # 127.0.0.1 = this PC only
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN")
# Name announced on the local network (mDNS): http://llmbot.local:8765 instead of the IP. Empty = don't announce.
DASHBOARD_NAME = _env("DASHBOARD_NAME", "llmbot")
# The same dashboard over HTTPS (self-signed certificate in data/): phones only allow the microphone (voice input in
# the chat) on https pages. 0 = off.
DASHBOARD_HTTPS_PORT = int(_num("DASHBOARD_HTTPS_PORT", 8766))
STORE: Any = store_mod.FileStore(DATA_DIR)

_settings: dict[str, dict] = {}


def default_settings() -> dict:
    return {
        "engine": DEFAULT_ENGINE,
        "local_model": LLM_MODEL,
        "cc_backend": CC_BACKEND,
        "cc_model": CC_MODEL,
        "cc_perm": CC_PERMISSION,
        "workspace": next(iter(WORKSPACES)),
        "cc_session": None,
        "cc_session_path": None,  # workspace folder the session was created in
        "cc_session_at": None,  # epoch seconds of the session's last job
        "cc_session_setup": None,  # CC_SETUP_FINGERPRINT the session was created under
        "style": REPLY_STYLE,  # chat | cards
        "voice": VOICE_DEFAULT,  # all | replies | off
        "tg_stats": "spoiler",  # Telegram only: the stats line as a tap-to-reveal spoiler, or "off"
    }


def get_settings(channel_id: int) -> dict:
    s = {**default_settings(), **_settings.get(str(channel_id), {})}
    if s["workspace"] not in WORKSPACES:
        s["workspace"] = next(iter(WORKSPACES))
    return s


def update_settings(channel_id: int, **changes) -> dict:
    s = get_settings(channel_id)
    if ("cc_backend" in changes and changes["cc_backend"] != s["cc_backend"]) or (
        "workspace" in changes and changes["workspace"] != s["workspace"]
    ):
        changes.setdefault("cc_session", None)  # session is tied to backend + workspace
    s.update(changes)
    _settings[str(channel_id)] = s
    STORE.save("settings", SETTINGS_FILE, _settings)
    return s


# =============================================================================
# Access control
# =============================================================================


def is_allowed(user_id: int) -> bool:
    if is_telegram_id(user_id):  # Telegram: only listed users, never "everyone"
        return user_id in TELEGRAM_ALLOWED_USER_IDS or user_id in TELEGRAM_OWNER_IDS
    if is_web_id(user_id):  # the dashboard's chat: whoever has its access key
        return user_id == WEB_USER_ID and DASHBOARD_CHAT in ("owner", "user")
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS or user_id in OWNER_IDS


def is_owner(user_id: int) -> bool:
    if is_web_id(user_id):
        return user_id == WEB_USER_ID and DASHBOARD_CHAT == "owner"
    return user_id in (TELEGRAM_OWNER_IDS if is_telegram_id(user_id) else OWNER_IDS)


def is_allowed_in(user_id: int, guild: Any) -> bool:
    """is_allowed for a Discord message/interaction. An empty ALLOWED_USER_IDS means "everyone in the server", not
    everyone on Discord: anyone who shares any server with the bot can DM it, so DMs are then owners-only."""
    if not is_allowed(user_id):
        return False
    return (guild is not None or is_telegram_id(user_id) or is_web_id(user_id) or bool(ALLOWED_USER_IDS)
            or is_owner(user_id))


CC_ENABLED = bool(OWNER_IDS or TELEGRAM_OWNER_IDS)


# =============================================================================
# Front ends: Discord and Telegram share everything above and below; each one owns some channel ids and can
# open a channel object (with .id, .send(), .typing()) for scheduled posts and reminders.
# =============================================================================
_frontends: list = []


def frontend_for(channel_id: int):
    return next((fe for fe in _frontends if fe.owns(channel_id)), None)


async def resolve_channel(channel_id: int):
    """Channel object for a stored channel id (reminders, scheduled tasks), or None if unavailable."""
    fe = frontend_for(channel_id)
    if fe is None:
        return None
    try:
        return await fe.get_channel(channel_id)
    except Exception as e:
        log.warning("channel %s unavailable: %s", channel_id, oneline(redact(e), 200))
        return None


def where(channel_id: int) -> str:
    """How to name a channel in lists: a Discord channel mention, or 'Telegram: <chat name>'."""
    fe = frontend_for(channel_id)
    return fe.where(channel_id) if fe else f"channel {channel_id}"


# Names of people and chats as last seen, kept across restarts: Discord and Telegram only tell the bot a name when a
# message arrives, so without this the dashboard would show "Telegram user 123456789" after every restart.
NAMES_FILE = DATA_DIR / "names.json"
_known_names: dict[str, str] = {}  # "u:<id>" / "c:<id>" -> name


def remember_name(key: str, name: str | None) -> str | None:
    if name and _known_names.get(key) != name:
        _known_names[key] = name
        STORE.save("names", NAMES_FILE, _known_names)
    return name


def chat_label(channel_id: int) -> str:
    """A readable chat name for the dashboard: '#general · My Server', 'DM with Alex', 'Telegram: Alex (private)'."""
    key = f"c:{channel_id}"
    if is_web_id(channel_id):
        return where(channel_id)
    if is_telegram_id(channel_id):
        name = (getattr(_telegram, "_chats", {}) or {}).get(channel_id)
        return remember_name(key, f"Telegram: {name}") if name else _known_names.get(key) or where(channel_id)
    ch = bot.get_channel(channel_id)
    if ch is None:
        return _known_names.get(key) or f"Discord channel {channel_id}"
    if isinstance(ch, discord.DMChannel):
        return remember_name(key, f"DM with {ch.recipient.display_name}" if ch.recipient else "Discord DM")
    guild = getattr(ch, "guild", None)
    return remember_name(key, f"#{getattr(ch, 'name', channel_id)}" + (f" · {guild.name}" if guild else ""))


def user_label(user_id: int) -> str:
    key = f"u:{user_id}"
    if is_web_id(user_id):
        return "You (dashboard)"
    if is_telegram_id(user_id):
        name = (getattr(_telegram, "_names", {}) or {}).get(user_id)
        return remember_name(key, name) if name else _known_names.get(key) or f"Telegram user {user_id}"
    u = bot.get_user(user_id)
    return remember_name(key, u.display_name) if u else _known_names.get(key) or f"Discord user {user_id}"


# =============================================================================
# Activity: what the bot is doing and has done, for the web dashboard (llmbot/dashboard.py). Events are kept in
# memory (the latest EVENTS_KEEP) and in the store (data/events.jsonl or Postgres), so history survives restarts.
# =============================================================================
EVENTS_KEEP = 500
STARTED_AT = time.time()
_events: deque = deque(maxlen=EVENTS_KEEP)
_event_lock = threading.Lock()  # the log handler can run in worker threads (Whisper)
_event_last_id = 0
_inflight: dict[int, dict] = {}  # work in progress other than Claude Code jobs (local replies, voice notes)


def note_event(kind: str, text: str, *, level: str = "info", channel_id: int | None = None,
               user_id: int | None = None, **data) -> dict:
    """Record something that happened. kind groups events in the dashboard's filters (local, claude, reminder,
    task, power, model, voice, bot, warning, error); data holds extra fields shown with it (None values dropped)."""
    global _event_last_id
    now = time.time()
    with _event_lock:
        _event_last_id = eid = max(int(now * 1000), _event_last_id + 1)  # unique, and sorts by time
    ev = {k: (redact(v) if isinstance(v, str) else v) for k, v in data.items() if v is not None}
    try:
        if channel_id:
            ev["where"] = chat_label(channel_id)
        if user_id:
            ev["who"] = user_label(user_id)
    except Exception:
        pass
    ev.update(id=eid, ts=now, kind=kind, level=level, text=clip(redact(text), 2000), channel_id=channel_id,
              user_id=user_id)
    _events.append(ev)
    try:
        STORE.record_event(ev)
    except Exception:
        pass  # the store logs its own failures (as warnings, which the event handler skips for llmbot.store)
    return ev


@contextlib.contextmanager
def activity(kind: str, text: str, channel_id: int | None = None, user_id: int | None = None, **data):
    """Show work in progress on the dashboard ('now') while the block runs."""
    with _event_lock:
        key = max(int(time.time() * 1000), max(_inflight, default=0) + 1)
        _inflight[key] = {"kind": kind, "text": clip(redact(text), 500), "channel_id": channel_id,
                          "user_id": user_id, "started": time.time(), **data}
    try:
        yield
    finally:
        _inflight.pop(key, None)


class EventLogHandler(logging.Handler):
    """Warnings and errors from any logger also appear in the activity feed."""
    _busy = threading.local()

    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("llmbot.store") or getattr(self._busy, "on", False):
            return  # store failures would loop (recording the event fails again)
        self._busy.on = True
        try:
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f": {type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            error = record.levelno >= logging.ERROR
            note_event("error" if error else "warning", text, level="error" if error else "warning",
                       source=record.name)
        except Exception:
            pass
        finally:
            self._busy.on = False

# =============================================================================
# Text utilities
# =============================================================================


def strip_think(text: str | None) -> str:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1]
    if "<think>" in t:  # unterminated block
        t = t.split("<think>", 1)[0]
    return t.strip()


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Split under Discord's 2000-char limit on line boundaries, re-opening code fences across chunks."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    fence: str | None = None

    def flush():
        nonlocal cur, size
        body = "\n".join(cur) + ("\n```" if fence is not None else "")
        chunks.append(body)
        cur = [fence] if fence is not None else []
        size = len(fence) + 1 if fence is not None else 0

    for line in text.split("\n"):
        for piece in [line[i:i + limit] for i in range(0, len(line), limit)] or [""]:
            if cur and size + len(piece) + 1 > limit:
                flush()
            cur.append(piece)
            size += len(piece) + 1
            if piece.lstrip().startswith("```"):
                fence = None if fence is not None else piece.strip()
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()] or ["(empty reply)"]


def oneline(text: Any, n: int = 160) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def now_local() -> datetime:
    return datetime.now(TZ)


# =============================================================================
# Tools: web_search, fetch_page
# =============================================================================


async def web_search(query: str, max_results: int = 5) -> str:
    n = max(1, min(int(max_results or 5), 10))
    if SEARXNG_URL:
        r = await http.get(f"{SEARXNG_URL}/search", params={"q": query, "format": "json"}, timeout=20)
        r.raise_for_status()
        results = [
            {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
            for x in r.json().get("results", [])[:n]
        ]
    else:
        from ddgs import DDGS

        raw = await asyncio.to_thread(lambda: list(DDGS().text(query, max_results=n) or []))
        results = [{"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")} for x in raw]
    if not results:
        return "No results."
    return "\n".join(f"{i}. {x['title']}\n   {x['url']}\n   {oneline(x['snippet'], 300)}" for i, x in enumerate(results, 1))


class UnsafeURL(Exception):
    pass


_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


async def assert_public_url(url: str) -> str:
    """Reject non-http(s) URLs and any host resolving to a non-global address (SSRF guard). Returns a checked IP to
    connect to, so a second DNS lookup can't swap in a private address (DNS rebinding)."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise UnsafeURL(f"scheme '{p.scheme}' not allowed")
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeURL("missing host")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise UnsafeURL("localhost is not allowed")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("bad port")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise UnsafeURL(f"cannot resolve {host}")
    checked: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"{host} resolves to a private/local address")
        checked.append(str(ip))
    if not checked:
        raise UnsafeURL(f"cannot resolve {host}")
    return checked[0]


def pinned_request(url: str, ip: str) -> tuple[str, dict[str, str], dict]:
    """(URL with the host replaced by the checked IP, Host header, httpx extensions). TLS still verifies the
    certificate against the real hostname (SNI)."""
    p = urlsplit(url)
    netloc = (f"[{ip}]" if ":" in ip else ip) + (f":{p.port}" if p.port else "")
    host = p.hostname or ""
    host_header = (f"[{host}]" if ":" in host else host) + (f":{p.port}" if p.port else "")
    ext = {"sni_hostname": host} if p.scheme == "https" else {}
    return urlunsplit((p.scheme, netloc, p.path or "/", p.query, "")), {"Host": host_header}, ext


_TEXT_TYPES = {"text/plain", "text/markdown", "application/json", "text/csv"}
_HTML_TYPES = {"text/html", "application/xhtml+xml", "application/xml", "text/xml", "application/rss+xml", "application/atom+xml"}


async def fetch_page(url: str) -> str:
    import trafilatura

    headers = {"User-Agent": "Mozilla/5.0 (compatible; pc-pilot/1.0)", "Accept": "text/html,*/*;q=0.5"}
    # Own client without keep-alive: the connection pool is keyed by IP, so a pooled TLS connection made for one
    # site could otherwise carry a request for another site on the same (CDN) address.
    async with httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0)) as client:
        for _hop in range(6):
            ip = await assert_public_url(url)  # re-checked on every redirect hop
            target, host, ext = pinned_request(url, ip)
            async with client.stream("GET", target, headers={**headers, **host}, extensions=ext,
                                     follow_redirects=False, timeout=20) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                if r.status_code >= 400:
                    return f"HTTP {r.status_code} fetching {url}"
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype not in _TEXT_TYPES | _HTML_TYPES:
                    return f"Cannot read content type '{ctype or 'unknown'}' at {url}"
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= FETCH_MAX_BYTES:
                        break
                encoding = r.charset_encoding or "utf-8"
            break
        else:
            return "Too many redirects."

    if ctype in _HTML_TYPES:
        text = await asyncio.to_thread(trafilatura.extract, bytes(buf), url=url, include_comments=False)
        if not text:
            return f"No readable main text found at {url}"
    else:
        text = bytes(buf).decode(encoding, errors="replace")
    return f"URL: {url}\n\n{clip(text.strip(), FETCH_MAX_CHARS)}"


# =============================================================================
# Scheduler (cron tasks)
# =============================================================================

_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def normalize_crontab(expr: str) -> str:
    """APScheduler 3.x treats numeric day_of_week 0 as Monday; convert to names so 0/7=Sunday like real cron."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron must have 5 fields: minute hour day month day_of_week")
    fields[4] = re.sub(r"(?<![/\w])([0-7])(?![\w])", lambda m: _DOW_NAMES[int(m.group(1))], fields[4])
    return " ".join(fields)


def validate_cron(expr: str) -> CronTrigger:
    """Parse a 5-field cron in TZ and enforce the minimum interval between runs."""
    try:
        trig = CronTrigger.from_crontab(normalize_crontab(expr.strip()), timezone=TZ)
    except ValueError as e:
        raise ValueError(f"invalid cron '{expr}': {e}")
    now = now_local()
    prev = trig.get_next_fire_time(None, now)
    if prev is None:
        raise ValueError("cron never fires")
    horizon = now + timedelta(days=32)
    for _ in range(3500):
        nxt = trig.get_next_fire_time(prev, prev + timedelta(seconds=1))
        if nxt is None or nxt > horizon:
            break
        if nxt - prev < TASK_MIN_INTERVAL:
            raise ValueError(f"runs more often than every {int(TASK_MIN_INTERVAL.total_seconds() // 60)} minutes")
        prev = nxt
    return trig


scheduler = AsyncIOScheduler(timezone=TZ)
_tasks: dict[str, dict] = {}


def _save_tasks() -> None:
    STORE.save("tasks", TASKS_FILE, list(_tasks.values()))


def _schedule_job(task: dict) -> None:
    if task.get("at"):  # one-shot; one missed while the bot was off runs shortly after start (never skipped)
        at = max(datetime.fromisoformat(task["at"]), now_local() + timedelta(seconds=10))
        trigger, grace = DateTrigger(at, timezone=TZ), None
    else:
        trigger, grace = validate_cron(task["cron"]), 300
    scheduler.add_job(run_scheduled_task, trigger, args=[task["id"]], id=task["id"],
                      replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=grace)


def task_defaults(task: dict) -> dict:
    """What a task runs on is pinned when it's created, so changing a chat's model later doesn't move it. Anything
    not given (and tasks from before this existed) takes the chat's current choice."""
    s = get_settings(task["channel_id"])
    if task.get("engine") == "claude":
        task.setdefault("backend", s["cc_backend"])
        task.setdefault("model", s["cc_model"])
        task.setdefault("workspace", s["workspace"])
    else:
        task.setdefault("model", s["local_model"])
    task.setdefault("perm", "read")  # read | edit | full (Claude Code); the local model only ever gets web tools
    task.setdefault("reminders", False)  # may set reminders when it runs (never tasks: no self-perpetuating chains)
    task.setdefault("approved_by", None)  # the owner who last raised it (Claude Code, more than read, reminders)
    return task


def add_task(cron: str, prompt: str, description: str, channel_id: int, user_id: int, *,
             at: datetime | None = None, engine: str = "local", model: str | None = None,
             backend: str | None = None, workspace: str | None = None) -> dict:
    """A scheduled prompt: recurring (cron) or once (at), on a pinned engine + model. Starts read-only; owners can
    widen it in /tasks."""
    if len(_tasks) >= TASK_MAX:
        raise ValueError(f"task limit reached ({TASK_MAX}); cancel one first")
    if not prompt.strip():
        raise ValueError("empty prompt")
    if at is None:
        validate_cron(cron)
    task = {
        "id": uuid.uuid4().hex[:6], "cron": "" if at else cron.strip(), "at": at.isoformat() if at else None,
        "engine": engine, "prompt": clip(prompt.strip(), 4000),
        "description": clip((description or prompt).strip(), 100),
        "channel_id": channel_id, "created_by": user_id, "created_at": now_local().isoformat(),
    }
    for k, v in (("model", model), ("backend", backend if engine == "claude" else None),
                 ("workspace", workspace if engine == "claude" else None)):
        if v:
            task[k] = v
    task_defaults(task)
    _tasks[task["id"]] = task
    _schedule_job(task)
    _save_tasks()
    note_event("task", task["description"], channel_id=channel_id, user_id=user_id, status="scheduled", ref=task["id"],
               engine=engine, cron=task["cron"] or None, due=task["at"])
    return task


def cancel_task(task_id: str, user_id: int) -> str:
    task = _tasks.get(task_id.strip())
    if not task:
        return f"No task with id '{task_id}'."
    if task["created_by"] != user_id and not is_owner(user_id):
        return "Only the task's creator or an owner can cancel it."
    _tasks.pop(task["id"])
    if scheduler.get_job(task["id"]):
        scheduler.remove_job(task["id"])
    _save_tasks()
    note_event("task", task["description"], channel_id=task["channel_id"], user_id=user_id, status="cancelled",
               ref=task["id"])
    return f"Cancelled task {task['id']} ({task['description']})."


def next_run(task_id: str) -> datetime | None:
    job = scheduler.get_job(task_id)
    return getattr(job, "next_run_time", None)  # absent until the scheduler has started


def task_authority(task: dict) -> bool:
    """Claude Code, more than read-only, and reminders need an owner behind the task, checked again at every run:
    its creator, or the owner who approved the change (someone removed from OWNER_IDS stops counting)."""
    return is_owner(task["created_by"]) or (task.get("approved_by") is not None and is_owner(task["approved_by"]))


def task_runs_on(t: dict) -> str:
    """'💻 `qwen3.5:9b`' / '🤖 🦙 `qwen3.5:9b` · ✏️ Edit · 🔔 reminders'."""
    if t.get("engine") == "claude":
        be = t.get("backend", "anthropic")
        text = f"🤖 {BACKENDS[be][0] + ' ' if be != 'anthropic' else ''}`{t.get('model')}`"
    else:
        text = f"💻 `{t.get('model')}`"
    if t.get("perm", "read") != "read":
        text += f" · {PERMS[t['perm']][0]} {PERMS[t['perm']][1]}"
    if t.get("reminders"):
        text += " · 🔔 reminders"
    return text


async def update_task(task_id: str, user_id: int, *, engine: str | None = None, backend: str | None = None,
                      model: str | None = None, perm: str | None = None, reminders: bool | None = None) -> str:
    """Change what a task runs on / may do. The one place these rules are checked, for every front end:
    - the creator or an owner can move a task between local models (non-owners: only models the server lists);
    - only owners can put it on Claude Code, give it more than read-only, or let it set reminders;
    - full access never goes to Claude Code on Ollama (the local model gets no shell), and a task can never
      schedule more tasks."""
    t = _tasks.get(task_id)
    if not t or not visible_to(t["created_by"], user_id):
        return f"No task with id '{task_id}'."
    owner = is_owner(user_id)
    if t["created_by"] != user_id and not owner:
        return "Only the task's creator or an owner can change it."
    new = dict(t)
    raised = False
    if engine is not None:
        if not model:
            return "Pick a model."
        if engine == "claude":
            if not (CC_ENABLED and owner):
                return "⛔ Only owners can put a task on Claude Code."
            if backend not in BACKENDS:
                return f"Unknown Claude Code backend '{backend}'."
            new.update(engine="claude", backend=backend, model=model,
                       workspace=t.get("workspace") or get_settings(t["channel_id"])["workspace"])
            raised = True
        elif engine == "local":
            if not owner and model not in await list_local_models():
                return f"⚠️ `{clip(model, 100)}` isn't one of the server's models."
            new.update(engine="local", model=model, perm="read")
            new.pop("backend", None)
            new.pop("workspace", None)
        else:
            return f"Unknown engine '{engine}'."
    if perm is not None:
        if not owner:
            return "⛔ Only owners can change what a task may do."
        if perm not in PERMS:
            return f"Unknown permission '{perm}'."
        if new["engine"] != "claude" and perm != "read":
            return "The local model only gets web search and page fetch (and reminders, if allowed)."
        new["perm"] = perm
        raised = raised or perm != "read"
    if reminders is not None:
        if not owner:
            return "⛔ Only owners can let a task set reminders."
        new["reminders"] = bool(reminders)
        raised = raised or bool(reminders)
    if new["engine"] == "claude" and new["perm"] == "full" and new.get("backend") == "ollama":
        return "⛔ Full access isn't available on the Ollama backend: the local model never gets a shell. Use Edit."
    if raised:
        new["approved_by"] = user_id
    t.update(new)
    for k in ("backend", "workspace"):
        if k not in new:
            t.pop(k, None)
    _save_tasks()
    note_event("task", t["description"], channel_id=t["channel_id"], user_id=user_id, status="changed", ref=t["id"],
               engine=t["engine"], model=t["model"], perm=t["perm"], reminders=t["reminders"] or None)
    return f"✅ `{t['id']}` now runs on {task_runs_on(t)}."


def load_tasks() -> None:
    for task in list(_tasks.values()):
        try:
            task_defaults(task)
            _schedule_job(task)
        except Exception as e:
            log.warning("Dropping task %s: %s", task.get("id"), e)
            _tasks.pop(task["id"], None)
    _save_tasks()
    log.info("Loaded %d scheduled task(s)", len(_tasks))


def task_label(t: dict) -> str:
    """How a task is shown in Discord: 'once, <time> (in 5 hours)' or 'cron `0 8 * * *` · next in 5 hours'."""
    nr = next_run(t["id"])
    rel = discord.utils.format_dt(nr, "R") if nr else "—"
    if t.get("at"):
        return f"{task_runs_on(t)} · once, {discord.utils.format_dt(datetime.fromisoformat(t['at']), 'f')} ({rel})"
    return f"{task_runs_on(t)} · cron `{t['cron']}` · next {rel}"


TASK_RE = re.compile(r"\[\[task:\s*([^\]\n|]+?)\s*\|\s*([^\]\n]+?)\s*\]\]", re.I)
TASK_PER_REPLY = 3


def extract_tasks(text: str, channel_id: int, user_id: int, snap: "CCSnap | None" = None) -> tuple[str, list[str]]:
    """[[task: WHEN | PROMPT]] / [[task: cron ... | PROMPT]] markers in Claude's reply -> Claude Code tasks, pinned to
    the backend, model and workspace of the job that wrote them."""
    lines: list[str] = []
    pin = dict(backend=snap.backend, model=snap.model, workspace=snap.workspace) if snap else {}
    for i, m in enumerate(TASK_RE.finditer(text)):
        if i >= TASK_PER_REPLY:
            lines.append(f"⚠️ only {TASK_PER_REPLY} tasks per reply; the rest were ignored")
            break
        when, prompt = m.group(1).strip(), m.group(2).strip()
        try:
            if when.lower().startswith("cron"):
                t = add_task(when[4:].strip(" :"), prompt, prompt, channel_id, user_id, engine="claude", **pin)
            else:
                t = add_task("", prompt, prompt, channel_id, user_id, at=parse_when(when), engine="claude", **pin)
            lines.append(f"🗓️ Task `{t['id']}` scheduled: {task_label(t)}: {clip(prompt, 150)}")
        except ValueError as e:
            lines.append(f"⚠️ Task not scheduled: {e}")
    return TASK_RE.sub("", text).strip(), lines


def visible_to(creator: int, user_id: int | None) -> bool:
    """Reminders and tasks hold private text (and say which chat they post to): owners see all, others only their
    own. user_id None = no filter."""
    return user_id is None or creator == user_id or is_owner(user_id)


def my_tasks(user_id: int | None) -> list[dict]:
    return [t for t in _tasks.values() if visible_to(t["created_by"], user_id)]


def my_reminders(user_id: int | None) -> list[dict]:
    return sorted((r for r in _reminders.values() if visible_to(r["user_id"], user_id)), key=lambda r: r["when"])


def tasks_text(user_id: int | None = None) -> str:
    tasks = my_tasks(user_id)
    if not tasks:
        return "No scheduled tasks."
    lines = []
    for t in tasks:
        nr = next_run(t["id"])
        when = f"once at {t['at']}" if t.get("at") else f"cron '{t['cron']}'"
        lines.append(f"{t['id']}: {t['description']} | {when} | next {nr.isoformat() if nr else '-'} | "
                     f"{t.get('engine', 'local')} | channel {t['channel_id']}")
    return "\n".join(lines)


# =============================================================================
# Reminders (one-shot: "remind me to … in 10 min")
# =============================================================================

REMINDERS_FILE = DATA_DIR / "reminders.json"
REMINDER_MAX = 50
REMINDER_MAX_AHEAD = timedelta(days=366)
REMINDER_LATE = timedelta(seconds=90)  # fired this much after its time -> say it's late (PC asleep / bot off)
REMINDER_SNOOZE = timedelta(minutes=10)
# Claude Code sets reminders with [[remind: WHEN | TEXT]] lines in its reply (see CC_SYSTEM_APPEND)
REMIND_RE = re.compile(r"\[\[remind:\s*([^\]\n|]+?)\s*\|\s*([^\]\n]+?)\s*\]\]", re.I)
REMIND_PER_REPLY = 5

_UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
                 "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
                 "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                 "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800}
_REL_RE = re.compile(r"(\d+(?:\.\d+)?|an?(?=\s))\s*([a-z]+)")  # "a"/"an" only as a word ("an hour", not "at")
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?")
_WHEN_HELP = "say e.g. 'in 10 min', 'in 2h', '18:30', '6pm', 'tomorrow 9am' or '2026-10-01 09:00'"


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """'in 10 min' / '1h30m' / '18:30' / '6pm' / 'tomorrow 9am' / '2026-10-01 09:00' / ISO -> aware datetime in TZ.
    A bare time that has already passed today means tomorrow."""
    now = now or now_local()
    s = " ".join(text.lower().replace(",", " ").split())
    s = re.sub(r"^(in|after)\s+", "", s)
    rel = s.replace(" and ", " ").strip()
    parts = _REL_RE.findall(rel) if re.fullmatch(rf"(?:{_REL_RE.pattern}\s*)+", rel) else []
    if parts and all(unit in _UNIT_SECONDS for _, unit in parts):  # "12am" matches the shape but isn't relative
        secs = sum((1 if num in ("a", "an") else float(num)) * _UNIT_SECONDS[unit] for num, unit in parts)
        dt = now + timedelta(seconds=secs)
    else:
        try:
            dt = datetime.fromisoformat(s.upper().replace(" ", "T", 1) if re.match(r"\d{4}-\d\d-\d\d \d", s) else s.upper())
            dt = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
        except ValueError:
            day = None
            if m := re.search(r"\b(today|tonight|tomorrow|tmrw)\b", s):
                day = now.date() + timedelta(days=0 if m.group(1) in ("today", "tonight") else 1)
                s = (s[:m.start()] + s[m.end():]).strip()
            if m := re.search(r"\b(\d{4}-\d\d-\d\d)\b", s):
                day = datetime.fromisoformat(m.group(1)).date()
                s = (s[:m.start()] + s[m.end():]).strip()
            s = re.sub(r"^(at|on)\s+|\s+(at|on)$", "", s).replace(" at ", " ").strip()
            m = _TIME_RE.fullmatch(s)
            if not m or not (m.group(2) or m.group(3)):  # a bare "5" is ambiguous
                raise ValueError(f"couldn't read the time '{text.strip()}'; {_WHEN_HELP}")
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            if m.group(3):
                if not 1 <= hour <= 12:
                    raise ValueError(f"'{text.strip()}' isn't a valid time")
                hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
            if hour > 23 or minute > 59:
                raise ValueError(f"'{text.strip()}' isn't a valid time")
            dt = datetime.combine(day or now.date(), datetime.min.time(), TZ).replace(hour=hour, minute=minute)
            if day is None and dt <= now:
                dt += timedelta(days=1)
    if dt <= now:
        raise ValueError(f"{dt:%d %b %H:%M} has already passed")
    if dt - now > REMINDER_MAX_AHEAD:
        raise ValueError("that's more than a year away")
    return dt


_reminders: dict[str, dict] = {}


def _save_reminders() -> None:
    STORE.save("reminders", REMINDERS_FILE, list(_reminders.values()))


def _schedule_reminder(r: dict) -> None:
    # Missed ones (bot was off) fire shortly after start; misfire_grace_time=None = never silently skip one.
    at = max(datetime.fromisoformat(r["when"]), now_local() + timedelta(seconds=10))
    scheduler.add_job(fire_reminder, DateTrigger(at, timezone=TZ), args=[r["id"]], id=f"rem-{r['id']}",
                      replace_existing=True, misfire_grace_time=None)


def add_reminder(when: str | datetime, text: str, channel_id: int, user_id: int) -> dict:
    text = clip(" ".join(str(text).split()), 500)
    if not text:
        raise ValueError("nothing to remind about")
    if len(_reminders) >= REMINDER_MAX:
        raise ValueError(f"reminder limit reached ({REMINDER_MAX}); cancel some in /tasks")
    dt = when if isinstance(when, datetime) else parse_when(when)
    r = {"id": "r" + uuid.uuid4().hex[:5], "when": dt.isoformat(), "text": text, "channel_id": channel_id,
         "user_id": user_id, "created_at": now_local().isoformat()}
    _reminders[r["id"]] = r
    _schedule_reminder(r)
    _save_reminders()
    log.info("Reminder %s set for %s in channel %s", r["id"], r["when"], channel_id)
    note_event("reminder", text, channel_id=channel_id, user_id=user_id, status="set", ref=r["id"], due=r["when"])
    return r


def cancel_reminder(rid: str, user_id: int) -> str:
    r = _reminders.get(rid.strip())
    if not r:
        return f"No reminder with id '{rid}'."
    if r["user_id"] != user_id and not is_owner(user_id):
        return "Only the person it's for or an owner can cancel it."
    _reminders.pop(r["id"])
    if scheduler.get_job(f"rem-{r['id']}"):
        scheduler.remove_job(f"rem-{r['id']}")
    _save_reminders()
    note_event("reminder", r["text"], channel_id=r["channel_id"], user_id=user_id, status="cancelled", ref=r["id"])
    return f"Cancelled reminder {r['id']} ({r['text']})."


def load_reminders() -> None:
    for r in list(_reminders.values()):
        try:
            _schedule_reminder(r)
        except Exception as e:
            log.warning("Dropping reminder %s: %s", r.get("id"), e)
            _reminders.pop(r["id"], None)
    _save_reminders()
    log.info("Loaded %d reminder(s)", len(_reminders))


def reminder_line(r: dict) -> str:
    """Confirmation shown in Discord; the timestamps render in each viewer's own timezone."""
    dt = datetime.fromisoformat(r["when"])
    same_day = dt.date() == now_local().date()
    return (f"⏰ Reminder `{r['id']}` set for {discord.utils.format_dt(dt, 't' if same_day else 'f')} "
            f"({discord.utils.format_dt(dt, 'R')}): {clip(r['text'], 200)}")


def reminders_text(user_id: int | None = None) -> str:
    rems = my_reminders(user_id)
    if not rems:
        return "No reminders."
    return "\n".join(f"{r['id']}: {r['text']} | at {r['when']} | channel {r['channel_id']}" for r in rems)


def extract_reminders(text: str, channel_id: int, user_id: int) -> tuple[str, list[str]]:
    """Turn [[remind: WHEN | TEXT]] markers in Claude's reply into reminders. Returns (text without markers,
    confirmation/error lines)."""
    lines: list[str] = []
    for i, m in enumerate(REMIND_RE.finditer(text)):
        if i >= REMIND_PER_REPLY:
            lines.append(f"⚠️ only {REMIND_PER_REPLY} reminders per reply; the rest were ignored")
            break
        try:
            lines.append(reminder_line(add_reminder(m.group(1), m.group(2), channel_id, user_id)))
        except ValueError as e:
            lines.append(f"⚠️ Reminder not set: {e}")
    return REMIND_RE.sub("", text).strip(), lines


async def fire_reminder(rid: str) -> None:
    r = _reminders.pop(rid, None)
    if not r:
        return
    _save_reminders()
    uid, when = r["user_id"], datetime.fromisoformat(r["when"])
    channel = await resolve_channel(r["channel_id"])
    if channel is None and not is_telegram_id(r["channel_id"]) and bot.is_ready():
        log.warning("Reminder %s: channel %s unavailable, sending as DM", rid, r["channel_id"])
        try:
            channel = await (await bot.fetch_user(uid)).create_dm()
        except discord.HTTPException:
            pass
    if channel is None:
        log.error("Reminder %s could not be delivered (channel %s)", rid, r["channel_id"])
        return
    late = now_local() - when > REMINDER_LATE
    note_event("reminder", r["text"], channel_id=r["channel_id"], user_id=uid, status="late" if late else "fired",
               ref=rid, due=r["when"])
    text = redact(f"⏰ <@{uid}> {r['text']}")
    if late:
        text += f"\n-# late: this was due {discord.utils.format_dt(when, 'f')} (bot was offline or the PC was asleep)"
    try:  # ping only the person the reminder is for, never roles/@everyone even if the text contains them
        await channel.send(text, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                                                    users=[discord.Object(uid)]),
                           view=ReminderView(r))
    except Exception:
        log.exception("Reminder %s could not be posted", rid)


# =============================================================================
# Local engine
# =============================================================================

_history: dict[int, list[dict]] = {}
_llm_lock = asyncio.Lock()
_local_active = 0
_model_cache: tuple[float, list[str]] = (0.0, [])


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_DEFS = {
    "web_search": _fn("web_search", "Search the web. Returns titles, URLs and snippets.",
                      {"query": {"type": "string"}, "max_results": {"type": "integer", "description": "1-10, default 5"}}, ["query"]),
    "fetch_page": _fn("fetch_page", "Download a public web page and return its main text (truncated).",
                      {"url": {"type": "string"}}, ["url"]),
    "schedule_task": _fn(
        "schedule_task",
        f"Run a prompt later: once ('at') or repeatedly (cron). Cron is 5-field in {TIMEZONE} (minute hour day month day_of_week; 0=Sunday). "
        "Minimum interval 15 minutes. The prompt runs later with no chat history, so make it self-contained; "
        "results are posted to this channel.",
        {"cron": {"type": "string", "description": "5-field cron for repeating runs; leave empty when using 'at'"},
         "at": {"type": "string", "description": "Run ONCE instead, e.g. 'in 2 hours', '08:00', 'tomorrow 9am'"},
         "prompt": {"type": "string", "description": "Self-contained instruction to run each time"},
         "description": {"type": "string", "description": "Short label"}}, ["prompt", "description"]),
    "list_tasks": _fn("list_tasks", "List scheduled tasks.", {}, []),
    "cancel_task": _fn("cancel_task", "Cancel a scheduled task by id.", {"task_id": {"type": "string"}}, ["task_id"]),
    "set_reminder": _fn(
        "set_reminder",
        "Remind the user ONCE: at that time the bot posts the text in this channel and pings them. "
        f"when: relative ('in 10 min', 'in 2 hours') or a time in {TIMEZONE} ('18:30', '6pm', 'tomorrow 9am', "
        "'2026-10-01 09:00'). For repeating reminders use schedule_task.",
        {"when": {"type": "string"}, "text": {"type": "string", "description": "What to remind them about"}},
        ["when", "text"]),
    "list_reminders": _fn("list_reminders", "List pending reminders.", {}, []),
    "cancel_reminder": _fn("cancel_reminder", "Cancel a reminder by id.", {"reminder_id": {"type": "string"}}, ["reminder_id"]),
    "propose_claude_code": _fn(
        "propose_claude_code",
        "Propose running a task with Claude Code, an agent that can read/edit files and run commands in the "
        "user's project workspace. Use ONLY when the request needs the computer (files, code, shell). "
        "It never runs automatically; the owner must approve.",
        {"task": {"type": "string", "description": "Clear, complete instruction for Claude Code"},
         "reason": {"type": "string", "description": "Why the computer is needed"}}, ["task", "reason"]),
}
READONLY_TOOLS = ["web_search", "fetch_page"]
CHAT_TOOLS = ["web_search", "fetch_page", "schedule_task", "list_tasks", "cancel_task",
              "set_reminder", "list_reminders", "cancel_reminder"]


@dataclass
class ToolCtx:
    channel_id: int
    user_id: int
    tools: list[str]
    proposal: dict | None = None
    max_chars: int = 0  # largest request sent to the model (for the context estimate)
    reminders: list[str] = field(default_factory=list)  # confirmation lines for reminders set in this reply
    model: str = ""  # the model answering (a task it schedules runs on the same one)


@dataclass
class LocalResult:
    text: str
    tools_used: list[str]
    proposal: dict | None
    sent: str = ""  # the user message as sent (time-stamped); stored in history so the next prompt's prefix matches


async def execute_tool(name: str, args: dict, ctx: ToolCtx) -> str:
    if name not in ctx.tools:
        return f"Tool '{name}' is not available here."
    try:
        if name == "web_search":
            return await web_search(str(args.get("query", "")), args.get("max_results") or 5)
        if name == "fetch_page":
            return await fetch_page(str(args.get("url", "")))
        if name == "schedule_task":
            at = str(args.get("at") or "").strip()
            t = add_task(str(args.get("cron") or ""), str(args.get("prompt", "")), str(args.get("description", "")),
                         ctx.channel_id, ctx.user_id, at=parse_when(at) if at else None, model=ctx.model or None)
            return f"Scheduled task {t['id']} ('{t['description']}'), next run {next_run(t['id'])}."
        if name == "list_tasks":
            return tasks_text(ctx.user_id)
        if name == "cancel_task":
            return cancel_task(str(args.get("task_id", "")), ctx.user_id)
        if name == "set_reminder":
            r = add_reminder(str(args.get("when", "")), str(args.get("text", "")), ctx.channel_id, ctx.user_id)
            ctx.reminders.append(reminder_line(r))
            return (f"Reminder {r['id']} set for {datetime.fromisoformat(r['when']):%a %d %b %H:%M} ({TIMEZONE}). "
                    "The bot shows the confirmed time under your reply; acknowledge briefly.")
        if name == "list_reminders":
            return reminders_text(ctx.user_id)
        if name == "cancel_reminder":
            return cancel_reminder(str(args.get("reminder_id", "")), ctx.user_id)
        if name == "propose_claude_code":
            ctx.proposal = {"task": str(args.get("task", "")).strip(), "reason": str(args.get("reason", "")).strip()}
            return "Proposal recorded. The user will see a confirmation card; briefly tell them what you proposed."
    except UnsafeURL as e:
        return f"Refused: {e}"
    except (ValueError, httpx.HTTPError) as e:
        return f"Error: {e}"
    except Exception as e:
        log.exception("tool %s failed", name)
        return f"Error: {type(e).__name__}: {e}"
    return f"Unknown tool '{name}'."


async def chat_completion(model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    global _local_active
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    if LLM_IDLE_UNLOAD and is_loaded(_llm_root(), model) is not None:  # Ollama
        # Ollama's default keep_alive (5 min) is shorter than LLM_IDLE_UNLOAD, so the model was reloaded from disk and
        # lost its prompt cache after 5 quiet minutes. Keep it a bit longer; idle_unloader unloads it on time.
        payload["keep_alive"] = f"{LLM_IDLE_UNLOAD + 120}s"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    async with _llm_lock:  # single GPU: one generation at a time
        _local_active += 1
        try:
            r = await http.post(f"{LLM_URL}/chat/completions", json=payload, headers=headers,
                                timeout=httpx.Timeout(600, connect=10))
        finally:
            _local_active -= 1
            touch_model(_llm_root(), model)  # idle clock starts when generation ends
    if r.status_code >= 400:
        raise RuntimeError(f"LLM server returned HTTP {r.status_code}: {oneline(r.text, 300)}")
    return r.json()["choices"][0]["message"]


def system_prompt(extra: str = "") -> str:
    return f"{SYSTEM_PROMPT}\n\n{TIME_NOTE} Timezone: {TIMEZONE}.{extra}"


def stamped(prompt: str) -> str:
    return f"[Now: {now_local():%A %d %B %Y, %H:%M}]\n{prompt}"


async def run_local(prompt: str, model: str, ctx: ToolCtx, history: list[dict] | None) -> LocalResult:
    ctx.model = ctx.model or model
    extra = ""
    if "propose_claude_code" in ctx.tools:
        extra = ("\nIf the request needs the user's computer (files, code, running commands), call "
                 "propose_claude_code instead of guessing.")
    sent = stamped(prompt)
    messages = [{"role": "system", "content": system_prompt(extra)}, *(history or []), {"role": "user", "content": sent}]
    tools = [TOOL_DEFS[t] for t in ctx.tools]
    used: list[str] = []
    for _round in range(MAX_TOOL_ROUNDS):
        ctx.max_chars = max(ctx.max_chars, len(json.dumps(messages, ensure_ascii=False)) + len(json.dumps(tools)))
        msg = await chat_completion(model, messages, tools)
        calls = msg.get("tool_calls") or []
        if not calls:
            return LocalResult(strip_think(msg.get("content")), used, ctx.proposal, sent)
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments") or {}
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                args = {}
            if name not in used:
                used.append(name)
            result = await execute_tool(name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": call.get("id") or uuid.uuid4().hex, "name": name,
                             "content": clip(result, 8000)})
    messages.append({"role": "user", "content": "Tool budget reached. Answer now using what you have; do not call tools."})
    msg = await chat_completion(model, messages, None)
    return LocalResult(strip_think(msg.get("content")), used, ctx.proposal, sent)


def remember(channel_id: int, prompt: str, answer: str) -> None:
    """Old exchanges are dropped several at a time, not one per message: dropping the oldest changes the start of the
    prompt, and Ollama then re-reads the whole history (~1s per 1.3k tokens here) instead of reusing its cache."""
    h = _history.setdefault(channel_id, [])
    h += [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]
    size = lambda: sum(len(m["content"]) for m in h)
    if len(h) > MAX_HISTORY_TURNS * 2:
        del h[: len(h) - max(1, MAX_HISTORY_TURNS * 3 // 4) * 2]
    if size() > HISTORY_MAX_CHARS:
        while len(h) > 2 and size() > HISTORY_MAX_CHARS * 3 // 4:
            del h[:2]
    _save_history()


def _save_history() -> None:
    STORE.save("history", HISTORY_FILE, {str(k): v for k, v in _history.items()})


def load_state() -> None:
    """Open the configured store (Postgres if DATABASE_URL is set) and load everything the bot keeps."""
    global STORE
    STORE = store_mod.open_store(DATABASE_URL, DATA_DIR)
    _settings.update(STORE.load("settings", SETTINGS_FILE, {}))
    _tasks.update({t["id"]: t for t in STORE.load("tasks", TASKS_FILE, [])})
    _reminders.update({r["id"]: r for r in STORE.load("reminders", REMINDERS_FILE, [])})
    _usage.update(STORE.load("usage", USAGE_FILE, {}))
    _history.update({int(k): v for k, v in STORE.load("history", HISTORY_FILE, {}).items()})
    _known_names.update(STORE.load("names", NAMES_FILE, {}))
    try:
        _events.extend(reversed(STORE.recent_events(EVENTS_KEEP)))
    except Exception as e:
        log.warning("Could not load the activity history: %s", oneline(redact(e), 200))
    # Names the activity history recorded before names.json existed (skipping "Telegram user 123"-style fallbacks)
    fallback = re.compile(r"^(Telegram|Discord) (user|channel|chat|group) ?-?\d*$|^Telegram (chat|group)$|^Web")
    added = False
    for e in _events:
        for key, name in ((f"u:{e.get('user_id')}", e.get("who")), (f"c:{e.get('channel_id')}", e.get("where"))):
            if name and not key.endswith("None") and key not in _known_names and not fallback.match(name):
                _known_names[key] = name
                added = True
    if added:
        STORE.save("names", NAMES_FILE, _known_names)
    log.info("Storage: %s", STORE.describe())


def forget_history(channel_id: int) -> None:
    """/reset: the local model's chat memory for this channel (kept across restarts otherwise)."""
    if _history.pop(channel_id, None) is not None:
        _save_history()


async def list_local_models() -> list[str]:
    """Ollama /api/tags first, then the server's /v1/models. Cached briefly."""
    global _model_cache
    if time.monotonic() - _model_cache[0] < 20 and _model_cache[1]:
        return _model_cache[1]
    root = LLM_URL[:-3] if LLM_URL.endswith("/v1") else LLM_URL
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    models: list[str] = []
    try:
        r = await http.get(f"{root}/api/tags", timeout=2.5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        try:
            r = await http.get(f"{LLM_URL}/models", headers=headers, timeout=2.5)
            r.raise_for_status()
            models = [m["id"] for m in r.json().get("data", [])]
        except Exception as e:
            log.info("Model list unavailable: %s", e)
    _model_cache = (time.monotonic(), sorted(models))
    return _model_cache[1]


def _ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    default = Path(os.getenv("LOCALAPPDATA") or "~").expanduser() / "Programs/Ollama/ollama.exe"  # the Windows installer's
    return str(default) if default.exists() else None


async def ensure_ollama() -> None:
    """Start `ollama serve` when a local Ollama URL is configured and nothing answers there (e.g. right after boot)."""
    if not OLLAMA_AUTOSTART:
        return
    local = [u for u in (LLM_URL, OLLAMA_URL) if urlsplit(u).hostname in ("localhost", "127.0.0.1", "::1")
             and (urlsplit(u).port or 80) == 11434]
    if not local:
        return  # a remote server, or another local server (LM Studio, llama.cpp…): not ours to start
    root = urlunsplit(urlsplit(local[0])._replace(path=""))
    up = False
    try:
        up = (await http.get(f"{root}/api/version", timeout=2)).status_code == 200
    except httpx.HTTPError:
        pass
    exe = None if up else _ollama_exe()
    if up or not exe:
        if not up:
            log.info("Nothing answers at %s and ollama.exe wasn't found, so it wasn't started", root)
        return
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
    try:
        subprocess.Popen([exe, "serve"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=sys.platform != "win32")
    except OSError as e:
        log.warning("Couldn't start Ollama: %s", oneline(redact(e), 200))
        return
    for _ in range(30):
        await asyncio.sleep(1)
        try:
            if (await http.get(f"{root}/api/version", timeout=2)).status_code == 200:
                break
        except httpx.HTTPError:
            pass
    else:
        log.warning("Started Ollama, but it isn't answering at %s after 30s", root)
        return
    log.info("Started Ollama (nothing was answering at %s)", root)
    note_event("bot", "Started Ollama: nothing was answering on this PC")


async def list_ollama_models() -> list[str]:
    try:
        r = await http.get(f"{OLLAMA_URL}/api/tags", timeout=2.5)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))
    except Exception:
        return []


async def cc_models(backend: str) -> list[str]:
    if backend == "anthropic":
        return ANTHROPIC_MODELS
    if backend == "ollama":
        return await list_ollama_models()
    return CC_CUSTOM_MODELS


# =============================================================================
# Model memory: lazy load, idle unload
# =============================================================================
# Ollama loads a model on its first request (chat, scheduled task or Claude Code on the
# ollama backend); the bot never preloads. Models the bot used are unloaded with
# keep_alive=0 after LLM_IDLE_UNLOAD idle seconds. Ollama's own keep_alive (default 5m,
# OLLAMA_KEEP_ALIVE) still applies, so whichever is shorter wins.

_used_models: dict[tuple[str, str], float] = {}  # (ollama root, model) -> last activity
_loaded_models: dict[str, set[str]] = {}  # ollama root -> loaded model names (from /api/ps)


def _llm_root() -> str:
    return (LLM_URL[:-3] if LLM_URL.endswith("/v1") else LLM_URL).rstrip("/")


def _tagged(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


_ctx_limits: dict[tuple[str, str], int] = {}


async def ctx_limit(root: str, model: str) -> int | None:
    """The context window Ollama actually gives this model (/api/ps context_length, e.g. 32768). Claude Code assumes
    200k for any model, and Ollama silently drops the start of a prompt that doesn't fit, so this is what counts."""
    key = (root.rstrip("/"), _tagged(model))
    try:
        r = await http.get(f"{key[0]}/api/ps", timeout=3)
        for m in r.json().get("models") or []:
            if key[1] in (m.get("name"), m.get("model")) and m.get("context_length"):
                _ctx_limits[key] = int(m["context_length"])
    except Exception:
        pass
    return _ctx_limits.get(key) or int(os.environ.get("OLLAMA_CONTEXT_LENGTH") or 0) or None


def compact_at(limit: int | None) -> int:
    """Tokens at which to suggest /compact: CC_COMPACT_HINT_TOKENS, or 75% of a smaller real limit."""
    if limit:
        return min(CC_COMPACT_HINT_TOKENS or limit, int(limit * 0.75))
    return CC_COMPACT_HINT_TOKENS


def ctx_text(ctx: int, limit: int | None, approx: bool = False) -> str:
    """'5k/32k ctx' (context windows are powers of two: 32768 reads as 32k, not 33k)."""
    lim = (f"/{limit // 1024}k" if limit % 1024 == 0 else f"/{limit / 1000:.0f}k") if limit else ""
    return f"{'~' if approx else ''}{ctx / 1000:.0f}k{lim} ctx"


def touch_model(root: str, model: str) -> None:
    _used_models[(root.rstrip("/"), model)] = time.monotonic()


def models_busy() -> bool:
    job = _cc_current
    return _llm_lock.locked() or bool(job and job.snap.backend == "ollama")


async def refresh_loaded() -> None:
    for root in {_llm_root(), OLLAMA_URL}:
        try:
            r = await http.get(f"{root}/api/ps", timeout=3)
            r.raise_for_status()
            _loaded_models[root] = {_tagged(m.get("name") or m.get("model", "")) for m in r.json().get("models", [])}
        except Exception:
            _loaded_models.pop(root, None)  # not Ollama, or not running


def is_loaded(root: str, model: str) -> bool | None:
    """True/False from the last /api/ps poll; None if unknown (server isn't Ollama or is down)."""
    loaded = _loaded_models.get(root.rstrip("/"))
    return None if loaded is None else _tagged(model) in loaded


async def unload_model(root: str, model: str) -> bool:
    _used_models.pop((root, model), None)
    if not is_loaded(root, model):  # only unload what /api/ps says is loaded, so we never trigger a load
        return False
    try:
        r = await http.post(f"{root}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
        ok = r.status_code < 400
    except httpx.HTTPError:
        ok = False
    if ok:
        _loaded_models.get(root, set()).discard(_tagged(model))
        log.info("Unloaded %s from memory", model)
        note_event("model", f"Unloaded {model} from memory")
    return ok


async def unload_all() -> list[str]:
    if models_busy():
        return []
    await refresh_loaded()
    return [m for (root, m) in list(_used_models) if await unload_model(root, m)]


_whisper = None  # faster_whisper.WhisperModel, loaded on the first voice note
_whisper_last = 0.0
_whisper_lock = asyncio.Lock()


def _decode_16k(data: bytes):
    """Any audio PyAV can read (Discord voice notes are OGG/Opus) -> 16 kHz mono float32, which Whisper expects.
    Done here rather than by faster-whisper, whose decoder breaks on newer PyAV (unknown 'metadata_errors')."""
    import av
    import numpy as np

    chunks = []
    limit, n = (VOICE_MAX_SECONDS + 1) * 16000, 0  # stop early: a few MB of Opus can decode to hours of audio
    with av.open(io.BytesIO(data)) as container:
        rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            new = [f.to_ndarray() for f in rs.resample(frame)]
            chunks += new
            n += sum(c.shape[-1] for c in new)
            if n > limit:
                raise ValueError(f"voice note is longer than {VOICE_MAX_SECONDS}s (VOICE_MAX_SECONDS)")
        chunks += [f.to_ndarray() for f in rs.resample(None)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


def _transcribe_sync(data: bytes) -> tuple[str, float]:
    global _whisper
    audio = _decode_16k(data)
    seconds = len(audio) / 16000
    if seconds > VOICE_MAX_SECONDS:
        raise ValueError(f"voice note is {seconds:.0f}s; the limit is {VOICE_MAX_SECONDS}s (VOICE_MAX_SECONDS)")
    if _whisper is None:
        from faster_whisper import WhisperModel  # first use downloads the model once (~460 MB for "small")
        log.info("Loading Whisper '%s' on %s (%s)", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE)
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE)
    segments, _info = _whisper.transcribe(audio, language=WHISPER_LANGUAGE, vad_filter=True, beam_size=5)
    return " ".join(s.text.strip() for s in segments).strip(), seconds


async def transcribe(data: bytes) -> tuple[str, float]:
    """(text, seconds). One at a time; the model stays loaded until LLM_IDLE_UNLOAD seconds of no voice notes."""
    global _whisper_last
    async with _whisper_lock:
        try:
            with activity("voice", f"Transcribing a voice note ({len(data) // 1024} KB)"):
                return await asyncio.to_thread(_transcribe_sync, data)
        finally:
            _whisper_last = time.monotonic()


def _unload_whisper_if_idle() -> None:
    global _whisper
    if (_whisper is not None and LLM_IDLE_UNLOAD and not _whisper_lock.locked()
            and time.monotonic() - _whisper_last >= LLM_IDLE_UNLOAD):
        _whisper = None
        import gc
        gc.collect()
        log.info("Unloaded Whisper from memory (idle)")


async def idle_unloader() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            _unload_whisper_if_idle()
            await refresh_loaded()
            if not LLM_IDLE_UNLOAD or models_busy():
                continue
            now = time.monotonic()
            for (root, model), last in list(_used_models.items()):
                if is_loaded(root, model) is False:
                    _used_models.pop((root, model), None)  # Ollama already unloaded it
                elif now - last >= LLM_IDLE_UNLOAD:
                    await unload_model(root, model)
        except Exception:
            log.exception("idle unloader")


def memory_status(model: str) -> str:
    loaded = is_loaded(_llm_root(), model)
    if loaded is None:
        return ""
    if not loaded:
        return " · 💤 not in memory"
    last = _used_models.get((_llm_root(), model))
    if last and LLM_IDLE_UNLOAD:
        at = datetime.now(TZ) + timedelta(seconds=max(0, LLM_IDLE_UNLOAD - (time.monotonic() - last)))
        return f" · 🧠 loaded, unloads {discord.utils.format_dt(at, 'R')}"
    return " · 🧠 loaded"


# =============================================================================
# Claude Code runner
# =============================================================================


def claude_bin() -> str | None:
    if CLAUDE_BIN:
        return CLAUDE_BIN if Path(CLAUDE_BIN).exists() else shutil.which(CLAUDE_BIN)
    return shutil.which("claude")


@dataclass(frozen=True)
class CCSnap:
    backend: str
    model: str
    perm: str
    workspace: str
    resume: str | None = None
    note: str | None = None  # shown as the first progress line (e.g. why a new session started)
    compact: bool = False  # a /compact run (needs slash commands, which the lean flags disable)


def session_state(s: dict) -> tuple[str | None, str | None]:
    """(resumable session id, reason it isn't resumable). Sessions only continue in the same workspace folder
    (by path, not name) and within CC_SESSION_IDLE_MINUTES of their last job."""
    sid = s.get("cc_session")
    if not sid:
        return None, None
    if s.get("cc_session_path") != str(WORKSPACES[s["workspace"]]):
        return None, "workspace folder changed"
    if s.get("cc_session_setup") != CC_SETUP_FINGERPRINT:
        return None, "bot's Claude Code setup changed"
    idle_min = (time.time() - (s.get("cc_session_at") or 0)) / 60
    if CC_SESSION_IDLE_MINUTES > 0 and idle_min >= CC_SESSION_IDLE_MINUTES:
        return None, f"idle {idle_min:.0f} min"
    return sid, None


def snapshot(channel_id: int, resume: bool = False) -> CCSnap:
    s = get_settings(channel_id)
    sid, why = session_state(s) if resume else (None, None)
    note = f"🆕 New session ({why}; saves re-sending the old history)" if why else None
    return CCSnap(s["cc_backend"], s["cc_model"], s["cc_perm"], s["workspace"], sid, note)


# ---- Local web tools for Claude Code on Ollama / custom backends ---------------------------------------------
# Claude Code's WebSearch runs on Anthropic's servers (and WebFetch summarises with an Anthropic model), so neither
# works when Claude Code talks to Ollama. The bot then starts itself as a small MCP server (`python -m llmbot
# --mcp-web`) that exposes its own web_search / fetch_page: DuckDuckGo or SearXNG, trafilatura, SSRF checks on
# every redirect hop. Its action tools (reminders, tasks, send/delete file) only become markers the bot checks;
# there is no shell.
MCP_WEB_TOOLS = ["mcp__bot__web_search", "mcp__bot__fetch_page", "mcp__bot__set_reminder",
                 "mcp__bot__schedule_prompt", "mcp__bot__send_file", "mcp__bot__delete_file"]
# Small local models often say "reminder set!" without writing the [[remind: …]] marker; real tools are far more
# reliable for them. These four only validate and answer; the bot sees the successful calls in the stream and turns
# them into the same markers, so all the usual rules (workspace-only files, limits, no chains from scheduled runs)
# apply unchanged.
ACTION_TOOLS = {
    "set_reminder": _fn("set_reminder", "Ping the user ONCE at a time with a text (e.g. 'take your meds'). when: 'in 10 "
                        "min', 'in 2 hours', '18:30', '6pm', 'tomorrow 9am' or 'YYYY-MM-DD HH:MM' (user's local time). Use ONLY this "
                        "for reminders; do not also call schedule_prompt.",
                        {"when": {"type": "string"}, "text": {"type": "string"}}, ["when", "text"]),
    "schedule_prompt": _fn("schedule_prompt", "NOT for reminders (use set_reminder). Run a prompt later when something "
                           "must be looked up or done then (news, weather, prices), posting the answer to the user: once "
                           "('when', same formats as set_reminder) or repeatedly ('cron', 5-field, user's timezone, "
                           "0=Sunday, min every 15 min). The prompt runs with no memory of this chat: make it "
                           "self-contained.", {"prompt": {"type": "string"}, "when": {"type": "string"},
                                              "cron": {"type": "string"}}, ["prompt"]),
    "send_file": _fn("send_file", "Attach a file from the working directory to your Discord reply.",
                     {"path": {"type": "string", "description": "Relative path"}}, ["path"]),
    "delete_file": _fn("delete_file", "Delete a file from the working directory after your reply (and any attached "
                       "files) has been posted.", {"path": {"type": "string", "description": "Relative path"}}, ["path"]),
}


def _mcp_action(name: str, args: dict) -> tuple[str, bool]:
    """Validate an action tool call inside the MCP server (cwd = the job's workspace). Returns (text, is_error)."""
    one = lambda v: " ".join(str(v or "").replace("]", ")").split())  # must fit on one marker line
    if name == "set_reminder":
        dt = parse_when(one(args.get("when")))
        if not one(args.get("text")):
            return "text is empty", True
        return f"Reminder will be set for {dt:%a %d %b %H:%M} ({TIMEZONE}); the bot confirms it under your reply.", False
    if name == "schedule_prompt":
        if not one(args.get("prompt")):
            return "prompt is empty", True
        if one(args.get("cron")):
            validate_cron(one(args.get("cron")))
            return f"Will run on cron '{one(args.get('cron'))}' ({TIMEZONE}); the bot confirms it under your reply.", False
        dt = parse_when(one(args.get("when")))
        return f"Will run once at {dt:%a %d %b %H:%M} ({TIMEZONE}); the bot confirms it under your reply.", False
    path = one(args.get("path"))
    root = Path.cwd().resolve()
    f = (root / path).resolve()
    if not path or not f.is_relative_to(root) or f == root:
        return "path must be a file inside the working directory", True
    if not f.is_file():
        return f"{path}: not found", True
    return (f"{path} will be attached to your reply." if name == "send_file"
            else f"{path} will be deleted after your reply is posted."), False


def action_markers(actions: list[tuple[str, dict]]) -> str:
    """Successful action-tool calls -> the equivalent [[...]] markers for the normal pipeline."""
    one = lambda v: " ".join(str(v or "").replace("]", ")").split())
    out = []
    reminder_times = {one(a.get("when")).lower() for n, a in actions if n == "set_reminder"}
    for name, a in actions:
        if name == "schedule_prompt" and not one(a.get("cron")) and one(a.get("when")).lower() in reminder_times:
            continue  # small models sometimes call both for one reminder; the reminder is what was meant
        if name == "set_reminder":
            out.append(f"[[remind: {one(a.get('when')).replace('|', ' ')} | {one(a.get('text'))}]]")
        elif name == "schedule_prompt":
            when = f"cron {one(a.get('cron'))}" if one(a.get("cron")) else one(a.get("when"))
            out.append(f"[[task: {when.replace('|', ' ')} | {one(a.get('prompt'))}]]")
        elif name == "send_file":
            out.append(f"[[attach: {one(a.get('path'))}]]")
        elif name == "delete_file":
            out.append(f"[[delete: {one(a.get('path'))}]]")
    return "\n".join(out)
MCP_WEB_CONFIG = DATA_DIR / "mcp_web.json"


def local_web(snap: CCSnap) -> bool:
    return snap.backend != "anthropic"


def mcp_web_config() -> str:
    # Claude Code starts it in the workspace, so point Python at this repo. -P: don't put the cwd (the workspace,
    # which Claude can write to in edit mode) on sys.path, or a workspace file named llmbot/, httpx.py … would run.
    cfg = {"mcpServers": {"bot": {"type": "stdio", "command": sys.executable, "args": ["-P", "-m", "llmbot", "--mcp-web"],
                                  "env": {"PYTHONPATH": str(BASE_DIR), "PYTHONSAFEPATH": "1"}}}}
    _atomic_write_json(MCP_WEB_CONFIG, cfg)  # rewritten each time so it follows the running version
    return str(MCP_WEB_CONFIG)


def perm_args(snap: CCSnap) -> list[str]:
    if not local_web(snap):
        return PERM_ARGS[snap.perm]
    if snap.perm == "full":
        return [*PERM_ARGS["full"], "--disallowedTools", "WebSearch", "WebFetch"]
    tools = "Read,Glob,Grep" + (",Edit,Write" if snap.perm == "edit" else "")
    allowed = [t for t in READ_TOOLS if not t.startswith("Web")] + (["Edit(./**)"] if snap.perm == "edit" else [])
    return [*PERM_ARGS[snap.perm][:4], "--tools", tools, "--allowedTools", *allowed, *MCP_WEB_TOOLS]


async def _mcp_web_server() -> None:
    """Minimal MCP server over stdio (newline-delimited JSON-RPC): initialize, tools/list, tools/call, ping."""
    global http
    http = httpx.AsyncClient(headers={"User-Agent": "pc-pilot/1.0"})
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    loop = asyncio.get_running_loop()
    tools = [{"name": d["function"]["name"], "description": d["function"]["description"],
              "inputSchema": d["function"]["parameters"]}
             for d in (TOOL_DEFS["web_search"], TOOL_DEFS["fetch_page"], *ACTION_TOOLS.values())]

    def send(obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    while line := await loop.run_in_executor(None, sys.stdin.readline):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue  # notifications (initialized, cancelled) need no reply
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                      "serverInfo": {"name": "pc-pilot-web", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            name, args, err = params.get("name"), params.get("arguments") or {}, False
            try:
                if name == "web_search":
                    text = await web_search(str(args.get("query", "")), int(args.get("max_results") or 5))
                elif name == "fetch_page":
                    text = await fetch_page(str(args.get("url", "")))
                elif name in ACTION_TOOLS:
                    text, err = _mcp_action(name, args)
                else:
                    text, err = f"Unknown tool {name}", True
            except UnsafeURL as e:
                text, err = f"Refused: {e}", True
            except ValueError as e:
                text, err = f"Error: {e}", True
            except Exception as e:
                text, err = f"Error: {type(e).__name__}: {e}", True
            result = {"content": [{"type": "text", "text": clip(text, 12000)}], "isError": err}
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    await http.aclose()


def build_cc_command(binary: str, snap: CCSnap) -> list[str]:
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", snap.model]
    if not CC_EXTRAS:
        # no MCP servers/skills/plugins: ~70% fewer tokens per call (measured). /compact is a slash command, so a
        # compact run keeps them enabled (verified: with --disable-slash-commands it replies "isn't available").
        cmd += [a for a in LEAN_ARGS if not (snap.compact and a == "--disable-slash-commands")]
    if local_web(snap):
        cmd += ["--mcp-config", mcp_web_config()]
    if CC_SYSTEM_APPEND:
        cmd += ["--append-system-prompt", CC_SYSTEM_APPEND]
    if CC_EFFORT in ("low", "medium", "high", "xhigh", "max"):
        cmd += ["--effort", CC_EFFORT]
    if CC_MAX_BUDGET_USD > 0 and snap.backend == "anthropic":
        cmd += ["--max-budget-usd", f"{CC_MAX_BUDGET_USD:g}"]
    if snap.resume:
        cmd += ["--resume", snap.resume]
    if snap.perm != "full":
        cmd += SANDBOX_ARGS
    return cmd + perm_args(snap)  # --allowedTools is variadic, keep it last


def build_cc_env(snap: CCSnap) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    if snap.backend == "ollama":
        env.update(ANTHROPIC_BASE_URL=OLLAMA_URL, ANTHROPIC_AUTH_TOKEN="ollama", ANTHROPIC_API_KEY="")
    elif snap.backend == "custom":
        env.update(ANTHROPIC_BASE_URL=CC_CUSTOM_BASE_URL, ANTHROPIC_AUTH_TOKEN=CC_CUSTOM_TOKEN, ANTHROPIC_API_KEY="")
    if snap.backend != "anthropic":
        for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = snap.model
    return env


def summarize_tool(name: str, inp: Any) -> str:
    inp = inp if isinstance(inp, dict) else {}
    key = {
        "Bash": "command", "PowerShell": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Glob": "pattern", "Grep": "pattern",
        "WebFetch": "url", "WebSearch": "query", "Task": "description", "Agent": "description",
    }.get(name)
    detail = inp.get(key) if key else None
    if isinstance(detail, str) and key and key.endswith("path") and len(detail) > 60:
        parts = re.split(r"[\\/]", detail)
        detail = "…/" + "/".join(parts[-3:])
    if detail is None:
        detail = json.dumps(inp, ensure_ascii=False) if inp else ""
    detail = redact(detail)  # before truncating, so a cut-off secret can't slip past the patterns
    return f"**{name}** `{oneline(detail, 140).replace('`', 'ˋ')}`" if detail else f"**{name}**"


def _flatten_content(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(_flatten_content(x.get("text", "") if isinstance(x, dict) else x) for x in c)
    return str(c or "")


def cost_breakdown(result: dict) -> str:
    """Per-model token/cost table from a result event's modelUsage (includes calls not in the session file,
    such as the Haiku call behind WebSearch). The CLI reports these as running totals for the whole session."""
    rows = ["[cost breakdown, session totals] model | input | cache write | cache read | output | web searches | USD"]
    for model, u in (result.get("modelUsage") or {}).items():
        rows.append(f"  {model} | {u.get('inputTokens', 0)} | {u.get('cacheCreationInputTokens', 0)} | "
                    f"{u.get('cacheReadInputTokens', 0)} | {u.get('outputTokens', 0)} | {u.get('webSearchRequests', 0)} | "
                    f"${u.get('costUSD', 0):.4f}")
    rows.append(f"  total: ${result.get('total_cost_usd') or 0:.4f}")
    return "\n".join(rows) if len(rows) > 2 else "[cost breakdown] not reported by the CLI"


class StreamParser:
    """Turns Claude Code stream-json events into short progress lines and a full transcript."""

    def __init__(self):
        self.session_id: str | None = None
        self.model: str | None = None
        self.result: dict | None = None
        self.transcript: list[str] = []
        self._denied: set[str] = set()
        self.context_tokens: int | None = None  # prompt size of the latest API call = what the next message re-reads
        self.compacted: tuple[int, int] | None = None  # (tokens before, after) when this run compacted the session
        self._pending: dict[str, tuple[str, dict]] = {}
        self.actions: list[tuple[str, dict]] = []  # successful mcp__bot__ action tool calls (set_reminder, …)
        self.api_calls: set[str] = set()  # distinct model requests this run (assistant message ids)

    def feed_line(self, raw: str) -> list[str]:
        raw = raw.strip()
        if not raw:
            return []
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            self.transcript.append(f"[non-json] {clip(redact(raw), 500)}")
            return []
        return self.feed(ev) if isinstance(ev, dict) else []

    def feed(self, ev: dict) -> list[str]:
        out: list[str] = []
        t = ev.get("type")
        if ev.get("session_id"):
            self.session_id = ev["session_id"]
        if t == "system":
            st = ev.get("subtype")
            if st == "init":
                self.model = ev.get("model")
                out.append(f"🟢 Session started · `{self.model}`")
            elif st == "permission_denied":
                self._denied.add(ev.get("tool_use_id", ""))
                out.append(f"⛔ Blocked: **{ev.get('tool_name', '?')}**")
            elif st == "status" and ev.get("status") == "compacting":
                out.append("🗜️ Compacting the conversation…")
            elif st == "compact_boundary":
                meta = ev.get("compact_metadata") or {}
                pre, post = int(meta.get("pre_tokens") or 0), int(meta.get("post_tokens") or 0)
                # post_tokens is the conversation summary only; the ~11k of fixed instructions and tool definitions
                # still go with every message, so the next message measures the real context size.
                self.compacted, self.context_tokens = (pre, post), None
                self.transcript.append(f"[compact] {meta.get('trigger')} {pre} -> {post} tokens")
                out.append(f"🗜️ Compacted ({meta.get('trigger', 'manual')}): {pre:,} → {post:,} tokens")
        elif t == "assistant":
            if (ev.get("message") or {}).get("id"):
                self.api_calls.add(ev["message"]["id"])
            u = (ev.get("message") or {}).get("usage") or {}
            if u:
                self.context_tokens = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                                       + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("output_tokens") or 0))
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    text = redact(b["text"].strip())
                    self.transcript.append(f"[assistant]\n{text}")
                    out.append(f"💬 {oneline(text, 180)}")
                elif b.get("type") == "tool_use":
                    short = str(b.get("name", "")).removeprefix("mcp__bot__")
                    if short in ACTION_TOOLS and isinstance(b.get("input"), dict):
                        self._pending[b.get("id", "")] = (short, b["input"])
                    inp = redact(json.dumps(b.get("input"), ensure_ascii=False))
                    self.transcript.append(f"[tool_use] {b.get('name')} {clip(inp, 2000)}")
                    out.append(f"🔧 {summarize_tool(b.get('name', '?'), b.get('input'))}")
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = redact(_flatten_content(b.get("content")))
                    err = bool(b.get("is_error"))
                    act = self._pending.pop(b.get("tool_use_id", ""), None)
                    if act and not err:
                        self.actions.append(act)
                    self.transcript.append(f"[tool_result{' ERROR' if err else ''}]\n{clip(text, 2000)}")
                    if err and b.get("tool_use_id") not in self._denied:
                        out.append(f"⚠️ {oneline(text, 160)}")
        elif t == "result":
            self.result = ev
            self.transcript.append(f"[result] is_error={ev.get('is_error')} turns={ev.get('num_turns')} "
                                   f"duration_ms={ev.get('duration_ms')} cost={ev.get('total_cost_usd')}")
            self.transcript.append(cost_breakdown(ev))
        return out


@dataclass
class CCJob:
    channel: discord.abc.Messageable
    task: str
    snap: CCSnap
    user_id: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parser: StreamParser = field(default_factory=StreamParser)
    lines: deque = field(default_factory=lambda: deque(maxlen=10))
    status: str = "queued"  # queued | running | done
    proc: asyncio.subprocess.Process | None = None
    started: float | None = None
    ended: float | None = None
    stop_requested: bool = False
    timed_out: bool = False
    error: str | None = None
    stderr_tail: str = ""
    msg: discord.Message | None = None
    dirty: bool = True
    cost_this: float | None = None  # what this message cost (see record_cost)
    cost_session: float | None = None  # the session's running total
    chat: bool = False  # reply as a plain message (typing indicator, no cards/buttons) instead of embeds
    out: Any = None  # where a chat reply goes (Out: reply to the user's message or the slash command)
    notices: list[str] = field(default_factory=list)  # what the bot did for [[remind:]] / [[delete:]] markers
    scheduled: bool = False  # started by a scheduled task (can't schedule more)
    reminders_ok: bool = False  # a scheduled job whose task may set reminders
    ctx_limit: int | None = None  # the model's real context window (Ollama), when known
    ctx_approx: bool = False  # context_tokens is an estimate
    asked_at: float = field(default_factory=time.time)  # wall clock: where its activity-feed entry belongs

    def elapsed(self) -> int:
        if not self.started:
            return 0
        return int((self.ended or time.monotonic()) - self.started)

    def outcome(self) -> str:
        r = self.parser.result
        if self.stop_requested:
            return "stopped"
        if self.timed_out or self.error or not r or r.get("is_error"):
            return "error"
        return "success"


_cc_lock = asyncio.Lock()
_cc_waiting = 0
_cc_current: CCJob | None = None
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro: Awaitable) -> asyncio.Task:
    t = asyncio.ensure_future(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            k = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F",
                                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await k.wait()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def cc_prompt(job: CCJob) -> str:
    """What Claude Code receives. Claude otherwise assumes UTC (5h30 behind IST), so "8am today" at 02:30 IST became
    tomorrow. The time goes in the message, not the system prompt, so the cached prefix stays the same."""
    if job.snap.compact or job.task.strip().startswith("/"):
        return job.task
    return f"[Now: {now_local():%A %d %B %Y, %H:%M} {TIMEZONE}]\n{job.task}"


async def _execute(job: CCJob) -> None:
    binary = claude_bin()
    if not binary:
        job.error = "Claude Code CLI not found (install it or set CLAUDE_BIN)."
        return
    ws = WORKSPACES.get(job.snap.workspace)
    if ws is None:
        job.error = f"Workspace '{job.snap.workspace}' is not whitelisted."
        return
    ws.mkdir(parents=True, exist_ok=True)
    kw: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    )
    job.status, job.started = "running", time.monotonic()
    job.dirty = True
    proc = await asyncio.create_subprocess_exec(
        *build_cc_command(binary, job.snap), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(ws), env=build_cc_env(job.snap), limit=64 * 1024 * 1024, **kw,
    )
    job.proc = proc
    try:
        proc.stdin.write(cc_prompt(job).encode("utf-8"))  # prompt via stdin: no argv quoting/injection issues
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        proc.stdin.close()

    async def read_stderr():
        buf = b""
        async for chunk in proc.stderr:
            buf = (buf + chunk)[-4000:]
        job.stderr_tail = buf.decode("utf-8", "replace")

    async def read_stdout():
        async for raw in proc.stdout:
            new = job.parser.feed_line(raw.decode("utf-8", "replace"))
            if new:
                job.lines.extend(new)
                job.dirty = True
            if job.parser.result:  # done; the CLI may linger on background tasks, so stop reading
                return

    err_task = asyncio.create_task(read_stderr())
    try:
        await asyncio.wait_for(read_stdout(), timeout=CLAUDE_TIMEOUT)
        await asyncio.wait_for(proc.wait(), timeout=5)  # grace period after the result event
    except asyncio.TimeoutError:
        if not job.parser.result:
            job.timed_out = True
            job.error = f"Timed out after {CLAUDE_TIMEOUT}s."
    finally:
        await kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), 10)
            await asyncio.wait_for(err_task, 5)
        except asyncio.TimeoutError:
            err_task.cancel()
    if not job.parser.result and not job.error and not job.stop_requested:
        job.error = f"Claude Code exited (code {proc.returncode}) without a result."


async def _render_loop(job: CCJob) -> None:
    last = 0.0
    while job.status != "done":
        await asyncio.sleep(PROGRESS_EDIT_EVERY)
        if job.status == "done" or job.msg is None:
            break
        if job.dirty or time.monotonic() - last > 10:
            job.dirty, last = False, time.monotonic()
            try:
                await job.msg.edit(embed=progress_embed(job))
            except discord.HTTPException as e:
                log.warning("progress edit failed: %s", e)


@contextlib.asynccontextmanager
async def _typing(channel, enabled: bool):
    """Show "<bot> is typing…" for the whole job (chat mode). Never lets a typing error break the job."""
    cm = None
    if enabled and hasattr(channel, "typing"):
        try:
            cm = channel.typing()
            await cm.__aenter__()
        except Exception:
            cm = None
    try:
        yield
    finally:
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass


async def _run_job(job: CCJob) -> None:
    async with _typing(job.channel, job.chat):
        await _run_job_inner(job)


async def _run_job_inner(job: CCJob) -> None:
    global _cc_waiting, _cc_current
    renderer = asyncio.create_task(_render_loop(job))
    try:
        _cc_waiting += 1
        try:
            await _cc_lock.acquire()
        finally:
            _cc_waiting -= 1
        try:
            if not job.stop_requested:
                _cc_current = job
                await _execute(job)
                # --resume on a missing session fails before "init"; retry once as a new session.
                if (job.snap.resume and not job.stop_requested and job.parser.model is None
                        and (job.parser.result or {}).get("is_error")):
                    job.snap = replace(job.snap, resume=None)
                    job.parser, job.error, job.stderr_tail = StreamParser(), None, ""
                    job.lines.append("↩️ Previous session not found; starting a new one")
                    job.dirty = True
                    await _execute(job)
        finally:
            _cc_current = None
            _cc_lock.release()
    except Exception as e:
        log.exception("Claude Code job failed")
        job.error = f"{type(e).__name__}: {e}"
    finally:
        job.ended = time.monotonic()
        job.status = "done"
        renderer.cancel()
        await _finalize(job)


_usage: dict = {}


def spent_today() -> float:
    return float(_usage.get("usd", 0.0)) if _usage.get("date") == now_local().date().isoformat() else 0.0


def add_spend(usd: float) -> None:
    today = now_local().date().isoformat()
    if _usage.get("date") != today:
        _usage.update(date=today, usd=0.0, jobs=0)  # keeps the per-session totals ("sessions")
    _usage["usd"] = round(_usage["usd"] + usd, 6)
    _usage["jobs"] += 1
    STORE.save("usage", USAGE_FILE, _usage)


def budget_block(snap: CCSnap) -> str | None:
    """Refusal message if the daily Claude Code budget is used up (Anthropic backend only)."""
    if snap.backend == "anthropic" and CC_DAILY_BUDGET_USD > 0 and spent_today() >= CC_DAILY_BUDGET_USD:
        return (f"💸 Daily Claude Code budget reached (${spent_today():.2f} of ${CC_DAILY_BUDGET_USD:g}, API-equivalent). "
                "It resets at midnight; raise CC_DAILY_BUDGET_USD in .env to allow more.")
    return None


def record_cost(job: CCJob) -> None:
    """The CLI's total_cost_usd is a running total for the whole session (verified: it rises across resumes and
    stays put on a no-op turn). Per-message cost = this total minus the session's previous total, which we keep
    per session id. A resumed session we have no record of (started before v11) only gets its session total."""
    total = (job.parser.result or {}).get("total_cost_usd")
    if not isinstance(total, (int, float)):
        return
    seen: dict = _usage.setdefault("sessions", {})
    sid = job.parser.session_id
    prev = seen.get(sid) if job.snap.resume else 0.0
    job.cost_session = float(total)
    job.cost_this = max(0.0, total - prev) if prev is not None else None
    if sid:
        seen.pop(sid, None)
        seen[sid] = total
        for old in list(seen)[:-300]:  # keep the file small
            seen.pop(old)
    if job.snap.backend == "anthropic":
        add_spend(job.cost_this if job.cost_this is not None else total)
    else:
        STORE.save("usage", USAGE_FILE, _usage)


def unbacked_claims(asked: str, reply: str, raw: str) -> list[str]:
    """Small local models sometimes answer "Done, reminder set!" without doing it (seen with qwen3.5:9b). When the
    user asked for an action, the reply claims it, and the bot saw no matching marker/tool call, say so plainly:
    a silent false "done" is worst for things like medication reminders."""
    a, r = asked.lower(), reply.lower()
    out = []
    if (re.search(r"\bremind|\bschedul|\bevery (day|morning|evening|night|week|hour|monday|weekday)|\bdaily\b", a)
            and re.search(r"reminder (is |has been )?(set|scheduled|created)|\b(i'?ll|i will|will) (remind|ping|check|"
                          r"let you know|notify)|you'?ll (be reminded|get (a )?ping|be pinged)|\bscheduled\b|\bset up\b", r)
            and not (REMIND_RE.search(raw) or TASK_RE.search(raw))):
        out.append("⚠️ not done: it says this is set up, but no reminder or task was created (check /tasks, or use /remind)")
    if (re.search(r"\bdelet|\bremove", a) and re.search(r"\b(deleted|removed|deleting)\b", r)
            and not DELETE_RE.search(raw)):
        out.append("⚠️ not done: it says the file was deleted, but nothing was deleted")
    if (re.search(r"\b(send|attach|share)\b", a) and re.search(r"\b(attached|sent (it|you|the file)|here'?s the file)\b", r)
            and not ATTACH_RE.search(raw)):
        out.append("⚠️ not done: it says a file was sent, but nothing was attached")
    return out


async def _finalize(job: CCJob) -> None:
    record_cost(job)
    # Context = the last request's prompt (Ollama's count matches its own log: ~4.4-5.3k on qwen3.5:9b).
    if job.snap.backend == "ollama":
        job.ctx_limit = await ctx_limit(OLLAMA_URL, job.snap.model)
    r0 = job.parser.result
    deletes: list[Path] = []
    text = (r0 or {}).get("result") or ""
    if r0 and job.parser.actions:
        text = r0["result"] = f"{text}\n{action_markers(job.parser.actions)}".strip()
    raw = text
    if r0 and job.outcome() == "success" and any(rx.search(text) for rx in (REMIND_RE, TASK_RE, DELETE_RE)):
        ch_id = getattr(job.channel, "id", 0)
        if job.scheduled:  # no self-perpetuating chains from unattended runs
            if TASK_RE.search(text):
                text = TASK_RE.sub("", text).strip()
                job.notices.append("⚠️ scheduled runs can't create tasks")
            if job.reminders_ok:
                text, lines = extract_reminders(text, ch_id, job.user_id)
                job.notices += lines
            elif REMIND_RE.search(text):
                text = REMIND_RE.sub("", text).strip()
                job.notices.append("⚠️ this task isn't allowed to set reminders (an owner can allow it in /tasks)")
        else:
            text, job.notices = extract_reminders(text, ch_id, job.user_id)
            text, lines = extract_tasks(text, ch_id, job.user_id, job.snap)
            job.notices += lines
        text, deletes, lines = plan_deletes(text, job.snap)
        job.notices += lines
        r0["result"] = text or "👍"
    if r0 and job.outcome() == "success" and not job.scheduled and not job.snap.compact:
        job.notices += unbacked_claims(job.task, r0.get("result") or "", raw)
    # Remember the session for "continue", unless backend/workspace changed during the run.
    if job.snap.backend == "ollama":
        touch_model(OLLAMA_URL, job.snap.model)
    ch_id = getattr(job.channel, "id", 0)
    cur = get_settings(ch_id)
    if job.parser.session_id and cur["cc_backend"] == job.snap.backend and cur["workspace"] == job.snap.workspace:
        update_settings(ch_id, cc_session=job.parser.session_id, cc_session_at=time.time(),
                        cc_session_path=str(WORKSPACES.get(job.snap.workspace, "")),
                        cc_session_setup=CC_SETUP_FINGERPRINT, cc_session_ctx=job.parser.context_tokens,
                        cc_session_ctx_limit=job.ctx_limit)
    r = job.parser.result or {}
    this = f"${job.cost_this:.4f}" if job.cost_this is not None else "unknown"
    log.info("CC job %s: %s, %s turns, this message %s, session total $%.4f, context %s tokens",
             job.id, job.outcome(), r.get("num_turns"), this, job.cost_session or 0, job.parser.context_tokens)
    STORE.record_job({"job_id": job.id, "frontend": "web" if is_web_id(ch_id) else "telegram" if is_telegram_id(ch_id)
                      else "discord",
                      "channel_id": ch_id, "user_id": job.user_id, "backend": job.snap.backend, "model": job.snap.model,
                      "perm": job.snap.perm, "outcome": job.outcome(), "turns": r.get("num_turns"),
                      "cost_usd": job.cost_this, "session_cost_usd": job.cost_session,
                      "context_tokens": job.parser.context_tokens, "seconds": job.elapsed(),
                      "session_id": job.parser.session_id, "prompt": clip(redact(job.task), 500)})
    note_event("claude", job.task, level="error" if job.outcome() == "error" else "info", channel_id=ch_id,
               user_id=job.user_id, status=job.outcome(), job=job.id, backend=job.snap.backend, model=job.snap.model,
               perm=job.snap.perm, scheduled=job.scheduled or None, turns=r.get("num_turns"), cost=job.cost_this,
               seconds=job.elapsed(), context=job.parser.context_tokens, error=job.error,
               reply=clip(r.get("result") or "", 600) or None, notices=job.notices or None, started=job.asked_at)
    posted =await (_send_chat_reply(job) if job.chat else _send_result_card(job))
    if deletes and not posted:  # never delete a file the user didn't receive
        log.warning("Reply for job %s not posted; skipped deleting %d file(s)", job.id, len(deletes))
        deletes = []
    # After the reply went out: attachments were read into memory while rendering, so "send it, then delete it" works.
    for p in deletes:
        try:
            p.unlink()
            log.info("Deleted %s from workspace %s (job %s)", p.name, job.snap.workspace, job.id)
        except OSError as e:
            log.warning("delete %s failed: %s", p, e)
            try:
                await job.channel.send(quiet_grey(redact(f"-# couldn't delete `{p.name}`: {e.strerror or e}")))
            except discord.HTTPException:
                pass


async def _send_result_card(job: CCJob) -> bool:
    embed, files = result_embed(job)
    try:
        if job.msg:
            await job.msg.edit(embed=progress_embed(job), view=None)
    except discord.HTTPException:
        pass
    kwargs: dict[str, Any] = {"embed": embed, "view": CCResultView(job)}
    if files:
        kwargs["files"] = files
    if job.msg:
        kwargs["reference"] = job.msg.to_reference(fail_if_not_exists=False)
        kwargs["mention_author"] = False
    try:
        await job.channel.send(**kwargs)
    except discord.HTTPException:
        log.exception("could not post result card")
        return False
    return True


_last_job: dict[int, CCJob] = {}  # per channel, for /stop and /log


async def start_cc_job(channel: discord.abc.Messageable, task: str, snap: CCSnap, user_id: int, *,
                       out: "Out | None" = None, chat: bool = False, scheduled: bool = False,
                       reminders_ok: bool = False) -> CCJob:
    job = CCJob(channel=channel, task=task, snap=snap, user_id=user_id, chat=chat, out=out or Out(channel),
                scheduled=scheduled, reminders_ok=reminders_ok)
    if snap.note:
        job.lines.append(snap.note)
    if not chat:  # chat mode shows only the typing indicator, then a plain reply
        job.msg = await channel.send(embed=progress_embed(job), view=CCProgressView(job))
    _last_job[getattr(channel, "id", 0)] = job
    _spawn(_run_job(job))
    return job


# =============================================================================
# Embeds
# =============================================================================
COLOR_RUN = 0x5865F2
COLOR_OK = 0x2ECC71
COLOR_ERR = 0xE74C3C
COLOR_STOP = 0xF1C40F


def _snap_footer(snap: CCSnap) -> str:
    return f"{snap.backend} · {snap.model} · {snap.perm} · {snap.workspace}"


def progress_embed(job: CCJob) -> discord.Embed:
    if job.status == "queued":
        title, color = f"⏳ Claude Code: waiting ({_cc_waiting} in queue)", COLOR_STOP
    elif job.status == "running":
        title, color = "🤖 Claude Code: running", COLOR_RUN
    else:
        o = job.outcome()
        title, color = {"success": ("✅ Claude Code: finished", COLOR_OK), "stopped": ("⏹️ Claude Code: stopped", COLOR_STOP)}.get(
            o, ("❌ Claude Code: failed", COLOR_ERR))
    lines = "\n".join(job.lines) or "_starting…_"
    e = discord.Embed(title=title, color=color, description=f"**Task:** {clip(job.task, 400)}\n\n{clip(lines, 3000)}")
    e.set_footer(text=f"{_snap_footer(job.snap)} · ⏱ {job.elapsed()}s" + (" · resumed" if job.snap.resume else ""))
    return e


ATTACH_RE = re.compile(r"\[\[attach:\s*([^\]\n]+?)\s*\]\]", re.I)
ATTACH_MAX_FILES = 9  # Discord allows 10 per message; one slot is kept for result.md
ATTACH_MAX_BYTES = 10 * 1024 * 1024  # Discord's default upload limit
# Never upload files that typically hold credentials, even from inside the workspace.
ATTACH_BLOCKED = re.compile(r"(^\.env(\..*)?$|\.(pem|key|p12|pfx|kdbx|keystore|jks)$|^id_(rsa|dsa|ecdsa|ed25519)"
                            r"|^(credentials|secrets?)(\.\w+)?$|^\.(netrc|npmrc|pypirc|git-credentials|pgpass)$)", re.I)


DELETE_RE = re.compile(r"\[\[delete:\s*([^\]\n]+?)\s*\]\]", re.I)
DELETE_MAX = 20


def plan_deletes(text: str, snap: CCSnap) -> tuple[str, list[Path], list[str]]:
    """[[delete: path]] markers -> files to delete after the reply is posted. Claude Code has no delete tool without
    shell access; this gives edit/full mode a way to remove regular files inside the job's workspace only (never
    folders, never outside it). Returns (text without markers, paths, notice lines)."""
    paths: list[Path] = []
    lines: list[str] = []
    ws = WORKSPACES.get(snap.workspace)
    root = ws.resolve() if ws is not None else None
    for raw in (m.group(1).strip().strip("`\"'") for m in DELETE_RE.finditer(text)):
        name = redact(raw)
        p = (Path(raw) if Path(raw).is_absolute() else (root or Path.cwd()) / raw).resolve()
        if snap.perm not in ("edit", "full"):
            lines.append(f"⚠️ `{name}` not deleted: {snap.perm}-only mode (change in /panel → ⚙️ Settings)")
        elif root is None or not p.is_relative_to(root) or p == root:
            lines.append(f"⚠️ `{name}` not deleted: outside the workspace")
        elif not p.is_file():
            lines.append(f"⚠️ `{name}` not deleted: " + ("folders can't be deleted this way" if p.is_dir() else "not found"))
        elif len(paths) >= DELETE_MAX:
            lines.append(f"⚠️ `{name}` not deleted: more than {DELETE_MAX} files")
        elif p not in paths:
            paths.append(p)
            lines.append(f"🗑️ deleted `{p.relative_to(root).as_posix()}` from the workspace")
    return DELETE_RE.sub("", text).strip(), paths, lines


def extract_attachments(text: str, workspace: Path) -> tuple[str, list[discord.File], list[str]]:
    """Turn [[attach: path]] markers in Claude's reply into Discord files. Only regular files inside the job's
    workspace (after resolving symlinks and ../), under the size limit and not credential-like; text files are
    redacted like everything else. Returns (text without markers, files, notes about what was skipped)."""
    files: list[discord.File] = []
    notes: list[str] = []
    root = workspace.resolve()
    seen: set[Path] = set()
    for raw in (m.group(1).strip().strip("`\"'") for m in ATTACH_RE.finditer(text)):
        p = Path(raw)
        p = (p if p.is_absolute() else root / p).resolve()
        name = redact(raw)
        if p in seen:
            continue
        seen.add(p)
        if not p.is_relative_to(root):
            notes.append(f"`{name}`: outside the workspace")
        elif not p.is_file():
            notes.append(f"`{name}`: not found")
        elif ATTACH_BLOCKED.search(p.name):
            notes.append(f"`{name}`: blocked (may contain credentials)")
        elif p.stat().st_size > ATTACH_MAX_BYTES:
            notes.append(f"`{name}`: larger than {ATTACH_MAX_BYTES // 1024 // 1024} MB")
        elif len(files) >= ATTACH_MAX_FILES:
            notes.append(f"`{name}`: more than {ATTACH_MAX_FILES} files")
        else:
            data = p.read_bytes()
            try:
                data = redact(data.decode("utf-8")).encode("utf-8")  # text: scrub secrets; binary: send as-is
            except UnicodeDecodeError:
                pass
            files.append(discord.File(io.BytesIO(data), filename=p.name))
    return ATTACH_RE.sub("", text).strip(), files, notes


def result_text(job: CCJob, chat: bool = False) -> tuple[str, list[discord.File], list[str]]:
    """Final reply text (redacted, attachment markers resolved), attached files, and notes about skipped files."""
    r = job.parser.result or {}
    outcome = job.outcome()
    text = (r.get("result") or "").strip()
    if job.parser.compacted and not text:
        pre, post = job.parser.compacted
        text = (f"🗜️ Compacted: the conversation ({pre:,} tokens) is now a {post:,}-token summary." if chat else
                f"The conversation ({pre:,} tokens) was replaced by a **{post:,}-token summary**. Messages continue "
                "from the summary. About 11k tokens of fixed instructions and tools still go with every message, "
                "so compacting pays off most on long conversations.")
    if r.get("subtype") == "error_max_budget_usd":
        text = f"💸 Stopped at the per-job budget (CC_MAX_BUDGET_USD=${CC_MAX_BUDGET_USD:g}).\n\n{text}".strip()
    if not text:
        text = (job.error or ("Stopped by user." if job.stop_requested else "") or "\n".join(r.get("errors") or [])
                or clip(job.stderr_tail.strip(), 1500) or "(no output)")
    elif job.error:
        text = f"{job.error}\n\n{text}"
    ws = WORKSPACES.get(job.snap.workspace)
    files, notes = [], []
    if ws is not None and outcome != "stopped":
        text, files, notes = extract_attachments(text, ws)
    if outcome == "error":  # what to try, from fixed rules over everything the CLI said
        text = with_hint(text, job.error, "\n".join(r.get("errors") or []), r.get("subtype"), job.stderr_tail,
                         where="claude", model=job.snap.model)
    return redact(text) or ("📎" if chat and files else "📎 Attached below." if files else "(no output)"), files, notes


def host_label(url: str, model: str) -> str:
    """'qwen3.5:9b self-hosted' when the model server is this machine or the LAN, else 'model via host'."""
    host = (urlsplit(url).hostname or "").lower()
    try:
        private = host in ("localhost", "") or ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        private = host.endswith((".local", ".lan", ".home", ".localhost"))
    return f"{model} self-hosted" if private else f"{model} via {host}"


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF⌀-⏿☀-➿⬀-⯿️‍⃣]+")


def quiet_grey(text: str) -> str:
    """Grey '-#' lines are meant to be unobtrusive: plain text, no emoji (the user asked). Other lines untouched."""
    return "\n".join(re.sub(r" {2,}", " ", _EMOJI_RE.sub("", ln)).rstrip() if ln.startswith("-# ") else ln
                     for ln in text.split("\n"))


# Ends the routine part of a stats line (model, cost, context, session). Invisible on Discord; lets a front end
# that can't make text small/grey (Telegram) hide that part while keeping any warnings after it visible.
STATS_MARK = "\u2063"  # INVISIBLE SEPARATOR


def stats_line(parts: list[str], warnings: list[str]) -> str:
    return "-# " + " · ".join(parts) + STATS_MARK + "".join(" · " + w for w in warnings)


def fmt_usd(x: float) -> str:
    return "<$0.001" if 0 < x < 0.001 else f"${x:.3f}"


def chat_stats(job: CCJob, notes: list[str]) -> str:
    """The one small grey line under a chat reply: model, cost, today's spend, context, plus warnings if any."""
    parts: list[str] = []
    warnings: list[str] = []
    if job.snap.note:
        parts.append("new chat")
    if job.snap.backend == "anthropic":
        parts.append(job.snap.model)
    else:  # Claude Code on Ollama / a custom gateway: say where it runs, and time (local replies are slow)
        parts.append(host_label(OLLAMA_URL if job.snap.backend == "ollama" else CC_CUSTOM_BASE_URL, job.snap.model))
        parts.append(f"{job.elapsed()}s")
    if job.snap.backend == "anthropic":
        if job.cost_this is not None:
            parts.append(fmt_usd(job.cost_this))
        elif job.cost_session is not None:
            parts.append(f"session {fmt_usd(job.cost_session)}")
        cap = f"/${CC_DAILY_BUDGET_USD:g}" if CC_DAILY_BUDGET_USD > 0 else ""
        parts.append(f"today ${spent_today():.2f}{cap}")
    ctx = job.parser.context_tokens
    if ctx:
        parts.append(ctx_text(ctx, job.ctx_limit, job.ctx_approx))
        if job.ctx_limit and ctx >= job.ctx_limit * 0.9:
            warnings.append("context almost full, older messages will be dropped: send /compact")
        elif compact_at(job.ctx_limit) and ctx >= compact_at(job.ctx_limit):
            warnings.append("long chat, send /compact")
    if job.parser.session_id:  # same short id as the cards and `claude --resume <id>` in a terminal
        parts.append(f"session {job.parser.session_id[:8]}")
    denied = sorted({d.get("tool_name", "?") for d in (job.parser.result or {}).get("permission_denials") or []})
    if denied:
        warnings.append(f"⛔ blocked {', '.join(denied)} ({job.snap.perm} mode, change in /panel)")
    if notes:
        warnings.append("⚠️ " + "; ".join(n.replace("`", "") for n in notes))
    if job.outcome() == "stopped":
        warnings.append("stopped")
    return stats_line(parts, warnings)


async def _send_chat_reply(job: CCJob) -> bool:
    """Post the result like a person would: plain message(s) replying to the user, one small stats line."""
    text, files, notes = result_text(job, chat=True)
    if job.outcome() == "error" and not text.startswith("⚠️"):
        text = f"⚠️ {text}"
    chunks = split_message(text)
    if len(chunks) > 4:  # very long: first part in chat, the rest as a file
        files.append(discord.File(io.BytesIO(text.encode("utf-8")), filename="reply.md"))
        chunks = [clip(chunks[0], 1800) + "\n*(full reply in reply.md)*"]
    if job.notices:
        chunks[-1] += "".join(f"\n-# {line}" for line in job.notices)
    stats = chat_stats(job, notes)
    if len(chunks[-1]) + len(stats) + 1 <= 1990:
        chunks[-1] += "\n" + stats
    else:
        chunks.append(stats)
    try:
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            await job.out(content=chunk, files=files if last and files else None)
    except discord.HTTPException:
        log.exception("could not post chat reply")
        return False
    return True


def result_embed(job: CCJob) -> tuple[discord.Embed, list[discord.File]]:
    r = job.parser.result or {}
    outcome = job.outcome()
    color = {"success": COLOR_OK, "stopped": COLOR_STOP}.get(outcome, COLOR_ERR)
    title = {"success": "✅ Result", "stopped": "⏹️ Stopped"}.get(outcome, "❌ Error")
    if job.parser.compacted and not (r.get("result") or "").strip():
        title = "🗜️ Compacted"
    text, files, notes = result_text(job)
    if len(text) > 3800:
        files.append(discord.File(io.BytesIO(text.encode("utf-8")), filename="result.md"))
        text = clip(text, 1500) + "\n\n*(full result attached as result.md)*"
    e = discord.Embed(title=title, color=color, description=text)
    if files or notes:
        names = [f"📎 `{f.filename}`" for f in files if f.filename != "result.md"]
        e.add_field(name="Attachments", inline=False, value=clip("\n".join(names + [f"⚠️ {n}" for n in notes]), 1024))
    if job.notices:
        e.add_field(name="Done by the bot", inline=False, value=clip(redact("\n".join(job.notices)), 1024))
    if r.get("num_turns") is not None:
        e.add_field(name="Turns", value=str(r["num_turns"]))
    e.add_field(name="Time", value=f"{(r.get('duration_ms') or job.elapsed() * 1000) / 1000:.0f}s")
    if job.snap.backend == "anthropic" and job.cost_session is not None:
        if job.cost_this is not None:
            cost = f"${job.cost_this:.4f}" + (f"\n-# session ${job.cost_session:.4f}" if job.snap.resume else "")
        else:
            cost = f"${job.cost_session:.4f}\n-# session total"
        e.add_field(name="API-equiv. cost", value=cost)
    ctx = job.parser.context_tokens
    if ctx:
        big = compact_at(job.ctx_limit) and ctx >= compact_at(job.ctx_limit) and not job.parser.compacted
        e.add_field(name="Context", value=ctx_text(ctx, job.ctx_limit, job.ctx_approx).replace(" ctx", " tokens")
                    + (" ⚠️" if big else ""))
        if big:
            e.add_field(name="🗜️ Time to compact?", inline=False, value=(
                f"This conversation is {ctx / 1000:.0f}k tokens and every message re-reads all of it. "
                "**🗜️ Compact** shrinks it to a summary; **🆕 New session** (in /panel) starts clean."))
    denials = r.get("permission_denials") or []
    if denials:
        items = [f"• {d.get('tool_name', '?')} {oneline(summarize_tool(d.get('tool_name', ''), d.get('tool_input')).split(' ', 1)[-1], 80) if d.get('tool_input') else ''}"
                 for d in denials[:6]]
        more = f"\n…and {len(denials) - 6} more" if len(denials) > 6 else ""
        e.add_field(name=f"⛔ Blocked actions ({len(denials)})", inline=False,
                    value=clip("\n".join(items) + more + "\n-# Raise permissions in /panel → ⚙️ Settings, then 🔁 Retry.", 1024))
    e.set_footer(text=f"{_snap_footer(job.snap)}" + (f" · session {job.parser.session_id[:8]}" if job.parser.session_id else ""))
    return e, files


def transcript_file(job: CCJob) -> discord.File:
    head = [f"Task: {job.task}", f"Settings: {_snap_footer(job.snap)} resume={job.snap.resume}",
            f"Session: {job.parser.session_id}", f"Outcome: {job.outcome()} ({job.elapsed()}s)", "=" * 60]
    body = "\n\n".join(job.parser.transcript)
    tail = f"\n\n[stderr tail]\n{job.stderr_tail}" if job.stderr_tail.strip() else ""
    text = redact("\n".join(head) + "\n" + body + tail)
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=f"claude-{job.id}.txt")


def confirm_embed(task: str, snap: CCSnap, reason: str | None = None, title: str = "🤖 Run with Claude Code?") -> discord.Embed:
    full = snap.perm == "full"
    e = discord.Embed(title=title, color=COLOR_ERR if full else COLOR_RUN, description=clip(task, 3500))
    if reason:
        e.add_field(name="Why", value=clip(reason, 1024), inline=False)
    e.add_field(name="Model", value=f"{BACKENDS[snap.backend][0]} {snap.backend} · `{snap.model}`")
    e.add_field(name="Permissions", value=f"{PERMS[snap.perm][0]} {PERMS[snap.perm][1]}")
    e.add_field(name="Workspace", value=f"`{snap.workspace}`")
    e.add_field(name="Session", value=f"resume `{snap.resume[:8]}`" if snap.resume else "new")
    if full:
        e.set_footer(text="⚠️ FULL ACCESS: Claude Code can run any command without asking.")
    return e


def panel_embed(channel_id: int) -> discord.Embed:
    s = get_settings(channel_id)
    e = discord.Embed(title="🎛️ Control panel", color=COLOR_RUN, description=f"Settings for {where(channel_id)}")
    eng = ENGINES[s["engine"]]
    e.add_field(name="Engine", value=f"{eng[0]} {eng[1]}")
    st = STYLES[s["style"]]
    e.add_field(name="Reply style", value=f"{st[0]} {st[1]}")
    e.add_field(name="Local model", value=f"`{s['local_model']}`")
    e.add_field(name="Local status", value=("🔴 busy" if _llm_lock.locked() else "🟢 idle") + memory_status(s["local_model"]))
    e.add_field(name="Claude Code", value=f"{BACKENDS[s['cc_backend']][0]} {s['cc_backend']} · `{s['cc_model']}`")
    p = PERMS[s["cc_perm"]]
    e.add_field(name="Permissions", value=f"{p[0]} {p[1]}")
    e.add_field(name="Workspace", value=f"`{s['workspace']}`")
    sid, why = session_state(s)
    if sid:
        idle = (time.time() - (s.get("cc_session_at") or 0)) / 60
        sess = f"`{sid[:8]}…` · idle {idle:.0f}m"
        ctx = s.get("cc_session_ctx")
        if ctx:
            lim = s.get("cc_session_ctx_limit")
            sess += f" · {ctx_text(ctx, lim, s['cc_backend'] != 'anthropic')}"
            if compact_at(lim) and ctx >= compact_at(lim):
                sess += " ⚠️ send `/compact`"
    else:
        sess = f"new next time ({why})" if why else "none (new)"
    e.add_field(name="CC session", value=sess)
    cli = f"✅ {CLAUDE_VERSION}" if claude_bin() and CLAUDE_VERSION else ("✅ found" if claude_bin() else "❌ not found")
    e.add_field(name="CLI", value=cli if CC_ENABLED else f"{cli} · disabled (no OWNER_IDS)")
    busy = "🔴 running" if _cc_current else "🟢 idle"
    if _cc_waiting:
        busy += f" · {_cc_waiting} queued"
    e.add_field(name="CC status", value=busy)
    cap = f" of ${CC_DAILY_BUDGET_USD:g}" if CC_DAILY_BUDGET_USD > 0 else ""
    e.add_field(name="CC spend today", value=f"${spent_today():.2f}{cap} · {'lean' if not CC_EXTRAS else 'extras on'}"
                + (f" · effort {CC_EFFORT}" if CC_EFFORT else ""))
    e.set_footer(text=redact(f"Workspace path: {WORKSPACES[s['workspace']]}"))
    return e


def tasks_embed(user_id: int | None = None) -> discord.Embed:
    """user_id: whose list (owners see everything); None = everything."""
    tasks, rems = my_tasks(user_id), my_reminders(user_id)
    e = discord.Embed(title=f"⏰ Tasks ({len(tasks)}/{TASK_MAX}) & reminders ({len(rems)})", color=COLOR_RUN)
    if not tasks and not rems:
        e.description = ("Nothing scheduled. Just ask (\"remind me to … in 10 min\", \"every morning at 9 …\"), "
                         "or use `/remind` and `/schedule`.")
    for r in rems[:12]:
        dt = datetime.fromisoformat(r["when"])
        e.add_field(name=f"🔔 `{r['id']}` {clip(r['text'], 200)}", inline=False,
                    value=f"{discord.utils.format_dt(dt, 'f')} ({discord.utils.format_dt(dt, 'R')}) · {where(r['channel_id'])}")
    for t in tasks[:25 - min(len(rems), 12)]:
        e.add_field(name=f"🔁 `{t['id']}` {clip(t['description'], 200)}", inline=False,
                    value=f"{task_label(t)} · {where(t['channel_id'])}")
    return e


# =============================================================================
# Output helper: follow-up to an interaction, or reply/send in a channel
# =============================================================================


class Out:
    def __init__(self, channel: discord.abc.Messageable, interaction: discord.Interaction | None = None,
                 reply_to: discord.Message | None = None, prefix: str | None = None, ping: int | None = None):
        self.channel, self.interaction, self.reply_to = channel, interaction, reply_to
        self.prefix = prefix  # put above the first text reply (e.g. what a voice note was heard as)
        self.ping = ping  # user id allowed to be pinged by the first message (scheduled results); nobody else ever

    async def __call__(self, **kw) -> discord.Message:
        kw = {k: v for k, v in kw.items() if v is not None}
        if "content" in kw and self.prefix:
            prefix, self.prefix = self.prefix, None
            if len(prefix) + len(kw["content"]) + 1 <= 2000:
                kw["content"] = f"{prefix}\n{kw['content']}"
            else:
                await self(content=prefix)
        if "content" in kw and self.ping:
            kw["allowed_mentions"] = discord.AllowedMentions(everyone=False, roles=False, replied_user=False,
                                                             users=[discord.Object(self.ping)])
            self.ping = None
        if "content" in kw:
            kw["content"] = quiet_grey(redact(kw["content"]))
        if self.interaction is not None:
            try:
                return await self.interaction.followup.send(wait=True, **kw)
            except discord.HTTPException as e:
                if not (isinstance(e, discord.NotFound) or e.code == 50027):  # 50027 = expired token
                    raise
                self.interaction = None
        if self.reply_to is not None:
            ref, self.reply_to = self.reply_to, None
            try:
                return await ref.reply(mention_author=False, **kw)
            except discord.HTTPException:
                pass
        return await self.channel.send(**kw)


# =============================================================================
# High-level flows
# =============================================================================


def with_hint(message: str, *details: object, where: str = "", model: str | None = None) -> str:
    """Add a "💡 try this" line from the fixed rules in llmbot/hints.py (no model involved) when one matches."""
    return redact(hints_mod.with_hint(message, *details, where=where, model=model or LLM_MODEL, llm_url=LLM_URL,
                                      claude_timeout=CLAUDE_TIMEOUT))


async def answer_local(channel, user_id: int, prompt: str, out: Out, *, model: str | None = None,
                       allow_propose: bool = False, note: str | None = None, use_history: bool = True) -> None:
    ch_id = channel.id
    model = model or get_settings(ch_id)["local_model"]
    tools = CHAT_TOOLS + (["propose_claude_code"] if allow_propose and CC_ENABLED and is_owner(user_id) else [])
    ctx = ToolCtx(ch_id, user_id, tools)
    started, asked_at = time.monotonic(), time.time()
    try:
        with activity("local", prompt, ch_id, user_id, model=model):
            res = await run_local(prompt, model, ctx, list(_history.get(ch_id, [])) if use_history else None)
    except httpx.HTTPError as e:
        note_event("local", prompt, level="error", channel_id=ch_id, user_id=user_id, model=model,
                   error=f"could not reach the local model ({type(e).__name__})", started=asked_at)
        await out(content=with_hint(f"⚠️ Could not reach the local model ({type(e).__name__}).", str(e),
                                    where="local", model=model))
        return
    except Exception as e:
        log.exception("local engine failed")
        await out(content=with_hint(f"⚠️ Local model error: {oneline(redact(e), 300)}", type(e).__name__,
                                    where="local", model=model))
        return
    text = redact(res.text) or "_(empty reply)_"
    note_event("local", prompt, channel_id=ch_id, user_id=user_id, model=model,
               seconds=round(time.monotonic() - started, 1), tools=res.tools_used or None, reply=clip(text, 600),
               started=asked_at)
    if use_history:
        remember(ch_id, res.sent or prompt, text)
    chat = get_settings(ch_id)["style"] == "chat"
    footer = [f"-# {line}" for line in ctx.reminders]
    if chat:  # one small line, like the Claude Code chat replies
        # "local engine": the plain local model (short chat memory, no session/files), unlike Claude Code on Ollama
        # ~4 characters per token; Ollama's own counts leave out cached tokens. Memory = exchanges it still sees.
        limit = await ctx_limit(_llm_root(), model)
        mem = len(_history.get(ch_id, [])) // 2
        parts = ([host_label(LLM_URL, model), "local engine", f"{time.monotonic() - started:.0f}s",
                  ctx_text(ctx.max_chars // 4, limit, True), f"memory {mem}/{MAX_HISTORY_TURNS}"]
                 + (["used " + ", ".join(res.tools_used)] if res.tools_used else []))
        footer.append(stats_line(parts, [note] if note else []))
    else:
        if res.tools_used:
            footer.append("-# used: " + ", ".join(res.tools_used))
        if note:
            footer.append(f"-# {note}")
    chunks = split_message(text + ("\n" + "\n".join(footer) if footer else ""))
    view = None if chat else LocalReplyView(ch_id, prompt, model, allow_propose)
    for i, chunk in enumerate(chunks):
        await out(content=chunk, view=view if i == len(chunks) - 1 else None)
    if res.proposal and res.proposal.get("task"):
        snap = snapshot(ch_id)
        await out(embed=confirm_embed(res.proposal["task"], snap, res.proposal.get("reason")),
                  view=CCConfirmView(ch_id, res.proposal["task"], snap, res.proposal.get("reason")))


NEEDED_PERMS = {"view_channel": "View Channel", "send_messages": "Send Messages", "embed_links": "Embed Links",
                "attach_files": "Attach Files", "read_message_history": "Read Message History"}


def missing_perms(channel, inter: discord.Interaction | None = None) -> list[str]:
    """Channel permissions the bot lacks for posting cards. Slash commands work without them; channel.send doesn't."""
    if inter is not None and inter.guild is not None:
        perms = inter.app_permissions
    elif getattr(channel, "guild", None) is not None and hasattr(channel, "permissions_for"):
        perms = channel.permissions_for(channel.guild.me)
    else:
        return []  # DMs, or nothing to check
    return [label for attr, label in NEEDED_PERMS.items() if not getattr(perms, attr, True)]


def perm_help(missing: list[str]) -> str:
    name = bot.user.display_name if bot.user else "The bot"
    return (f"⛔ {name} can't post in this channel. Missing: **{', '.join(missing)}**.\n"
            f"Fix: Edit Channel → Permissions → add **{name}** and allow those, then try again.")


async def request_cc(channel, user_id: int, task: str, snap: CCSnap, out: Out, *, force_confirm: bool = False,
                     reason: str | None = None, chat: bool = False) -> None:
    """Start a Claude Code job, or show a confirmation card (always for full access)."""
    missing = missing_perms(channel, out.interaction)
    if missing:  # progress/result cards are channel messages, so check before starting anything
        await out(content=perm_help(missing))
        return
    if blocked := budget_block(snap):
        await out(content=blocked)
        return
    if force_confirm or snap.perm == "full":
        await out(embed=confirm_embed(task, snap, reason), view=CCConfirmView(channel.id, task, snap, reason))
        return
    job = await start_cc_job(channel, task, snap, user_id, out=out, chat=chat)
    if out.interaction is not None and not chat:  # chat mode: the slash command's "thinking…" becomes the reply
        await out(content=f"🤖 Claude Code job `{job.id}` started ↓")


UPLOAD_DIR = "discord_uploads"  # inside the workspace, so Claude Code's path-scoped Read(./**) can open them
UPLOAD_MAX_FILES = 10
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
UPLOAD_KEEP = timedelta(hours=24)


def _prune_uploads(folder: Path) -> None:
    cutoff = time.time() - UPLOAD_KEEP.total_seconds()
    for f in folder.glob("*"):
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def save_uploads(atts: list, workspace: Path) -> tuple[list[str], list[str]]:
    """Save Discord attachments into <workspace>/discord_uploads so Claude Code can Read them (it sees images and
    PDFs natively, and reads text/code). Returns (prompt lines, problems). Files are removed after a day."""
    folder = workspace / UPLOAD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    _prune_uploads(folder)
    lines: list[str] = []
    problems: list[str] = []
    for att in atts[:UPLOAD_MAX_FILES]:
        if att.size > UPLOAD_MAX_BYTES:
            problems.append(f"{att.filename}: larger than {UPLOAD_MAX_BYTES // 1024 // 1024} MB")
            continue
        name = re.sub(r"[^\w.-]", "_", att.filename)[-80:].lstrip(".") or "file"
        dest = folder / f"{att.id}-{name}"
        try:
            dest.write_bytes(await att.read())
        except (discord.HTTPException, OSError) as e:
            problems.append(f"{att.filename}: couldn't download ({type(e).__name__})")
            continue
        kind = (att.content_type or "").split(";")[0] or "file"
        lines.append(f"- {UPLOAD_DIR}/{dest.name} ({kind}, {att.size / 1024:.0f} KB)")
    if len(atts) > UPLOAD_MAX_FILES:
        problems.append(f"only the first {UPLOAD_MAX_FILES} attachments were used")
    return lines, problems


async def handle_prompt(channel, user_id: int, prompt: str, out: Out, attachments: list | None = None) -> None:
    """Route a prompt through the channel's engine. attachments: Discord files sent with the message."""
    s = get_settings(channel.id)
    engine, chat = s["engine"], s["style"] == "chat"
    atts = [a for a in attachments or [] if not a.is_voice_message()]
    cc_ok = engine == "claude" and CC_ENABLED and is_owner(user_id)
    if atts and not cc_ok:
        await out(content="-# 📎 Attachments only work with Claude Code (the local model gets text only); "
                          "they were ignored." + ("" if engine == "claude" else " Switch engine in /panel."))
        if not prompt.strip():
            return
    if atts and cc_ok:
        ws = WORKSPACES.get(s["workspace"])
        if ws is None:
            await out(content="⚠️ This channel's workspace no longer exists; attachments ignored.")
        else:
            lines, problems = await save_uploads(atts, ws)
            if problems:
                await out(content="-# 📎 " + "; ".join(problems))
            if lines:
                prompt = ((prompt.strip() or "(The user sent only these files, no message. Look at them and respond "
                           "as seems useful.)") + "\n\n[Files the user attached in the chat, saved in your working "
                          "directory. Open them with the Read tool (it shows images and PDFs):]\n" + "\n".join(lines))
    if not prompt.strip():
        return
    if engine == "claude":
        if cc_ok:
            snap = snapshot(channel.id, resume=CC_CONTINUE)
            if prompt.strip().lower() == "/compact":  # typed via /ask, /mavis or @mention
                if not snap.resume:
                    await out(content="🗜️ Nothing to compact: this channel has no active Claude Code session.")
                    return
                snap, prompt = compact_snap(snap, snap.resume), "/compact"
            # Chat-like: keep talking in the channel's session (🆕 New CC session in /panel starts fresh)
            await request_cc(channel, user_id, prompt, snap, out, chat=chat)
            return
        why = "Claude Code is disabled (no OWNER_IDS)" if not CC_ENABLED else "Claude Code is owner-only"
        await answer_local(channel, user_id, prompt, out, note=f"ℹ️ {why}; answered with the local model.")
        return
    await answer_local(channel, user_id, prompt, out, allow_propose=(engine == "auto"))


async def run_scheduled_task(task_id: str) -> None:
    task = _tasks.get(task_id)
    if not task:
        return
    if task.get("at"):  # one-shot: remove before running so a crash can't repeat it
        _tasks.pop(task_id, None)
        _save_tasks()
    channel = await resolve_channel(task["channel_id"])
    if channel is None:
        log.warning("Task %s: channel %s unavailable", task_id, task["channel_id"])
        return
    uid = task["created_by"]
    head = f"⏰ <@{uid}> **{task['description']}** · `{task_id}`"
    if task.get("at") and now_local() - datetime.fromisoformat(task["at"]) > REMINDER_LATE:
        head += f"\n-# late: was due {discord.utils.format_dt(datetime.fromisoformat(task['at']), 'f')} (bot was offline or the PC was asleep)"
    task_defaults(task)
    authority = task_authority(task)  # re-checked every run: OWNER_IDS may have changed since it was allowed
    reminders_ok = task["reminders"] and authority
    if task["reminders"] and not authority:
        head += "\n-# no owner behind this task any more: running without reminders"
    if task.get("engine") == "claude":
        out = Out(channel, prefix=head, ping=uid)
        if CC_ENABLED and authority:
            # Unattended: fresh session, on the task's own backend/model/workspace; normal budgets apply
            perm = task["perm"]
            if perm == "full" and task["backend"] == "ollama":
                perm = "edit"  # the local model never gets a shell
            ws = task["workspace"] if task["workspace"] in WORKSPACES else next(iter(WORKSPACES))
            snap = CCSnap(task["backend"], task["model"], perm, ws)
            if blocked := budget_block(snap):
                await out(content=f"{blocked}\n-# scheduled task skipped")
                return
            log.info("Task %s: running with Claude Code (%s/%s, %s)", task_id, snap.backend, snap.model, snap.perm)
            await start_cc_job(channel, task["prompt"], snap, uid, out=out, chat=True, scheduled=True,
                               reminders_ok=reminders_ok)
            return
        head += "\n-# Claude Code isn't available for this task (it needs an owner); used the local model."
        model = get_settings(task["channel_id"])["local_model"]
    else:
        model = task["model"]
    tools = READONLY_TOOLS + (["set_reminder", "list_reminders"] if reminders_ok else [])  # never schedule_task
    ctx = ToolCtx(task["channel_id"], task["created_by"], tools)
    started, asked_at = time.monotonic(), time.time()
    try:
        with activity("task", task["description"], task["channel_id"], uid, model=model):
            res = await run_local(task["prompt"], model, ctx, None)
        text = res.text or "_(empty reply)_"
        note_event("task", task["description"], channel_id=task["channel_id"], user_id=uid, status="ran",
                   ref=task_id, engine="local", model=model, seconds=round(time.monotonic() - started, 1),
                   tools=res.tools_used or None, reply=clip(strip_think(text), 600), started=asked_at)
        if ctx.reminders:
            text += "\n" + "\n".join(ctx.reminders)
        if res.tools_used:
            text += "\n-# used: " + ", ".join(res.tools_used)
    except Exception as e:
        log.exception("scheduled task %s failed", task_id)
        text = with_hint(f"⚠️ Task failed: {oneline(redact(e), 300)}", type(e).__name__, where="local", model=model)
    out = Out(channel, ping=uid)
    for chunk in split_message(redact(f"{head}\n{text}")):
        await out(content=chunk)


# =============================================================================
# Views
# =============================================================================


class GuardedView(discord.ui.View):
    """Allow-list check on every component; owner check for Claude Code components."""

    owner_all = False
    owner_custom_ids: set[str] = set()

    def needs_owner(self, inter: discord.Interaction) -> bool:
        cid = (inter.data or {}).get("custom_id", "")
        return self.owner_all or cid in self.owner_custom_ids

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not is_allowed_in(inter.user.id, inter.guild):
            await inter.response.send_message("⛔ You're not allowed to use this bot.", ephemeral=True)
            return False
        if self.needs_owner(inter) and not is_owner(inter.user.id):
            msg = "⛔ Claude Code is disabled (no OWNER_IDS)." if not CC_ENABLED else "⛔ Only owners can use Claude Code controls."
            await inter.response.send_message(msg, ephemeral=True)
            return False
        return True

    async def on_error(self, inter: discord.Interaction, error: Exception, item) -> None:
        log.exception("view error", exc_info=error)
        try:
            send = inter.followup.send if inter.response.is_done() else inter.response.send_message
            await send(with_hint(f"⚠️ {type(error).__name__}: {oneline(redact(error), 200)}"), ephemeral=True)
        except discord.HTTPException:
            pass


async def _quick(coro: Awaitable, default: Any, timeout: float = 1.8) -> Any:
    try:
        return await asyncio.wait_for(coro, timeout)
    except Exception:
        return default


def _options(values: list[str], current: str, emoji: str | None = None) -> list[discord.SelectOption]:
    vals = list(dict.fromkeys([current, *values]))[:25] if current else values[:25]
    return [discord.SelectOption(label=clip(v, 100), value=v[:100], emoji=emoji, default=(v == current)) for v in vals]


class PanelView(GuardedView):
    owner_custom_ids = {"p:ccbackend", "p:ccmodel", "p:perm", "p:newsess"}

    def __init__(self, channel_id: int, local_models: list[str], cc_model_list: list[str]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.channel_id = channel_id
        s = get_settings(channel_id)

        eng = discord.ui.Select(custom_id="p:engine", row=0, placeholder="Engine", options=[
            discord.SelectOption(label=f"Engine: {lbl}", value=k, emoji=em, default=(k == s["engine"]))
            for k, (em, lbl) in ENGINES.items()])
        eng.callback = self.on_engine
        self.add_item(eng)

        lm = discord.ui.Select(custom_id="p:localmodel", row=1, placeholder="Local model",
                               options=_options(local_models, s["local_model"], "💻"))
        lm.callback = self.on_local_model
        self.add_item(lm)

        be = discord.ui.Select(custom_id="p:ccbackend", row=2, placeholder="Claude Code backend", options=[
            discord.SelectOption(label=f"CC backend: {lbl}", value=k, emoji=em, description=clip(d, 100),
                                 default=(k == s["cc_backend"])) for k, (em, lbl, d) in BACKENDS.items()])
        be.callback = self.on_backend
        self.add_item(be)

        cm = discord.ui.Select(custom_id="p:ccmodel", row=3, placeholder="Claude Code model",
                               options=_options(cc_model_list, s["cc_model"], "🤖"))
        cm.callback = self.on_cc_model
        self.add_item(cm)

        for cid, label, emoji, style, cb in [
            ("p:perm", "Settings", "⚙️", discord.ButtonStyle.secondary, self.on_perm),
            ("p:newsess", "New CC session", "🆕", discord.ButtonStyle.secondary, self.on_new_session),
            ("p:reset", "Reset chat", "🧹", discord.ButtonStyle.secondary, self.on_reset),
            ("p:tasks", "Tasks", "⏰", discord.ButtonStyle.secondary, self.on_tasks),
            ("p:refresh", "Refresh", "🔄", discord.ButtonStyle.primary, self.on_refresh),
        ]:
            b = discord.ui.Button(custom_id=cid, label=label, emoji=emoji, style=style, row=4)
            b.callback = cb
            self.add_item(b)

    @classmethod
    async def build(cls, channel_id: int) -> "PanelView":
        # Discord needs an interaction response within 3s, so model lookups get a hard cap.
        s = get_settings(channel_id)
        local, cc = await asyncio.gather(
            _quick(list_local_models(), _model_cache[1]), _quick(cc_models(s["cc_backend"]), []))
        return cls(channel_id, local, cc)

    def needs_owner(self, inter: discord.Interaction) -> bool:
        data = inter.data or {}
        if data.get("custom_id") == "p:engine" and set(data.get("values", [])) & {"claude", "auto"}:
            return True
        return super().needs_owner(inter)

    async def rerender(self, inter: discord.Interaction) -> None:
        self.stop()
        await inter.response.edit_message(embed=panel_embed(self.channel_id), view=await PanelView.build(self.channel_id))

    async def on_engine(self, inter):
        update_settings(self.channel_id, engine=inter.data["values"][0])
        await self.rerender(inter)

    async def on_local_model(self, inter):
        update_settings(self.channel_id, local_model=inter.data["values"][0])
        await self.rerender(inter)

    async def on_backend(self, inter):
        backend = inter.data["values"][0]
        if backend == "custom" and not CC_CUSTOM_BASE_URL:
            await inter.response.send_message("🔌 Custom gateway isn't configured (set CC_CUSTOM_BASE_URL).", ephemeral=True)
            return
        models = await _quick(cc_models(backend), [], 1.0)
        default =CC_MODEL if backend == CC_BACKEND and CC_MODEL else None
        if backend == "ollama" and LLM_MODEL in models:
            default = LLM_MODEL
        update_settings(self.channel_id, cc_backend=backend, cc_model=default or (models[0] if models else "sonnet"))
        await self.rerender(inter)

    async def on_cc_model(self, inter):
        update_settings(self.channel_id, cc_model=inter.data["values"][0])
        await self.rerender(inter)

    async def on_perm(self, inter):
        # The panel is ephemeral, so the sub-panel swaps in place (ephemeral messages can't be edited later).
        self.stop()
        await inter.response.edit_message(embed=perm_embed(self.channel_id), view=PermView(self.channel_id))

    async def on_new_session(self, inter):
        update_settings(self.channel_id, cc_session=None)
        await self.rerender(inter)

    async def on_reset(self, inter):
        forget_history(self.channel_id)
        await self.rerender(inter)
        await inter.followup.send("🧹 Local chat history cleared.", ephemeral=True)

    async def on_tasks(self, inter):
        await inter.response.send_message(embed=tasks_embed(inter.user.id), view=TasksView(inter.user.id), ephemeral=True)

    async def on_refresh(self, inter):
        await self.rerender(inter)


def perm_embed(channel_id: int) -> discord.Embed:
    s = get_settings(channel_id)
    p = PERMS[s["cc_perm"]]
    e = discord.Embed(title="⚙️ Settings", color=COLOR_ERR if s["cc_perm"] == "full" else COLOR_RUN)
    st = STYLES[s["style"]]
    e.add_field(name="Reply style", value=f"{st[0]} **{st[1]}**: {st[2]}", inline=False)
    if VOICE_ENABLED:
        vm = VOICE_MODES[s["voice"]]
        e.add_field(name="Voice notes", value=f"{vm[0]} **{vm[1].split(': ')[1]}**: {vm[2]}", inline=False)
    if is_telegram_id(channel_id):
        e.add_field(name="Stats line", value="**Tap to reveal**: hidden behind a spoiler under each reply"
                    if s["tg_stats"] != "off" else "**Off**: not shown (warnings still are)", inline=False)
    e.add_field(name="Permissions", value=f"{p[0]} **{p[1]}**: {p[2]}", inline=False)
    e.add_field(name="Workspace", value=redact(f"`{s['workspace']}` → `{WORKSPACES[s['workspace']]}`"), inline=False)
    e.set_footer(text="Changing the workspace starts a new Claude Code session.")
    return e


class PermView(GuardedView):
    owner_all = True

    def __init__(self, channel_id: int):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.channel_id = channel_id
        s = get_settings(channel_id)
        style = discord.ui.Select(custom_id="pv:style", placeholder="Reply style", options=[
            discord.SelectOption(label=f"Reply style: {lbl}", value=k, emoji=em, description=clip(d, 100),
                                 default=(k == s["style"])) for k, (em, lbl, d) in STYLES.items()])
        style.callback = self.on_style
        self.add_item(style)
        if VOICE_ENABLED:
            voice = discord.ui.Select(custom_id="pv:voice", placeholder="Voice notes", options=[
                discord.SelectOption(label=lbl, value=k, emoji=em, description=clip(d, 100), default=(k == s["voice"]))
                for k, (em, lbl, d) in VOICE_MODES.items()])
            voice.callback = self.on_voice
            self.add_item(voice)
        perm = discord.ui.Select(custom_id="pv:perm", placeholder="Permission profile", options=[
            discord.SelectOption(label=lbl, value=k, emoji=em, description=clip(d, 100), default=(k == s["cc_perm"]))
            for k, (em, lbl, d) in PERMS.items()])
        perm.callback = self.on_perm
        self.add_item(perm)
        ws = discord.ui.Select(custom_id="pv:ws", placeholder="Workspace", options=[
            discord.SelectOption(label=clip(name, 100), value=name, emoji="📁", description=clip(redact(path), 100),
                                 default=(name == s["workspace"])) for name, path in list(WORKSPACES.items())[:25]])
        ws.callback = self.on_ws
        self.add_item(ws)
        back = discord.ui.Button(custom_id="pv:back", label="Back to panel", emoji="⬅️", style=discord.ButtonStyle.primary)
        back.callback = self.on_back
        self.add_item(back)
        if is_telegram_id(channel_id):  # Telegram can't show small grey text (Discord's rows are full anyway)
            off = s["tg_stats"] == "off"
            stats = discord.ui.Button(custom_id="pv:stats", label="Stats line: show" if off else "Stats line: turn off",
                                      emoji="📊", style=discord.ButtonStyle.secondary)
            stats.callback = self.on_stats
            self.add_item(stats)

    async def _refresh(self, inter):
        self.stop()
        await inter.response.edit_message(embed=perm_embed(self.channel_id), view=PermView(self.channel_id))

    async def on_back(self, inter):
        self.stop()
        await inter.response.edit_message(embed=panel_embed(self.channel_id), view=await PanelView.build(self.channel_id))

    async def on_perm(self, inter):
        update_settings(self.channel_id, cc_perm=inter.data["values"][0])
        await self._refresh(inter)

    async def on_ws(self, inter):
        update_settings(self.channel_id, workspace=inter.data["values"][0])
        await self._refresh(inter)

    async def on_voice(self, inter):
        update_settings(self.channel_id, voice=inter.data["values"][0])
        await self._refresh(inter)

    async def on_style(self, inter):
        update_settings(self.channel_id, style=inter.data["values"][0])
        await self._refresh(inter)

    async def on_stats(self, inter):
        off = get_settings(self.channel_id)["tg_stats"] == "off"
        update_settings(self.channel_id, tg_stats="spoiler" if off else "off")
        await self._refresh(inter)


class TasksView(GuardedView):
    def __init__(self, user_id: int | None = None):
        super().__init__(timeout=600)
        self.user_id = user_id
        opts = [discord.SelectOption(label=clip(f"🔔 {r['id']} · {r['text']}", 100), value=r["id"],
                                     description=clip(f"{datetime.fromisoformat(r['when']):%d %b %H:%M}", 100))
                for r in my_reminders(user_id)][:12]
        opts += [discord.SelectOption(label=clip(f"🔁 {t['id']} · {t['description']}", 100), value=t["id"],
                                      description=clip(f"once {datetime.fromisoformat(t['at']):%d %b %H:%M}" if t.get("at")
                                                       else f"cron {t['cron']}", 100)) for t in my_tasks(user_id)]
        if opts:
            sel = discord.ui.Select(custom_id="t:cancel", placeholder="🗑️ Cancel a reminder or task…", options=opts[:25])
            sel.callback = self.on_cancel
            self.add_item(sel)
        tasks = my_tasks(user_id)
        if tasks:
            edit = discord.ui.Select(custom_id="t:edit", placeholder="⚙️ Change what a task runs on / may do…", options=[
                discord.SelectOption(label=clip(f"{t['id']} · {t['description']}", 100), value=t["id"],
                                     emoji="🤖" if t.get("engine") == "claude" else "💻",
                                     description=clip(f"{t.get('model')} · {PERMS[t.get('perm', 'read')][1]}"
                                                      + (" · reminders" if t.get("reminders") else ""), 100))
                for t in tasks[:25]])
            edit.callback = self.on_edit
            self.add_item(edit)

    async def on_cancel(self, inter):
        value = inter.data["values"][0]
        msg = cancel_reminder(value, inter.user.id) if value in _reminders else cancel_task(value, inter.user.id)
        self.stop()
        await inter.response.edit_message(content=msg, embed=tasks_embed(self.user_id), view=TasksView(self.user_id))

    async def on_edit(self, inter):
        tid = inter.data["values"][0]
        t = _tasks.get(tid)
        if not t or not visible_to(t["created_by"], inter.user.id):
            await inter.response.edit_message(content=f"No task with id '{tid}'.", embed=tasks_embed(self.user_id),
                                              view=TasksView(self.user_id))
            return
        self.stop()
        await inter.response.edit_message(content=None, embed=task_embed(t),
                                          view=await TaskEditView.build(tid, inter.user.id, self.user_id))


def task_embed(t: dict, note: str | None = None) -> discord.Embed:
    full = t.get("perm") == "full"
    e = discord.Embed(title=f"⚙️ Task `{t['id']}`: {clip(t['description'], 200)}", description=note,
                      color=COLOR_ERR if full else COLOR_RUN)
    e.add_field(name="When", value=task_label(t), inline=False)
    e.add_field(name="Prompt", value=clip(t["prompt"], 1000), inline=False)
    if t.get("engine") == "claude":
        p = PERMS[t.get("perm", "read")]
        e.add_field(name="May", value=f"{p[0]} **{p[1]}**: {p[2]}", inline=False)
        e.add_field(name="Workspace", value=f"`{t.get('workspace')}`")
    else:
        e.add_field(name="May", value="🌐 web search and page fetch", inline=False)
    e.add_field(name="Reminders", value="🔔 may set reminders" if t.get("reminders") else "🔕 can't set reminders")
    e.add_field(name="Chat", value=where(t["channel_id"]))
    e.set_footer(text="Runs in a fresh session with no chat memory. Owners choose Claude Code, permissions and "
                      "reminders; a task can never schedule more tasks.")
    return e


class TaskEditView(GuardedView):
    """One task: what it runs on (engine + model) and what it may do. update_task() checks every change again."""

    def __init__(self, task_id: str, viewer: int, list_user: int | None, choices: list[tuple[str, str, str]]):
        super().__init__(timeout=600)
        self.task_id, self.list_user = task_id, list_user
        t = _tasks[task_id]
        owner = is_owner(viewer)
        current = (f"claude|{t.get('backend')}|{t.get('model')}" if t.get("engine") == "claude"
                   else f"local||{t.get('model')}")
        opts, seen = [], set()
        for value, label, emoji in choices:
            if value not in seen and len(value) <= 100:
                seen.add(value)
                opts.append(discord.SelectOption(label=clip(label, 100), value=value, emoji=emoji,
                                                 default=(value == current)))
        if current not in seen:  # its model may not be listed right now (server down, custom name)
            opts.insert(0, discord.SelectOption(label=clip(f"{t.get('model')} (current)", 100), value=current[:100],
                                                default=True, emoji="🤖" if t.get("engine") == "claude" else "💻"))
        runs = discord.ui.Select(custom_id="te:model", placeholder="Runs on…", options=opts[:25])
        runs.callback = self.on_model
        self.add_item(runs)
        if owner and t.get("engine") == "claude":
            perm = discord.ui.Select(custom_id="te:perm", placeholder="May…", options=[
                discord.SelectOption(label=f"May: {lbl}", value=k, emoji=em, description=clip(d, 100),
                                     default=(k == t.get("perm", "read")))
                for k, (em, lbl, d) in PERMS.items() if not (k == "full" and t.get("backend") == "ollama")])
            perm.callback = self.on_perm
            self.add_item(perm)
        if owner:
            on = bool(t.get("reminders"))
            rem = discord.ui.Button(custom_id="te:rem", label="Reminders: turn off" if on else "Reminders: allow",
                                    emoji="🔕" if on else "🔔", style=discord.ButtonStyle.secondary)
            rem.callback = self.on_reminders
            self.add_item(rem)
        back = discord.ui.Button(custom_id="te:back", label="Back", emoji="⬅️", style=discord.ButtonStyle.primary)
        back.callback = self.on_back
        self.add_item(back)

    @classmethod
    async def build(cls, task_id: str, viewer: int, list_user: int | None) -> "TaskEditView":
        """Choices: the local server's models for everyone; Claude Code's backends and models for owners."""
        local = await _quick(list_local_models(), _model_cache[1])
        choices = [(f"local||{m}", f"Local · {m}", "💻") for m in local]
        if CC_ENABLED and is_owner(viewer):
            backends = [b for b in BACKENDS if b != "custom" or CC_CUSTOM_BASE_URL]
            lists = await asyncio.gather(*(_quick(cc_models(b), []) for b in backends))
            for b, models in zip(backends, lists):
                choices += [(f"claude|{b}|{m}", f"Claude Code · {b} · {m}", "🤖") for m in models]
        return cls(task_id, viewer, list_user, choices)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not await super().interaction_check(inter):
            return False
        t = _tasks.get(self.task_id)
        if t and t["created_by"] != inter.user.id and not is_owner(inter.user.id):
            await inter.response.send_message("Only the task's creator or an owner can change it.", ephemeral=True)
            return False
        return True

    async def _show(self, inter, note: str) -> None:
        self.stop()
        t = _tasks.get(self.task_id)
        if not t:  # cancelled (or a one-shot that ran) meanwhile
            await inter.response.edit_message(content=note, embed=tasks_embed(self.list_user),
                                              view=TasksView(self.list_user))
            return
        await inter.response.edit_message(content=None, embed=task_embed(t, note),
                                          view=await TaskEditView.build(self.task_id, inter.user.id, self.list_user))

    async def on_model(self, inter):
        engine, backend, model = (inter.data["values"][0].split("|", 2) + ["", ""])[:3]
        await self._show(inter, await update_task(self.task_id, inter.user.id, engine=engine,
                                                  backend=backend or None, model=model))

    async def on_perm(self, inter):
        perm = inter.data["values"][0]
        if perm == "full" and is_owner(inter.user.id):  # unattended shell access: confirm first
            self.stop()
            e = discord.Embed(title="⚠️ Give this task full access?", color=COLOR_ERR, description=(
                "Every time it runs, nobody watching, Claude Code can run **any command** on this PC in the "
                "workspace and beyond, without asking. Only do this for a prompt you wrote and trust.\n\n"
                f"**Prompt:** {clip(_tasks[self.task_id]['prompt'], 1500)}"))
            await inter.response.edit_message(embed=e, view=TaskFullConfirmView(self.task_id, self.list_user))
            return
        await self._show(inter, await update_task(self.task_id, inter.user.id, perm=perm))

    async def on_reminders(self, inter):
        t = _tasks.get(self.task_id)
        await self._show(inter, await update_task(self.task_id, inter.user.id,
                                                  reminders=not (t or {}).get("reminders")))

    async def on_back(self, inter):
        self.stop()
        await inter.response.edit_message(content=None, embed=tasks_embed(self.list_user), view=TasksView(self.list_user))


class TaskFullConfirmView(GuardedView):
    owner_all = True

    def __init__(self, task_id: str, list_user: int | None):
        super().__init__(timeout=120)
        self.task_id, self.list_user = task_id, list_user
        go = discord.ui.Button(custom_id="tf:go", label="Yes, full access", emoji="⚠️", style=discord.ButtonStyle.danger)
        go.callback = self.on_go
        back = discord.ui.Button(custom_id="tf:back", label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
        back.callback = self.on_back
        self.add_item(go)
        self.add_item(back)

    async def on_go(self, inter):
        await TaskEditView._show(self, inter, await update_task(self.task_id, inter.user.id, perm="full"))

    async def on_back(self, inter):
        await TaskEditView._show(self, inter, "Unchanged.")


class ReminderView(GuardedView):
    """Buttons on a fired reminder. Only the person it's for (or an owner) can use them."""

    def __init__(self, r: dict):
        super().__init__(timeout=6 * 3600)
        self.r = r

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not await super().interaction_check(inter):
            return False
        if inter.user.id != self.r["user_id"] and not is_owner(inter.user.id):
            await inter.response.send_message("That reminder isn't yours.", ephemeral=True)
            return False
        return True

    async def _close(self, inter: discord.Interaction, note: str) -> None:
        self.stop()
        await inter.response.edit_message(content=f"{inter.message.content}\n-# {note}", view=None,
                                          allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.secondary, custom_id="rem:done")
    async def done(self, inter: discord.Interaction, _button):
        await self._close(inter, "✅ done")

    @discord.ui.button(label=f"{int(REMINDER_SNOOZE.total_seconds() // 60)} min", emoji="💤",
                       style=discord.ButtonStyle.secondary, custom_id="rem:snooze")
    async def snooze(self, inter: discord.Interaction, _button):
        r = add_reminder(now_local() + REMINDER_SNOOZE, self.r["text"], self.r["channel_id"], self.r["user_id"])
        await self._close(inter, f"💤 snoozed until {discord.utils.format_dt(datetime.fromisoformat(r['when']), 't')}")


class TaskModal(discord.ui.Modal):
    def __init__(self, title: str, default: str, on_done: Callable[[discord.Interaction, str], Awaitable[None]],
                 label: str = "Task"):
        super().__init__(title=clip(title, 45), timeout=900)
        self.text = discord.ui.TextInput(label=label, style=discord.TextStyle.paragraph, default=clip(default, 4000) or None,
                                         max_length=4000, required=True)
        self.add_item(self.text)
        self._on_done = on_done

    async def on_submit(self, inter: discord.Interaction) -> None:
        await self._on_done(inter, self.text.value)


class LocalReplyView(GuardedView):
    owner_custom_ids = {"r:cc"}

    def __init__(self, channel_id: int, prompt: str, model: str, allow_propose: bool):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.channel_id, self.prompt, self.model, self.allow_propose = channel_id, prompt, model, allow_propose

    @discord.ui.button(label="Retry", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="r:retry")
    async def retry(self, inter: discord.Interaction, _b):
        h = _history.get(self.channel_id, [])
        if len(h) >= 2 and h[-2]["content"] == self.prompt and h[-1]["role"] == "assistant":
            del h[-2:]
        self.stop()
        await inter.response.edit_message(view=None)
        async with inter.channel.typing():
            await answer_local(inter.channel, inter.user.id, self.prompt, Out(inter.channel),
                               model=self.model, allow_propose=self.allow_propose)

    @discord.ui.button(label="Hand to Claude Code", emoji="🤖", style=discord.ButtonStyle.primary, custom_id="r:cc")
    async def hand(self, inter: discord.Interaction, _b):
        snap = snapshot(self.channel_id)
        await inter.response.send_message(embed=confirm_embed(self.prompt, snap),
                                          view=CCConfirmView(self.channel_id, self.prompt, snap))

    @discord.ui.button(label="Reset", emoji="🧹", style=discord.ButtonStyle.secondary, custom_id="r:reset")
    async def reset(self, inter: discord.Interaction, _b):
        forget_history(self.channel_id)
        await inter.response.send_message("🧹 Local chat history cleared for this channel.", ephemeral=True)


class CCConfirmView(GuardedView):
    owner_all = True

    def __init__(self, channel_id: int, task: str, snap: CCSnap, reason: str | None = None):
        super().__init__(timeout=3600)
        self.channel_id, self.task, self.snap, self.reason = channel_id, task, snap, reason

    @discord.ui.button(label="Run", emoji="▶️", style=discord.ButtonStyle.success, custom_id="c:run")
    async def run(self, inter: discord.Interaction, _b):
        missing = missing_perms(inter.channel, inter)
        if missing:
            await inter.response.send_message(perm_help(missing), ephemeral=True)
            return
        if blocked := budget_block(self.snap):
            await inter.response.send_message(blocked, ephemeral=True)
            return
        self.stop()
        await inter.response.edit_message(embed=confirm_embed(self.task, self.snap, self.reason, "▶️ Started, see progress below"),
                                          view=None)
        await start_cc_job(inter.channel, self.task, self.snap, inter.user.id,
                           chat=get_settings(self.channel_id)["style"] == "chat")

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="c:edit")
    async def edit(self, inter: discord.Interaction, _b):
        async def done(minter: discord.Interaction, text: str):
            self.task = text.strip()
            await minter.response.edit_message(embed=confirm_embed(self.task, self.snap, self.reason), view=self)
        await inter.response.send_modal(TaskModal("Edit Claude Code task", self.task, done))

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary, custom_id="c:cancel")
    async def cancel(self, inter: discord.Interaction, _b):
        self.stop()
        await inter.response.edit_message(embed=confirm_embed(self.task, self.snap, self.reason, "✖️ Cancelled"), view=None)


class CCProgressView(GuardedView):
    owner_all = True

    def __init__(self, job: CCJob):
        super().__init__(timeout=None)
        self.job = job

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="g:stop")
    async def stop_btn(self, inter: discord.Interaction, _b):
        self.job.stop_requested = True
        await inter.response.send_message("⏹️ Stopping…", ephemeral=True)
        if self.job.proc:
            await kill_tree(self.job.proc)


class CCResultView(GuardedView):
    owner_all = True

    def __init__(self, job: CCJob):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.job = job
        if not job.parser.session_id:
            self.follow_up.disabled = True
        ctx = job.parser.context_tokens or 0
        if not job.parser.session_id or job.parser.compacted:
            self.compact_btn.disabled = True
        elif compact_at(job.ctx_limit) and ctx >= compact_at(job.ctx_limit):
            self.compact_btn.style = discord.ButtonStyle.primary  # highlighted when it's worth doing

    @discord.ui.button(label="Follow up", emoji="💬", style=discord.ButtonStyle.primary, custom_id="x:follow")
    async def follow_up(self, inter: discord.Interaction, _b):
        job = self.job

        async def done(minter: discord.Interaction, text: str):
            # Same backend/model/workspace + session; permissions from the channel's current setting.
            snap = replace(job.snap, resume=job.parser.session_id, perm=get_settings(job.channel.id)["cc_perm"],
                           note=None)  # the original job's "new session" note doesn't apply to a follow-up
            await minter.response.defer()
            await request_cc(minter.channel, minter.user.id, text.strip(), snap, Out(minter.channel))
        await inter.response.send_modal(TaskModal("Follow up (same session)", "", done, label="Message"))

    @discord.ui.button(label="Retry", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="x:retry")
    async def retry(self, inter: discord.Interaction, _b):
        await inter.response.defer()
        await request_cc(inter.channel, inter.user.id, self.job.task, snapshot(self.job.channel.id), Out(inter.channel))

    @discord.ui.button(label="Full log", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="x:log")
    async def full_log(self, inter: discord.Interaction, _b):
        await inter.response.send_message(file=transcript_file(self.job), ephemeral=True)

    @discord.ui.button(label="Compact", emoji="🗜️", style=discord.ButtonStyle.secondary, custom_id="x:compact")
    async def compact_btn(self, inter: discord.Interaction, _b):
        await inter.response.defer()
        await request_cc(inter.channel, inter.user.id, "/compact",
                         compact_snap(self.job.snap, self.job.parser.session_id), Out(inter.channel))


def compact_snap(snap: CCSnap, session_id: str) -> CCSnap:
    # Read-only: compaction runs no tools, and this avoids the full-access confirmation card.
    return replace(snap, resume=session_id, perm="read", note=None, compact=True)


# =============================================================================
# Bot + slash commands
# =============================================================================


class Tree(app_commands.CommandTree):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if is_allowed_in(inter.user.id, inter.guild):
            return True
        if inter.type == discord.InteractionType.application_command:
            await inter.response.send_message("⛔ You're not allowed to use this bot.", ephemeral=True)
        return False

    async def on_error(self, inter: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Tell the user what went wrong instead of leaving "thinking…" forever."""
        cause = getattr(error, "original", error)
        log.error("command %s failed", inter.command.name if inter.command else "?", exc_info=cause)
        if isinstance(cause, discord.Forbidden):
            msg = perm_help(missing_perms(inter.channel, inter) or list(NEEDED_PERMS.values()))
        else:
            msg = with_hint(f"⚠️ Something went wrong: {type(cause).__name__}: {oneline(redact(cause), 300)}")
        try:
            if inter.response.is_done():
                await inter.followup.send(msg, ephemeral=True)
            else:
                await inter.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class LLMBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        # ^ model/Claude output can never ping @everyone, roles or users (prompt injection via web pages etc.)
        self.tree = Tree(self)
        self._cleaned = False

    async def setup_hook(self) -> None:
        await core_start()
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                except discord.Forbidden:
                    log.warning("Can't add slash commands to server %s: is the bot in it (invited with the "
                                "applications.commands scope)?", gid)
            # Per-server mode: drop global copies left from global mode, or every command would show twice.
            if await self.tree.fetch_commands():
                await self.http.bulk_upsert_global_commands(self.application_id, [])
                log.info("Removed global slash commands (per-server mode)")
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await core_stop()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (commands: %s)", self.user, ", ".join(map(str, GUILD_IDS)) or "global")
        note_event("bot", f"Discord connected as {self.user}")
        if self._cleaned:
            return
        self._cleaned = True
        joined = {g.id for g in self.guilds}
        for gid in set(GUILD_IDS) - joined:
            log.warning("GUILD_ID %s: the bot isn't in that server; invite it first", gid)
        for g in self.guilds:
            if g.id in GUILD_IDS:
                continue
            # Servers left out of GUILD_ID (or all of them in global mode) keep old per-server copies until cleared,
            # which shows every command twice next to the global ones.
            try:
                if await self.tree.fetch_commands(guild=g):
                    await self.http.bulk_upsert_guild_commands(self.application_id, g.id, [])
                    log.info("Removed leftover slash commands from server %s (%s)", g.name, g.id)
            except discord.HTTPException as e:
                log.warning("Couldn't check slash commands in server %s: %s", g.id, e)

    def _bot_role_ids(self, message: discord.Message) -> set[int]:
        """IDs of the bot's own managed role(s) in this server. Discord's @ autocomplete offers both the bot user
        and a role with the same name, so "@Mavis" is often a role mention."""
        me = message.guild.me if message.guild else None
        return {r.id for r in (me.roles if me else []) if r.is_bot_managed()}

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        role_ids = self._bot_role_ids(message)
        via_role = any(r.id in role_ids for r in message.role_mentions)
        # Replying to one of the bot's messages counts as talking to it (no @ needed), like with a person.
        ref = message.reference.resolved if message.reference else None
        replied_to_me = isinstance(ref, discord.Message) and ref.author.id == self.user.id
        addressed = self.user in message.mentions or via_role or replied_to_me
        voice = next((a for a in message.attachments if a.is_voice_message()), None) if VOICE_ENABLED else None
        if voice is not None and not addressed:
            # A voice note can't @mention anyone, so the channel's voice mode decides; DMs always count.
            mode = get_settings(message.channel.id)["voice"]
            addressed = message.guild is None or mode == "all"
        if not addressed:
            return
        if not is_allowed_in(message.author.id, message.guild):
            log.info("Ignored message from user %s (not in ALLOWED_USER_IDS, or a DM with no allow-list)",
                     message.author.id)
            return
        if voice is not None and message.guild is not None and get_settings(message.channel.id)["voice"] == "off":
            return
        if via_role and self.user not in message.mentions:
            log.info("Mention via the bot's role in channel %s", message.channel.id)
        prompt = re.sub(rf"<@!?{self.user.id}>", "", message.content)
        for rid in role_ids:
            prompt = prompt.replace(f"<@&{rid}>", "")
        prompt = prompt.strip()
        prefix = None
        if voice is not None:
            async with message.channel.typing():
                heard = await self._hear(message, voice)
            if not heard:
                return
            prompt = f"{prompt}\n{heard}".strip()
            prefix = f'-# heard: "{clip(heard, 300)}"'
        files = [a for a in message.attachments if not a.is_voice_message()]
        if not prompt and not files:
            await message.reply("Hi! Ask me anything, or see `/help`.", mention_author=False)
            return
        async with message.channel.typing():
            await handle_prompt(message.channel, message.author.id, prompt,
                                Out(message.channel, reply_to=message, prefix=prefix), attachments=files)

    async def _hear(self, message: discord.Message, att: discord.Attachment) -> str | None:
        """Transcribe a voice note; on failure tell the user (briefly) and return None."""
        async def say(text: str):
            try:
                await message.reply(quiet_grey(text), mention_author=False)
            except discord.HTTPException:
                pass
        if att.duration and att.duration > VOICE_MAX_SECONDS:
            await say(f"-# 🎙️ That's {att.duration:.0f}s; I only listen to voice notes up to {VOICE_MAX_SECONDS}s.")
            return None
        if att.size > 25 * 1024 * 1024:
            await say("-# 🎙️ That voice note is too large.")
            return None
        try:
            text, seconds = await transcribe(await att.read())
        except ImportError:
            log.error("Voice note received but faster-whisper isn't installed (pip install -r requirements.txt)")
            await say("-# 🎙️ Voice notes aren't set up on the bot yet (faster-whisper missing).")
            return None
        except Exception as e:
            log.exception("transcription failed")
            await say(with_hint(f"-# 🎙️ Couldn't transcribe that: {oneline(redact(e), 150)}", type(e).__name__,
                                where="voice"))
            return None
        log.info("Voice note %.0fs from %s transcribed (%d chars)", seconds, message.author.id, len(text))
        if not text:
            await say("-# 🎙️ I couldn't make out any speech in that.")
            return None
        return text


bot = LLMBot()


class DiscordFrontend:
    """Discord's side of the front-end interface (see _frontends)."""

    def owns(self, channel_id: int) -> bool:
        return bool(DISCORD_TOKEN) and not is_telegram_id(channel_id) and not is_web_id(channel_id)

    async def get_channel(self, channel_id: int):
        return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

    def where(self, channel_id: int) -> str:
        return f"<#{channel_id}>"


_frontends.append(DiscordFrontend())
_started = False
_telegram = None  # llmbot.telegram.Telegram when TELEGRAM_BOT_TOKEN is set


async def core_start() -> None:
    """Start the shared backend once (whichever front end comes up first), then the Telegram front end."""
    global http, CLAUDE_VERSION, _started, _telegram
    if _started:
        return
    _started = True
    http = httpx.AsyncClient(headers={"User-Agent": "pc-pilot/1.0"})
    scheduler.start()
    load_tasks()
    load_reminders()
    _spawn(ensure_ollama())
    _spawn(idle_unloader())
    _spawn(power_watch())
    _spawn(power_back_on_start())
    binary = claude_bin()
    if binary:
        try:
            p = await asyncio.create_subprocess_exec(binary, "--version", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 20)
            CLAUDE_VERSION = out.decode().strip().split()[0] if out.strip() else None
        except Exception as e:
            log.warning("claude --version failed: %s", e)
    log.info("Claude Code CLI: %s (%s)", binary or "not found", CLAUDE_VERSION)
    note_event("bot", f"Bot started (llmbot {__version__}, storage: {STORE.kind})")
    if DASHBOARD_PORT:
        from llmbot import dashboard

        try:
            await dashboard.start(sys.modules[__name__])
        except Exception as e:  # e.g. the port is taken: the bot itself keeps working
            log.error("Dashboard not started on port %s: %s", DASHBOARD_PORT, oneline(redact(e), 200))
    if TELEGRAM_BOT_TOKEN:
        from llmbot import telegram as telegram_frontend  # the Telegram front end; uses this module as its backend

        _telegram = telegram_frontend.Telegram(sys.modules[__name__], TELEGRAM_BOT_TOKEN)
        _frontends.insert(0, _telegram)
        _spawn(_telegram.run())


async def core_stop() -> None:
    note_event("bot", "Bot stopping")
    if "llmbot.dashboard" in sys.modules:
        await sys.modules["llmbot.dashboard"].stop()
    if _telegram is not None:
        await _telegram.close()
    if _cc_current and _cc_current.proc:
        _cc_current.stop_requested = True
        await kill_tree(_cc_current.proc)
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if http:
        try:  # free the GPU on exit
            await asyncio.wait_for(refresh_loaded(), 5)
            for root, model in list(_used_models):
                await asyncio.wait_for(unload_model(root, model), 10)
        except Exception:
            pass
        await http.aclose()


@bot.tree.command(name="panel", description="Control panel for this channel (only you can see it)")
async def panel_cmd(inter: discord.Interaction):
    embed = panel_embed(inter.channel_id)
    missing = missing_perms(inter.channel, inter)
    if missing:  # the panel itself still works (ephemeral), but replies/cards won't
        embed.color = COLOR_ERR
        embed.description = perm_help(missing)
    await inter.response.send_message(embed=embed, view=await PanelView.build(inter.channel_id), ephemeral=True)


@bot.tree.command(name="ask", description="Ask using this channel's engine")
@app_commands.describe(prompt="Your message", file="Optional image, PDF or file (Claude Code)")
async def ask_cmd(inter: discord.Interaction, prompt: str, file: discord.Attachment | None = None):
    await inter.response.defer(thinking=True)
    await handle_prompt(inter.channel, inter.user.id, prompt, Out(inter.channel, inter),
                        attachments=[file] if file else None)


@bot.tree.command(name="local", description="Ask the local model directly")
@app_commands.describe(prompt="Your message", model="Model override (defaults to the channel's)")
async def local_cmd(inter: discord.Interaction, prompt: str, model: str | None = None):
    await inter.response.defer(thinking=True)
    # Non-owners pick from what the server lists (on a paid gateway such as LiteLLM, any name could cost money)
    if model and not is_owner(inter.user.id) and model not in await list_local_models():
        await inter.followup.send(f"⚠️ `{clip(model, 100)}` isn't one of the server's models.", ephemeral=True)
        return
    await answer_local(inter.channel, inter.user.id, prompt, Out(inter.channel, inter), model=model)


@local_cmd.autocomplete("model")
async def local_model_ac(inter: discord.Interaction, current: str):
    models = await list_local_models()
    return [app_commands.Choice(name=m[:100], value=m[:100]) for m in models if current.lower() in m.lower()][:25]


@bot.tree.command(name="claude", description="Run a task with Claude Code (owners only)")
@app_commands.describe(task="What Claude Code should do", continue_session="Resume this channel's last session")
async def claude_cmd(inter: discord.Interaction, task: str, continue_session: bool = False):
    if not (CC_ENABLED and is_owner(inter.user.id)):
        await inter.response.send_message("⛔ Claude Code is owner-only" + ("" if CC_ENABLED else " and disabled (no OWNER_IDS)") + ".",
                                          ephemeral=True)
        return
    snap = snapshot(inter.channel_id, resume=continue_session)
    if continue_session and not snap.resume:
        await inter.response.send_message("No previous session in this channel; starting a new one.", ephemeral=True)
        await request_cc(inter.channel, inter.user.id, task, snap, Out(inter.channel))
        return
    await inter.response.defer(thinking=True)
    await request_cc(inter.channel, inter.user.id, task, snap, Out(inter.channel, inter))


@bot.tree.command(name="schedule", description=f"Schedule a recurring prompt (5-field cron, {TIMEZONE})")
@app_commands.describe(cron="e.g. '0 9 * * 1-5' = 09:00 on weekdays (0=Sunday)", prompt="Self-contained prompt to run")
async def schedule_cmd(inter: discord.Interaction, cron: str, prompt: str):
    try:
        engine = ("claude" if get_settings(inter.channel_id)["engine"] == "claude" and CC_ENABLED
                  and is_owner(inter.user.id) else "local")
        t = add_task(cron, prompt, prompt, inter.channel_id, inter.user.id, engine=engine)
    except ValueError as e:
        await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    nr = next_run(t["id"])
    await inter.response.send_message(
        f"⏰ Scheduled `{t['id']}`: {t['description']}\ncron `{t['cron']}` · next {discord.utils.format_dt(nr, 'F') if nr else '—'}\n"
        + f"-# Runs on {task_runs_on(t)}, read-only, in a fresh session; you get pinged. /tasks changes the model "
          "or what it may do.")


@bot.tree.command(name="remind", description=f"One-time reminder: pings you here at that time ({TIMEZONE})")
@app_commands.describe(when="e.g. 'in 10 min', 'in 2h', '18:30', '6pm', 'tomorrow 9am'", what="What to remind you about")
async def remind_cmd(inter: discord.Interaction, when: str, what: str):
    try:
        r = add_reminder(when, what, inter.channel_id, inter.user.id)
    except ValueError as e:
        await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
        return
    await inter.response.send_message(redact(reminder_line(r)), ephemeral=True)


@bot.tree.command(name="tasks", description="List and cancel reminders and scheduled tasks")
async def tasks_cmd(inter: discord.Interaction):
    # Only you see it: reminders are often personal ("take my meds")
    await inter.response.send_message(embed=tasks_embed(inter.user.id), view=TasksView(inter.user.id), ephemeral=True)


@bot.tree.command(name="reset", description="Clear this channel's local chat history")
async def reset_cmd(inter: discord.Interaction):
    forget_history(inter.channel_id)
    await inter.response.send_message("🧹 Local chat history cleared.", ephemeral=True)


def _register_bot_command() -> None:
    """/<BOT_COMMAND> <prompt>: same as /ask, under your bot's own name."""
    if not BOT_COMMAND:
        return
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", BOT_COMMAND) or bot.tree.get_command(BOT_COMMAND):
        log.warning("BOT_COMMAND '%s' is invalid or clashes with a built-in command; skipped", BOT_COMMAND)
        return

    @app_commands.describe(prompt="Anything: a question, a task, small talk", file="Optional image, PDF or file")
    async def named_cmd(inter: discord.Interaction, prompt: str, file: discord.Attachment | None = None):
        await ask_cmd.callback(inter, prompt, file)

    bot.tree.add_command(app_commands.Command(name=BOT_COMMAND, description="Talk to the bot (uses this channel's engine)",
                                              callback=named_cmd))


def help_text(user_id: int) -> str:
    name = bot.user.display_name if bot.user else "the bot"
    named = f", `/{BOT_COMMAND} <msg>`" if BOT_COMMAND and bot.tree.get_command(BOT_COMMAND) else ""
    idle = f"{CC_SESSION_IDLE_MINUTES:g} min idle or " if CC_SESSION_IDLE_MINUTES > 0 else ""
    lines = [
        f"**Talking to {name}**",
        f"@{name} <msg>, reply to one of its messages, `/ask <msg>`{named}: uses this channel's engine "
        f"(set in `/panel`). Claude Code keeps the conversation going until {idle}🆕 New session.",
        "",
        "**Commands**",
        "`/panel`: engine, models, 🆕 new session, ⏰ tasks, ⚙️ settings (reply style, permissions, workspace)",
        "`/claude <task>`: a task with a live progress card, Stop and Follow up buttons",
        "`/local <msg> [model]`: ask the local model directly",
        "`/compact` (send it as a message): shrink a long conversation to a summary",
        "`/stop`: stop the reply that's running · `/log`: full log of the last reply (only you see it)",
        "`/remind <when> <what>` or just ask (\"remind me to take my meds in 10 min\"): one-time ping, with ✅/💤 buttons",
        "Scheduled prompts: just ask (\"at 8am tell me the latest tweets from …\", \"every morning at 9 …\"); it runs "
        "then with web search and pings you. `/schedule <cron> <prompt>` for repeating ones · `/tasks`: list and cancel",
        "`/reset`: clear the local model's chat history · `/unload`: free GPU memory now",
        "`/power` (owners): lock, sleep, hibernate, restart or shut down the PC; the bot posts when it's back",
        "`/dashboard` (owners): web page with what the bot is doing and has done, for your phone on the same Wi-Fi",
        "📎 **Images and files**: attach them to your message (or `file:` in `/ask`); Claude Code sees images and "
        "PDFs and reads text/code. Kept for a day in the workspace's discord_uploads folder.",
        *(["🎙️ **Voice notes**: send one here and it's transcribed on the PC and answered like a typed "
           "message (answer all / replies only / off in ⚙️ Settings)."] if VOICE_ENABLED else []),
        "",
        "**The small grey line** under a reply: model · what that reply cost · today's spend / daily cap · "
        "context size (used/limit on your own models; ~ = estimate) · session. It adds \"long chat, send /compact\" "
        "when the conversation gets big. On the plain local engine, \"memory 3/"
        f"{MAX_HISTORY_TURNS}\" is how many recent exchanges it still remembers (no session; `/reset` clears it).",
    ]
    if not is_owner(user_id):
        lines.append("\n-# Claude Code, /claude, /stop and /log are owner-only; you get the local model.")
    return "\n".join(lines)


@bot.tree.command(name="help", description="How to use the bot (only you see it)")
async def help_cmd(inter: discord.Interaction):
    await inter.response.send_message(help_text(inter.user.id), ephemeral=True)


@bot.tree.command(name="stop", description="Stop the Claude Code reply that's running in this channel")
async def stop_cmd(inter: discord.Interaction):
    if not is_owner(inter.user.id):
        await inter.response.send_message("⛔ Only owners can stop Claude Code.", ephemeral=True)
        return
    job = _last_job.get(inter.channel_id)
    if not job or job.status == "done":
        await inter.response.send_message("Nothing is running here.", ephemeral=True)
        return
    job.stop_requested = True
    if job.proc:
        await kill_tree(job.proc)
    await inter.response.send_message("⏹️ Stopped.", ephemeral=True)


@bot.tree.command(name="log", description="Full log of the last Claude Code reply in this channel (only you see it)")
async def log_cmd(inter: discord.Interaction):
    if not is_owner(inter.user.id):
        await inter.response.send_message("⛔ Only owners can see Claude Code logs.", ephemeral=True)
        return
    job = _last_job.get(inter.channel_id)
    if not job:
        await inter.response.send_message("No Claude Code reply in this channel since the bot started.", ephemeral=True)
        return
    await inter.response.send_message(file=transcript_file(job), ephemeral=True)


@bot.tree.command(name="dashboard", description="Link to the web dashboard: live activity and history (only you see it)")
async def dashboard_cmd(inter: discord.Interaction):
    if not is_owner(inter.user.id):
        await inter.response.send_message("⛔ Only owners can open the dashboard.", ephemeral=True)
        return
    from llmbot import dashboard

    await inter.response.send_message(dashboard.link_text(), ephemeral=True)


@bot.tree.command(name="unload", description="Free GPU memory now by unloading the local model(s)")
async def unload_cmd(inter: discord.Interaction):
    if models_busy():
        await inter.response.send_message("🔴 A model is generating right now; try again when it's idle.", ephemeral=True)
        return
    await inter.response.defer(ephemeral=True, thinking=True)
    done = await unload_all()
    await inter.followup.send(f"💤 Unloaded: {', '.join(f'`{m}`' for m in done)}" if done else "💤 Nothing to unload.",
                              ephemeral=True)


# =============================================================================
# Power controls (owners only, never a model tool): lock / sleep / hibernate / restart / shut down the PC,
# then say "back online" in the chat that asked: after a restart from a note in data/, after sleep from the time gap.
# =============================================================================
POWER_FILE = DATA_DIR / "power.json"
POWER_DELAY = 30  # seconds of warning before a restart / shutdown, so it can still be cancelled
POWER_ACTIONS = {  # key: (emoji, label, what happens)
    "lock": ("🔒", "Lock", "Locks the screen. Everything keeps running, including the bot."),
    "sleep": ("😴", "Sleep", "The bot is offline until someone wakes the PC (a key, the mouse, the power button or a laptop's lid). "
                            "It posts here when it's awake."),
    "hibernate": ("🛌", "Hibernate", "Like sleep, but saved to disk and using no power. Offline until someone presses "
                                    "the power button. It posts here when it's back."),
    "restart": ("🔁", "Restart", f"Restarts in {POWER_DELAY}s (/power → Cancel stops it). The bot posts here when "
                                "it's back."),
    "shutdown": ("🔌", "Shut down", f"Shuts down in {POWER_DELAY}s (/power → Cancel stops it). It can't be turned "
                                   "on from chat: someone has to press the power button."),
}
_POWER_PAST = {"sleep": "sleep", "hibernate": "hibernation", "restart": "the restart", "shutdown": "the shutdown"}
_power_tick = time.time()


def power_supported() -> bool:
    return sys.platform == "win32"


def startup_installed() -> bool:
    """Whether scripts/bot_control.ps1 install added the bot to Startup apps (it starts when someone signs in)."""
    appdata = os.getenv("APPDATA")
    folder = Path(appdata or ".") / "Microsoft/Windows/Start Menu/Programs/Startup"
    return bool(appdata) and any((folder / n).exists() for n in ("pc-pilot.lnk", "Discord LLM Bot.lnk"))  # new, old


BOOT_TASKS = ("pc-pilot (boot)", "Discord LLM Bot (boot)")  # what scripts/bot_control.ps1 boot registers (new, old)
_autostart_cache: tuple[float, str | None] = (-1e9, None)


def autostart() -> str | None:
    """How the bot comes back after a restart: "boot" (scheduled task, no sign-in needed), "logon" (Startup apps,
    once someone signs in) or None."""
    global _autostart_cache
    if time.monotonic() - _autostart_cache[0] < 60:
        return _autostart_cache[1]
    mode = None
    for task in BOOT_TASKS if sys.platform == "win32" else ():
        try:
            r = subprocess.run(["schtasks", "/query", "/tn", task, "/fo", "csv", "/nh"], capture_output=True,
                               text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode == 0 and "Disabled" not in r.stdout:
                mode = "boot"
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
    if mode is None and startup_installed():
        mode = "logon"
    _autostart_cache = (time.monotonic(), mode)
    return mode


AUTOSTART_TIP = "On the PC, run scripts\\bot_control.ps1 boot as administrator."


def power_pending() -> dict | None:
    return STORE.load("power", POWER_FILE, None)


def _dur(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


async def _run_cmd(*cmd: str) -> str | None:
    """Run a fixed system command (no shell); None on success, else its message."""
    p = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), 20)
    return None if p.returncode == 0 else (out.decode(errors="replace").strip() or f"exit code {p.returncode}")


def _lock() -> bool:
    import ctypes
    k32, wts = ctypes.windll.kernel32, ctypes.windll.wtsapi32
    screen = k32.WTSGetActiveConsoleSessionId()
    mine = ctypes.c_ulong()
    if k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(mine)) and mine.value == screen:
        return bool(ctypes.windll.user32.LockWorkStation())
    # Started at boot, the bot runs in Windows' background session, where LockWorkStation does nothing. Disconnecting
    # the screen's session instead shows the sign-in screen while its apps keep running, which is what a lock does.
    if screen == 0xFFFFFFFF:
        return True  # no screen session at all
    buf, size = ctypes.c_wchar_p(), ctypes.c_ulong()
    if wts.WTSQuerySessionInformationW(None, screen, 5, ctypes.byref(buf), ctypes.byref(size)):  # 5 = WTSUserName
        user = buf.value
        wts.WTSFreeMemory(buf)
        if not user:
            return True  # nobody signed in: it's already at the sign-in screen
    return bool(wts.WTSDisconnectSession(None, screen, False))


def _suspend(hibernate: bool) -> bool:
    import ctypes
    return bool(ctypes.WinDLL("powrprof").SetSuspendState(hibernate, False, False))  # returns after waking up


async def _suspend_soon(hibernate: bool) -> None:
    await asyncio.sleep(3)  # let the confirmation post first
    ok = await asyncio.to_thread(_suspend, hibernate)
    if not ok:
        log.warning("SetSuspendState failed (hibernate=%s)", hibernate)


async def do_power(action: str, channel_id: int, user_id: int) -> str | None:
    """Start a power action. Returns None when it started, else an error for the user."""
    if not power_supported():
        return "Power controls only work when the bot runs on Windows."
    log.info("Power: %s requested by %s in %s", action, user_id, channel_id)
    note_event("power", f"{action} requested", channel_id=channel_id, user_id=user_id, action=action)
    if action == "lock":
        return None if _lock() else "Windows refused to lock the screen."
    if action == "cancel":
        err = await _run_cmd("shutdown", "/a")
        STORE.delete("power", POWER_FILE)
        return "Nothing was pending." if err and "1116" in err else err
    STORE.save("power", POWER_FILE, {"action": action, "channel_id": channel_id, "user_id": user_id, "at": time.time()})
    if action in ("restart", "shutdown"):
        err = await _run_cmd("shutdown", "/r" if action == "restart" else "/s", "/t", str(POWER_DELAY),
                             "/c", "Requested from chat through the bot")
    else:
        _spawn(_suspend_soon(action == "hibernate"))
        err = None
    if err:
        STORE.delete("power", POWER_FILE)
    return err


async def _post_power_notice(rec: dict, text: str, tries: int = 60) -> None:
    """Post in the chat that asked, pinging only that user. Retries while the network / front end comes back."""
    uid = rec["user_id"]
    for _ in range(tries):
        channel = await resolve_channel(rec["channel_id"])
        if channel is not None:
            try:
                await channel.send(redact(f"<@{uid}> {text}"), allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, replied_user=False, users=[discord.Object(uid)]))
                return
            except Exception as e:
                log.warning("power notice not posted yet: %s", oneline(redact(e), 200))
        await asyncio.sleep(5)
    log.error("Power notice could not be delivered (channel %s)", rec["channel_id"])


async def power_back_on_start() -> None:
    """New process: if the PC went down on request, say we're back (and how long it took)."""
    rec = power_pending()
    if not rec:
        return
    STORE.delete("power", POWER_FILE)
    if time.time() - rec.get("at", 0) > 7 * 86400:
        return
    what = _POWER_PAST.get(rec.get("action"), "the restart")
    await _post_power_notice(rec, f"✅ Back online after {what} (down for about {_dur(time.time() - rec['at'])}).")


async def power_watch() -> None:
    """Same process: notice waking from sleep (a jump in wall-clock time), and requests that never happened."""
    global _power_tick
    _power_tick = time.time()
    while True:
        await asyncio.sleep(10)
        now = time.time()
        gap, _power_tick = now - _power_tick, now
        if gap > 60:
            note_event("bot", f"Resumed after about {_dur(gap)} asleep or suspended")
        rec = power_pending()
        if not rec:
            continue
        waited = now - rec.get("at", now)
        if rec["action"] in ("sleep", "hibernate") and gap > 60:
            STORE.delete("power", POWER_FILE)
            await _post_power_notice(rec, f"✅ Awake again after {_POWER_PAST[rec['action']]} "
                                          f"(offline for about {_dur(gap)}).")
        elif waited > (120 if rec["action"] in ("sleep", "hibernate") else POWER_DELAY + 180):
            STORE.delete("power", POWER_FILE)
            await _post_power_notice(rec, f"⚠️ The PC didn't {POWER_ACTIONS[rec['action']][1].lower()} "
                                          "(cancelled on the PC, or Windows refused). It's still on.", tries=3)


def power_embed(note: str | None = None) -> discord.Embed:
    e = discord.Embed(title="⚡ PC power", color=COLOR_RUN, description=note or "Owners only. Pick an action; "
                      "everything except Lock asks you to confirm.")
    rec = power_pending()
    if rec and rec["action"] in ("restart", "shutdown"):
        e.add_field(name="Pending", value=f"{POWER_ACTIONS[rec['action']][1]} at "
                    f"<t:{int(rec['at']) + POWER_DELAY}:T>. Press ✖️ Cancel to stop it.", inline=False)
    for em, label, desc in POWER_ACTIONS.values():
        e.add_field(name=f"{em} {label}", value=desc, inline=False)
    mode = autostart()
    if mode == "logon":
        e.set_footer(text="After a restart the bot comes back only once someone signs in on the PC. To have it "
                          f"come back at boot: {AUTOSTART_TIP}")
    elif mode is None:
        e.set_footer(text=f"The bot doesn't start on its own, so after a restart it stays offline. {AUTOSTART_TIP}")
    return e


class PowerView(GuardedView):
    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not (is_allowed(inter.user.id) and is_owner(inter.user.id)):
            await inter.response.send_message("⛔ Only owners can control the PC.", ephemeral=True)
            return False
        return True

    def __init__(self, channel_id: int):
        super().__init__(timeout=600)
        self.channel_id = channel_id
        for key, (em, label, _) in POWER_ACTIONS.items():
            b = discord.ui.Button(custom_id=f"pw:{key}", label=label, emoji=em, style=discord.ButtonStyle.secondary
                                  if key == "lock" else discord.ButtonStyle.danger)
            b.callback = self._picker(key)
            self.add_item(b)
        rec = power_pending()
        if rec and rec["action"] in ("restart", "shutdown"):
            b = discord.ui.Button(custom_id="pw:cancel", label="Cancel", emoji="✖️", style=discord.ButtonStyle.primary)
            b.callback = self._picker("cancel")
            self.add_item(b)

    def _picker(self, key: str):
        async def cb(inter: discord.Interaction):
            self.stop()
            if key in ("lock", "cancel"):  # harmless: no confirmation
                err = await do_power(key, self.channel_id, inter.user.id)
                done = "🔒 Locked." if key == "lock" else "✖️ Cancelled. The PC stays on."
                await inter.response.edit_message(content=f"⚠️ {err}" if err else done, embed=None, view=None)
                return
            em, label, desc = POWER_ACTIONS[key]
            e = discord.Embed(title=f"{em} {label} the PC?", description=desc, color=COLOR_ERR)
            await inter.response.edit_message(embed=e, view=PowerConfirmView(self.channel_id, key))
        return cb


class PowerConfirmView(PowerView):
    def __init__(self, channel_id: int, action: str):
        discord.ui.View.__init__(self, timeout=120)
        self.channel_id, self.action = channel_id, action
        em, label, _ = POWER_ACTIONS[action]
        go = discord.ui.Button(custom_id="pw:go", label=f"Yes, {label.lower()}", emoji=em, style=discord.ButtonStyle.danger)
        go.callback = self.on_go
        back = discord.ui.Button(custom_id="pw:back", label="Back", emoji="⬅️", style=discord.ButtonStyle.secondary)
        back.callback = self.on_back
        self.add_item(go)
        self.add_item(back)

    async def on_go(self, inter: discord.Interaction):
        self.stop()
        err = await do_power(self.action, self.channel_id, inter.user.id)
        if err:
            text = with_hint(f"⚠️ Couldn't {POWER_ACTIONS[self.action][1].lower()}: {oneline(redact(err), 300)}",
                             where="power")
        elif self.action in ("restart", "shutdown"):
            verb = "Restarting" if self.action == "restart" else "Shutting down"
            text = f"{POWER_ACTIONS[self.action][0]} {verb} in {POWER_DELAY}s. /power → Cancel stops it."
            if self.action == "restart":
                text += {"boot": " I'll post here when I'm back, about a minute after Windows starts.",
                         "logon": " I'll post here once someone signs in on the PC.",
                         }.get(autostart(), " I don't start on my own, so I'll stay offline until someone starts me.")
        else:
            text = f"{POWER_ACTIONS[self.action][0]} Going to {self.action} in a few seconds. I'll post here when I'm awake."
        await inter.response.edit_message(content=text, embed=None, view=None)

    async def on_back(self, inter: discord.Interaction):
        self.stop()
        await inter.response.edit_message(content=None, embed=power_embed(), view=PowerView(self.channel_id))


@bot.tree.command(name="power", description="Lock, sleep, hibernate, restart or shut down the PC (owners)")
async def power_cmd(inter: discord.Interaction):
    if not (is_allowed(inter.user.id) and is_owner(inter.user.id)):
        await inter.response.send_message("⛔ Only owners can control the PC.", ephemeral=True)
        return
    if not power_supported():
        await inter.response.send_message("Power controls only work when the bot runs on Windows.", ephemeral=True)
        return
    await inter.response.send_message(embed=power_embed(), view=PowerView(inter.channel_id), ephemeral=True)


# =============================================================================
# Main
# =============================================================================
INSTANCE_PORT = 47823  # localhost port held while the bot runs, to prevent a second copy
_instance_sock: socket.socket | None = None


def _setup_logging() -> None:
    """data/bot.log, rolled over into gzipped archives; all log files together stay under LOG_MAX_TOTAL_MB."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    removed = logs_mod.enforce(DATA_DIR)  # the limits may have been lowered since the last run
    fh = logs_mod.CappedRotatingFileHandler(DATA_DIR / "bot.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().addHandler(EventLogHandler())  # warnings and errors show in the dashboard's activity feed
    logging.getLogger().setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines hold full URLs (Telegram's has the token)
    if sys.stderr is not None:  # None under pythonw.exe (startup launch, no console)
        discord.utils.setup_logging(level=level)
    log.info("Logs: up to %g MB in %s (%g MB per file%s%s)", LOG_MAX_TOTAL_MB, DATA_DIR, logs_mod.file_bytes / logs_mod.MB,
             ", gzipped archives" if logs_mod.compress else "",
             f", archives kept {LOG_KEEP_DAYS:g} days" if LOG_KEEP_DAYS else "")
    if removed:
        log.info("Removed %d old log archive(s) to stay under the limit", len(removed))


def _claim_single_instance() -> bool:
    global _instance_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
    except OSError:
        s.close()
        return False
    _instance_sock = s
    return True


def _wait_for_network(timeout: float = 300) -> bool:
    """At logon Wi-Fi may not be up yet; wait until discord.com (or Telegram's API) resolves."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            socket.getaddrinfo("discord.com" if DISCORD_TOKEN else "api.telegram.org", 443)
            return True
        except OSError:
            if time.monotonic() > deadline:
                return False
            time.sleep(5)


def main() -> None:
    if "--mcp-web" in sys.argv:  # started by Claude Code as its web-tools server (see mcp_web_config)
        asyncio.run(_mcp_web_server())
        return
    _setup_logging()
    _register_bot_command()
    try:
        load_state()
    except Exception as e:
        log.error("%s", oneline(redact(e), 300))
        sys.exit(1)
    web_chat = bool(DASHBOARD_PORT) and DASHBOARD_CHAT in ("owner", "user")
    if not DISCORD_TOKEN and not TELEGRAM_BOT_TOKEN and not web_chat:
        log.error("Neither DISCORD_TOKEN nor TELEGRAM_BOT_TOKEN is set, and the dashboard chat is off (see .env.example).")
        sys.exit(1)
    if not _claim_single_instance():
        log.error("Another copy of the bot is already running; exiting.")
        sys.exit(1)
    if not CC_ENABLED:
        log.warning("OWNER_IDS and ALLOWED_USER_IDS are empty: Claude Code is disabled.")
    if DISCORD_TOKEN and not ALLOWED_USER_IDS:
        log.warning("ALLOWED_USER_IDS is empty: everyone in the server can use the local model.")
    if TELEGRAM_BOT_TOKEN and not (TELEGRAM_ALLOWED_USER_IDS or TELEGRAM_OWNER_IDS):
        log.warning("TELEGRAM_ALLOWED_USER_IDS is empty: the Telegram bot answers nobody (send it /start to get your id).")
    for name, path in WORKSPACES.items():
        if path == BASE_DIR or path in BASE_DIR.parents:
            log.warning("Workspace '%s' contains the bot folder: Claude Code could read .env (your tokens).", name)
    if not web_chat:  # with the dashboard chat, start right away instead: it works without internet
        wait = 1800 if "--boot" in sys.argv else 300  # started at boot (bot_control.ps1 boot): nobody is there to retry
        if not _wait_for_network(wait):
            log.error("No network after %d minutes; exiting.", wait // 60)
            sys.exit(1)
    asyncio.run(_run())


async def _run() -> None:
    """The backend first (dashboard and its chat, local models, reminders, scheduled prompts), then the front ends.
    Discord and Telegram keep retrying while there's no internet; everything local works meanwhile."""
    await core_start()
    if not DISCORD_TOKEN:
        try:
            await asyncio.Event().wait()
        finally:
            await core_stop()
        return
    import aiohttp

    async with bot:  # closing the client also stops the backend (LLMBot.close -> core_stop)
        delay = 5
        while True:
            try:
                await bot.start(DISCORD_TOKEN)  # setup_hook -> core_start is then a no-op
                return
            except discord.LoginFailure:
                log.error("Discord rejected DISCORD_TOKEN; Discord stays off (the dashboard and Telegram keep working).")
                await asyncio.Event().wait()
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError, discord.GatewayNotFound, discord.HTTPException) as e:
                log.warning("Can't reach Discord (%s); retrying in %ss. The dashboard chat and local models keep "
                            "working.", oneline(redact(e), 150) or type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)


if __name__ == "__main__":
    main()
