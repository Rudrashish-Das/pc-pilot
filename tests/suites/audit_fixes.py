"""Regression checks for the bugs found in the October 2026 audit (one section per fix)."""
import asyncio, logging, os, tempfile, time
from pathlib import Path
from types import SimpleNamespace
from _setup import B, TMP  # noqa  (first: isolates the bot)
from llmbot import logs as L
B.is_telegram_id = lambda x: False  # small fake Discord ids

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.USAGE_FILE = tmp / "usage.json"; B.REMINDERS_FILE = tmp / "rem.json"
B.TASKS_FILE = tmp / "tasks.json"; B._usage.clear(); B._reminders.clear(); B._tasks.clear()
WS = TMP / "ws_audit"; WS.mkdir(exist_ok=True)
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
OWNER, OTHER, CH = 1, 2, 4242
B.OWNER_IDS.add(OWNER); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.update({OWNER, OTHER})


def check(c, label):
    assert c, label
    print("  ok", label)


class Ch:
    id = CH

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def job_with(result: str, *, scheduled=False, session="11111111-2222-3333-4444-555555555555", resume=None):
    snap = B.CCSnap("anthropic", "haiku", "edit", "default", resume=resume)
    msgs = []

    async def out(**kw):
        if len(kw.get("content") or "") > 2000:
            raise AssertionError(f"Discord would refuse a {len(kw['content'])}-character message")
        msgs.append(kw.get("content"))
    job = B.CCJob(channel=Ch(), task="do it", snap=snap, user_id=OWNER, chat=True, out=out, scheduled=scheduled)
    job.parser.session_id = session
    job.parser.result = {"type": "result", "result": result, "is_error": False, "num_turns": 2, "total_cost_usd": 0.01}
    job.status = "done"
    return job, msgs


async def notices_kept():
    print("== 1: notes from a skill block survive a reminder marker")
    job, msgs = job_with("Done.\n[[skill: csv-merge | merging csv files]]\nuse pandas\n[[/skill]]\n"
                         "[[remind: in 10 min | check the merge]]")
    await B._finalize(job)
    check(any("learned skill `csv-merge`" in n for n in job.notices), f"skill notice kept: {job.notices}")
    check(any("Reminder" in n for n in job.notices), "reminder notice there too")
    B._reminders.clear(); B.skills_mod.forget("csv-merge")


async def scheduled_session():
    print("== 2: a scheduled run doesn't take over the chat's session")
    B.update_settings(CH, cc_backend="anthropic", workspace="default", cc_session="aaaaaaaa-chat", cc_session_pinned=True)
    job, _ = job_with("Here's the news.", scheduled=True, session="bbbbbbbb-task")
    await B._finalize(job)
    s = B.get_settings(CH)
    check(s["cc_session"] == "aaaaaaaa-chat" and s["cc_session_pinned"], f"chat keeps its session: {s['cc_session']}")
    job, _ = job_with("Hi.", session="cccccccc-user")
    await B._finalize(job)
    check(B.get_settings(CH)["cc_session"] == "cccccccc-user", "a normal run still saves its session")


async def patient_channel():
    print("== 3: due reminders wait for the front end instead of being dropped")
    calls = {"n": 0}
    ready = {"v": False}
    ch = Ch()

    class SlowFe:  # like Discord before login: no channel until connected
        def owns(self, cid): return cid == CH
        def ready(self): return ready["v"]
        async def get_channel(self, cid):
            calls["n"] += 1
            if not ready["v"]:
                raise RuntimeError("not logged in")
            return ch
    fe = SlowFe()
    B._frontends.insert(0, fe)
    B._reminders["rwait"] = {"id": "rwait", "when": B.now_local().isoformat(), "text": "meds", "channel_id": CH,
                             "user_id": OWNER, "created_at": ""}
    t = asyncio.ensure_future(B.fire_reminder("rwait"))
    await asyncio.sleep(3)
    check(not t.done() and "rwait" in B._reminders, "still pending (and saved) while not connected")
    ready["v"] = True
    await asyncio.wait_for(t, 40)
    check(ch.sent and "meds" in ch.sent[0] and "rwait" not in B._reminders, "delivered once connected")
    # a channel that's really gone while connected: no waiting
    async def gone(cid): raise RuntimeError("Unknown Channel")
    fe.get_channel = gone
    t0 = time.monotonic()
    check(await B.resolve_channel_patiently(CH) is None and time.monotonic() - t0 < 1, "connected + missing = fail fast")
    B._frontends.remove(fe)


async def long_reply():
    print("== 4: long reply + notes stays under Discord's limit")
    job, msgs = job_with("x" * 1880)
    job.notices = [f"⏰ Reminder `r{i}` set for <t:1790000000:f> (<t:1790000000:R>): " + "y" * 150 for i in range(3)]
    await B._send_chat_reply(job)
    check(len(msgs) >= 2 and all(len(m) <= 2000 for m in msgs), f"split into {len(msgs)} messages")
    check(sum(m.count("Reminder `r") for m in msgs) == 3, "every note posted")


def cron_ranges():
    print("== 5: weekday ranges from Sunday")
    check(B.validate_cron("0 9 * * 0-6") and B.validate_cron("0 9 * * 0-3"), "0-6 and 0-3 accepted")
    trig = B.validate_cron("0 9 * * */2")
    t = B.now_local()
    days = set()
    for _ in range(7):
        t = trig.get_next_fire_time(None, t + B.timedelta(seconds=1))
        days.add(t.strftime("%a"))
    check(days == {"Sun", "Tue", "Thu", "Sat"}, f"*/2 = Sun, Tue, Thu, Sat like cron: {sorted(days)}")


