"""/power through the Telegram front end with every system call faked (nothing is locked, slept or restarted):
owner-only, confirmation, the shutdown commands, cancel, the "back online" notice after a restart and after sleep,
and the warning when a requested restart never happened."""
import asyncio, json, os, time

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111,222"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B  # noqa  (first: isolates the bot)
from telegram_frontend import FakeTg, msg, tap, check  # noqa  (importing runs nothing: its main() is guarded)

OWNER, GUEST, CHAT = 111, 222, 111
cmds, suspends, locks = [], [], []


async def fake_run(*cmd):
    cmds.append(cmd)
    return fake_run.result


fake_run.result = None
B._run_cmd = fake_run
B._suspend = lambda hibernate: suspends.append(hibernate) or True
B._lock = lambda: locks.append(1) or True
B.power_supported = lambda: True


def buttons(tg):
    return {b["text"]: b["callback_data"] for b in tg.buttons(tg.sent()[-1])} if tg.sent() else {}


async def open_power(tg, uid=OWNER):
    await tg._dispatch({"message": msg(uid, "/power")})
    return buttons(tg)


def confirm_buttons(tg):
    last = next(p for m, p, _ in reversed(tg.calls) if m == "editMessageText")
    return {b["text"]: b["callback_data"] for b in tg.buttons(last)}


def edits(tg):
    return [p.get("text", "") for m, p, _ in tg.calls if m == "editMessageText"]


async def main():
    tg = FakeTg()
    B._frontends.insert(0, tg)
    B.scheduler.start()

    print("== who can use it")
    n = len(tg.calls)
    await tg._dispatch({"message": msg(GUEST, "/power", chat=CHAT)})
    check("Only owners" in tg.last_text() and not buttons(tg), "guest: /power refused")
    bt = await open_power(tg)
    check({"🔒 Lock", "😴 Sleep", "🛌 Hibernate", "🔁 Restart", "🔌 Shut down"} <= set(bt), f"owner: action buttons {list(bt)}")
    check("✖️ Cancel" not in bt, "no Cancel while nothing is pending")
    await tg._dispatch(tap(GUEST, bt["🔁 Restart"]))
    check(not cmds and not B.POWER_FILE.exists(), "guest tap on Restart does nothing")

    print("== lock: immediate")
    await tg._dispatch(tap(OWNER, bt["🔒 Lock"]))
    check(locks == [1] and "Locked" in edits(tg)[-1], "locks the screen, no confirmation")

    print("== restart: confirm, command, cancel")
    bt = await open_power(tg)
    await tg._dispatch(tap(OWNER, bt["🔁 Restart"]))
    check(not cmds and "Restart the laptop?" in edits(tg)[-1], "asks to confirm first")
    conf = confirm_buttons(tg)
    await tg._dispatch(tap(OWNER, conf["🔁 Yes, restart"]))
    check(cmds[-1][:4] == ("shutdown", "/r", "/t", str(B.POWER_DELAY)), f"runs shutdown /r /t 30: {cmds[-1]}")
    rec = json.loads(B.POWER_FILE.read_text())
    check(rec["action"] == "restart" and rec["channel_id"] == CHAT and rec["user_id"] == OWNER, "remembers who asked, where")
    check("Restart" in edits(tg)[-1] and "Cancel" in edits(tg)[-1], f"says when, and how to cancel: {edits(tg)[-1]!r}")
    bt = await open_power(tg)
    check("✖️ Cancel" in bt and "Pending" in tg.last_text(), "/power shows the pending restart with Cancel")
    await tg._dispatch(tap(OWNER, bt["✖️ Cancel"]))
    check(cmds[-1] == ("shutdown", "/a") and not B.POWER_FILE.exists() and "stays on" in edits(tg)[-1], "cancel aborts it")

    print("== a failed command")
    fake_run.result = "Access is denied.(5)"
    bt = await open_power(tg)
    await tg._dispatch(tap(OWNER, bt["🔌 Shut down"]))
    conf = confirm_buttons(tg)
    await tg._dispatch(tap(OWNER, conf["🔌 Yes, shut down"]))
    check(cmds[-1][1] == "/s" and "Couldn't shut down: Access is denied" in edits(tg)[-1] and not B.POWER_FILE.exists(),
          "error shown, nothing left pending")
    fake_run.result = None

    print("== back online after a restart (new process)")
    B._atomic_write_json(B.POWER_FILE, {"action": "restart", "channel_id": CHAT, "user_id": OWNER, "at": time.time() - 100})
    await B.power_back_on_start()
    check("Back online after the restart (down for about 1m 4" in tg.last_text() and not B.POWER_FILE.exists(),
          f"posts in the chat that asked: {tg.last_text()!r}")

    print("== sleep, then awake again (same process)")
    bt = await open_power(tg)
    await tg._dispatch(tap(OWNER, bt["😴 Sleep"]))
    conf = confirm_buttons(tg)
    await tg._dispatch(tap(OWNER, conf["😴 Yes, sleep"]))
    await asyncio.sleep(3.5)
    check(suspends == [False], "sleeps (not hibernate) after a short delay")
    watch = asyncio.create_task(B.power_watch())
    await asyncio.sleep(0.2)
    B._power_tick -= 600  # as if the laptop was asleep for 10 minutes
    await asyncio.sleep(10.5)
    check("Awake again after sleep (offline for about 10m" in tg.last_text() and not B.POWER_FILE.exists(),
          f"wake noticed: {tg.last_text()!r}")

    print("== a restart that never happened")
    B._atomic_write_json(B.POWER_FILE, {"action": "restart", "channel_id": CHAT, "user_id": OWNER, "at": time.time() - 600})
    await asyncio.sleep(10.5)
    check("didn't restart" in tg.last_text() and not B.POWER_FILE.exists(), f"says so: {tg.last_text()!r}")
    watch.cancel()

    print("== not a model tool")
    names = B.CHAT_TOOLS + B.READONLY_TOOLS + ["propose_claude_code"]
    check(not any(w in t for t in names for w in ("power", "shutdown", "lock", "sleep")), "no power tool for models")
    B.scheduler.shutdown(wait=False)
    print("\nALL POWER CHECKS PASSED")


asyncio.run(main())
