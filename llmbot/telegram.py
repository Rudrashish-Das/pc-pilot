"""
Telegram front end for the bot. The backend (engines, Claude Code sessions, reminders, scheduled prompts, per-chat
settings, budgets) is the core module (llmbot/core.py), shared with Discord; this file only translates.

- Messages in a private chat (or, in groups, @mentions and replies to the bot) go to core.handle_prompt, exactly
  like Discord messages. Photos, files and voice notes work the same way.
- The core talks to "channels" with Discord-shaped calls (send(content, embed=, view=, files=), message.edit(),
  channel.typing()). TgChannel/TgMessage accept those and render them for Telegram: Discord markdown -> Telegram HTML,
  embeds -> formatted text, "-#" grey lines -> italics, <t:..> timestamps -> local time text.
- The core's buttons and menus (discord.ui views: /panel, confirm, Stop, Retry, reminders' Done/Snooze, …) become
  inline keyboards. A tap calls the same callback with a small stand-in for discord.Interaction, after the same
  allow-list/owner checks (view.interaction_check). Select menus open as a list of buttons; text boxes (modals)
  become "send the text as your next message".

Topics: in a group with Topics turned on, or a private chat with the bot's threaded mode on (@BotFather), each topic
is its own "channel" for the core: its own Claude Code session, engine, permissions and local-model memory. The core
only knows channels by one int id, so a topic gets an id from TOPIC_BASE up (still a Telegram id, see
core.is_telegram_id), kept in the "tg_topics" document with its chat and thread; the main chat / General topic keeps
the chat id. A new topic starts with a copy of its chat's settings (not its session). "Full access without asking"
turned on or off in the main chat also applies to its topics (core.set_no_ask); in a topic, only to that topic.

Started by core_start() when TELEGRAM_BOT_TOKEN is set. Uses the plain Bot API over httpx (long polling), no extra
dependency. The token is part of every API URL, so errors are re-raised without the URL and log lines are scrubbed.
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any

import discord
import httpx

log = logging.getLogger("llmbot.telegram")
MISSING: Any = object()
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
DOWNLOAD_MAX = 20 * 1024 * 1024  # Bot API getFile limit
STALE_AFTER = timedelta(minutes=30)  # ignore messages older than this (sent while the bot was off)
PENDING_INPUT_SECONDS = 900
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
# Channel ids for topics: above any real Telegram id (< 2^52 ≈ 4.5e15), below Discord's (> 1e16)
TOPIC_BASE = 9 * 10 ** 15

COMMANDS = [
    ("panel", "Engine, models, session, settings for this chat"),
    ("new", "Start a new Claude Code session"),
    ("topic", "topic <name>: new topic = a separate conversation"),
    ("usage", "Context window, Claude plan limits, spend"),
    ("skills", "What Claude Code learned from earlier tasks"),
    ("compact", "Shrink a long Claude Code conversation"),
    ("stop", "Stop the reply that's running"),
    ("tasks", "List and cancel reminders and scheduled prompts"),
    ("remind", "remind <when> | <what>"),
    ("schedule", "schedule <cron> | <prompt>"),
    ("claude", "Run a task with Claude Code (progress card)"),
    ("local", "Ask the local model directly"),
    ("reset", "Clear the local model's chat memory"),
    ("log", "Full log of the last Claude Code reply"),
    ("unload", "Free GPU memory now"),
    ("power", "Lock, sleep, restart or shut down the PC"),
    ("dashboard", "Link to the web dashboard (owners, private chat)"),
    ("help", "How to use this bot"),
]


class TgError(discord.HTTPException):
    """A failed Bot API call. Subclasses discord.HTTPException so the core's existing error handling applies."""

    def __init__(self, description: str, code: int = 0):
        Exception.__init__(self, description)
        self.status = self.code = code
        self.text = description
        self.response = None


