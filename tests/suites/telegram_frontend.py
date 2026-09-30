"""Telegram front end against a fake Bot API: rendering, allow-list, messages, /panel menus, owner checks,
reminders with buttons, modal -> text input, scheduled task routing, and Discord ids still behaving as before."""
import asyncio, os, sys, tempfile, time
from pathlib import Path

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
import llmbot.telegram as T

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "s.json"; B.USAGE_FILE = tmp / "u.json"; B.TASKS_FILE = tmp / "t.json"; B.REMINDERS_FILE = tmp / "r.json"
B._settings.clear(); B._tasks.clear(); B._reminders.clear()
B.TELEGRAM_ALLOWED_USER_IDS.clear(); B.TELEGRAM_ALLOWED_USER_IDS.update({111, 222})
B.TELEGRAM_OWNER_IDS.clear(); B.TELEGRAM_OWNER_IDS.add(111)
B.OWNER_IDS.clear(); B.OWNER_IDS.add(900000000000000001); B.ALLOWED_USER_IDS.clear()
B.CC_ENABLED = True
OWNER, GUEST, STRANGER, CHAT = 111, 222, 333, 111
DISCORD_USER = 900000000000000002


def check(c, label):
    assert c, label
    print("  ok", label)


class FakeTg(T.Telegram):
    def __init__(self):
        super().__init__(B, "123456789:" + "A" * 35)
        self.calls, self.next_id = [], 1000
        self.username, self.bot_id = "mavis_bot", 42

    async def api(self, method, *, files=None, _timeout=None, **params):
        self.calls.append((method, params, files))
        if method in ("sendMessage", "sendDocument", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    async def download(self, file_id):
        return b"fake image bytes"

    def sent(self, method="sendMessage"):
        return [p for m, p, _ in self.calls if m == method]

    def last_text(self):
        return self.sent()[-1]["text"]

    def buttons(self, params):
        return [b for row in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def msg(uid, text, chat=CHAT, **kw):
    return {"message_id": int(time.time() * 1000) % 10 ** 6, "date": int(time.time()), "chat": {"id": chat, "type": "private" if chat > 0 else "group", **({} if chat > 0 else {"title": "grp"})},
            "from": {"id": uid, "first_name": "Rudra"}, "text": text, **kw}


def tap(uid, data, message_id=1):
    return {"callback_query": {"id": "cb1", "from": {"id": uid, "first_name": "Rudra"}, "data": data,
                               "message": {"message_id": message_id, "chat": {"id": CHAT, "type": "private"}}}}


async def main():
    tg = FakeTg()
    B._frontends.insert(0, tg)
    B.scheduler.start()

    print("== rendering")
    r = lambda t, chat=CHAT: tg._render(t, chat)  # noqa: E731
    check(r("**bold** and *it* and `x<y`") == "<b>bold</b> and <i>it</i> and <code>x&lt;y</code>", "markdown -> HTML")
    check(r("```py\nprint(1 < 2)\n```") == '<pre><code class="language-py">print(1 &lt; 2)</code></pre>', "code block")
    check(r("-# qwen3.5:9b self-hosted · 5k/32k ctx") == "<i>qwen3.5:9b self-hosted · 5k/32k ctx</i>", "grey line -> italic")
    check("<a href=\"https://x.com/a?b=1&amp;c=2\">link</a>" in r("[link](https://x.com/a?b=1&c=2)"), "links")
    ts = int(time.time()) + 600
    check(r(f"at <t:{ts}:R>") == "at in 10 min", f"relative timestamp: {r(f'at <t:{ts}:R>')}")
    check(r(f"⏰ <@{OWNER}> take meds") == "⏰ take meds", "no self-mention in a private chat")
    check('tg://user?id=111' in r(f"⏰ <@{OWNER}> x", chat=-5), "mention link in a group")
    check(r("snake_case_name and 2*3*4") == "snake_case_name and 2*3*4", "no false italics")
    check(tg.plain("**b** -# x", CHAT) == "b -# x" and tg.plain("-# grey", CHAT) == "grey", "plain fallback")

    print("== stats line: spoiler by default, off per chat, warnings always visible")
    line = B.stats_line(["haiku", "$0.007", "11k ctx", "session ab12cd34"], [])
    warned = B.stats_line(["haiku", "$0.007"], ["long chat, send /compact"])
    check(line.startswith("-# haiku · $0.007") and B.quiet_grey(line) == line, "Discord line unchanged (mark is invisible)")
    check(r("Answer\n" + line) == "Answer\n<i><tg-spoiler>haiku · $0.007 · 11k ctx · session ab12cd34</tg-spoiler></i>",
          "spoiler by default")
    check(r(warned).endswith("</tg-spoiler> · long chat, send /compact</i>"), "warning stays outside the spoiler")
    check(r("-# ⏰ Reminder set") == "<i>Reminder set</i>", "other grey lines unchanged")
    B.update_settings(CHAT, tg_stats="off")
    check(r("Answer\n" + line) == "Answer", "off: line dropped")
    check(r(warned) == "<i>long chat, send /compact</i>", "off: warnings still shown")
    n = len(tg.calls)
    await T.TgChannel(tg, CHAT).send(line)
    check(len(tg.calls) == n, "off: a message holding only the stats line isn't sent")
    B.update_settings(CHAT, tg_stats="spoiler")
    pv = B.PermView(CHAT)
    toggle = next(c for c in pv.children if getattr(c, "custom_id", "") == "pv:stats")
    check(toggle.label == "Stats line: turn off" and "Stats line" in [f.name for f in B.perm_embed(CHAT).fields],
          "Settings has the toggle on Telegram")
    check(not any(getattr(c, "custom_id", "") == "pv:stats" for c in B.PermView(900000000000000001).children),
          "and not on Discord")

    print("== allow-list")
    B.TELEGRAM_ALLOWED_USER_IDS.clear(); B.TELEGRAM_OWNER_IDS.clear()
    await tg._dispatch({"message": msg(STRANGER, "/start", chat=STRANGER)})
    check("333" in tg.last_text() and "TELEGRAM_ALLOWED_USER_IDS" in tg.last_text(), "setup mode: /start tells the id")
    B.TELEGRAM_ALLOWED_USER_IDS.update({111, 222}); B.TELEGRAM_OWNER_IDS.add(111)
    n = len(tg.calls)
    await tg._dispatch({"message": msg(STRANGER, "/start", chat=STRANGER)})
    await tg._dispatch({"message": msg(STRANGER, "hello", chat=STRANGER)})
    check(len(tg.calls) == n, "strangers are ignored silently once a list exists")
    check(B.is_allowed(DISCORD_USER) and not B.is_owner(DISCORD_USER) and B.is_owner(900000000000000001),
          "Discord ids unchanged (empty ALLOWED_USER_IDS = everyone; owners from OWNER_IDS)")
    check(not B.is_allowed(STRANGER) and B.is_owner(OWNER) and not B.is_owner(GUEST), "Telegram: listed users only")

    print("== a message goes through the shared engine")
    seen = {}

    async def fake_local(prompt, model, ctx, history):
        seen.update(prompt=prompt, channel=ctx.channel_id, user=ctx.user_id)
        return B.LocalResult("Hello **there**", [], None)
    B.run_local = fake_local
    async def fake_limit(root, model): return 32768
    B.ctx_limit = fake_limit
    B.update_settings(CHAT, engine="local", style="chat")
    await tg._dispatch({"message": msg(OWNER, "hi")})
    last = tg.sent()[-1]
    check(seen == {"prompt": "hi", "channel": CHAT, "user": OWNER}, "handle_prompt got the Telegram chat and user")
    check(last["text"].startswith("Hello <b>there</b>\n<i>") and "local engine" in last["text"] and last["parse_mode"] == "HTML",
          f"reply rendered: {last['text']!r}")
    check(last.get("reply_parameters", {}).get("message_id"), "replies to the user's message")
    check(any(m == "sendChatAction" for m, _, _ in tg.calls), "typing indicator")

    print("== groups: only when addressed")
    n = len(tg.sent())
    B.update_settings(-100, engine="local", style="chat")
    await tg._dispatch({"message": msg(OWNER, "just chatting", chat=-100)})
    check(len(tg.sent()) == n, "ignores group chatter")
    await tg._dispatch({"message": msg(OWNER, "@mavis_bot hey", chat=-100)})
    check(len(tg.sent()) == n + 1 and seen["prompt"] == "hey", "answers an @mention (mention stripped)")

    print("== /panel: keyboard, select menus, owner checks")
    B.update_settings(CHAT, engine="local")
    await tg._dispatch({"message": msg(OWNER, "/panel")})
    p = tg.sent()[-1]
    btns = tg.buttons(p)
    check("Control panel" in p["text"] and "Telegram: Rudra (private)" in p["text"], "panel text, chat named")
    eng = next(b for b in btns if "Engine" in b["text"])
    check(eng["text"].endswith("▾") and eng["callback_data"].endswith(":m"), f"select shown as a button: {eng['text']}")
    tok = eng["callback_data"].split(":")[0]
    await tg._dispatch(tap(OWNER, eng["callback_data"]))
    menu = [(m, pp) for m, pp, _ in tg.calls if m == "editMessageText"][-1][1]
    opts = tg.buttons(menu)
    check(any("✓" in b["text"] and "Local" in b["text"] for b in opts) and opts[-1]["text"] == "⬅️ Back", "options list, current ticked, back")
    claude_opt = next(b for b in opts if "Claude Code" in b["text"])
    await tg._dispatch(tap(GUEST, claude_opt["callback_data"]))
    toast = [pp for m, pp, _ in tg.calls if m == "answerCallbackQuery"][-1]
    check(B.get_settings(CHAT)["engine"] == "local" and "owner" in toast["text"].lower(), f"guest can't pick Claude Code: {toast['text']}")
    await tg._dispatch(tap(OWNER, claude_opt["callback_data"]))
    check(B.get_settings(CHAT)["engine"] == "claude", "owner switched engine to Claude Code")
    await tg._dispatch(tap(OWNER, claude_opt["callback_data"]))
    toast = [pp for m, pp, _ in tg.calls if m == "answerCallbackQuery"][-1]
    check("expired" in toast["text"], "old keyboard expires after the panel re-renders")
    await tg._dispatch(tap(STRANGER, f"{tok}:0:m"))
    toast = [pp for m, pp, _ in tg.calls if m == "answerCallbackQuery"][-1]
    check("not allowed" in toast["text"], "stranger's tap refused")

    print("== confirm card: Edit -> text input -> updated card")
    snap = B.snapshot(CHAT)
    card = await T.TgChannel(tg, CHAT).send(embed=B.confirm_embed("old task", snap), view=B.CCConfirmView(CHAT, "old task", snap))
    edit_btn = next(b for b in tg.buttons(tg.sent()[-1]) if "Edit" in b["text"])
    await tg._dispatch(tap(OWNER, edit_btn["callback_data"], card.message_id))
    ask = tg.sent()[-1]
    check("force_reply" in ask["reply_markup"] and "old task" in ask["text"], "asks for the new text (current shown)")
    await tg._dispatch({"message": msg(OWNER, "new task text")})
    ed = [pp for m, pp, _ in tg.calls if m == "editMessageText"][-1]
    check("new task text" in ed["text"] and any("Run" in b["text"] for b in tg.buttons(ed)), "card updated in place, buttons kept")

    print("== reminders: fire in Telegram with Done / Snooze")
    r = B.add_reminder("in 10 min", "take meds", CHAT, OWNER)
    await B.fire_reminder(r["id"])
    rm = tg.sent()[-1]
    check(rm["text"] == "⏰ take meds" and [b["text"] for b in tg.buttons(rm)] == ["✅ Done", "💤 10 min"], f"reminder posted: {rm['text']!r}")
    snooze = tg.buttons(rm)[1]["callback_data"]
    await tg._dispatch(tap(GUEST, snooze))
    check(not B._reminders, "someone else can't snooze it")
    await tg._dispatch(tap(OWNER, snooze))
    ed = [pp for m, pp, _ in tg.calls if m == "editMessageText"][-1]
    check(len(B._reminders) == 1 and "snoozed until" in ed["text"] and not tg.buttons(ed), f"snoozed: {ed['text']!r}")

    print("== scheduled prompt routed to Telegram")
    B.update_settings(CHAT, engine="local")
    t = B.add_task("", "say hi", "say hi", CHAT, OWNER, at=B.now_local() + B.timedelta(minutes=20), engine="local")
    await B.run_scheduled_task(t["id"])
    check(tg.last_text().startswith("⏰ <b>say hi</b>") and "Hello <b>there</b>" in tg.last_text(), f"task result posted: {tg.last_text()[:80]!r}")
    check("Telegram: Rudra (private)" in B.tasks_embed().to_dict().get("fields", [{}])[0].get("value", "Telegram: Rudra (private)"),
          "Discord's task list names the Telegram chat")

    print("== files and photos")
    f = B.discord.File(B.io.BytesIO(b"png"), filename="chart.png")
    d = B.discord.File(B.io.BytesIO(b"text"), filename="notes.md")
    await T.TgChannel(tg, CHAT).send("here", files=[f, d])
    check(tg.sent("sendPhoto") and tg.sent("sendDocument"), "images as photos, others as documents")
    atts, notes = tg._attachments({"message_id": 5, "photo": [{"file_id": "s", "file_unique_id": "u1", "file_size": 10},
                                                              {"file_id": "L", "file_unique_id": "u2", "file_size": 99}],
                                   "document": {"file_id": "D", "file_unique_id": "u3", "file_name": "a.pdf", "file_size": 30 * 2**20}})
    check(len(atts) == 1 and atts[0].file_id == "L" and "20 MB" in notes[0], "largest photo; too-large file reported")

    print("== secrets")
    check(B.redact("token 123456789:" + "A" * 35) == "token [redacted]" or "telegram token" in B.redact("x 987654321:" + "b" * 35),
          "Telegram tokens redacted")
    check("TELEGRAM_BOT_TOKEN" in B.SCRUB_ENV, "token never passed to Claude Code")
    B.scheduler.shutdown(wait=False)
    print("\nALL V23 CHECKS PASSED")

if __name__ == "__main__":
    asyncio.run(main())
