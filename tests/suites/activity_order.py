"""Activity feed order: a reply is logged when it finishes, but what it did along the way (a reminder set, a task
scheduled) must read as coming after the request. Events carry `started` (when the request came in); the page sorts
by started-or-ts, while `id` stays in logging order for polling (since=) and paging (before=)."""
import asyncio, json

from _setup import B  # noqa  (first: isolates the bot)
import httpx


def check(c, label):
    assert c, label
    print("  ok", label)


turn = {"n": 0}


def llm(request):
    turn["n"] += 1
    if turn["n"] == 1:  # the model sets a reminder, slowly
        call = {"id": "c1", "type": "function",
                "function": {"name": "set_reminder", "arguments": json.dumps({"when": "in 10 min", "text": "walk"})}}
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}}]})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Reminder set."}}]})


class Ch:
    id = 4242


async def main():
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(llm))
    sent = []

    async def out(**kw): sent.append(kw.get("content"))

    first = B._event_last_id
    await B.answer_local(Ch(), 1, "remind me to walk in 10 min", out, model="m")
    evs = [e for e in B._events if e["id"] > first]
    rem = next(e for e in evs if e["kind"] == "reminder")
    reply = next(e for e in evs if e["kind"] == "local")
    check(rem["id"] < reply["id"], "logging order: the reminder is logged before the reply that set it")
    check(reply["started"] <= rem["ts"] <= reply["ts"], "the reply carries when the request came in")
    at = lambda e: e.get("started") or e["ts"]  # noqa: E731  (the page's order, newest first)
    feed = sorted(evs, key=lambda e: (at(e), e["id"]), reverse=True)
    check(feed.index(rem) < feed.index(reply), "feed (newest first): the reminder shows above its request")

    job = B.CCJob(channel=None, task="t", snap=B.snapshot(4242), user_id=1)
    check(abs(job.asked_at - __import__("time").time()) < 5, "Claude Code jobs remember when they were asked")
    rows = B.STORE.recent_events(50)
    check(any(r.get("started") == reply["started"] for r in rows), "started survives the store (history after restart)")


asyncio.run(main())
print("\nALL ACTIVITY ORDER CHECKS PASSED")