class _ScrubToken(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self.token in msg:
            record.msg, record.args = msg.replace(self.token, "[redacted]"), None
        return True


# =============================================================================
# Discord markdown -> Telegram HTML
# =============================================================================


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def fmt_ts(epoch: int, style: str, tz) -> str:
    dt = datetime.fromtimestamp(epoch, tz)
    now = datetime.now(tz)
    if style == "R":
        secs = (dt - now).total_seconds()
        a = abs(secs)
        if a < 45:
            return "now" if a < 5 else ("in a few seconds" if secs > 0 else "a few seconds ago")
        for unit, n in (("day", 86400), ("hour", 3600), ("min", 60)):
            if a >= n * (0.75 if unit != "min" else 1):
                v = round(a / n)
                span = f"{v} {unit}" + ("s" if v != 1 and unit != "min" else "")
                return f"in {span}" if secs > 0 else f"{span} ago"
    day = ("today" if dt.date() == now.date() else "tomorrow" if dt.date() == now.date() + timedelta(days=1)
           else "yesterday" if dt.date() == now.date() - timedelta(days=1) else None)
    if style == "t":
        return f"{dt:%H:%M}"
    if style == "T":
        return f"{dt:%H:%M:%S}"
    if style == "d":
        return f"{dt:%d/%m/%Y}"
    if style == "D":
        return f"{dt.day} {dt:%B %Y}"
    if style == "F":
        return f"{dt:%A}, {dt.day} {dt:%b %Y, %H:%M}"
    return f"{day} {dt:%H:%M}" if day else f"{dt.day} {dt:%b %Y, %H:%M}"


_INLINE = [
    (re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*"), r"<b>\1</b>"),
    (re.compile(r"__(?!\s)(.+?)(?<!\s)__"), r"<u>\1</u>"),
    (re.compile(r"~~(?!\s)(.+?)(?<!\s)~~"), r"<s>\1</s>"),
    (re.compile(r"\|\|(?!\s)(.+?)(?<!\s)\|\|"), r"<tg-spoiler>\1</tg-spoiler>"),
    (re.compile(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<![\s*])\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<![\w])_(?![\s_])([^_\n]+?)(?<![\s_])_(?![\w])"), r"<i>\1</i>"),
]


def render(text: str, *, as_html: bool, tz, mention, where) -> str:
    """Discord-flavoured text -> Telegram HTML (as_html) or clean plain text (fallback when HTML is rejected)."""
    keep: list[str] = []

    def hold(h: str, plain: str) -> str:
        keep.append(h if as_html else plain)
        return f"\x00{len(keep) - 1}\x00"

    def code_block(m: re.Match) -> str:
        lang, body = m.group(1) or "", m.group(2).strip("\n")
        cls = f' class="language-{_esc(lang)}"' if lang else ""
        return hold(f"<pre><code{cls}>{_esc(body)}</code></pre>", body)

    t = str(text or "")
    t = re.sub(r"```([\w+-]*)\n?(.*?)```", code_block, t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", lambda m: hold(f"<code>{_esc(m.group(1))}</code>", m.group(1)), t)
    t = re.sub(r"<t:(-?\d+)(?::([tTdDfFR]))?>", lambda m: hold(_esc(fmt_ts(int(m.group(1)), m.group(2) or "f", tz)),
                                                                fmt_ts(int(m.group(1)), m.group(2) or "f", tz)), t)
    t = re.sub(r" ?<@!?(\d+)>", lambda m: (lambda h, p: hold(h, p) if h else "")(*mention(int(m.group(1)))), t)
    t = re.sub(r" ?<@&\d+>", "", t)
    t = re.sub(r"<#(-?\d+)>", lambda m: hold(_esc(where(int(m.group(1)))), where(int(m.group(1)))), t)
    t = re.sub(r"<a?:(\w+):\d+>", r":\1:", t)
    t = re.sub(r"\[([^\]\n]+)\]\(<?(https?://[^\s)>]+)>?\)",
               lambda m: hold(f'<a href="{html.escape(m.group(2), quote=True)}">{_esc(m.group(1))}</a>',
                              f"{m.group(1)} ({m.group(2)})"), t)
    t = re.sub(r"<(https?://[^\s>]+)>", lambda m: hold(_esc(m.group(1)), m.group(1)), t)
    if as_html:
        t = _esc(t)
    out: list[str] = []
    quote: list[str] = []

    def flush_quote():
        if quote:
            out.append(f"<blockquote>{chr(10).join(quote)}</blockquote>" if as_html else "\n".join(f"> {q}" for q in quote))
            quote.clear()

    gt = "&gt; " if as_html else "> "
    for ln in t.split("\n"):
        if as_html:
            for rx, rep in _INLINE:
                ln = rx.sub(rep, ln)
        if ln.startswith(gt) or ln == gt.strip():
            quote.append(ln[len(gt):])
            continue
        flush_quote()
        if m := re.match(r"-# (.*)", ln):
            ln = f"<i>{m.group(1)}</i>" if as_html else m.group(1)
        elif m := re.match(r"#{1,3} (.*)", ln):
            ln = f"<b>{m.group(1)}</b>" if as_html else m.group(1)
        elif not as_html:
            ln = re.sub(r"\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|\|\|(.+?)\|\|", lambda m: next(g for g in m.groups() if g), ln)
        out.append(ln)
    flush_quote()
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], "\n".join(out)).strip()


# =============================================================================
# Channel / message stand-ins the core can talk to
# =============================================================================


class _Typing:
    def __init__(self, tg: "Telegram", cid: int):
        self.tg, self.cid, self.task = tg, cid, None

    async def _ping(self):
        chat, thread = self.tg.place(self.cid)
        try:
            await self.tg.api("sendChatAction", chat_id=chat, message_thread_id=thread, action="typing")
        except Exception:
            pass

    async def _loop(self):
        while True:
            await asyncio.sleep(4.5)  # the indicator lasts ~5 s
            await self._ping()

    async def __aenter__(self):
        await self._ping()
        self.task = asyncio.create_task(self._loop())

    async def __aexit__(self, *exc):
        if self.task:
            self.task.cancel()


class TgChannel:
    """A chat, or one topic in it. .id is the core's channel id (see Telegram.cid)."""
    guild = None

    def __init__(self, tg: "Telegram", cid: int):
        self.tg, self.id = tg, cid

    def typing(self) -> _Typing:
        return _Typing(self.tg, self.id)

    async def send(self, content: str | None = None, *, embed=None, embeds=None, view=None, file=None, files=None,
                   reference=None, reply_to=None, **_ignored) -> "TgMessage":
        embed = embed or (embeds[0] if embeds else None)
        files = list(files or []) + ([file] if file else [])
        ref = reply_to or reference
        return await self.tg.send(self.id, content, embed=embed, view=view, files=files,
                                  reply_to=getattr(ref, "message_id", None))


class TgMessage:
    def __init__(self, tg: "Telegram", cid: int, message_id: int | None, content: str | None = None,
                 embed=None, view=None):
        self.tg, self.cid, self.message_id = tg, cid, message_id
        self.chat_id = tg.place(cid)[0]  # the real chat, for Bot API calls
        self.id = message_id
        self.content, self.embed, self.view = content or "", embed, view
        self.channel = TgChannel(tg, cid)

    async def edit(self, content=MISSING, *, embed=MISSING, view=MISSING, **_ignored) -> "TgMessage":
        if content is not MISSING:
            self.content = content or ""
        if embed is not MISSING:
            self.embed = embed
        if view is not MISSING:
            self.view = view
        if self.message_id is not None:
            await self.tg.edit(self)
        return self

    async def reply(self, content: str | None = None, **kw) -> "TgMessage":
        kw.pop("mention_author", None)
        return await self.channel.send(content, reply_to=self, **kw)

    def to_reference(self, **_):
        return self

    async def delete(self):
        if self.message_id is not None:
            await self.tg.api("deleteMessage", chat_id=self.chat_id, message_id=self.message_id)


class TgAttachment:
    """Looks like a discord.Attachment to core.save_uploads."""

    def __init__(self, tg: "Telegram", file_id: str, unique_id: str, filename: str, size: int, content_type: str):
        self.tg, self.file_id, self.id = tg, file_id, unique_id
        self.filename, self.size, self.content_type = filename, size, content_type

    def is_voice_message(self) -> bool:
        return False

    async def read(self) -> bytes:
        try:
            return await self.tg.download(self.file_id)
        except TgError as e:
            raise OSError(str(e)) from None


class _User:
    def __init__(self, u: dict):
        self.id = u["id"]
        self.name = self.display_name = u.get("first_name") or u.get("username") or str(u["id"])
        self.mention = self.name


class _Response:
    def __init__(self, fi: "FakeInteraction"):
        self.fi, self._done = fi, False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_):
        self._done = True

    async def send_message(self, content: str | None = None, *, ephemeral: bool = False, **kw):
        self._done = True
        await self.fi.post(content, ephemeral=ephemeral, **kw)

    async def edit_message(self, **kw):
        self._done = True
        await self.fi.message.edit(**kw)

    async def send_modal(self, modal):
        self._done = True
        await self.fi.tg.ask_for_text(self.fi, modal)


