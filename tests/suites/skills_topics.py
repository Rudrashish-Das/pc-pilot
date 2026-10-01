"""Learned skills (save from a reply, pick relevant ones for later prompts, once per session, scheduled runs can't
save, the save_skill tool on local backends), the [Access: ...] line, and Telegram topics as separate channels."""
import asyncio, os, time
from types import SimpleNamespace

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B, TMP  # noqa  (first: isolates the bot)
import llmbot.skills as S
import llmbot.telegram as T

OWNER, CHAT, GROUP = 111, 111, -1001234567890


def check(c, label):
    assert c, label
    print("  ok", label)


print("== skills: saved from a reply")
S.load(B.STORE, TMP / "skills.json")
reply = ("Done, the backup ran.\n[[skill: Backup Photos | back up the phone photos folder to the D: drive]]\n"
         "1. robocopy C:\\Photos D:\\Backup /MIR\n2. check the log for 'FAILED'\n[[/skill]]")
text, notes = S.extract(reply, allowed=True, channel_id=CHAT, user_id=OWNER)
check(text == "Done, the backup ran." and notes and "learned skill `backup-photos` (v1)" in notes[0], f"saved: {notes}")
check(S.get("backup photos")["body"].startswith("1. robocopy"), "stored under a slug")
text, notes = S.extract(reply.replace("/MIR", "/MIR /R:1"), allowed=True)
check("updated skill `backup-photos` (v2)" in notes[0] and "/R:1" in S.get("backup-photos")["body"], "same name replaces")
text, notes = S.extract(reply, allowed=False)
check(text == "Done, the backup ran." and "can't save" in notes[0] and S.get("backup-photos")["version"] == 2,
      "scheduled runs can't save (block still removed)")
secret = "[[skill: s | x]]\nexport API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz\n[[/skill]]"
S.extract(secret, allowed=True, redact=B.redact)
check("sk-ant-abc" not in S.get("s")["body"], "secrets redacted")
check(S.forget("s") and not S.get("s"), "forget")
reloaded = B.STORE.load("skills", TMP / "skills.json", [])
check([s["name"] for s in reloaded] == ["backup-photos"], "persisted")

print("== skills: picked for similar tasks, once per session")
S.put("weather-report", "weather forecast for a city", "use wttr.in/<city>?format=3")
block, names = S.prompt_block("please back up my photos to D", None)
check(names == ["backup-photos"] and "robocopy" in block and "[Your saved skills:" in block, "relevant one + index on a new session")
check("weather-report" in block.split("]")[0], "index lists all names")
block2, names2 = S.prompt_block("what's for dinner", "sess1")
check(block2 == "" and names2 == [], "nothing for an unrelated message in a running session")
S.mark_sent("sess1", names)
block3, names3 = S.prompt_block("back up photos again", "sess1")
check(names3 == [] and "robocopy" not in block3, "not repeated in the same session")
check(S.get("backup-photos")["uses"] == 1, "use counted")

print("== cc_prompt: time, access, skills")
snap = B.CCSnap("anthropic", "sonnet", "full", next(iter(B.WORKSPACES)), None)
job = SimpleNamespace(snap=snap, task="back up my photos", skills=[])
p = B.cc_prompt(job)
check(p.startswith("[Now: ") and "[Access: full: run any command" in p and "robocopy" in p and p.endswith("back up my photos"),
      "all three before the message")
check(job.skills == ["backup-photos"], "job remembers what it was given")
job = SimpleNamespace(snap=B.replace(snap, perm="edit"), task="hello", skills=[])
check("[Access: edit:" in B.cc_prompt(job) and "no shell" in B.cc_prompt(job), "edit access spelled out")
job = SimpleNamespace(snap=B.replace(snap, compact=True), task="/compact", skills=[])
check(B.cc_prompt(job) == "/compact", "compact unchanged")

