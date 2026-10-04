"""GUEST_IDS: /status and /ping only, on Telegram and Discord, with the PC faked (no game list, event log or dialog
is touched): what a guest can and can't do, the status text, a ping's reply / close / "not signed in" / too many,
the dialog process itself (its window is filled in by the test), and how games are recognised and named."""
import asyncio, html, io, json, os, sys, time
from types import SimpleNamespace

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111,222"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B  # noqa  (first: isolates the bot)
from telegram_frontend import FakeTg, msg, check  # noqa  (importing runs nothing: its main() is guarded)
import llmbot.telegram as T
from llmbot import pcstatus as P

OWNER, USER, GUEST = 111, 222, 333
DISCORD_GUEST = 4 * 10 ** 17
B.GUEST_IDS.update({GUEST, DISCORD_GUEST})

now = time.time()
fake = SimpleNamespace(games=[("VALORANT", now - 75 * 60)], since=now - (3 * 3600 + 5 * 60), screen="ok",
                       replies=[], shown=[], sources=[])
RUNNING = P.running_games
P.running_games = lambda: fake.games
P.on_since = lambda: fake.since
P.desktop = lambda: fake.screen


async def show_ping(key, sender, message, source=""):
    fake.shown.append((sender, message))
    fake.sources.append(source)
    while not fake.replies:
        await asyncio.sleep(0.01)
    r = fake.replies.pop(0)
    if isinstance(r, Exception):
        raise r
    return r
REAL_WINDOW = P.PingWindow
P.show_ping = show_ping
prompts = []


async def no_prompt(*a, **kw):
    prompts.append(a)
B.handle_prompt = no_prompt


async def settle():
    for _ in range(20):
        await asyncio.sleep(0.01)