class _Followup:
    def __init__(self, fi: "FakeInteraction"):
        self.fi = fi

    async def send(self, content: str | None = None, *, ephemeral: bool = False, wait: bool = False, **kw):
        return await self.fi.post(content, ephemeral=ephemeral, **kw)


class FakeInteraction:
    """Enough of discord.Interaction for the core's view callbacks."""

    guild = None
    app_permissions = None
    type = discord.InteractionType.component

    def __init__(self, tg: "Telegram", message: TgMessage, user: dict, data: dict, callback_id: str | None):
        self.tg, self.message, self.data, self.callback_id = tg, message, data, callback_id
        self.user = _User(user)
        self.channel = message.channel
        self.channel_id = message.cid
        self.response = _Response(self)
        self.followup = _Followup(self)
        self.answered = callback_id is None

    async def answer(self, text: str = "", alert: bool = False):
        if self.answered:
            return
        self.answered = True
        try:
            await self.tg.api("answerCallbackQuery", callback_query_id=self.callback_id, text=text[:200],
                              show_alert=alert or None)
        except TgError:
            pass

    async def post(self, content: str | None = None, *, ephemeral: bool = False, embed=None, view=None, file=None,
                   files=None, **_ignored):
        # Short private notes ("⛔ Only owners…", "⏹️ Stopping…") become the tap's pop-up instead of a message.
        if ephemeral and content and not (embed or view or file or files) and not self.answered:
            plain = self.tg.plain(content, self.channel_id)
            if len(plain) <= 200:
                await self.answer(plain, alert=len(plain) > 60)
                return None
        return await self.channel.send(content, embed=embed, view=view, file=file, files=files)


class _ViewEntry:
    def __init__(self, view, msg: TgMessage):
        self.view, self.msg, self.created = view, msg, time.monotonic()

    def expired(self) -> bool:
        return self.view.is_finished() or bool(self.view.timeout and time.monotonic() - self.created > self.view.timeout)


# =============================================================================
# The front end
# =============================================================================


