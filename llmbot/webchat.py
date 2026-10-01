"""Chat with the bot from the web dashboard: a third front end next to Discord and Telegram, on the same backend.

Works over the local network only, so also without internet (the local model through Ollama, Claude Code on the
Ollama backend, reminders, scheduled prompts). Like llmbot/telegram.py it only translates:
- The core talks to "channels" with Discord-shaped calls (send(content, embed=, view=, files=), message.edit(),
  channel.typing()). WebChannel/WebMessage keep those as JSON messages the page polls (llmbot/dashboard.html). The
  text stays Discord markdown; the page renders it (as text nodes, never as HTML).
- The core's buttons and menus (discord.ui views: /panel, confirm, Stop, Retry, reminders' Done/Snooze, …) become
  buttons and drop-downs on the page. A click runs the same callback with a small stand-in for discord.Interaction,
  after the same checks (view.interaction_check). Text boxes (modals) open as a text dialog.
- There's one web user, core.WEB_USER_ID: whoever has the dashboard key. DASHBOARD_CHAT=owner (default) makes it an
  owner (Claude Code, /power), =user gives it the local model only, =off turns the chat off.

Chats (ids core.WEB_ID_BASE - n) and their last MESSAGES_KEPT messages are saved in the store ("webchat"); files the
bot sends go to data/webchat_files. Buttons only work until the bot restarts, as on Discord.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any

import discord

log = logging.getLogger("llmbot.webchat")
MISSING: Any = object()
MESSAGES_KEPT = 300  # per chat
CHATS_MAX = 50
FILE_MAX = 25 * 1024 * 1024
FILES_KEEP_DAYS = 30
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

core: Any = None
_fe: "WebFrontend | None" = None


# =============================================================================
# Channel / message stand-ins the core can talk to
# =============================================================================


class _Typing:
    def __init__(self, chat: "Chat"):
        self.chat = chat

    async def __aenter__(self):
        self.chat.typing += 1
        self.chat.touch()

    async def __aexit__(self, *exc):
        self.chat.typing = max(0, self.chat.typing - 1)
        self.chat.touch()


class WebChannel:
    guild = None

    def __init__(self, chat: "Chat"):
        self.chat, self.id = chat, chat.id

    def typing(self) -> _Typing:
        return _Typing(self.chat)

    async def send(self, content: str | None = None, *, embed=None, embeds=None, view=None, file=None, files=None,
                   reference=None, reply_to=None, ephemeral: bool = False, **_ignored) -> "WebMessage":
        embed = embed or (embeds[0] if embeds else None)
        files = list(files or []) + ([file] if file else [])
        ref = reply_to or reference
        return self.chat.post("bot", content, embed=embed, view=view, files=files,
                              reply_to=getattr(ref, "id", None), ephemeral=ephemeral)


class WebMessage:
    def __init__(self, chat: "Chat", mid: int, content: str | None, embed, view):
        self.chat, self.id, self.message_id = chat, mid, mid
        self.content, self.embed, self.view = content or "", embed, view
        self.channel = WebChannel(chat)

    async def edit(self, content=MISSING, *, embed=MISSING, view=MISSING, **_ignored) -> "WebMessage":
        if content is not MISSING:
            self.content = content or ""
        if embed is not MISSING:
            self.embed = embed
        if view is not MISSING:
            self.view = view
        self.chat.update(self)
        return self

    async def reply(self, content: str | None = None, **kw) -> "WebMessage":
        kw.pop("mention_author", None)
        return await self.channel.send(content, reply_to=self, **kw)

    def to_reference(self, **_):
        return self

    async def delete(self):
        self.chat.remove(self.id)


class WebAttachment:
    """An uploaded file, shaped like a discord.Attachment for core.save_uploads."""

    def __init__(self, filename: str, data: bytes, content_type: str):
        self.id = secrets.randbelow(10 ** 12)
        self.filename, self.size, self.content_type, self._data = filename, len(data), content_type or "", data

    def is_voice_message(self) -> bool:
        return False

    async def read(self) -> bytes:
        return self._data


class _User:
    id = 0
    name = display_name = mention = "You"

    def __init__(self):
        self.id = core.WEB_USER_ID


class _Response:
    def __init__(self, fi: "WebInteraction"):
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
        self.fi.modal = modal


class _Followup:
    def __init__(self, fi: "WebInteraction"):
        self.fi = fi

    async def send(self, content: str | None = None, *, ephemeral: bool = False, wait: bool = False, **kw):
        return await self.fi.post(content, ephemeral=ephemeral, **kw)


class WebInteraction:
    """Enough of discord.Interaction for the core's view callbacks."""

    guild = None
    app_permissions = None
    type = discord.InteractionType.component

    def __init__(self, message: WebMessage, data: dict):
        self.message, self.data = message, data
        self.user = _User()
        self.channel = message.channel
        self.channel_id = message.chat.id
        self.response = _Response(self)
        self.followup = _Followup(self)
        self.modal = None  # set by send_modal: the page opens a text dialog
        self.toast: str | None = None

    async def post(self, content: str | None = None, *, ephemeral: bool = False, embed=None, view=None, file=None,
                   files=None, **_ignored):
        # Short private notes ("⛔ Only owners…", "⏹️ Stopping…") become a pop-up on the page instead of a message.
        if ephemeral and content and not (embed or view or file or files) and len(content) <= 300:
            self.toast = render_text(content)
            return None
        return await self.channel.send(content, embed=embed, view=view, file=file, files=files, ephemeral=ephemeral)


