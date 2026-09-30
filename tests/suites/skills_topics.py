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
check(B.skills_reply(OWNER, "").startswith("**Skills** (3)") and "**backup-photos** v2" in B.skills_reply(OWNER, "show backup photos"),
      "/skills list and show")
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
    check(s["cc_session"] is None and s["cc_no_ask_until"] is None, "but not its session or 'without asking'")
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

    print("== topics: remembered across restarts")
    tg2 = FakeTg()
    check(tg2.place(tid) == (CHAT, 55) and tg2.cid(CHAT, 55) == tid, "registry reloaded")

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
