"""What to try when something fails: fixed rules, no model involved (a failing model can't be asked for help, and
advice has to be right). Each rule is a regex over the error text (exception, CLI output, stderr) plus where it
happened, and a suggestion with the user's actual settings filled in.

    suggest("httpx.ConnectError: All connection attempts failed", where="local", llm_url="http://localhost:11434/v1")
    -> "The local model server isn't answering. Start Ollama (open the app, or run `ollama serve`) ..."
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    pattern: str
    hint: str
    where: tuple[str, ...] = ()  # empty = anywhere; else only for these places (local, claude, voice, power, chat)

    def matches(self, text: str, where: str) -> bool:
        return (not self.where or where in self.where) and re.search(self.pattern, text, re.I | re.S) is not None


# First match wins per topic; order matters (specific before general).
RULES: list[Rule] = [
    # ---- local model server (Ollama / LM Studio / llama.cpp)
    Rule(r"model ['\"]?[\w.:/-]+['\"]? not found|model .{0,40}not found, try pulling|pull model manifest",
         "The model `{model}` isn't installed on the server. Run `ollama pull {model}` on the PC, or pick an "
         "installed model in /panel.", ("local", "claude")),
    Rule(r"requires more system memory|out of memory|cudaMalloc|CUDA error|failed to allocate|insufficient memory",
         "Not enough memory for this model. Send /unload, close GPU-heavy apps, or pick a smaller model in /panel. "
         "A smaller OLLAMA_CONTEXT_LENGTH also saves memory.", ("local", "claude", "voice")),
    Rule(r"llama runner process has terminated|runner process (has )?(terminated|exited)",
         "The model crashed while loading (usually memory or a GPU driver hiccup). Restart Ollama from the tray, "
         "then try again; if it repeats, use a smaller model.", ("local", "claude")),
    Rule(r"ConnectError|ConnectTimeout|connection refused|All connection attempts failed|actively refused|"
         r"Could not reach the local model|Cannot connect to host",
         "The local model server isn't answering at {llm_url}. Start Ollama (open the app, or run `ollama serve`) "
         "and send the message again. If it runs elsewhere, check LLM_URL in .env.", ("local",)),
    Rule(r"ReadTimeout|read timed out|\btimed out\b",
         "The model took too long, often because it was still loading into memory. Try again in a minute, or use a "
         "smaller / already-loaded model (/panel shows which one is loaded).", ("local",)),
    Rule(r"HTTP 40[13]|unauthori[sz]ed|invalid api key|forbidden",
         "The model server refused the request: check LLM_API_KEY in .env.", ("local",)),
    Rule(r"context (length|window)|maximum context|too many tokens|exceeds? (the )?(context|max)",
         "The conversation is too long for the model. Send /reset to clear the local chat memory, or raise "
         "OLLAMA_CONTEXT_LENGTH.", ("local",)),

    # ---- Claude Code
    Rule(r"No such file|WinError 2\b|FileNotFoundError|not recognized as an internal|claude.{0,20}not found|"
         r"Claude Code CLI not found",
         "The Claude Code CLI wasn't found. Install it on the PC (`irm https://claude.ai/install.ps1 | iex`) or "
         "set CLAUDE_BIN in .env to the full path of claude.exe, then restart the bot.", ("claude",)),
    Rule(r"not logged in|please run /login|/login|invalid api key|authentication_error|oauth token (has )?expired|"
         r"invalid x-api-key|(?:HTTP|API Error:?|status(?: code)?:?|error code:?)\s*401\b",
         "Claude Code isn't logged in on the PC. Open a terminal there, run `claude`, log in (/login), quit, then "
         "retry.", ("claude",)),
    Rule(r"credit balance is too low|insufficient credit|billing|payment required|(?:HTTP|API Error:?|status(?: code)?:?|error code:?)\s*402\b",
         "The Anthropic account is out of credit. Top it up at console.anthropic.com, or switch the backend to "
         "🦙 Ollama in /panel → ⚙️ Settings to use your own model for free.", ("claude",)),
    Rule(r"usage limit|limit (will )?reset|reached your .{0,20}limit|5-hour limit|weekly limit",
         "Your Claude plan's usage limit is reached; it resets at the time shown above. Until then switch the backend "
         "to 🦙 Ollama in /panel, or use the local engine.", ("claude",)),
    Rule(r"rate[_ ]limit|(?:HTTP|API Error:?|status(?: code)?:?|error code:?)\s*(?:429|529)\b|overloaded|too many requests",
         "Anthropic is busy or rate-limiting. Wait a minute and press 🔁 Retry (or send it again), or switch to "
         "haiku in /panel.", ("claude",)),
    Rule(r"prompt is too long|input length and .{0,20}exceed|context (length|window)|maximum context|"
         r"too many tokens",
         "The conversation no longer fits the model's context. Send /compact to summarise it, or start fresh with "
         "🆕 New session in /panel (/new on Telegram).", ("claude",)),
    Rule(r"Timed out after",
         "It ran into the time limit (CLAUDE_TIMEOUT={claude_timeout}s). Split the task into smaller steps, or "
         "raise CLAUDE_TIMEOUT in .env.", ("claude",)),
    Rule(r"per-job budget|error_max_budget|max[_ ]budget",
         "It hit the per-job budget (CC_MAX_BUDGET_USD). Continue with 💬 Follow up, or raise the cap in .env.",
         ("claude",)),
    Rule(r"ConnectError|connection refused|All connection attempts failed|ECONNREFUSED|Unable to connect",
         "Claude Code couldn't reach its model server. On the 🦙 Ollama backend, make sure Ollama is running; "
         "otherwise check the PC's internet connection.", ("claude",)),
    Rule(r"exited \(code|without a result|\(no output\)",
         "Claude Code stopped without an answer. Press 📄 Full log (or /log) to see why; running `claude -p hi` in a "
         "terminal on the PC shows setup problems directly.", ("claude",)),

    # ---- voice notes
    Rule(r"faster.?whisper|No module named",
         "Voice notes need faster-whisper: run `pip install -r requirements.txt` in the bot's venv and restart.",
         ("voice",)),
    Rule(r"Invalid data|could not (open|decode)|av\.error|InvalidDataError|codec",
         "That audio couldn't be decoded. Send a normal voice note (hold the mic button) instead of an audio file.",
         ("voice",)),

    # ---- /power
    Rule(r"\(1190\)|already been scheduled|already scheduled",
         "A shutdown or restart is already scheduled. Open /power and press ✖️ Cancel first.", ("power",)),
    Rule(r"Access is denied|\(5\)|privilege",
         "Windows refused. The bot must run as the signed-in user (scripts\\bot_control.ps1 start), and some "
         "company-managed PCs block shutdown from apps.", ("power",)),

    # ---- anywhere
    Rule(r"getaddrinfo failed|Name or service not known|nodename nor servname|Temporary failure in name resolution|"
         r"No address associated",
         "The PC can't resolve internet names (DNS). It's probably offline: check its Wi-Fi."),
    Rule(r"Missing Access|Missing Permissions|(?:HTTP|API Error:?|status(?: code)?:?|error code:?)\s*5000[13]\b",
         "The bot lacks permissions in this channel. Edit Channel → Permissions → add the bot with View Channel, "
         "Send Messages, Embed Links, Attach Files and Read Message History."),
    Rule(r"file is too big|Request Entity Too Large|(?:HTTP|API Error:?|status(?: code)?:?|error code:?)\s*413\b",
         "That file is too large (Telegram lets bots download up to 20 MB; Discord uploads are limited by the "
         "server's boost level). Send a smaller file or a link."),
    Rule(r"could not connect to server|OperationalError|psycopg|connection to server at",
         "The bot can't reach its Postgres database. Check that the PostgreSQL service is running and DATABASE_URL "
         "in .env is right."),
]


def suggest(*texts: object, where: str = "", limit: int = 2, **values: object) -> str | None:
    """Suggestions for this failure, or None when no rule knows it (then the error alone is shown)."""
    blob = "\n".join(str(t) for t in texts if t)
    if not blob:
        return None
    out: list[str] = []
    for rule in RULES:
        if rule.matches(blob, where):
            hint = rule.hint.format_map(_Values(values))
            if hint not in out:
                out.append(hint)
            if len(out) >= limit:
                break
    return " ".join(out) if out else None


def with_hint(message: str, *texts: object, where: str = "", **values: object) -> str:
    """The error message plus a "💡 Try:" line when a rule matches (the message itself is always part of the match)."""
    hint = suggest(message, *texts, where=where, **values)
    return f"{message}\n💡 {hint}" if hint else message


class _Values(dict):
    def __missing__(self, key: str) -> str:  # a hint mentioning a value the caller didn't pass
        return {"model": "that model", "llm_url": "LLM_URL", "claude_timeout": "CLAUDE_TIMEOUT"}.get(key, key)