# =============================================================================
# Chats
# =============================================================================


def render_text(text: str | None) -> str:
    """The core's text for the page: secrets redacted, mentions as names. Markdown stays for the page to render."""
    s = core.redact(text or "").replace(core.STATS_MARK, " ")
    s = re.sub(r"<@!?(-?\d+)>", lambda m: "@" + core.user_label(int(m.group(1))), s)
    s = re.sub(r"<#(-?\d+)>", lambda m: core.chat_label(int(m.group(1))), s)
    return re.sub(r"<@&\d+>", "@role", s)


def _embed(e) -> dict | None:
    if e is None:
        return None
    return {"title": render_text(e.title), "description": render_text(e.description),
            "fields": [{"name": render_text(f.name), "value": render_text(f.value), "inline": bool(f.inline)}
                       for f in e.fields],
            "footer": render_text(e.footer.text) if e.footer and e.footer.text else None,
            "color": e.color.value if e.color else None}


def _controls(view) -> list[dict]:
    out = []
    for i, item in enumerate(getattr(view, "children", [])):
        if isinstance(item, discord.ui.Select):
            out.append({"i": i, "type": "select", "placeholder": item.placeholder or "Choose…", "row": item.row,
                        "disabled": item.disabled,
                        "options": [{"label": o.label, "description": o.description, "default": o.default,
                                     "emoji": str(o.emoji) if o.emoji else ""} for o in item.options]})
        elif isinstance(item, discord.ui.Button):
            out.append({"i": i, "type": "button", "label": item.label or "", "row": item.row,
                        "emoji": str(item.emoji) if item.emoji else "", "style": item.style.name,
                        "disabled": item.disabled, "url": item.url})
    return out


