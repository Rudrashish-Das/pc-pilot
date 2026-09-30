"""Telegram only (no DISCORD_TOKEN): the backend starts the way main() does it, answers a Telegram message on the local
engine, opens /panel, and delivers a reminder and a scheduled task to the Telegram chat, without touching Discord."""
import asyncio, json, os, sys

os.environ["DISCORD_TOKEN"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = "123456789:" + "A" * 35
os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111"
from _setup import B, TMP  # noqa  (first: isolates the bot)
import httpx
import llmbot.telegram as T
from telegram_frontend import msg  # noqa  (importing runs nothing: its main() is guarded)

CHAT = 111


def check(c, label):
    assert c, label
    print("  ok", label)


class FakeTg(T.Telegram):
    """The real front end with the Bot API faked; core_start() builds it like the real one."""
    instance = None

    def __init__(self, core, token):
        super().__init__(core, token)
        self.calls, self.next_id, self.username, self.bot_id = [], 1000, "test_bot", 42
        FakeTg.instance = self

    async def api(self, method, *, files=None, _timeout=None, **params):
        self.calls.append((method, params, files))
        if method in ("sendMessage", "sendDocument", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    async def run(self):
        await asyncio.Event().wait()

    def texts(self):
        return [p["text"] for m, p, _ in self.calls if m == "sendMessage"]


def llm(req: httpx.Request):
    if req.url.path.endswith("/chat/completions"):
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Hello from the local model"}}]})
    return httpx.Response(200, json={})


async def main():
    check(not B.DISCORD_TOKEN and B.TELEGRAM_BOT_TOKEN, "config: Telegram token only")
    check(B.CC_ENABLED and B.is_owner(CHAT), "Claude Code enabled: owners fall back to TELEGRAM_ALLOWED_USER_IDS")
    T.Telegram = FakeTg
    await B.core_start()  # what _telegram_only() runs
    tg = FakeTg.instance
    check(tg is not None and B._frontends[0] is tg, "Telegram front end started by core_start()")
    check(not B.bot.is_ready() and B.frontend_for(900000000000000001) is None, "Discord never started, owns no ids")
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(llm))
    B.update_settings(CHAT, engine="local", style="chat")

    await tg._dispatch({"message": msg(111, "hi", chat=CHAT)})
    for _ in range(50):
        if any("Hello from the local model" in t for t in tg.texts()):
            break
        await asyncio.sleep(0.1)
    check(any("Hello from the local model" in t for t in tg.texts()), "message answered on the local engine")

    await tg._dispatch({"message": msg(111, "/panel", chat=CHAT)})
    check(any(m == "sendMessage" and (p.get("reply_markup") or {}).get("inline_keyboard") for m, p, _ in tg.calls[-3:]),
          "/panel opens with buttons")

    await tg._dispatch({"message": msg(999, "hello", chat=999)})
    check(not any(p.get("chat_id") == 999 for m, p, _ in tg.calls), "strangers ignored")

    r = B.add_reminder("in 1 hour", "stretch", CHAT, 111)
    await B.fire_reminder(r["id"])
    check(any("stretch" in t for t in tg.texts()), "reminder delivered to the Telegram chat")

    t = B.add_task("0 9 * * *", "say hi", "hi", CHAT, 111)
    n = len(tg.texts())
    await B.run_scheduled_task(t["id"])
    check(len(tg.texts()) > n, "scheduled task posts in the Telegram chat")
    check("<#" not in B.where(CHAT), "chats named the Telegram way in lists")

    await B.core_stop()
    check("discord.gateway" not in sys.modules or not B.bot.is_ready(), "no Discord login happened")
    print("\nALL TELEGRAM-ONLY CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
