"""Reminders and scheduled tasks whose Discord thread, Telegram topic or dashboard chat was deleted: they post in the
chat it belonged to (or a DM) with a note, the task moves there, and nothing is run for a reply nobody gets."""
import asyncio, tempfile
from pathlib import Path
from types import SimpleNamespace
import discord
from _setup import B, TMP  # noqa  (first: isolates the bot)
import llmbot.telegram as T

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.REMINDERS_FILE = tmp / "rem.json"; B.TASKS_FILE = tmp / "tasks.json"
B._reminders.clear(); B._tasks.clear(); B._settings.clear()
OWNER = 2 * 10 ** 17  # a Discord id
PARENT, THREAD, LONE = 3 * 10 ** 17, 3 * 10 ** 17 + 1, 3 * 10 ** 17 + 2
TG_USER, TG_GROUP = 111, -1001234
B.OWNER_IDS.add(OWNER); B.TELEGRAM_OWNER_IDS.add(TG_USER); B.TELEGRAM_ALLOWED_USER_IDS.add(TG_USER)


def check(c, label):
    assert c, label
    print("  ok", label)


ran = []


async def fake_local(prompt, model, ctx, history):  # the local model, answering instantly
    ran.append(prompt)
    return B.LocalResult("the answer", [], None, prompt)
B.run_local = fake_local


class DCh:
    def __init__(self, cid):
        self.id, self.sent = cid, []

    async def send(self, content=None, **kw):
        self.sent.append(content)


async def discord_thread():
    print("== Discord: deleted thread")
    parent, dm = DCh(PARENT), DCh(999)
    B.bot.is_ready = lambda: True
    B.bot.get_channel = lambda cid: parent if cid == PARENT else None

    async def fetch_channel(cid):
        if cid == PARENT:
            return parent
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
    B.bot.fetch_channel = fetch_channel

    async def fetch_user(uid):
        async def create_dm(): return dm
        return SimpleNamespace(create_dm=create_dm)
    B.bot.fetch_user = fetch_user

    B._settings[str(THREAD)] = {"parent": PARENT}  # what a thread gets when it's first used (inherit_settings)
    t = B.add_task("0 9 * * *", "news", "news", THREAD, OWNER)
    check(t.get("parent") == PARENT, "the task remembers the thread's channel")
    await B.run_scheduled_task(t["id"])
    check(parent.sent and "the answer" in parent.sent[-1] and "its thread was deleted, so it posts here now"
          in parent.sent[-1], "result posted in the channel, saying why")
    check(B._tasks[t["id"]]["channel_id"] == PARENT, "the task moved to the channel")
    parent.sent.clear()
    await B.run_scheduled_task(t["id"])
    check(parent.sent and "deleted" not in parent.sent[-1], "next run: a normal post there")
    B.cancel_task(t["id"], OWNER)

    r = B.add_reminder("in 1 hour", "stretch", THREAD, OWNER)
    await B.fire_reminder(r["id"])
    check("stretch" in parent.sent[-1] and "deleted" in parent.sent[-1], "reminder: posted in the channel")

    r = B.add_reminder("in 1 hour", "lonely", LONE, OWNER)  # a channel with no parent: a DM
    await B.fire_reminder(r["id"])
    check(dm.sent and "lonely" in dm.sent[-1] and "comes here as a DM" in dm.sent[-1], "no parent: a DM, saying why")


class FakeHttp:
    """The Bot API's HTTP side: the real Telegram.api() runs on top, so its deleted-topic handling is what's tested.
    Answers as the real API did when tested on 2026-10-04: a typing indicator or an unchanged editForumTopic succeed
    even for a deleted topic; a message fails with "message thread not found"; a reply to a missing message fails
    with "message to be replied not found" while the topic exists."""

    def __init__(self):
        self.calls, self.deleted, self.next_id = [], set(), 500

    async def post(self, url, json=None, data=None, files=None, timeout=None):
        method, params = url.rsplit("/", 1)[1], dict(json or data or {})
        self.calls.append((method, params))
        thread = params.get("message_thread_id")
        reply = (params.get("reply_parameters") or {})
        err = lambda d: {"ok": False, "error_code": 400, "description": f"Bad Request: {d}"}  # noqa: E731
        missing_reply = reply.get("message_id", 0) > self.next_id and not reply.get("allow_sending_without_reply")
        if method.startswith("send") and method != "sendChatAction" and thread is not None and int(thread) in self.deleted:
            body = err("message thread not found")
        elif missing_reply:
            body = err("message to be replied not found")
        else:
            self.next_id += 1
            body = {"ok": True, "result": {"message_id": self.next_id} if method.startswith("send") and
                    method != "sendChatAction" else True}
        return SimpleNamespace(json=lambda: body)

    async def aclose(self):
        pass

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