class Chat:
    def __init__(self, fe: "WebFrontend", cid: int, title: str, created: float, messages: list[dict] | None = None):
        self.fe, self.id, self.title, self.created = fe, cid, title, created
        self.messages: list[dict] = messages or []
        self.v = max((m.get("v", 0) for m in self.messages), default=0)
        self.typing = 0
        self.live: dict[int, WebMessage] = {}  # messages whose buttons still work (this run of the bot)
        self.removed: list[tuple[int, int]] = []  # (v, message id)
        self.updated = max((m["ts"] for m in self.messages), default=created)

    def touch(self) -> int:
        self.fe.v += 1
        self.v = self.fe.v
        return self.v

    def _find(self, mid: int) -> dict | None:
        return next((m for m in reversed(self.messages) if m["id"] == mid), None)

    def _serialize(self, msg: WebMessage, role: str, **extra) -> dict:
        return {"id": msg.id, "role": role, "text": render_text(msg.content), "embed": _embed(msg.embed),
                "controls": _controls(msg.view) if msg.view is not None else [], **extra}

    def post(self, role: str, content: str | None, *, embed=None, view=None, files=(), reply_to: int | None = None,
             ephemeral: bool = False) -> WebMessage:
        msg = WebMessage(self, self.fe.next_message_id(), content, embed, view)
        row = self._serialize(msg, role, ts=time.time(), reply_to=reply_to, ephemeral=ephemeral or None,
                              files=[self.fe.save_file(msg.id, f) for f in files])
        row["files"] = [f for f in row["files"] if f]
        row["v"] = self.touch()
        self.messages.append(row)
        if view is not None:
            self.live[msg.id] = msg
        self.updated = row["ts"]
        del self.messages[:-MESSAGES_KEPT]
        self.fe.save()
        return msg

    def update(self, msg: WebMessage) -> None:
        row = self._find(msg.id)
        if row is None:
            return
        row.update(self._serialize(msg, row["role"]))
        row["v"] = self.touch()
        if msg.view is not None:
            self.live[msg.id] = msg
        else:
            self.live.pop(msg.id, None)
        self.fe.save()

    def remove(self, mid: int) -> None:
        self.messages = [m for m in self.messages if m["id"] != mid]
        self.live.pop(mid, None)
        self.removed.append((self.touch(), mid))
        del self.removed[:-200]
        self.fe.save()

    def since(self, v: int) -> dict:
        """Messages added or changed after version v (0 = all), and ids removed since."""
        rows = [dict(m, live=m["id"] in self.live) for m in self.messages if m.get("v", 0) > v]
        return {"v": self.v, "messages": rows, "removed": [mid for rv, mid in self.removed if rv > v]}

    def summary(self) -> dict:
        s = core.get_settings(self.id)
        last = self.messages[-1] if self.messages else None
        return {"id": str(self.id), "title": self.title, "updated": self.updated, "count": len(self.messages),
                "engine": s["engine"], "local_model": s["local_model"], "cc_model": s["cc_model"],
                "cc_backend": s["cc_backend"], "last": (last["text"] or (last.get("embed") or {}).get("title") or "")[:120]
                if last else ""}


# =============================================================================
# The front end
# =============================================================================


