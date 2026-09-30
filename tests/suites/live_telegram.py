"""Real Claude Code (haiku) through the Telegram front end (fake Bot API): chat reply, session kept, card style with
live progress edits + Stop button, then the result card's buttons; and a file sent back as a Telegram document."""
import asyncio, os, re, sys, tempfile, time
from pathlib import Path

import httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
from telegram_frontend import FakeTg, msg, check  # noqa  (importing runs nothing: main() is guarded)

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "s.json"; B.USAGE_FILE = tmp / "u.json"; B.TASKS_FILE = tmp / "t.json"; B.REMINDERS_FILE = tmp / "r.json"
B._settings.clear(); B._tasks.clear(); B._reminders.clear()
WS = TMP / "ws"; WS.mkdir(exist_ok=True)
(WS / "notes.txt").write_text("codename: HELIOTROPE\n", encoding="utf-8")
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
B.TELEGRAM_ALLOWED_USER_IDS.clear(); B.TELEGRAM_ALLOWED_USER_IDS.add(111)
B.TELEGRAM_OWNER_IDS.clear(); B.TELEGRAM_OWNER_IDS.add(111)
B.CC_ENABLED = True
CHAT = 111


async def wait_job():
    await asyncio.sleep(1)
    job = B._last_job.get(CHAT)
    while job and job.status != "done":
        await asyncio.sleep(0.5)
    await asyncio.sleep(1)
    return job


async def main():
    B.http = httpx.AsyncClient()
    B.scheduler.start()
    tg = FakeTg()
    B._frontends.insert(0, tg)
    B.update_settings(CHAT, engine="claude", cc_backend="anthropic", cc_model="haiku", cc_perm="read", style="chat")

    print("== chat style")
    await tg._dispatch({"message": msg(111, "Remember: my cat is called Pixel. Reply with just OK.")})
    await wait_job()
    r1 = tg.last_text(); print("   ", repr(r1[-200:]))
    await tg._dispatch({"message": msg(111, "What is my cat called? One word.")})
    await wait_job()
    r2 = tg.last_text(); print("   ", repr(r2[-200:]))
    s1, s2 = (re.search(r"session (\w+)", r).group(1) for r in (r1, r2))
    check(s1 == s2 and "pixel" in r2.lower() and "<i>haiku" in r2, "session kept across Telegram messages, grey line italic")

    print("== file back as a document")
    B.update_settings(CHAT, cc_perm="edit")
    await tg._dispatch({"message": msg(111, "Send me notes.txt as a file. Don't paste it.")})
    await wait_job()
    docs = [f for m, p, f in tg.calls if m == "sendDocument"]
    check(docs and docs[-1]["document"][0] == "notes.txt", "attachment delivered as a Telegram document")

    print("== cards style: progress edits, Stop button, result buttons")
    B.update_settings(CHAT, style="cards", cc_perm="read")
    n_edits = sum(1 for m, _, _ in tg.calls if m == "editMessageText")
    await tg._dispatch({"message": msg(111, "What is the codename in notes.txt? One word.")})
    prog = tg.sent()[-1]
    check("Claude Code" in prog["text"] and any("Stop" in b["text"] for b in tg.buttons(prog)), "progress card with Stop")
    await wait_job()
    res = tg.sent()[-1]
    print("   ", repr(res["text"][:300]))
    check("HELIOTROPE" in res["text"] and {"💬 Follow up", "🔁 Retry", "📄 Full log"} <= {b["text"] for b in tg.buttons(res)},
          "result card with buttons")
    check(sum(1 for m, _, _ in tg.calls if m == "editMessageText") > n_edits, "progress card edited live")
    log_btn = next(b for b in tg.buttons(res) if "Full log" in b["text"])
    await tg._dispatch({"callback_query": {"id": "c", "from": {"id": 111, "first_name": "R"}, "data": log_btn["callback_data"],
                                           "message": {"message_id": 1, "chat": {"id": CHAT, "type": "private"}}}})
    check([f for m, p, f in tg.calls if m == "sendDocument"][-1]["document"][0].startswith("claude-"), "Full log button sends the transcript")
    B.scheduler.shutdown(wait=False)
    print("\nALL V23 LIVE CHECKS PASSED")

asyncio.run(main())