def resumed_cost():
    print("== 6: resuming a session with no recorded total doesn't count its lifetime cost")
    B._usage.clear()
    job, _ = job_with("ok", session="dddddddd-terminal", resume="dddddddd-terminal")
    job.parser.result["total_cost_usd"] = 20.0
    B.record_cost(job)
    check(B.spent_today() == 0.0 and job.cost_this is None, f"today: ${B.spent_today()}")
    job, _ = job_with("ok", session="dddddddd-terminal", resume="dddddddd-terminal")
    job.parser.result["total_cost_usd"] = 20.05
    B.record_cost(job)
    check(abs(B.spent_today() - 0.05) < 1e-9, "the next message counts exactly")


def overflow_time():
    print("== 8: absurd relative times are a normal error")
    for s in ("in 999999999999 days", "in 99999999999999999999 weeks"):
        try:
            B.parse_when(s)
        except ValueError:
            continue
        raise AssertionError(s)
    check(True, "ValueError, not OverflowError")
    _, lines = B.extract_reminders("[[remind: in 999999999999 days | x]]", CH, OWNER)
    check(lines[0].startswith("⚠️ Reminder not set"), "marker shows a warning instead of crashing the reply")


def webchat_ids():
    print("== 9: a deleted web chat's id isn't reused")
    from llmbot import webchat as W
    fe = W.WebFrontend(B)
    a = fe.new_chat("a"); b = fe.new_chat("b")
    B.update_settings(b.id, engine="claude", cc_session="eeeeeeee-old")
    B.remember(b.id, "hi", "hello")
    fe.delete_chat(b.id)
    c = fe.new_chat("c")
    check(c.id != b.id and B.get_settings(c.id).get("cc_session") is None and not B._history.get(c.id),
          "new chat starts clean")
    check(str(b.id) not in B._settings and b.id not in B._history, "deleted chat's settings and memory removed")
    fe.flush()
    check(W.WebFrontend(B).new_chat("d").id < c.id, "still not reused after a restart")
    print("== 9b: leftovers from chats deleted before the fix (no last_id saved yet)")
    B.STORE.delete("webchat", B.DATA_DIR / "webchat.json")
    B._settings.clear(); B._history.clear()
    fe = W.WebFrontend(B)
    a = fe.new_chat("a"); b = fe.new_chat("b")
    orphan = b.id - 1  # deleted the old way: settings and memory still there
    B.update_settings(orphan, engine="claude", cc_session="ffffffff-orphan"); B.remember(orphan, "hi", "hello")
    fe.flush()
    fe = W.WebFrontend(B)  # the restart that runs the migration
    check(str(orphan) not in B._settings and orphan not in B._history, "orphaned settings and memory removed")
    n = fe.new_chat("n")
    check(n.id < orphan and B.get_settings(n.id).get("cc_session") is None, "orphaned id retired, new chat clean")


def pid_alive():
    print("== 10: one _pid_alive")
    check(B._pid_alive(os.getpid()), "this process is alive")
    check(not B._pid_alive(4_000_000), "a made-up pid isn't")


def log_rollover():
    print("== 11: a failed log rollover doesn't recurse")
    d = Path(tempfile.mkdtemp())
    old = L.file_bytes
    L.file_bytes = 200
    h = L.CappedRotatingFileHandler(d / "bot.log")
    lg = logging.getLogger("llmbot")
    lg.addHandler(h)
    real = L.archive
    tries = {"n": 0}

    def locked(path):
        tries["n"] += 1
        raise PermissionError("file is in use")
    L.archive = locked
    try:
        for i in range(20):
            lg.warning("line %d %s", i, "z" * 50)
    finally:
        L.archive = real
        lg.removeHandler(h); h.close(); L.file_bytes = old
    check(tries["n"] == 1, f"tried once, then backs off ({tries['n']} tries)")
    check("rollover failed" in (d / "bot.log").read_text(encoding="utf-8"), "the failure is logged")


def cancel_privacy():
    print("== 12: others' tasks and reminders aren't confirmed to exist")
    t = B.add_task("0 9 * * 1-5", "news", "news", CH, OWNER)
    r = B.add_reminder("in 1 hour", "private", CH, OWNER)
    check(B.cancel_task(t["id"], OTHER).startswith("No task") and t["id"] in B._tasks, "task: 'no task'")
    check(B.cancel_reminder(r["id"], OTHER).startswith("No reminder") and r["id"] in B._reminders, "reminder: 'no reminder'")
    B.cancel_task(t["id"], OWNER); B.cancel_reminder(r["id"], OWNER)
    from llmbot import dashboard as D
    check(D.qint("12", 0) == 12 and D.qint("abc", 5) == 5 and D.qint(None, None) is None, "dashboard number parsing")


async def main():
    B.scheduler.start()
    await notices_kept(); await scheduled_session(); await patient_channel(); await long_reply()
    cron_ranges(); resumed_cost(); overflow_time(); webchat_ids(); pid_alive(); log_rollover(); cancel_privacy()

asyncio.run(main())
print("\nALL AUDIT CHECKS PASSED")