class WebFrontend:
    def __init__(self, core_module):
        global core
        core = core_module
        self.path = core.DATA_DIR / "webchat.json"
        self.files = core.DATA_DIR / "webchat_files"
        self.chats: dict[int, Chat] = {}
        self.v = 0
        self._next_mid = int(time.time() * 1000)
        self._modals: dict[str, tuple[Any, WebMessage, float]] = {}
        self._save_handle: asyncio.TimerHandle | None = None
        data =core.STORE.load("webchat", self.path, {}) or {}
        for c in data.get("chats", []):
            chat = Chat(self, int(c["id"]), c.get("title") or "Chat", c.get("created") or time.time(), c.get("messages"))
            self.chats[chat.id] = chat
            self.v = max(self.v, chat.v)
        self._prune_files()

    # ---- front-end interface used by the core (see core._frontends)
    def owns(self, channel_id: int) -> bool:
        return core.is_web_id(channel_id)

    async def get_channel(self, channel_id: int) -> WebChannel:
        chat = self.chats.get(channel_id)
        if chat is None:  # e.g. a reminder for a chat that was deleted since: bring it back rather than lose it
            chat = self.new_chat("Reminders", channel_id)
        return WebChannel(chat)

    def where(self, channel_id: int) -> str:
        chat = self.chats.get(channel_id)
        return f"Web: {chat.title}" if chat else "Web chat"

    # ---- storage
    def next_message_id(self) -> int:
        self._next_mid += 1
        return self._next_mid

    def save(self) -> None:
        """Saved at most every 2 s (progress cards edit their message every few seconds)."""
        if self._save_handle is not None:
            return
        try:
            self._save_handle = asyncio.get_running_loop().call_later(2, self.flush)
        except RuntimeError:  # no event loop (tests, shutdown): save now
            self.flush()

    def flush(self) -> None:
        self._save_handle = None
        core.STORE.save("webchat", self.path, {"chats": [
            {"id": c.id, "title": c.title, "created": c.created, "messages": c.messages} for c in self.chats.values()]})

    def save_file(self, mid: int, f: discord.File) -> dict | None:
        try:
            f.reset()
            data = f.fp.read()
        except Exception:
            return None
        name = re.sub(r"[^\w.-]", "_", f.filename or "file")[-80:].lstrip(".") or "file"
        if len(data) > FILE_MAX:
            return {"name": name, "size": len(data), "url": None, "note": "too large to keep"}
        self.files.mkdir(parents=True, exist_ok=True)
        stored = f"{mid}-{secrets.token_hex(4)}-{name}"
        (self.files / stored).write_bytes(data)
        return {"name": name, "size": len(data), "url": f"/api/chat-file/{stored}",
                "image": name.lower().endswith(IMAGE_EXT)}

    def file_path(self, stored: str) -> Path | None:
        if not re.fullmatch(r"\d+-[0-9a-f]{8}-[\w.-]+", stored):
            return None
        p = self.files / stored
        return p if p.is_file() else None

    def _prune_files(self) -> None:
        cutoff = time.time() - FILES_KEEP_DAYS * 86400
        for p in self.files.glob("*") if self.files.exists() else []:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    # ---- chats
    def new_chat(self, title: str = "", cid: int | None = None) -> Chat:
        if cid is None:
            cid = min(self.chats, default=-core.WEB_ID_BASE) - 1
        if not core.is_web_id(cid) or cid == core.WEB_USER_ID:
            raise ValueError("not a web chat id")
        while len(self.chats) >= CHATS_MAX:  # drop the least recently used
            self.delete_chat(min(self.chats.values(), key=lambda c: c.updated).id)
        chat = Chat(self, cid, (title or "").strip()[:60] or time.strftime("Chat %d %b %H:%M"), time.time())
        self.chats[cid] = chat
        core.update_settings(cid, style="chat")  # plain replies with a stats line; the dashboard shows progress
        chat.touch()
        self.save()
        return chat

    def delete_chat(self, cid: int) -> None:
        chat = self.chats.pop(cid, None)
        if chat is None:
            return
        for row in chat.messages:
            for f in row.get("files") or []:
                p = self.file_path((f.get("url") or "").rsplit("/", 1)[-1])
                if p:
                    p.unlink(missing_ok=True)
        self.save()

    # ---- the user's side
    async def send(self, chat: Chat, text: str, attachments: list[WebAttachment]) -> None:
        text = (text or "").strip()
        names = ", ".join(a.filename for a in attachments)
        me = chat.post("user", text + (f"\n-# 📎 {names}" if names else ""))
        if not text and not attachments:
            return
        if chat.title.startswith("Chat ") and text and not text.startswith("/") and not any(
                m["role"] == "user" and not m["text"].startswith("/") for m in chat.messages[:-1]):  # after the first message
            chat.title = core.oneline(text, 40)
        out = core.Out(WebChannel(chat), reply_to=me)
        uid = core.WEB_USER_ID
        if not core.is_allowed(uid):
            await out(content="⛔ The dashboard chat is off (DASHBOARD_CHAT).")
            return
        cmd, _, args = text.partition(" ") if text.startswith("/") else ("", "", "")
        try:
            if cmd.lower() == "/skill":  # files sent with it go to the skill
                name, _, request = args.strip().partition(" ")
                if not name:
                    await core.refresh_cc_skills()
                    await out(content=core.cc_skills_text() + "\n\nUsage: /skill <name> [what you want], e.g. "
                                      "`/skill pdf summarise this` with the file attached")
                    return
                async with WebChannel(chat).typing():
                    await core.run_skill(WebChannel(chat), uid, name, request.strip(), out, attachments=attachments)
                return
            if cmd and await self._command(cmd[1:].lower(), args.strip(), chat, out):
                return
            async with WebChannel(chat).typing():
                await core.handle_prompt(WebChannel(chat), uid, text, out, attachments)
        except Exception as e:
            log.exception("web chat message failed")
            await out(content=core.with_hint(f"⚠️ Something went wrong: {type(e).__name__}: "
                                             f"{core.oneline(core.redact(e), 300)}", e))

    async def _command(self, cmd: str, args: str, chat: Chat, out) -> bool:
        """Slash commands typed in the web chat. False = not ours (e.g. /compact goes to handle_prompt)."""
        ch, uid, cid = WebChannel(chat), core.WEB_USER_ID, chat.id
        owner = core.CC_ENABLED and core.is_owner(uid)
        if cmd in ("help", "start"):
            await out(content=HELP)
        elif cmd == "panel":
            await ch.send(embed=core.panel_embed(cid), view=await core.PanelView.build(cid))
        elif cmd == "tasks":
            await ch.send(embed=core.tasks_embed(uid), view=core.TasksView(uid))
        elif cmd == "usage":
            await ch.send(embed=core.usage_embed(cid), view=core.UsageView(cid))
        elif cmd == "skills":
            if not args.strip():
                await core.refresh_cc_skills()
            await out(content=core.skills_reply(uid, args))
        elif cmd == "new":
            if not owner:
                await out(content="⛔ Claude Code isn't available here (DASHBOARD_CHAT, or no OWNER_IDS).")
            else:
                core.update_settings(cid, cc_session=None)
                await out(content="🆕 Your next message starts a new Claude Code session.")
        elif cmd == "reset":
            core.forget_history(cid)
            await out(content="🧹 Local chat history cleared.")
        elif cmd == "stop":
            job = core._last_job.get(cid)
            if not job or job.status == "done":
                await out(content="Nothing is running here.")
            elif not core.is_owner(uid):
                await out(content="⛔ Only owners can stop Claude Code.")
            else:
                job.stop_requested = True
                if job.proc:
                    await core.kill_tree(job.proc)
                await out(content="⏹️ Stopped.")
        elif cmd == "claude":
            if not owner:
                await out(content="⛔ Claude Code isn't available here (DASHBOARD_CHAT, or no OWNER_IDS).")
            elif not args:
                await out(content="Usage: /claude <task>")
            else:
                await core.request_cc(ch, uid, args, core.snapshot(cid), out)
        elif cmd == "local":
            if not args:
                await out(content="Usage: /local <message>")
            else:
                async with ch.typing():
                    await core.answer_local(ch, uid, args, out)
        elif cmd == "remind":
            when, sep, what = args.partition("|")
            if not sep or not what.strip():
                await out(content="Usage: `/remind in 10 min | take my meds` (or just say \"remind me to … in 10 min\")")
            else:
                try:
                    await out(content=core.reminder_line(core.add_reminder(when.strip(), what.strip(), cid, uid)))
                except ValueError as e:
                    await out(content=f"⚠️ {e}")
        elif cmd == "unload":
            if core.models_busy():
                await out(content="🔴 A model is generating right now; try again when it's idle.")
            else:
                done = await core.unload_all()
                await out(content=f"💤 Unloaded: {', '.join(f'`{m}`' for m in done)}" if done else "💤 Nothing to unload.")
        elif cmd == "power":
            if not core.is_owner(uid):
                await out(content="⛔ Only owners can control the PC.")
            elif not core.power_supported():
                await out(content="Power controls only work when the bot runs on Windows.")
            else:
                await ch.send(embed=core.power_embed(), view=core.PowerView(cid))
        else:
            return False
        return True

    async def press(self, chat: Chat, mid: int, idx: int, opt: int | None) -> dict:
        """A click on one of a message's buttons (or an option of its drop-down)."""
        msg = chat.live.get(mid)
        view = msg.view if msg else None
        if view is None or view.is_finished():
            return {"toast": "These buttons have expired; send the command again."}
        try:
            item = view.children[idx]
        except IndexError:
            return {"toast": "That button no longer exists."}
        if getattr(item, "disabled", False) or getattr(item, "url", None):
            return {"toast": "That button isn't available."}
        payload: dict = {"custom_id": item.custom_id, "component_type": 3 if opt is not None else 2}
        if opt is not None:
            try:
                payload["values"] = [item.options[opt].value]
            except (AttributeError, IndexError):
                return {"toast": "That option no longer exists."}
        fi = WebInteraction(msg, payload)
        if await view.interaction_check(fi):  # same checks as on Discord (allow-list, owner-only controls)
            try:
                await item.callback(fi)
            except Exception as e:
                await view.on_error(fi, e, item)
        return self._result(fi)

    def _result(self, fi: WebInteraction) -> dict:
        res: dict = {"toast": fi.toast}
        modal = fi.modal
        if modal is not None:
            box = next((c for c in modal.children if isinstance(c, discord.ui.TextInput)), None)
            if box is not None:
                tok = secrets.token_urlsafe(8)
                self._modals[tok] = (modal, fi.message, time.monotonic() + 900)
                res["modal"] = {"token": tok, "title": modal.title, "label": box.label, "default": box.default or "",
                                "long": box.style == discord.TextStyle.paragraph, "max": box.max_length}
        return res

    async def submit_modal(self, tok: str, text: str) -> dict:
        modal, origin, deadline = self._modals.pop(tok, (None, None, 0))
        if modal is None or time.monotonic() > deadline:
            return {"toast": "That form expired; open it again."}
        box = next(c for c in modal.children if isinstance(c, discord.ui.TextInput))
        box._value = text  # what discord.py sets from a submitted modal
        fi = WebInteraction(origin, {"custom_id": modal.custom_id, "components": []})
        try:
            await modal.on_submit(fi)
        except Exception as e:
            log.exception("form handler failed")
            await fi.post(core.with_hint(f"⚠️ {type(e).__name__}: {core.oneline(core.redact(e), 200)}"))
        return self._result(fi)