async def telegram():
    tg = FakeTg()
    B._frontends.insert(0, tg)
    say = lambda uid, text: tg._dispatch({"message": msg(uid, text, chat=uid)})  # noqa: E731

    print("== access")
    check(not B.is_allowed(GUEST) and B.can_check_pc(GUEST) and B.can_check_pc(OWNER) and not B.can_check_pc(USER),
          "guest: only /status and /ping; owners too; other users neither")
    B.TELEGRAM_OWNER_IDS.add(GUEST)  # listed as an owner too, by mistake
    check(not B.is_owner(GUEST) and not B.is_allowed(GUEST), "a guest listed as an owner is still only a guest")
    B.TELEGRAM_OWNER_IDS.discard(GUEST)
    for text in ("hi", "/panel", "/claude rm -rf", "/tasks", "/power", "/help"):
        n = len(tg.calls)
        await say(GUEST, text)
        check(prompts == [] and html.unescape(tg.last_text()) == T.GUEST_HELP and len(tg.calls) - n <= 2,
              f"guest {text!r}: only the two commands listed, nothing run")
    menus = [p for m, p, _ in tg.calls if m == "setMyCommands"]
    check(menus and menus[0]["scope"] == {"type": "chat", "chat_id": GUEST}
          and [c["command"] for c in menus[0]["commands"]] == ["status", "ping"], "guest's menu: /status, /ping")
    check(len(menus) == 1, "menu set once")
    await say(USER, "/status")
    check("Only owners and GUEST_IDS" in tg.last_text(), "an allowed non-owner: refused")

    print("== /status")
    await say(GUEST, "/status")
    t = tg.last_text()
    check("Playing **VALORANT** for 1h 15m" in t or "Playing <b>VALORANT</b> for 1h 15m" in t, f"the game, how long: {t!r}")
    check("Laptop on for 3h 05m" in t and "locked" not in t, "how long the laptop has been on")
    fake.games, fake.screen = [], "locked"
    await say(OWNER, "/status")
    t = tg.last_text()
    check("Not playing a game" in t and "screen is locked" in t, "owner: no game, locked")
    fake.screen = "ok"

    print("== /ping")
    B.PING_GAP = 0  # the spam guard has its own section below
    await say(GUEST, "/ping")
    check("Usage: /ping" in tg.last_text() and not fake.shown, "no message: usage")
    await say(GUEST, "/ping " + "a" * (B.PING_MAX_CHARS + 1))
    check(f"{B.PING_MAX_CHARS + 1} characters" in tg.last_text() and not fake.shown, "too long: refused, not cut")
    await say(GUEST, "/ping " + "\n".join("x" * (B.PING_MAX_LINES + 1)))
    check(f"{B.PING_MAX_LINES + 1} lines" in tg.last_text() and not fake.shown, "too many lines: refused")
    B._ping_times.clear()
    await say(GUEST, "/ping dinner's ready 🍝")
    await settle()
    check(fake.shown == [("Rudra", "dinner's ready 🍝")] and fake.sources == ["telegram"]
          and "on the laptop's screen" in tg.last_text(),
          "shown on the PC; the guest is told")
    ping_id = tg.sent()[-1]["reply_parameters"]["message_id"]  # the "Sent" note replies to her /ping
    fake.replies.append("on my way")
    await settle()
    last = tg.sent()[-1]
    check(last["chat_id"] == GUEST and (last.get("reply_parameters") or {}).get("message_id") == ping_id
          and "on my way" in last["text"] and "dinner" not in last["text"],
          "the answer comes back as a reply to her ping (not a quote of it)")
    await say(GUEST, "/ping hello?")
    fake.replies.append("")
    await settle()
    check("Seen (closed without a reply)" in tg.last_text(), "closed without a reply: seen")
    fake.screen = "locked"
    await say(GUEST, "/ping are you there")
    check("locked right now" in tg.last_text(), "locked: shows once unlocked")
    fake.replies.append(RuntimeError("no display"))
    await settle()
    check("couldn't be shown" in tg.last_text(), "dialog failed: the guest is told")
    fake.screen = "away"
    n = len(fake.shown)
    await say(GUEST, "/ping hi")
    check("Nobody is signed in" in tg.last_text() and len(fake.shown) == n, "nobody signed in: not shown")
    fake.screen = "ok"

    print("== spam guard")
    B._ping_times.clear()
    B.PING_GAP = 15
    await say(GUEST, "/ping one")
    await say(GUEST, "/ping two")
    check("Too many pings" in tg.last_text() and "Try again in" in tg.last_text(), "two within 15 s: the second refused")
    B.PING_GAP = 0
    for i in range(B.PING_LIMIT - 1):
        await say(GUEST, f"/ping {i}")
        check("Sent" in tg.last_text(), f"ping {i + 2} of {B.PING_LIMIT}: sent")
    await say(GUEST, "/ping too many")
    check("Too many pings" in tg.last_text() and "min" in tg.last_text(), f"ping {B.PING_LIMIT + 1} in 10 minutes: refused")
    n = len(fake.shown)
    for i in range(B.PING_LIMIT + 2):
        await say(OWNER, f"/ping owner {i}")
    check(len(fake.shown) == n + B.PING_LIMIT + 2, "owners aren't limited")
    B._ping_times[GUEST] = [t - B.PING_WINDOW for t in B._ping_times[GUEST]]  # 10 minutes later
    await say(GUEST, "/ping later")
    check("Sent" in tg.last_text(), "after 10 minutes: allowed again")
    fake.replies.extend([""] * 20)
    await settle()
    B._frontends.remove(tg)


class FakeProc:
    """The window's process: records the pings written to it; answer() makes it print a reply and exit."""
    started = []

    def __init__(self, broken=False):
        self.lines, self.returncode, self.done, self.out = [], None, asyncio.Event(), b""
        FakeProc.started.append(self)
        proc = self

        class In:
            def write(self, b):
                if broken:
                    raise BrokenPipeError
                proc.lines.append(json.loads(b))

            async def drain(self):
                pass

        class Out:
            def __init__(self, err): self.err = err
            async def read(self):
                await proc.done.wait()
                return b"" if self.err else proc.out
        self.stdin, self.stdout, self.stderr = In(), Out(False), Out(True)

    async def wait(self):
        await self.done.wait()
        self.returncode = 0

    def answer(self, reply):
        self.out = json.dumps({"reply": reply}).encode()
        self.done.set()


async def _bad_exit(proc):
    await proc.done.wait()
    proc.returncode = 3