print("== save_skill tool (Ollama/custom backends) -> the same marker")
check(B._mcp_action("save_skill", {"name": "x", "when": "", "steps": "a"})[1], "tool validates")
ok, err = B._mcp_action("save_skill", {"name": "Fix Wifi", "when": "wifi drops", "steps": "netsh wlan ..."})
check(not err and "fix-wifi" in ok, "tool accepts")
m = B.action_markers([("save_skill", {"name": "Fix Wifi", "when": "wifi drops", "steps": "1. netsh wlan show\n2. reset"})])
text, notes = S.extract(f"ok\n{m}", allowed=True)
check(text == "ok" and S.get("fix-wifi")["body"] == "1. netsh wlan show\n2. reset", "marker round-trips, multi-line")
check("**Learned skills** (3)" in B.skills_reply(OWNER, "") and "**backup-photos** v2" in B.skills_reply(OWNER, "show backup photos"),
      "/skills list and show")

print("== Claude Code's own skills")
B._cc_skills[:] = ["code-review", "dataviz", "anthropic-skills:pdf", "anthropic-skills:docx", "other:pdf-tools"]
listing = B.skills_reply(OWNER, "")
check(listing.startswith("**Claude Code's skills** (5)") and "• anthropic-skills: `pdf`, `docx`" in listing
      and "• built in: `code-review`, `dataviz`" in listing, "/skills lists them, grouped")
check(B.find_cc_skill("pdf") == ("anthropic-skills:pdf", []), "'pdf' finds anthropic-skills:pdf")
check(B.find_cc_skill("/code-review") == ("code-review", []), "exact name, leading / ok")
check(B.find_cc_skill("doc")[0] is None and "anthropic-skills:docx" in B.find_cc_skill("doc")[1], "unknown: suggestions")
job = SimpleNamespace(snap=snap, task="/anthropic-skills:pdf summarise it", skills=[])
p = B.cc_prompt(job)
check(p.startswith("/anthropic-skills:pdf summarise it\n\n[Now: ") and "[Access: full" in p,
      "skill call: stays first, time and access after it")
job = SimpleNamespace(snap=snap, task="/context", skills=[])
check(B.cc_prompt(job) == "/context", "other slash commands untouched")
job = SimpleNamespace(snap=snap, task="/etc/hosts what is this file?", skills=[])
check(B.cc_prompt(job).startswith("[Now: "), "a path is a message, not a command")
B._cc_skills.append("MyTool")
check(B.find_cc_skill("mytool") == ("MyTool", []), "names match regardless of case")
check(B.cc_prompt(SimpleNamespace(snap=snap, task="/mytool go", skills=[])).startswith("/mytool go\n\n[Now: "),
      "a skill typed in another case still gets time and access")
B._cc_skills.remove("MyTool")
text, notes = S.extract("Format:\n```\n[[skill: demo | x]]\nsteps\n[[/skill]]\n```", allowed=True)
check(not notes and S.get("demo") is None and "[[skill: demo" in text, "a marker in a code block is only an example")
cmd = B.build_cc_command("claude", B.replace(snap, perm="read"))
check("--disable-slash-commands" not in cmd and "--strict-mcp-config" in cmd, "skills on, MCP servers still off")
check("Read,Glob,Grep,WebSearch,WebFetch,Skill" in cmd and "Skill" in cmd[cmd.index("--allowedTools"):],
      "read-only jobs may load skills (they run with the job's own tools)")
started = []


async def fake_request(channel, uid, task, snap, out, **kw):
    started.append(task)


