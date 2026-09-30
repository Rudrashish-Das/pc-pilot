import asyncio, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
import discord, httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
B.is_telegram_id = lambda x: False  # these tests use small fake Discord ids

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.USAGE_FILE = tmp / "usage.json"; B.REMINDERS_FILE = tmp / "rem.json"
B.TASKS_FILE = tmp / "tasks.json"; B._usage.clear(); B._reminders.clear(); B._tasks.clear()
SP = TMP; WS = SP / "ws_v15"; WS.mkdir(exist_ok=True)
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
OWNER, CH = 1, 1515
B.OWNER_IDS.add(OWNER); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.add(OWNER)


def check(c, label):
    assert c, label
    print("  ok", label)


def when_tests():
    print("== parse_when")
    now = datetime(2026, 9, 30, 1, 33, tzinfo=B.TZ)
    P = lambda s: B.parse_when(s, now)
    d1 = timedelta(days=1)
    cases = {"in 1 min": now + timedelta(minutes=1), "1 min": now + timedelta(minutes=1),
             "in 2 hours": now + timedelta(hours=2), "1h30m": now + timedelta(minutes=90),
             "in 1 hour and 15 minutes": now + timedelta(minutes=75), "an hour": now + timedelta(hours=1),
             "in 45s": now + timedelta(seconds=45), "2 days": now + timedelta(days=2),
             "18:30": now.replace(hour=18, minute=30), "at 6pm": now.replace(hour=18, minute=0),
             "6:15 am": now.replace(hour=6, minute=15), "1:00": now.replace(hour=1, minute=0) + d1,
             "12am": now.replace(hour=0, minute=0) + d1, "tomorrow 9am": now.replace(hour=9, minute=0) + d1,
             "tomorrow at 21:00": now.replace(hour=21, minute=0) + d1, "9am tomorrow": now.replace(hour=9, minute=0) + d1,
             "2026-10-01 09:00": datetime(2026, 10, 1, 9, 0, tzinfo=B.TZ),
             "2026-10-01T09:00:00+00:00": datetime(2026, 10, 1, 14, 30, tzinfo=B.TZ)}
    for s, want in cases.items():
        got = P(s)
        assert got == want, (s, got, want)
    print(f"  ok {len(cases)} formats parse correctly")
    for bad in ["5", "soon", "in 3 fortnights", "25:00", "13pm", "2026-01-01 09:00", "in 400 days", "today 1:00"]:
        try:
            P(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    check(True, "bad / ambiguous / past / too-far inputs rejected")


sent = []


class Ch:
    id = CH

    async def send(self, content=None, **kw):
        sent.append((content, kw))


async def core():
    print("== add / fire / cancel / persistence")
    B.bot.get_channel = lambda cid: Ch()
    B.scheduler.start()
    r = B.add_reminder("in 2s", "take my meds", CH, OWNER)
    check(r["id"] in B.REMINDERS_FILE.read_text(), "saved to reminders.json")
    line = B.reminder_line(r)
    check(line.startswith(f"⏰ Reminder `{r['id']}` set for <t:") and ":R>)" in line, "confirmation uses Discord timestamps")
    await asyncio.sleep(12)  # scheduling floor is now+10s
    check(sent and sent[0][0].startswith(f"⏰ <@{OWNER}> take my meds"), f"fired: {sent[0][0]!r}")
    am = sent[0][1]["allowed_mentions"]
    check(am.everyone is False and am.roles is False and [u.id for u in am.users] == [OWNER], "pings only the requester")
    check(isinstance(sent[0][1]["view"], B.ReminderView), "Done / Snooze buttons attached")
    check(r["id"] not in B._reminders and "take my meds" not in B.REMINDERS_FILE.read_text(), "removed after firing")
    sent.clear()
    B._reminders["rold"] = {"id": "rold", "when": (B.now_local() - timedelta(minutes=30)).isoformat(), "text": "old one",
                            "channel_id": CH, "user_id": OWNER, "created_at": ""}
    await B.fire_reminder("rold")
    check("late: this was due" in sent[0][0], "missed reminder says it's late")
    r2 = B.add_reminder("in 1 hour", "@everyone <@&123> ping <@999>", CH, OWNER)
    check(B.cancel_reminder(r2["id"], 999).startswith("Only"), "others can't cancel")
    check(B.cancel_reminder(r2["id"], OWNER).startswith("Cancelled") and not B.scheduler.get_job(f"rem-{r2['id']}"),
          "cancel removes the job")
    r3 = B.add_reminder("in 3 hours", "reload me", CH, OWNER)
    B.scheduler.remove_job(f"rem-{r3['id']}"); B._reminders.clear()
    B._reminders.update({x["id"]: x for x in B._read_json(B.REMINDERS_FILE, [])}); B.load_reminders()
    check(B.scheduler.get_job(f"rem-{r3['id']}") is not None, "re-scheduled after restart")
    e = B.tasks_embed(); v = B.TasksView()
    check(any(r3["id"] in f.name for f in e.fields) and any(o.value == r3["id"] for o in v.children[0].options),
          "/tasks lists and cancels reminders")
    B.cancel_reminder(r3["id"], OWNER)

    class Resp:
        async def edit_message(self, **kw): self.kw = kw
    inter = type("I", (), {"user": type("U", (), {"id": OWNER})(), "response": Resp(),
                           "message": type("M", (), {"content": "⏰ <@1> meds"})()})()
    rv = B.ReminderView({"id": "rx", "text": "meds", "channel_id": CH, "user_id": OWNER})
    await rv.snooze.callback(inter)
    check(len(B._reminders) == 1 and "snoozed until" in inter.response.kw["content"] and inter.response.kw["view"] is None,
          "💤 snooze re-schedules")
    B._reminders.clear()


async def markers():
    print("== Claude markers + local tools")
    txt, lines = B.extract_reminders("Sure, I'll remind you!\n[[remind: in 1 min | Take your meds 💊]]", CH, OWNER)
    check(txt == "Sure, I'll remind you!" and lines[0].startswith("⏰ Reminder") and "Take your meds" in lines[0],
          "marker -> reminder, marker removed")
    txt, lines = B.extract_reminders("[[remind: whenever | x]]", CH, OWNER)
    check(lines[0].startswith("⚠️ Reminder not set"), "bad time -> visible warning")
    ctx = B.ToolCtx(CH, OWNER, B.CHAT_TOOLS)
    res = await B.execute_tool("set_reminder", {"when": "tomorrow 8am", "text": "standup"}, ctx)
    check(res.startswith("Reminder r") and ctx.reminders, "local set_reminder")
    check("standup" in await B.execute_tool("list_reminders", {}, ctx), "local list_reminders")
    check((await B.execute_tool("cancel_reminder", {"reminder_id": res.split()[1]}, ctx)).startswith("Cancelled"),
          "local cancel_reminder")
    check("set_reminder" not in B.READONLY_TOOLS, "scheduled runs can't create reminders")
    B._reminders.clear()


class Typ:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


async def live():
    print("== live: Claude (haiku) gets the original request")
    B.update_settings(CH, engine="claude", cc_model="haiku", cc_perm="read", style="chat")
    B.http = httpx.AsyncClient()
    out_msgs = []

    async def out(**kw): out_msgs.append(kw.get("content"))

    class LiveCh(Ch):
        def typing(self): return Typ()
    B.bot.get_channel = lambda cid: LiveCh()
    job = await B.start_cc_job(LiveCh(), "can you pls remind me to take my meds in 1 min?", B.snapshot(CH), OWNER,
                               out=out, chat=True)
    while job.status != "done":
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    print("     reply:", repr(out_msgs[-1]))
    check("[[remind" not in out_msgs[-1] and "-# ⏰ Reminder" in out_msgs[-1], "Claude set it; marker hidden, confirmation shown")
    r = next(iter(B._reminders.values()))
    delta = (datetime.fromisoformat(r["when"]) - B.now_local()).total_seconds()
    check(0 < delta <= 65 and "med" in r["text"].lower(), f"due in {delta:.0f}s: {r['text']!r}")
    sent.clear()
    await asyncio.sleep(max(delta, 10) + 2)
    check(sent and "med" in sent[0][0].lower(), f"pinged: {sent[0][0]!r}")


async def main():
    when_tests(); await core(); await markers()
    if LIVE:
        await live()

asyncio.run(main())
print("\nALL V15 CHECKS PASSED")