async def window():
    print("== one window for every ping")
    w = REAL_WINDOW()
    broken = [True]

    async def spawn(*a, **kw):
        b, broken[0] = broken[0], False
        return FakeProc(broken=b)
    real_spawn, asyncio.create_subprocess_exec = asyncio.create_subprocess_exec, spawn
    try:
        a = asyncio.ensure_future(w.show("chat1", "Priya", "hi"))
        await settle()
        check(len(FakeProc.started) == 2 and FakeProc.started[1].lines == [{"from": "Priya", "message": "hi", "source": ""}],
              "a window that closed just then: a new one opens")
        b = asyncio.ensure_future(w.show("chat1", "Priya", "hello??"))
        c = asyncio.ensure_future(w.show("chat2", "Rudra", "test"))
        await settle()
        proc = FakeProc.started[1]
        check(len(FakeProc.started) == 2 and [x["message"] for x in proc.lines] == ["hi", "hello??", "test"]
              and w.is_open(), "later pings go into the same window")
        proc.answer("on my way")
        await settle()
        check(a.result() is None and b.result() == "on my way" and c.result() == "on my way" and not w.is_open(),
              "one answer: the reply once per chat (for its latest ping)")
        d = asyncio.ensure_future(w.show("chat1", "Priya", "again"))
        await settle()
        check(len(FakeProc.started) == 3, "after it's answered, the next ping opens a window again")
        FakeProc.started[2].answer("")
        await settle()
        check(d.result() == "", "closed without a reply: ''")
        e = asyncio.ensure_future(w.show("chat1", "Priya", "x"))
        await settle()
        bad = FakeProc.started[3]
        bad.wait = lambda: _bad_exit(bad)
        bad.answer("yes")
        await settle()
        check(e.result() == "yes", "a reply printed before a crash on exit still gets through")
    finally:
        asyncio.create_subprocess_exec = real_spawn


async def discord():
    print("== Discord")
    said = []

    class Resp:
        async def send_message(self, content=None, **kw):
            said.append(content)

    def inter(uid, name):
        return SimpleNamespace(type=B.discord.InteractionType.application_command, data={"name": name},
                               user=SimpleNamespace(id=uid), guild=None, response=Resp())
    tree = B.bot.tree
    check(await tree.interaction_check(inter(DISCORD_GUEST, "status")), "guest: /status allowed")
    check(await tree.interaction_check(inter(DISCORD_GUEST, "ping")), "guest: /ping allowed")
    check(not await tree.interaction_check(inter(DISCORD_GUEST, "panel")) and "only /status and /ping" in said[-1],
          "guest: /panel refused, saying what works")
    check(not await tree.interaction_check(inter(DISCORD_GUEST, "claude")), "guest: /claude refused")
    replies = []

    async def reply(content, **kw):
        replies.append(content)
    m = SimpleNamespace(author=SimpleNamespace(bot=False, id=DISCORD_GUEST), guild=None, reference=None,
                        attachments=[], mentions=[], role_mentions=[], content="hey", reply=reply)
    await B.bot.on_message(m)
    check(replies and "/status" in replies[-1] and not prompts, "guest DM: told the two commands, nothing run")

    posts, acks = [], []

    class DM:
        id = 777

        async def send(self, content=None, **kw):
            posts.append((content, kw.get("reference")))

    class Ack:
        def to_reference(self, **kw):
            return ("ref-to-ack", kw)

    class Follow:
        async def send(self, content=None, **kw):
            acks.append((content, kw))
            return Ack()

    class Resp2:
        async def defer(self, **kw):
            acks.append(("defer", kw))
    B._ping_times.clear()
    fake.replies.clear()  # left over from the spam guard section
    i = SimpleNamespace(user=SimpleNamespace(id=DISCORD_GUEST, display_name="Priya"), guild=None, channel=DM(),
                        response=Resp2(), followup=Follow())
    await B.ping_cmd.callback(i, "dinner's ready")
    check(acks[0] == ("defer", {"ephemeral": False, "thinking": True}) and "dinner's ready" in acks[1][0]
          and acks[1][1].get("ephemeral") is False, "DM: the Sent note is a normal message that shows the ping")
    fake.replies.append("coming")
    await settle()
    check(posts == [("💬 coming", ("ref-to-ack", {"fail_if_not_exists": False}))],
          "the answer replies to it (and is still sent if it was deleted)")