async def skill_runs():
    real = B.request_cc
    B.request_cc = fake_request
    said = []

    async def out(**kw):
        said.append(kw.get("content"))
    try:
        await B.run_skill(SimpleNamespace(id=CHAT), OWNER, "pdf", "summarise it", out)
        check(started[-1] == "/anthropic-skills:pdf summarise it", "run_skill starts /<skill> <request>")
        await B.run_skill(SimpleNamespace(id=CHAT), 222, "pdf", "x", out)
        check("owner-only" in said[-1] and len(started) == 1, "owners only")
        await B.run_skill(SimpleNamespace(id=CHAT), OWNER, "nope", "x", out)
        check("No Claude Code skill named `nope`" in said[-1], "unknown skill refused")
        # made during a job (~/.claude/skills/test/SKILL.md): not in the list yet, found by asking again
        stub = B.discover_cc_skills

        async def now_has_test():
            B._cc_skills.append("test")
            return B._cc_skills
        B.discover_cc_skills = now_has_test
        try:
            await B.run_skill(SimpleNamespace(id=CHAT), OWNER, "test", "", out)
            check(started[-1] == "/test", "a skill made since the list was taken is found")
        finally:
            B.discover_cc_skills = stub
            B._cc_skills.remove("test")
    finally:
        B.request_cc = real
asyncio.run(skill_runs())
check("Only owners" in B.skills_reply(222, "forget fix-wifi") and S.get("fix-wifi"), "only owners forget")


# ---- Telegram topics
class FakeTg(T.Telegram):
    def __init__(self):
        super().__init__(B, "123456789:" + "A" * 35)
        self.calls, self.next_id = [], 1000
        self.username, self.bot_id = "mavis_bot", 42

    async def api(self, method, *, files=None, _timeout=None, **params):
        self.calls.append((method, params))
        if method == "createForumTopic":
            return {"message_thread_id": 77, "name": params["name"]}
        if method in ("sendMessage", "sendDocument", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def msg(text, chat=CHAT, thread=None, topic=None, **kw):
    m = {"message_id": int(time.time() * 1000) % 10 ** 6, "date": int(time.time()), "text": text,
         "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup", **({} if chat > 0 else {"title": "grp"})},
         "from": {"id": OWNER, "first_name": "Rudra"}, **kw}
    if thread:
        m.update(is_topic_message=True, message_thread_id=thread,
                 reply_to_message={"message_id": thread, "from": {"id": 42}, "forum_topic_created": {"name": topic} if topic else {}})
    return {"message": m}