class Telegram:
    def __init__(self, core, token: str):
        self.core, self.token = core, token
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
        self.username, self.bot_id = "", 0
        self.topics_in_private = False  # threaded mode, switched on for the bot in @BotFather
        self._closed = False
        self._views: OrderedDict[str, _ViewEntry] = OrderedDict()
        self._pending: dict[tuple[int, int], tuple] = {}  # (chat, user) -> (modal, text input, message, deadline)
        self._names: dict[int, str] = {}
        self._chats: dict[int, str] = {}
        self._topics: dict[int, dict] = {}  # topic channel id -> {"chat", "thread", "name"}
        self._topic_ids: dict[tuple[int, int], int] = {}
        self._topics_file = core.DATA_DIR / "tg_topics.json"
        for k, t in (core.STORE.load("tg_topics", self._topics_file, {}) or {}).items():
            self._topics[int(k)] = t
            self._topic_ids[(t["chat"], t["thread"])] = int(k)
        self._inherit_no_ask_once()
        scrub = _ScrubToken(token)
        for h in logging.getLogger().handlers:
            h.addFilter(scrub)
        log.addFilter(scrub)

    # ---- front-end interface used by the core (see core._frontends)
    def owns(self, channel_id: int) -> bool:
        return self.core.is_telegram_id(channel_id)

    async def get_channel(self, channel_id: int) -> TgChannel:
        return TgChannel(self, channel_id)

    def where(self, channel_id: int) -> str:
        chat, thread = self.place(channel_id)
        name = self._chats.get(chat)
        base = f"Telegram: {name}" if name else ("Telegram chat" if chat > 0 else "Telegram group")
        if thread is None:
            return base
        return f"{base} › {self._topics[channel_id].get('name') or f'topic {thread}'}"

    # ---- topics: one core channel id per (chat, topic)
    def place(self, cid: int) -> tuple[int, int | None]:
        """Core channel id -> (Telegram chat id, topic thread id or None)."""
        t = self._topics.get(cid)
        return (t["chat"], t["thread"]) if t else (cid, None)

    def subchannels(self, cid: int) -> list[int]:
        """The topic channel ids of a chat (none for a topic itself)."""
        return [k for k, t in self._topics.items() if t["chat"] == cid] if cid not in self._topics else []

    def _inherit_no_ask_once(self) -> None:
        """Topics made before they copied "full access without asking" get their chat's setting, once each."""
        changed = False
        for k, t in self._topics.items():
            if t.get("no_ask_copied"):
                continue
            t["no_ask_copied"] = changed = True
            until = self.core.no_ask_until(t["chat"])
            if until is not None and self.core.no_ask_until(k) is None:
                self.core.update_settings(k, cc_perm="full", cc_no_ask_until=until)
        if changed:
            self.core.STORE.save("tg_topics", self._topics_file, {str(k): v for k, v in self._topics.items()})

    def cid(self, chat_id: int, thread: int | None, name: str | None = None) -> int:
        """(chat, topic) -> the core's channel id for it; a new topic gets the next free id and its chat's settings."""
        if thread is None:
            return chat_id
        cid = self._topic_ids.get((chat_id, thread))
        if cid is None:
            cid = max(self._topics, default=TOPIC_BASE) + 1
            self._topics[cid] = {"chat": chat_id, "thread": thread, "name": name, "no_ask_copied": True}
            self._topic_ids[(chat_id, thread)] = cid
            self.core.inherit_settings(cid, chat_id)
        elif not name or self._topics[cid].get("name") == name:
            return cid
        self._topics[cid]["name"] = name or self._topics[cid].get("name")
        self.core.STORE.save("tg_topics", self._topics_file, {str(k): v for k, v in self._topics.items()})
        return cid

    # ---- Bot API
    async def api(self, method: str, *, files: dict | None = None, _timeout: float | None = None, **params) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            try:
                if files:
                    data = {k: json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v) for k, v in params.items()}
                    r = await self.http.post(f"{self.base}/{method}", data=data, files=files,
                                             timeout=_timeout or 120)
                else:
                    r = await self.http.post(f"{self.base}/{method}", json=params, timeout=_timeout or 30)
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:  # never let the URL (with the token) into an error message
                raise TgError(f"Telegram {method}: {type(e).__name__}") from None
            if body.get("ok"):
                return body.get("result")
            retry = (body.get("parameters") or {}).get("retry_after")
            if body.get("error_code") == 429 and retry and attempt < 3:
                await asyncio.sleep(float(retry) + 0.5)
                continue
            raise TgError(f"Telegram {method}: {body.get('description', 'error')}", body.get("error_code") or 0)
        raise TgError(f"Telegram {method}: rate limited", 429)

    async def download(self, file_id: str) -> bytes:
        f = await self.api("getFile", file_id=file_id)
        try:
            r = await self.http.get(f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}", timeout=120)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TgError(f"Telegram download: {type(e).__name__}") from None
        return r.content

    # ---- rendering
    def _mention(self, cid: int):
        chat_id = self.place(cid)[0]

        def mention(uid: int) -> tuple[str, str]:
            if chat_id > 0 or not self.core.is_telegram_id(uid):  # private chat: the reader is the person
                return "", ""
            name = self._names.get(uid, "you")
            return f'<a href="tg://user?id={uid}">{_esc(name)}</a>', name
        return mention

    def _render(self, text: str, cid: int, as_html: bool = True) -> str:
        return render(self.core.quiet_grey(self._stats(text or "", cid)), as_html=as_html, tz=self.core.TZ,
                      mention=self._mention(cid), where=self.core.where)

    def _stats(self, text: str, cid: int) -> str:
        """Telegram has no small grey text, so the routine part of a stats line (before core.STATS_MARK) becomes a
        tap-to-reveal spoiler, or is dropped when the chat turned it off. Warnings after the mark stay visible."""
        mark = self.core.STATS_MARK
        if mark not in text:
            return text
        off = self.core.get_settings(cid).get("tg_stats") == "off"
        out = []
        for ln in text.split("\n"):
            if ln.startswith("-# ") and mark in ln:
                stats, _, warn = ln[3:].partition(mark)
                warn = warn.strip().lstrip("·").strip()
                if off:
                    ln = f"-# {warn}" if warn else None
                else:
                    ln = f"-# ||{stats.strip()}||" + (f" · {warn}" if warn else "")
            if ln is not None:
                out.append(ln)
        return "\n".join(out)

    def plain(self, text: str, cid: int) -> str:
        return self._render(text, cid, as_html=False)

    def _body(self, content: str | None, embed, cid: int, as_html: bool) -> str:
        parts = [self._render(content, cid, as_html)] if content else []
        if embed is not None:
            r = lambda t: self._render(str(t or ""), cid, as_html)  # noqa: E731
            if embed.title:
                parts.append(f"<b>{r(embed.title)}</b>" if as_html else r(embed.title))
            if embed.description:
                parts.append(r(embed.description))
            fields = []
            for f in embed.fields:
                name = r(f.name)
                name = f"<b>{name}</b>" if as_html else name
                val = r(f.value)
                fields.append(f"{name}: {val}" if f.inline and "\n" not in val and len(val) < 80 else f"{name}\n{val}")
            if fields:
                parts.append("\n".join(fields))
            if embed.footer and embed.footer.text:
                parts.append(f"<i>{r(embed.footer.text)}</i>" if as_html else r(embed.footer.text))
        return "\n\n".join(p for p in parts if p.strip())

    # ---- keyboards from discord.ui views
    def _register(self, view, msg: TgMessage) -> str:
        tok = secrets.token_urlsafe(6)
        self._views[tok] = _ViewEntry(view, msg)
        while len(self._views) > 400:
            self._views.popitem(last=False)
        return tok

    @staticmethod
    def _label(item) -> str:
        return f"{item.emoji or ''} {item.label or ''}".strip() or "•"

    def _keyboard(self, view, tok: str) -> dict | None:
        rows: list[list[dict]] = []
        auto_open = False
        by_row: dict[int, list[dict]] = {}
        for i, item in enumerate(getattr(view, "children", [])):
            if isinstance(item, discord.ui.Select):
                cur = next((o for o in item.options if o.default), None)
                text = f"{cur.emoji or ''} {cur.label}".strip() if cur else (item.placeholder or "Choose…")
                rows.append([{"text": f"{text} ▾", "callback_data": f"{tok}:{i}:m"}])
                auto_open = False
                continue
            if not isinstance(item, discord.ui.Button) or item.disabled:
                continue
            btn = ({"text": self._label(item), "url": item.url} if item.url
                   else {"text": self._label(item), "callback_data": f"{tok}:{i}"})
            if item.row is not None:
                if item.row not in by_row:
                    by_row[item.row] = []
                    rows.append(by_row[item.row])
                by_row[item.row].append(btn)
                auto_open = False
            elif auto_open and len(rows[-1]) < 3:
                rows[-1].append(btn)
            else:
                rows.append([btn])
                auto_open = True
        # Telegram buttons are narrow: split explicit rows wider than 3
        out = []
        for row in rows:
            out += [row[j:j + 3] for j in range(0, len(row), 3)]
        return {"inline_keyboard": out} if out else None

    def _markup(self, msg: TgMessage) -> dict | None:
        if msg.view is None:
            return None
        return self._keyboard(msg.view, self._register(msg.view, msg))

    # ---- sending
    async def _send_text(self, cid: int, content, embed, markup, reply_to: int | None) -> dict:
        chat_id, thread = self.place(cid)
        reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        body = self._body(content, embed, cid, True)
        if body and len(body) <= TEXT_LIMIT:
            try:
                return await self.api("sendMessage", chat_id=chat_id, message_thread_id=thread, text=body,
                                      parse_mode="HTML",
                                      reply_markup=markup, reply_parameters=reply, link_preview_options={"is_disabled": True})
            except TgError as e:
                if "parse" not in str(e).lower() and "entit" not in str(e).lower() and "tag" not in str(e).lower():
                    raise
                log.info("HTML rejected (%s); sending plain text", e)
        plain = self._body(content, embed, cid, False) or "…"
        chunks = [plain[i:i + TEXT_LIMIT] for i in range(0, len(plain), TEXT_LIMIT)]
        sent = None
        for j, chunk in enumerate(chunks):
            sent = await self.api("sendMessage", chat_id=chat_id, message_thread_id=thread, text=chunk, reply_parameters=reply if j == 0 else None,
                                  reply_markup=markup if j == len(chunks) - 1 else None,
                                  link_preview_options={"is_disabled": True})
        return sent

    async def _send_file(self, cid: int, f, reply_to: int | None) -> dict:
        chat_id, thread = self.place(cid)
        f.reset()
        data, name = f.fp.read(), f.filename or "file"
        reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        if name.lower().endswith(IMAGE_EXT) and len(data) <= 10 * 1024 * 1024:
            try:
                return await self.api("sendPhoto", chat_id=chat_id, message_thread_id=thread, reply_parameters=reply,
                                      files={"photo": (name, data)})
            except TgError:
                pass  # e.g. unusual dimensions: send as a file instead
        return await self.api("sendDocument", chat_id=chat_id, message_thread_id=thread, reply_parameters=reply,
                              files={"document": (name, data)})

    async def send(self, cid: int, content: str | None, *, embed=None, view=None, files=(), reply_to=None) -> TgMessage:
        msg = TgMessage(self, cid, None, content, embed, view)
        if content and embed is None and view is None and not files and not self._render(content, cid):
            return msg  # only a stats line, and this chat turned it off
        if content or embed is not None or view is not None or not files:
            sent = await self._send_text(cid, content, embed, self._markup(msg), reply_to)
            msg.message_id = msg.id = sent["message_id"]
        for f in files:
            sent = await self._send_file(cid, f, reply_to if msg.message_id is None else None)
            if msg.message_id is None:
                msg.message_id = msg.id = sent["message_id"]
        return msg

    async def edit(self, msg: TgMessage) -> None:
        markup = self._markup(msg) or {"inline_keyboard": []}
        body = self._body(msg.content, msg.embed, msg.cid, True)
        try:
            try:
                if len(body) > TEXT_LIMIT:
                    raise TgError("too long for HTML")
                await self.api("editMessageText", chat_id=msg.chat_id, message_id=msg.message_id, text=body or "…",
                               parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})
            except TgError as e:
                if "not modified" in str(e):
                    return
                if not re.search(r"parse|entit|tag|too long", str(e), re.I):
                    raise
                plain = self._body(msg.content, msg.embed, msg.cid, False)[:TEXT_LIMIT] or "…"
                await self.api("editMessageText", chat_id=msg.chat_id, message_id=msg.message_id, text=plain,
                               reply_markup=markup, link_preview_options={"is_disabled": True})
        except TgError as e:
            if "not modified" not in str(e):
                raise

    # ---- text input in place of Discord modals
    async def ask_for_text(self, fi: FakeInteraction, modal) -> None:
        box = next((c for c in modal.children if isinstance(c, discord.ui.TextInput)), None)
        if box is None:
            return
        self._pending[(fi.channel_id, fi.user.id)] = (modal, box, fi.message, time.monotonic() + PENDING_INPUT_SECONDS)
        cur = f"\nCurrent text (tap to copy):\n```\n{box.default}\n```" if box.default else ""
        await fi.answer()
        chat, thread = self.place(fi.channel_id)
        await self.api("sendMessage", chat_id=chat, message_thread_id=thread, parse_mode="HTML",
                       text=self._render(f"✏️ **{modal.title}**\nSend the {box.label.lower()} as your next message, "
                                         f"or /cancel.{cur}", fi.channel_id),
                       reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": box.label[:64]})

    async def _submit_text(self, key, text: str, user: dict) -> None:
        modal, box, origin, _ = self._pending.pop(key)
        box._value = text  # what discord.py sets from a submitted modal
        fi = FakeInteraction(self, origin, user, {"custom_id": modal.custom_id, "components": []}, None)
        try:
            await modal.on_submit(fi)
        except Exception as e:
            log.exception("text input handler failed")
            await fi.post(self.core.with_hint(f"⚠️ {type(e).__name__}: {self.core.oneline(self.core.redact(e), 200)}"))

    # ---- updates
    async def run(self) -> None:
        delay = 5
        while not self._closed:
            try:
                me = await self.api("getMe")
                self.username, self.bot_id = me.get("username") or "", me["id"]
                self.topics_in_private = bool(me.get("has_topics_enabled"))
                await self.api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
                break
            except TgError as e:
                log.warning("Telegram start failed (%s); retrying in %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
        log.info("Telegram: logged in as @%s", self.username)
        self.core.note_event("bot", f"Telegram connected as @{self.username}")
        offset = None
        while not self._closed:
            try:
                updates = await self.api("getUpdates", offset=offset, timeout=50, _timeout=70,
                                         allowed_updates=["message", "callback_query"])
            except TgError as e:
                if self._closed:
                    break
                log.warning("Telegram polling: %s", e)  # 409 = another copy polls with this token
                await asyncio.sleep(10 if e.code == 409 else 5)
                continue
            except asyncio.CancelledError:
                break
            for u in updates or []:
                offset = u["update_id"] + 1
                self.core._spawn(self._dispatch(u))

    async def close(self) -> None:
        self._closed = True
        await self.http.aclose()

    async def _dispatch(self, u: dict) -> None:
        try:
            if "callback_query" in u:
                await self._on_callback(u["callback_query"])
            elif "message" in u:
                await self._on_message(u["message"])
        except Exception as e:
            log.exception("Telegram update failed")
            m = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
            chat, thread = (m.get("chat") or {}).get("id"), self._topic_of(m)[0]
            uid = ((u.get("message") or u.get("callback_query") or {}).get("from") or {}).get("id")
            if chat and uid and self.core.is_allowed(uid):
                try:
                    await self.api("sendMessage", chat_id=chat, message_thread_id=thread,
                                   text=self.core.with_hint(f"⚠️ Something went wrong: {type(e).__name__}: "
                                                            f"{self.core.oneline(self.core.redact(e), 300)}"))
                except TgError:
                    pass

    def _seen(self, user: dict, chat: dict, cid: int | None = None) -> None:
        self._names[user["id"]] = name = user.get("first_name") or user.get("username") or str(user["id"])
        self.core.remember_name(f"u:{user['id']}", name)  # kept across restarts, for the dashboard
        if chat.get("id"):
            self._chats[chat["id"]] = chat.get("title") or f"{chat.get('first_name') or name} (private)"
            self.core.remember_name(f"c:{chat['id']}", f"Telegram: {self._chats[chat['id']]}")
            if cid is not None and cid != chat["id"]:
                self.core.remember_name(f"c:{cid}", self.where(cid))

    @staticmethod
    def _topic_of(m: dict) -> tuple[int | None, str | None]:
        """(thread id, topic name if this message shows it) for a message in a topic; (None, None) otherwise.
        Plain groups also use message_thread_id for reply chains, so only is_topic_message counts."""
        if not m.get("is_topic_message"):
            return None, None
        made = (m.get("forum_topic_created") or m.get("forum_topic_edited")
                or (m.get("reply_to_message") or {}).get("forum_topic_created") or {})
        return m.get("message_thread_id"), made.get("name")

    async def _on_message(self, m: dict) -> None:
        core = self.core
        user, chat = m.get("from") or {}, m["chat"]
        uid = user.get("id")
        if not uid or user.get("is_bot"):
            return
        thread, topic_name = self._topic_of(m)
        private = chat.get("type") == "private"
        text = m.get("text") or m.get("caption") or ""
        cmd, args = None, ""
        if cm := re.match(r"^/([A-Za-z0-9_]+)(?:@(\w+))?(?:\s+([\s\S]*))?$", text.strip()):
            if cm.group(2) and cm.group(2).lower() != self.username.lower():
                return  # a command for another bot in the group
            cmd, args = cm.group(1).lower(), (cm.group(3) or "").strip()
        if not core.is_allowed(uid):
            if cmd == "start" and private and not (core.TELEGRAM_ALLOWED_USER_IDS or core.TELEGRAM_OWNER_IDS):
                # Setup: nobody is allowed yet, so tell the person their id (nothing else is revealed or done).
                await self.api("sendMessage", chat_id=chat["id"], message_thread_id=thread, parse_mode="HTML", text=(
                    f"Your Telegram user id is <code>{uid}</code>.\nAdd it to TELEGRAM_ALLOWED_USER_IDS in the bot's "
                    ".env, then restart the bot."))
            log.info("Ignored Telegram message from user %s (not in TELEGRAM_ALLOWED_USER_IDS)", uid)
            return
        cid = self.cid(chat["id"], thread, topic_name)
        self._seen(user, chat, cid)
        if time.time() - m.get("date", 0) > STALE_AFTER.total_seconds():
            log.info("Skipped a Telegram message from %s sent while the bot was offline", uid)
            return
        chan = TgChannel(self, cid)
        me = TgMessage(self, cid, m["message_id"], text)
        key = (cid, uid)
        if key in self._pending:
            if self._pending[key][3] < time.monotonic():
                self._pending.pop(key)
            elif cmd == "cancel":
                self._pending.pop(key)
                await me.reply("Cancelled.")
                return
            elif text and not cmd:
                await self._submit_text(key, text, user)
                return
        if cmd:
            await self._command(cmd, args, chan, me, user, private)
            return
        # In a topic, a message that isn't a reply points at the topic's first message: not a reply to the bot
        rt = m.get("reply_to_message") or {}
        replied_to_me = (rt.get("from") or {}).get("id") == self.bot_id and "forum_topic_created" not in rt
        mentioned = bool(self.username) and re.search(rf"@{re.escape(self.username)}\b", text, re.I)
        addressed = private or replied_to_me or bool(mentioned)
        voice = (m.get("voice") or m.get("video_note")) if core.VOICE_ENABLED else None
        if voice and not private:
            mode = core.get_settings(cid)["voice"]
            if mode == "off" or (mode == "replies" and not replied_to_me):
                return
            addressed = True
        if not addressed:
            return
        prompt = re.sub(rf"@{re.escape(self.username)}\b", "", text, flags=re.I).strip() if self.username else text.strip()
        prefix = None
        if voice:
            async with chan.typing():
                heard = await self._hear(me, voice)
            if not heard:
                return
            prompt = f"{prompt}\n{heard}".strip()
            prefix = f'-# heard: "{core.clip(heard, 300)}"'
        atts, notes = self._attachments(m)
        if notes:
            await me.reply("-# " + "; ".join(notes))
        if not prompt and not atts:
            if not notes:
                await me.reply("Hi! Ask me anything, or see /help.")
            return
        async with chan.typing():
            await core.handle_prompt(chan, uid, prompt, core.Out(chan, reply_to=me, prefix=prefix), attachments=atts)

    def _attachments(self, m: dict) -> tuple[list[TgAttachment], list[str]]:
        found = []
        if m.get("photo"):
            p = m["photo"][-1]  # largest size
            found.append((p, f"photo_{m['message_id']}.jpg", "image/jpeg"))
        for kind, default in (("document", "file"), ("audio", "audio"), ("video", "video.mp4"), ("animation", "animation.mp4")):
            if m.get(kind):
                d = m[kind]
                found.append((d, d.get("file_name") or default, d.get("mime_type") or "application/octet-stream"))
        atts, notes = [], []
        for d, name, ctype in found:
            if (d.get("file_size") or 0) > DOWNLOAD_MAX:
                notes.append(f"{name}: over 20 MB, which bots can't download on Telegram")
                continue
            atts.append(TgAttachment(self, d["file_id"], d.get("file_unique_id") or d["file_id"][-12:], name,
                                     d.get("file_size") or 0, ctype))
        return atts, notes

    async def _hear(self, me: TgMessage, voice: dict) -> str | None:
        core = self.core
        if (voice.get("duration") or 0) > core.VOICE_MAX_SECONDS:
            await me.reply(f"-# That's {voice['duration']}s; I only listen to voice notes up to {core.VOICE_MAX_SECONDS}s.")
            return None
        if (voice.get("file_size") or 0) > DOWNLOAD_MAX:
            await me.reply("-# That voice note is too large.")
            return None
        try:
            text, seconds = await core.transcribe(await self.download(voice["file_id"]))
        except ImportError:
            await me.reply("-# Voice notes aren't set up on the bot yet (faster-whisper missing).")
            return None
        except Exception as e:
            log.exception("transcription failed")
            await me.reply(core.with_hint(f"-# Couldn't transcribe that: {core.oneline(core.redact(e), 150)}",
                                          type(e).__name__, where="voice"))
            return None
        log.info("Telegram voice note %.0fs transcribed (%d chars)", seconds, len(text))
        if not text:
            await me.reply("-# I couldn't make out any speech in that.")
            return None
        return text

    # ---- commands (same behaviour as the Discord slash commands)
    async def _command(self, cmd: str, args: str, chan: TgChannel, me: TgMessage, user: dict, private: bool) -> None:
        core = self.core
        uid, cid = user["id"], chan.id
        owner = core.CC_ENABLED and core.is_owner(uid)
        out = core.Out(chan, reply_to=me)
        if cmd in ("start", "help"):
            await out(content=self.help_text(uid))
        elif cmd == "panel":
            await chan.send(embed=core.panel_embed(cid), view=await core.PanelView.build(cid))
        elif cmd == "tasks":
            await chan.send(embed=core.tasks_embed(uid), view=core.TasksView(uid))
        elif cmd == "usage":
            await chan.send(embed=core.usage_embed(cid), view=core.UsageView(cid))
        elif cmd == "skills":
            await out(content=core.skills_reply(uid, args))
        elif cmd == "topic":
            await self._new_topic(args, chan, out, private)
        elif cmd == "new":
            if not owner:
                await out(content="⛔ Only owners can manage Claude Code sessions.")
                return
            core.update_settings(cid, cc_session=None)
            await out(content="🆕 Your next message starts a new Claude Code session.")
        elif cmd in ("compact", "ask"):
            prompt = "/compact" if cmd == "compact" else args
            if not prompt:
                await out(content="Usage: /ask <message> (or just send the message)")
                return
            async with chan.typing():
                await core.handle_prompt(chan, uid, prompt, out)
        elif cmd == "claude":
            if not owner:
                await out(content="⛔ Claude Code is owner-only" + ("" if core.CC_ENABLED else " and disabled") + ".")
                return
            if not args:
                await out(content="Usage: /claude <task>")
                return
            await core.request_cc(chan, uid, args, core.snapshot(cid), out)
        elif cmd == "local":
            if not args:
                await out(content="Usage: /local <message>")
                return
            async with chan.typing():
                await core.answer_local(chan, uid, args, out)
        elif cmd == "remind":
            when, sep, what = args.partition("|")
            if not sep or not what.strip():
                await out(content="Usage: `/remind in 10 min | take my meds` (or just say \"remind me to … in 10 min\")")
                return
            try:
                r = core.add_reminder(when.strip(), what.strip(), cid, uid)
            except ValueError as e:
                await out(content=f"⚠️ {e}")
                return
            await out(content=core.reminder_line(r))
        elif cmd == "schedule":
            cron, sep, prompt = args.partition("|")
            if not sep or not prompt.strip():
                await out(content=f"Usage: `/schedule 0 9 * * 1-5 | <prompt>` (5-field cron, {core.TIMEZONE}), or just "
                                  "say \"every weekday at 9 tell me …\"")
                return
            engine = "claude" if core.get_settings(cid)["engine"] == "claude" and owner else "local"
            try:
                t = core.add_task(cron.strip(), prompt.strip(), prompt.strip(), cid, uid, engine=engine)
            except ValueError as e:
                await out(content=f"⚠️ {e}")
                return
            await out(content=f"⏰ Scheduled `{t['id']}`: {core.task_label(t)}\n-# Read-only, in a fresh session. "
                              "/tasks changes the model or what it may do.")
        elif cmd == "reset":
            core.forget_history(cid)
            await out(content="🧹 Local chat history cleared.")
        elif cmd == "stop":
            if not core.is_owner(uid):
                await out(content="⛔ Only owners can stop Claude Code.")
                return
            job = core._last_job.get(cid)
            if not job or job.status == "done":
                await out(content="Nothing is running here.")
                return
            job.stop_requested = True
            if job.proc:
                await core.kill_tree(job.proc)
            await out(content="⏹️ Stopped.")
        elif cmd == "log":
            if not core.is_owner(uid):
                await out(content="⛔ Only owners can see Claude Code logs.")
                return
            job = core._last_job.get(cid)
            if not job:
                await out(content="No Claude Code reply in this chat since the bot started.")
                return
            await chan.send(file=core.transcript_file(job), reply_to=me)
        elif cmd == "unload":
            if core.models_busy():
                await out(content="🔴 A model is generating right now; try again when it's idle.")
                return
            done = await core.unload_all()
            await out(content=f"💤 Unloaded: {', '.join(f'`{m}`' for m in done)}" if done else "💤 Nothing to unload.")
        elif cmd == "power":
            if not core.is_owner(uid):
                await out(content="⛔ Only owners can control the PC.")
                return
            if not core.power_supported():
                await out(content="Power controls only work when the bot runs on Windows.")
                return
            await chan.send(embed=core.power_embed(), view=core.PowerView(cid))
        elif cmd == "dashboard":
            if not core.is_owner(uid):
                await out(content="⛔ Only owners can open the dashboard.")
                return
            if not private:  # the link holds the access key
                await out(content="Send /dashboard to me in a private chat: the link holds the access key.")
                return
            from llmbot import dashboard

            await out(content=dashboard.link_text(angle=False))
        elif cmd == "cancel":
            await out(content="Nothing to cancel.")
        else:
            await out(content=f"Unknown command /{cmd}. See /help.")

    async def _new_topic(self, name: str, chan: TgChannel, out, private: bool) -> None:
        name = " ".join(name.split())[:128]
        if not name:
            await out(content="Usage: /topic <name>. Each topic is its own conversation: Claude Code session, engine, "
                              "permissions and memory, starting from this chat's settings.")
            return
        chat = self.place(chan.id)[0]
        try:
            made = await self.api("createForumTopic", chat_id=chat, name=name)
        except TgError as e:
            how = ("turn on threaded mode for this bot in @BotFather (Bot Settings), then try again" if private
                   else "turn on Topics in the group settings and make me an admin who can manage topics")
            await out(content=f"⚠️ Couldn't make a topic ({self.core.oneline(e, 120)}). To use topics: {how}.")
            return
        cid = self.cid(chat, made["message_thread_id"], name)
        await TgChannel(self, cid).send(f"🧵 **{name}**: a separate conversation. Its own Claude Code session and "
                                        "settings (copied from the main chat); /panel here changes only this topic.")

    def help_text(self, uid: int) -> str:
        core = self.core
        idle = f"{core.CC_SESSION_IDLE_MINUTES:g} min idle or " if core.CC_SESSION_IDLE_MINUTES > 0 else ""
        lines = [
            "**Talking to me**",
            "Just send a message (in groups: @mention me or reply to me). Same backend as the Discord bot; engine, "
            f"models and the Claude Code session are per chat, set in /panel. Claude Code keeps the conversation "
            f"going until {idle}/new.",
            "",
            "**Commands**",
            "/panel: engine, models, new session, tasks, settings (reply style, permissions, workspace)",
            "/claude <task>: a task with a live progress card and a Stop button · /local <msg>: the local model directly",
            "/compact: shrink a long conversation · /new: start a new Claude Code session",
            "/stop: stop the running reply · /log: full log of the last reply",
            "/remind <when> | <what>, or just ask (\"remind me to take my meds in 10 min\")",
            "Scheduled prompts: just ask (\"at 8am tell me the latest news on …\"); /schedule <cron> | <prompt>; /tasks lists and cancels",
            "/reset: clear the local model's memory · /unload: free GPU memory now",
            "/usage: context window, Claude plan limits (5-hour, weekly) and spend, with a button to check now",
            "/skills: what Claude Code learned from earlier tasks and reuses on similar ones · /skills show|forget <name>",
            "/topic <name>: a new topic = a separate conversation with its own session and settings",
            "/power (owners): lock, sleep, hibernate, restart or shut down the PC; I post here when I'm back",
            "/dashboard (owners): web page with what I'm doing now and what I've done, for your phone on the same Wi-Fi",
            "📎 Photos and files: attach them to your message (Claude Code sees images and PDFs).",
            *(["🎙️ Voice notes: transcribed on the PC and answered like a typed message."] if core.VOICE_ENABLED else []),
            "",
            "**The small italic line** under a reply: model · cost · today's spend · context used/limit · session. "
            "\"memory 3/6\" on the plain local engine = how many recent exchanges it still remembers.",
        ]
        if not core.is_owner(uid):
            lines.append("\n-# Claude Code, /claude, /stop and /log are owner-only; you get the local model.")
        return "\n".join(lines)

    # ---- button taps
    async def _on_callback(self, q: dict) -> None:
        core = self.core
        user, data, qmsg = q["from"], q.get("data") or "", q.get("message") or {}
        async def toast(text: str, alert: bool = False):
            try:
                await self.api("answerCallbackQuery", callback_query_id=q["id"], text=text[:200], show_alert=alert or None)
            except TgError:
                pass
        if not core.is_allowed(user["id"]):
            await toast("⛔ You're not allowed to use this bot.", True)
            return
        self._seen(user, qmsg.get("chat") or {"id": 0})
        tok, _, rest = data.partition(":")
        entry = self._views.get(tok)
        if entry is None or entry.expired():
            await toast("These buttons have expired; send the command again.")
            return
        view, msg = entry.view, entry.msg
        if (qmsg.get("chat") or {}).get("id") != msg.chat_id:  # buttons only act in the chat they were posted in
            await toast("These buttons have expired; send the command again.")
            return
        if rest == "b":  # back from a select's option list
            await self._show_menu(entry, tok, None)
            await toast("")
            return
        idx, _, opt = rest.partition(":")
        try:
            item = view.children[int(idx)]
        except (ValueError, IndexError):
            await toast("That button no longer exists.")
            return
        if getattr(item, "disabled", False) or getattr(item, "url", None):  # never shown as tappable
            await toast("That button isn't available.")
            return
        if opt == "m":
            await self._show_menu(entry, tok, int(idx))
            await toast("")
            return
        payload: dict = {"custom_id": item.custom_id, "component_type": 3 if opt else 2}
        if opt:
            payload["values"] = [item.options[int(opt)].value]
        fi = FakeInteraction(self, msg, user, payload, q["id"])
        # Same checks as on Discord: allow-list + owner-only controls (and "only the person it's for" on reminders)
        if await view.interaction_check(fi):
            try:
                await item.callback(fi)
            except Exception as e:
                await view.on_error(fi, e, item)
        await fi.answer()

    async def _show_menu(self, entry: _ViewEntry, tok: str, idx: int | None) -> None:
        """Swap the keyboard for one select's options (idx), or back to the full keyboard (None)."""
        msg = entry.msg
        if idx is None:
            markup = self._keyboard(entry.view, tok)
            extra = ""
        else:
            sel = entry.view.children[idx]
            rows = [[{"text": f"{'✓ ' if o.default else ''}{o.emoji or ''} {o.label}".strip(),
                      "callback_data": f"{tok}:{idx}:{j}"}] for j, o in enumerate(sel.options)]
            rows.append([{"text": "⬅️ Back", "callback_data": f"{tok}:b"}])
            markup = {"inline_keyboard": rows}
            desc = [f"**{o.label}**: {o.description}" for o in sel.options if o.description]
            extra = ("\n\n" + f"__{sel.placeholder or 'Options'}__\n" + "\n".join(desc)) if desc else ""
        body = self._body((msg.content or "") + extra if not msg.embed else msg.content, msg.embed, msg.cid, True)
        if extra and msg.embed:
            body += "\n\n" + self._render(extra.strip(), msg.cid)
        try:
            await self.api("editMessageText", chat_id=msg.chat_id, message_id=msg.message_id, text=body[:TEXT_LIMIT],
                           parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})
        except TgError as e:
            if "not modified" in str(e):
                return
            try:
                await self.api("editMessageReplyMarkup", chat_id=msg.chat_id, message_id=msg.message_id, reply_markup=markup)
            except TgError:
                pass