COMMANDS = [  # (name, arguments, what it does): for /help and the page's autocomplete
    ("panel", "", "Engine, models, new session, settings for this chat"),
    ("claude", "<task>", "Run a task with Claude Code"),
    ("local", "<message>", "Ask the local model directly"),
    ("new", "", "Start a new Claude Code session"),
    ("compact", "", "Shrink a long Claude Code conversation"),
    ("stop", "", "Stop the reply that's running"),
    ("tasks", "", "List and cancel reminders and scheduled prompts"),
    ("usage", "", "Context window, Claude plan limits (5-hour, weekly), spend"),
    ("skill", "<name> [request]", "Run one of Claude Code's skills (docx, pdf, xlsx, deep-research, …)"),
    ("skills", "[show|forget <name>]", "Claude Code's skills, and what it learned from earlier tasks"),
    ("remind", "<when> | <what>", "One-time reminder, e.g. in 10 min | take meds"),
    ("reset", "", "Clear the local model's chat memory"),
    ("unload", "", "Free GPU memory now"),
    ("power", "", "Lock, sleep, restart or shut down the PC"),
    ("help", "", "How this chat works"),
]

HELP = ("**Chatting from the dashboard**\n"
        "Same bot as on Discord and Telegram, over your local network, so it also works without internet (the local "
        "model, and Claude Code on the Ollama backend). Each chat here has its own engine, model and Claude Code "
        "session, set in ⚙️ /panel.\n\n**Commands** (type / to pick one)\n"
        + "\n".join(f"/{n}{' ' + a if a else ''}: {d}" for n, a, d in COMMANDS)
        + "\n\n📎 Attach images, PDFs and code for Claude Code. 🎙️ The mic button turns speech into text, "
          "transcribed on the PC.")