async def telegram_topic():
    print("== Telegram: deleted topic")
    tg = T.Telegram(B, "123456789:" + "A" * 35)
    tg.http, tg.username, tg.bot_id = FakeHttp(), "test_bot", 42
    B._frontends.insert(0, tg)
    news = tg.cid(TG_GROUP, 77, "News")
    t = B.add_task("0 9 * * *", "headlines", "headlines", news, TG_USER)
    check(t.get("parent") == TG_GROUP, "the task remembers the topic's group")
    await B.run_scheduled_task(t["id"])  # topic still there
    probes = [p for p in tg.http.sent() if p.get("reply_parameters")]
    check(probes and not tg.gone(news) and B._tasks[t["id"]]["channel_id"] == news, "probe: topic exists, nothing moved")
    check(tg.http.sent()[-1].get("message_thread_id") == 77 and "the answer" in tg.http.sent()[-1]["text"]
          and "deleted" not in tg.http.sent()[-1]["text"], "result posted in the topic as usual")
    tg.http.calls.clear()
    tg.http.deleted.add(77)
    ran.clear()
    await B.run_scheduled_task(t["id"])
    posts = [p for p in tg.http.sent() if not p.get("reply_parameters")]  # not the probe (which failed, as meant)
    check(tg.gone(news) and "(deleted)" in B.where(news), "the probe found the deleted topic")
    check(len(ran) == 1 and posts and all("message_thread_id" not in p for p in posts), "ran once, posted to the group")
    check("topic «News» was deleted, so it posts here now" in posts[-1]["text"] and "the answer" in posts[-1]["text"],
          "with a note saying why")
    check(B._tasks[t["id"]]["channel_id"] == TG_GROUP, "the task moved to the group")
    B.cancel_task(t["id"], TG_USER)

    print("== Telegram: topic deleted while a reply was being worked on")
    chat2 = tg.cid(TG_GROUP, 88, "Code")
    tg.http.calls.clear(); tg.http.deleted.add(88)
    await T.TgChannel(tg, chat2).send("here's your file")  # e.g. a Claude Code reply finishing after the delete
    texts = [p["text"] for p in tg.http.sent()]
    check(any("«Code» was deleted" in x for x in texts) and any("here's your file" in x for x in texts[1:]),
          "not lost: a note, then the reply, in the group")
    tg.http.calls.clear()
    await T.TgChannel(tg, chat2).send("and another")
    check([("message_thread_id" in p) for p in tg.http.sent()] == [False], "later messages go straight to the group")
    tg.cid(TG_GROUP, 88, "Code")  # a message arrives from it after all: it wasn't deleted
    check(not tg.gone(chat2), "a message from the topic clears the mark")
    B._frontends.remove(tg)


async def web_tab():
    print("== Dashboard: deleting a chat that has tasks")
    from llmbot import webchat as W, dashboard as D
    fe = W.WebFrontend(B)
    W._fe = fe
    D.webchat = W
    keep, drop = fe.new_chat("keep me"), fe.new_chat("drop me")
    t1 = B.add_task("0 9 * * *", "web news", "web news", keep.id, B.WEB_USER_ID)
    r1 = B.add_reminder("in 1 hour", "web ping", keep.id, B.WEB_USER_ID)
    t2 = B.add_task("0 10 * * *", "other", "other", drop.id, B.WEB_USER_ID)

    class Req:
        can_read_body = True

        def __init__(self, body): self.body = body
        async def json(self): return self.body
    res = await D.chat_delete(Req({}), fe, keep)
    check(sorted(i["id"] for i in res["confirm"]) == sorted([t1["id"], r1["id"]]) and keep.id in fe.chats,
          "the page is asked first; nothing deleted yet")
    await D.chat_delete(Req({"scheduled": "keep"}), fe, keep)
    home = next(c for c in fe.chats.values() if c.title == "Scheduled")
    check(keep.id not in fe.chats and B._tasks[t1["id"]]["channel_id"] == home.id
          and B._reminders[r1["id"]]["channel_id"] == home.id, "kept: moved to the Scheduled chat")
    check("Moved here from the deleted chat" in home.messages[-1]["text"], "which says what moved")
    await D.chat_delete(Req({"scheduled": "cancel"}), fe, drop)
    check(drop.id not in fe.chats and t2["id"] not in B._tasks, "cancel: deleted with it")


async def main():
    B.scheduler.start()
    await discord_thread(); await telegram_topic(); await web_tab()

asyncio.run(main())
print("\nALL DELETED-CHAT CHECKS PASSED")
