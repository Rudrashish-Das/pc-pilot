""""Full access without asking": turned on per chat from /panel -> Settings (owners, with a confirmation and a
duration), skips the confirmation card only for an owner's own full-access Claude Code messages, and never for the
local model (Ollama backend, or a job the local model proposes). Claude Code itself is faked: nothing runs."""
import asyncio, json, os, time

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111,222"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B  # noqa  (first: isolates the bot)
from telegram_frontend import FakeTg, msg, tap, check  # noqa  (importing runs nothing: its main() is guarded)
import httpx

OWNER, GUEST, CHAT = 111, 222, 111
started = []


async def fake_start_cc_job(channel, task, snap, user_id, **kw):
    started.append((task, snap.perm, user_id))
    return type("J", (), {"id": "job1"})()


B.start_cc_job = fake_start_cc_job
B.CC_ENABLED = True


def markup(tg):
    last = next(p for m, p, _ in reversed(tg.calls) if m in ("sendMessage", "editMessageText") and p.get("reply_markup"))
    return [b for row in last["reply_markup"]["inline_keyboard"] for b in row]


def button(tg, starts):
    return next(b["callback_data"] for b in markup(tg) if b["text"].startswith(starts))


def labels(tg):
    return [b["text"] for b in markup(tg)]


def text(tg):
    return next(p.get("text", "") for m, p, _ in reversed(tg.calls) if m in ("sendMessage", "editMessageText"))


async def ask(chan, uid=OWNER, task="list my Downloads folder"):
    """One Claude Code message: True if it started right away, False if it showed a confirmation card."""
    n = len(started)
    sent = []

    async def out(**kw): sent.append(kw)
    await B.request_cc(chan, uid, task, B.snapshot(CHAT), B.Out(chan))
    return len(started) > n


async def main():
    tg = FakeTg()
    B._frontends.insert(0, tg)
    B.scheduler.start()
    chan = await B.resolve_channel(CHAT)
    B.update_settings(CHAT, engine="claude", cc_backend="anthropic", cc_perm="full")

    print("== default: full access asks")
    check(not await ask(chan) and "Run with Claude Code?" in text(tg), "confirmation card")
    check(await ask(chan, task="x") is False, "every time")
    B.update_settings(CHAT, cc_perm="edit")
    check(await ask(chan), "edit mode never asked (unchanged)")
    B.update_settings(CHAT, cc_perm="full")

    print("== turning it on (Settings)")
    await tg._dispatch({"message": msg(OWNER, "/panel")})
    await tg._dispatch(tap(OWNER, button(tg, "⚙️")))
    check(any("Full access without asking" in x for x in labels(tg)), "☠️ button in Settings")
    await tg._dispatch(tap(GUEST, button(tg, "☠️")))
    check(B.no_ask_until(CHAT) is None, "a guest can't use it")
    await tg._dispatch(tap(OWNER, button(tg, "☠️")))
    check("Full access without asking?" in text(tg) and B.no_ask_until(CHAT) is None, "asks once, explains the risk")
    await tg._dispatch(tap(GUEST, button(tg, "☠️ Until")))
    check(B.no_ask_until(CHAT) is None, "a guest can't confirm it")
    await tg._dispatch(tap(OWNER, button(tg, "⏲️")))
    until = B.no_ask_until(CHAT)
    check(until and 11.9 * 3600 < until - time.time() <= 12 * 3600, "For 12 hours")
    check("Doesn't ask" in text(tg) and any("Ask again" in x for x in labels(tg)), "Settings shows it, with a way back")

    print("== what it changes")
    check(await ask(chan), "owner's full-access message runs right away")
    check(started[-1][1] == "full", "…with full access")
    check(not await ask(chan, uid=GUEST), "not for anyone else (a non-owner never gets Claude Code without a card)")

    class Ch:
        id = CHAT

    call = {"id": "c1", "type": "function", "function": {"name": "propose_claude_code",
                                                          "arguments": json.dumps({"task": "rm -rf", "reason": "r"})}}
    replies = iter([{"role": "assistant", "content": "", "tool_calls": [call]}, {"role": "assistant", "content": "ok"}])
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": next(replies)}]})))
    shown = []

    async def out(**kw): shown.append(kw)
    n = len(started)
    await B.answer_local(Ch(), OWNER, "clean up", out, model="m", allow_propose=True)
    check(len(started) == n and any(isinstance(k.get("view"), B.CCConfirmView) for k in shown),
          "a job the local model proposes still asks")

    print("== when it stops")
    B.update_settings(CHAT, cc_backend="ollama")
    check(B.get_settings(CHAT)["cc_no_ask_until"] is None and not await ask(chan), "moving to Ollama turns it off")
    B.update_settings(CHAT, cc_backend="anthropic", cc_no_ask_until=B.NO_ASK_FOREVER)
    check(await ask(chan), "Until I turn it off")
    B.update_settings(CHAT, cc_perm="edit")
    B.update_settings(CHAT, cc_perm="full")
    check(B.no_ask_until(CHAT) is None and not await ask(chan), "leaving full access turns it off for good")
    B.update_settings(CHAT, cc_no_ask_until=time.time() - 1)
    check(not await ask(chan), "expired: asks again")
    B.update_settings(CHAT, cc_no_ask_until=B.NO_ASK_FOREVER)
    await tg._dispatch({"message": msg(OWNER, "/panel")})
    await tg._dispatch(tap(OWNER, button(tg, "⚙️")))
    await tg._dispatch(tap(OWNER, button(tg, "☠️ Ask again")))
    check(B.no_ask_until(CHAT) is None and not await ask(chan), "one tap turns it off, no confirmation needed")
    B.update_settings(CHAT, cc_backend="ollama")
    await tg._dispatch({"message": msg(OWNER, "/panel")})
    await tg._dispatch(tap(OWNER, button(tg, "⚙️")))
    check(not any("☠️" in x for x in labels(tg)), "no ☠️ button on the Ollama backend")
    B.scheduler.shutdown(wait=False)
    print("\nALL NO-ASK CHECKS PASSED")


asyncio.run(main())