# =============================================================================
# Claude Code sessions: every session in the workspaces' folders (the bot's and ones started in a terminal), so
# the dashboard can list them and continue one in a chat, like `claude --resume`.
# =============================================================================

SESSION_IMPORT = 60  # messages shown when a session is opened in a chat
_HEADER = re.compile(r"^\[(Now|Access|Your saved skills)[^\n]*\n?", re.M)


def _projects_dir(path: Path) -> Path:
    """Claude Code keeps a folder's sessions in ~/.claude/projects/<path with every non-alphanumeric as '-'>."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _user_text(m: dict) -> str | None:
    """The typed text of a user line (None for tool results, slash-command noise and the bot's headers)."""
    c = (m.get("message") or {}).get("content")
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or not c.strip() or c.lstrip().startswith(("<command-", "<local-command", "<system-")):
        return None
    c = _HEADER.sub("", c).strip()
    if c.startswith("[Skills you saved"):
        c = _strip_skill_notes(c)
    return c.strip() or None


def _strip_skill_notes(c: str) -> str:
    """The message after the skill notes the bot puts first. Newer prompts mark where it starts; in older ones each
    note ("### name (use when: …)" + body) is matched against the saved skill, and only an edited or deleted one
    falls back to "the message starts after the note's last list line"."""
    if core.MESSAGE_MARK in c:
        return c.split(core.MESSAGE_MARK, 1)[1]
    rest = c.partition("\n")[2]
    bodies = {s["name"]: s.get("body") or "" for s in core.skills_mod.all_skills()}
    while rest.startswith("### "):
        head, _, after = rest.partition("\n")
        body = bodies.get(head[4:].split(" (use when:", 1)[0])
        if not body or not after.startswith(body):
            lines = rest.splitlines()
            last = max((i for i, ln in enumerate(lines) if ln.startswith(("- ", "### "))), default=-1)
            return "\n".join(lines[last + 1:])
        rest = after[len(body):].lstrip("\n")
    return rest


def _lines(path: Path, max_bytes: int | None = None):
    with path.open("rb") as f:
        data = f.read(max_bytes) if max_bytes else f.read()
    for raw in data.splitlines():
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if isinstance(m, dict) and not m.get("isSidechain") and not m.get("isMeta"):
            yield m


def session_info(path: Path, workspace: str) -> dict:
    title = None
    for m in _lines(path, 512 * 1024):
        if m.get("type") == "summary" and m.get("summary"):
            title = m["summary"]
            break
        if m.get("type") == "user" and title is None:
            title = _user_text(m)
    st = path.stat()
    return {"id": path.stem, "workspace": workspace, "title": core.oneline(title or "(no messages)", 80),
            "updated": st.st_mtime, "size": st.st_size}


def list_sessions(limit: int = 100) -> list[dict]:
    """Newest first, across all workspaces; each says which web chat (if any) is on it."""
    files = []
    for name, ws in core.WORKSPACES.items():
        d = _projects_dir(ws)
        files += [(p, name) for p in d.glob("*.jsonl")] if d.is_dir() else []
    files.sort(key=lambda f: -f[0].stat().st_mtime)
    held = {s.get("cc_session"): cid for cid in (_fe.chats if _fe else {})
            for s in [core.get_settings(cid)] if s.get("cc_session")}
    out = []
    for p, name in files[:limit]:
        try:
            info = session_info(p, name)
        except OSError:
            continue
        info["chat"] = str(held[info["id"]]) if info["id"] in held else None
        info["open"] = core.session_open_elsewhere(info["id"])  # e.g. still running in a terminal
        out.append(info)
    return out


def session_messages(path: Path, limit: int = SESSION_IMPORT) -> list[dict]:
    """{role, text, ts} of the conversation's last `limit` messages: what was typed and Claude's replies."""
    out: list[dict] = []
    for m in _lines(path):
        try:
            ts = datetime.fromisoformat(str(m.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
        if m.get("type") == "user":
            t = _user_text(m)
            if t:
                out.append({"role": "user", "text": t, "ts": ts})
        elif m.get("type") == "assistant":
            c = (m.get("message") or {}).get("content") or []
            t = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text").strip()
            if t:
                if out and out[-1]["role"] == "bot":  # one reply per turn, as in the chat
                    out[-1]["text"] += "\n\n" + t
                else:
                    out.append({"role": "bot", "text": t, "ts": ts})
    return [dict(r, text=core.clip(r["text"], 4000)) for r in out[-limit:]]


def session_path(sid: str, workspace: str) -> Path:
    ws = core.WORKSPACES.get(workspace)
    if ws is None or not re.fullmatch(r"[0-9a-f-]{36}", sid):
        raise ValueError("no such session")
    path = _projects_dir(ws) / f"{sid}.jsonl"
    if not path.is_file():
        raise ValueError("no such session")
    return path


def view_session(sid: str, workspace: str, limit: int = 300) -> dict:
    """Read-only look at a session: nothing is created or resumed."""
    path = session_path(sid, workspace)
    info = session_info(path, workspace)
    info["open"] = core.session_open_elsewhere(sid)
    info["chat"] = next((str(cid) for cid in _fe.chats if core.get_settings(cid).get("cc_session") == sid), None)
    return {"session": info, "messages": session_messages(path, limit)}


def open_session(sid: str, workspace: str) -> Chat:
    """A web chat on that Claude Code session (the one already on it, or a new one with the history copied in);
    the next message there resumes it."""
    path = session_path(sid, workspace)
    ws = core.WORKSPACES[workspace]
    for cid in _fe.chats:
        if core.get_settings(cid).get("cc_session") == sid:
            return _fe.chats[cid]
    chat = _fe.new_chat(session_info(path, workspace)["title"][:60])
    for row in session_messages(path):
        chat.post(row["role"], row["text"])
    where = core.session_open_elsewhere(sid)
    chat.post("bot", f"-# ↩️ Continuing Claude Code session `{sid[:8]}` in workspace **{workspace}**. "
                     + (f"It's still open elsewhere ({where}), so your next message continues a copy of it and "
                        "leaves the original alone." if where else "Your next message resumes it."))
    core.update_settings(chat.id, engine="claude", workspace=workspace, cc_session=sid, cc_session_path=str(ws),
                         cc_session_at=time.time(), cc_session_setup=core.CC_SETUP_FINGERPRINT,
                         cc_session_ctx=None, cc_session_pinned=True)  # picked on purpose: no idle cut-off
    return chat


def start(core_module) -> WebFrontend:
    global _fe
    _fe = WebFrontend(core_module)
    core_module._frontends.insert(0, _fe)
    log.info("Dashboard chat on (%s): %d chat(s)", core_module.DASHBOARD_CHAT, len(_fe.chats))
    return _fe


def frontend() -> WebFrontend | None:
    return _fe