async def main():
    B.TELEGRAM_ALLOWED_USER_IDS.clear(); B.TELEGRAM_ALLOWED_USER_IDS.add(OWNER)
    B.TELEGRAM_OWNER_IDS.clear(); B.TELEGRAM_OWNER_IDS.add(OWNER)
    tg = FakeTg()
    B._frontends.insert(0, tg)
    seen = []

    async def fake_local(prompt, model, ctx, history):
        seen.append(ctx.channel_id)
        return B.LocalResult("hi", [], None)
    B.run_local = fake_local
    async def fake_limit(root, model): return 32768
    B.ctx_limit = fake_limit

    print("== topics: each one its own channel")
    B.update_settings(CHAT, engine="local", style="chat", cc_perm="full", cc_session="main-sess",
                      cc_no_ask_until=B.NO_ASK_FOREVER, voice="replies")
    await tg._dispatch(msg("hello", thread=55, topic="Research"))
    tid = seen[-1]
    check(tid > T.TOPIC_BASE and B.is_telegram_id(tid) and tid != CHAT, f"topic got its own id {tid}")
    last = tg.sent()[-1]
    check(last["chat_id"] == CHAT and last["message_thread_id"] == 55, "reply goes to the topic")
    check(any(m == "sendChatAction" and p.get("message_thread_id") == 55 for m, p in tg.calls), "typing in the topic")
    s = B.get_settings(tid)
    check(s["engine"] == "local" and s["cc_perm"] == "full" and s["voice"] == "replies", "settings copied from the chat")
    check(s["cc_session"] is None, "but not its session")
    check(B.no_ask_until(tid) == B.NO_ASK_FOREVER, "'full access without asking' carries over to new topics")
    await tg._dispatch(msg("again", thread=55))
    check(seen[-1] == tid, "same topic, same id")
    await tg._dispatch(msg("main"))
    check(seen[-1] == CHAT and tg.sent()[-1].get("message_thread_id") is None, "main chat unchanged")
    await tg._dispatch(msg("/new", thread=55))
    check(B.get_settings(CHAT)["cc_session"] == "main-sess", "/new in a topic leaves the main chat's session alone")
    check(tg.where(tid) == "Telegram: Rudra (private) › Research", f"named: {tg.where(tid)}")
    ch = await B.resolve_channel(tid)
    await ch.send("reminder!")
    check(tg.sent()[-1]["message_thread_id"] == 55, "reminders/tasks for a topic post into it")

    print("== 'without asking': set in the main chat, holds in its topics")
    await tg._dispatch(msg("x", thread=56))
    t2 = seen[-1]
    check(B.set_no_ask(CHAT, None) == [tid, t2] and B.no_ask_until(tid) is None and B.no_ask_until(t2) is None,
          "turned off in the main chat: off in its topics")
    B.update_settings(t2, cc_perm="edit")
    until = time.time() + 3600
    B.set_no_ask(CHAT, until)
    check(B.no_ask_until(CHAT) == until and B.no_ask_until(tid) == until and B.no_ask_until(t2) == until,
          "turned on in the main chat: on in every topic (with full access)")
    check(B.set_no_ask(tid, None) == [] and B.no_ask_until(tid) is None and B.no_ask_until(t2) == until,
          "in a topic: only that topic")
    B.update_settings(t2, cc_backend="ollama")
    B.set_no_ask(CHAT, until)
    check(B.no_ask_until(t2) is None, "never on a topic that uses the Ollama backend")
    B.update_settings(t2, cc_backend="anthropic")
    B.update_settings(tid, cc_no_ask_until=None)

    print("== topics: remembered across restarts; older topics get the chat's setting once")
    for t in tg._topics.values():
        t.pop("no_ask_copied", None)
    B.STORE.save("tg_topics", tg._topics_file, {str(k): v for k, v in tg._topics.items()})
    tg2 = FakeTg()
    check(tg2.place(tid) == (CHAT, 55) and tg2.cid(CHAT, 55) == tid, "registry reloaded")
    check(B.no_ask_until(tid) == until, "older topic: copied from the chat")
    B.update_settings(tid, cc_no_ask_until=None)
    FakeTg()
    check(B.no_ask_until(tid) is None, "only once: turning it off in a topic sticks across restarts")
    B._frontends.remove(tg)
    B._frontends.insert(0, tg2)
    tg = tg2

    print("== /panel in a thread changes only that thread")

    def tap(data, thread):
        return {"callback_query": {"id": "cb", "from": {"id": OWNER, "first_name": "Rudra"}, "data": data,
                                   "message": {"message_id": 1, "chat": {"id": CHAT, "type": "private"},
                                               "is_topic_message": True, "message_thread_id": thread}}}

    def keyboard(params):
        return [b for row in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]

    def last_keyboard():
        return keyboard([p for m, p in tg.calls if m in ("sendMessage", "editMessageText")][-1])

    async def pick(button_text, option_text, thread):
        sel = next(b for b in last_keyboard() if button_text in b["text"])
        await tg._dispatch(tap(sel["callback_data"], thread))
        opt = next(b for b in last_keyboard() if option_text in b["text"])
        await tg._dispatch(tap(opt["callback_data"], thread))

    B.set_no_ask(CHAT, None)
    for c in (CHAT, tid, t2):
        B.update_settings(c, engine="local", cc_perm="full", style="chat")
    before = {c: dict(B.get_settings(c)) for c in (CHAT, t2)}
    await tg._dispatch(msg("/panel", thread=55))
    check(tg.sent()[-1]["message_thread_id"] == 55 and "Research" in tg.sent()[-1]["text"], "panel opens in the thread")
    await pick("Engine", "Claude Code", 55)
    check(B.get_settings(tid)["engine"] == "claude", "engine changed in this thread")
    settings_btn = next(b for b in last_keyboard() if "Settings" in b["text"])
    await tg._dispatch(tap(settings_btn["callback_data"], 55))
    await pick("Full access", "Read-only", 55)
    check(B.get_settings(tid)["cc_perm"] == "read", "permissions changed in this thread")
    await pick("Chat", "Cards", 55)
    check(B.get_settings(tid)["style"] == "cards", "reply style changed in this thread")
    after = {c: dict(B.get_settings(c)) for c in (CHAT, t2)}
    check(after == before, "main chat and the other thread untouched")
    B.update_settings(tid, cc_perm="full")
    B.set_no_ask(tid, B.NO_ASK_FOREVER)
    check(B.no_ask_until(tid) and not B.no_ask_until(t2) and not B.no_ask_until(CHAT), "'without asking' in a thread: that thread only")
    B.set_no_ask(tid, None)

    print("== Discord threads: start from their channel, follow its 'without asking'")
    DCHAN, DTHREAD, DTHREAD2 = 900000000000000100, 900000000000000101, 900000000000000102
    dfe = next(fe for fe in B._frontends if type(fe).__name__ == "DiscordFrontend")
    dfe.parent_of = lambda c: DCHAN if c in (DTHREAD, DTHREAD2) else None
    B.update_settings(DCHAN, engine="claude", cc_perm="full", cc_session="chan-sess", cc_no_ask_until=B.NO_ASK_FOREVER)
    s = B.get_settings(DTHREAD)
    check(s["cc_perm"] == "full" and B.no_ask_until(DTHREAD) and s["cc_session"] is None and s["parent"] == DCHAN,
          "new thread: channel's settings incl. 'without asking', own session")
    check(B.is_subchannel(DTHREAD) and not B.is_subchannel(DCHAN), "knows threads from channels")
    B.update_settings(DTHREAD, engine="local")
    check(B.get_settings(DCHAN)["engine"] == "claude", "a thread's /panel leaves the channel alone")
    B.get_settings(DTHREAD2)
    check(set(B.set_no_ask(DCHAN, None)) == {DTHREAD, DTHREAD2} and not B.no_ask_until(DTHREAD2),
          "'without asking' off in the channel: off in its threads")
    B.set_no_ask(DCHAN, B.NO_ASK_FOREVER)
    check(B.no_ask_until(DTHREAD) and B.no_ask_until(DTHREAD2), "and on again")
    check(B.set_no_ask(DTHREAD, None) == [] and B.no_ask_until(DTHREAD2), "in a thread: that thread only")
    check(B.get_settings(DCHAN + 50)["cc_perm"] == B.CC_PERMISSION, "ordinary channels still start from the defaults")
    del dfe.parent_of

    print("== groups with topics")
    B.update_settings(GROUP, engine="local", style="chat")
    n = len(seen)
    await tg._dispatch(msg("chatting", chat=GROUP, thread=9, topic="Ideas"))
    check(len(seen) == n, "the topic's first message isn't a reply to the bot (bot made the topic)")
    await tg._dispatch(msg("@mavis_bot go", chat=GROUP, thread=9))
    check(seen[-1] > T.TOPIC_BASE and tg.sent()[-1]["message_thread_id"] == 9, "addressed in a group topic")

    print("== /topic")
    await tg._dispatch(msg("/topic Trip plans"))
    made = [p for m, p in tg.calls if m == "createForumTopic"][-1]
    check(made == {"chat_id": CHAT, "name": "Trip plans"}, "creates it")
    check(tg.sent()[-1]["message_thread_id"] == 77 and "Trip plans" in tg.sent()[-1]["text"], "says hello inside it")
    check(tg.where(tg.cid(CHAT, 77)).endswith("› Trip plans"), "named")

    async def refuse(method, **kw):
        raise T.TgError("Bad Request: the chat is not a forum", 400)
    real = tg.api
    tg.api = lambda method, **kw: refuse(method, **kw) if method == "createForumTopic" else real(method, **kw)
    await tg._dispatch(msg("/topic nope"))
    check("BotFather" in tg.sent()[-1]["text"], "explains how to turn topics on")
    print("all ok")


asyncio.run(main())