def dialog():
    print("== the dialog process")
    import tkinter as tk
    from llmbot import pingbox
    pingbox.chime = lambda root: None
    out = io.StringIO()
    pings = [{"from": "Priya", "message": "hi 🍝", "source": "discord"},
             {"from": "Priya", "message": "hello??", "source": "telegram"}]
    sys.stdin = SimpleNamespace(buffer=io.BytesIO("".join(json.dumps(p) + "\n" for p in pings).encode()))
    real_stdout, sys.stdout = sys.stdout, out
    seen = {}

    def mainloop(root):  # fill in the box and press Enter, as someone at the PC would
        for _ in range(20):  # the second ping arrives while it's open
            root.update()
            time.sleep(0.02)
        def tree(w):
            for c in w.winfo_children():
                yield c
                yield from tree(c)
        labels = [w.cget("text") for w in tree(root) if isinstance(w, tk.Label) and w.cget("text")]
        seen["logo"] = not any(isinstance(w, tk.Label) and w.cget("image") for w in tree(root))
        seen["badge"] = [w for w in tree(root) if isinstance(w, tk.Canvas) and w.winfo_manager() and w.find_all()]
        box = next(w for w in root.winfo_children() if isinstance(w, tk.Entry))
        seen.update(title=root.title(), labels=labels, topmost=root.attributes("-topmost"))
        buttons = {w.cget("text"): w for w in tree(root) if isinstance(w, tk.Button)}
        seen.update(enter=bool(box.bind("<Return>")), buttons=sorted(buttons))
        box.insert(0, "on my way")
        buttons["Reply"].invoke()
    try:
        tk.Tk().destroy()
    except tk.TclError as e:  # no display
        sys.stdout = real_stdout
        print("  skipped:", e)
        return
    exits = []
    real_exit, os._exit = os._exit, exits.append
    try:
        tk.Tk.mainloop = mainloop
        pingbox.main()
    finally:
        os._exit = real_exit
        sys.stdout = real_stdout
    check(seen["title"] == "Ping from Priya" and seen["labels"] == ["Priya pinged you on Telegram (2 pings)", "hello??"] and seen["topmost"],
          f"one window, showing the latest ping: {seen['labels']}")
    check(seen["logo"], "no picture in the window (the logo is its icon)")
    check(len(seen["badge"]) == 1, "the app's badge (the latest ping's: Telegram)")
    check(seen["buttons"] == ["Close", "Reply"] and seen["enter"], "Reply and Close buttons; Enter sends")
    check(json.loads(out.getvalue()) == {"reply": "on my way"} and exits == [0], "prints the reply, exits at once")
    from llmbot import pingbox as PB
    check(PB.clipped("a\n" * 50).count("\n") < PB.MAX_LINES and PB.clipped("b" * 5000).endswith(" …")
          and PB.clipped("short") == "short", "the window keeps any text to a size that fits the screen")


def games():
    print("== recognising games")
    known = {r"d:\games\something\thing.exe"}
    cases = {
        r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe": "VALORANT",
        r"C:\Program Files (x86)\Steam\steamapps\common\War Thunder\win64\aces.exe": "War Thunder",
        r"D:\Games\Something\thing.exe": "Something",
        r"C:\Users\x\Downloads\Prison Architect64.exe": None,  # not in a library, not known: no
        r"C:\Riot Games\Riot Client\RiotClientServices.exe": None,
        r"C:\Program Files (x86)\Steam\steamapps\common\PUBG\TslGame\Binaries\Win64\BEService.exe": None,
        r"C:\Program Files (x86)\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe": None,
        r"C:\Program Files (x86)\Steam\steam.exe": None,
    }
    for exe, want in cases.items():
        got = P.game_name(exe) if P.is_game(exe, known) else None
        check(got == want, f"{exe.rsplit(chr(92), 1)[1]}: {got}")
    check(P.is_game(r"C:\Users\x\Downloads\Prison Architect64.exe", {r"c:\users\x\downloads\prison architect64.exe"})
          and P.game_name(r"C:\Users\x\Downloads\Prison Architect64.exe") == "Prison Architect",
          "known to Windows (Game Bar): yes, named after the file")
    import psutil
    procs = [SimpleNamespace(info={"exe": None, "name": "VALORANT-Win64-Shipping.exe", "create_time": now - 600}),
             SimpleNamespace(info={"exe": None, "name": "notepad.exe", "create_time": now - 60})]
    saved = (psutil.process_iter, P.known_game_exes)
    psutil.process_iter = lambda attrs=None: procs
    P.known_game_exes = lambda: {r"c:\riot games\valorant\live\shootergame\binaries\win64\valorant-win64-shipping.exe"}
    check(RUNNING() == [("VALORANT", now - 600)], "path hidden by an anti-cheat: matched by file name")
    psutil.process_iter, P.known_game_exes = saved
    check(P._iso("2026-10-04T10:38:54.0333035Z") == 1791110334.0333035 or
          abs(P._iso("2026-10-04T10:38:54.0333035Z") - 1791110334.033303) < 1e-3, "event log times parse")
    check(B._span(59) == "0 min" and B._span(3 * 3600 + 300) == "3h 05m" and B._span(26 * 3600) == "1d 2h", "spans")


async def main():
    B.scheduler.start()
    await telegram()
    await window()
    await discord()
    dialog()
    games()

asyncio.run(main())
print("\nALL GUEST CHECKS PASSED")
