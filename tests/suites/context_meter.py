import asyncio, re, sys, tempfile
from pathlib import Path
import httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
B.is_telegram_id = lambda x: False  # these tests use small fake Discord ids

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "s.json"; B.USAGE_FILE = tmp / "u.json"; B.TASKS_FILE = tmp / "t.json"; B.REMINDERS_FILE = tmp / "r.json"
B._tasks.clear(); B._reminders.clear(); B._usage.clear()
WS = TMP / "ws"; WS.mkdir(exist_ok=True)
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
B.OWNER_IDS.clear(); B.OWNER_IDS.add(1); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.add(1)
CH = 2222


def check(c, label):
    assert c, label
    print("  ok", label)


class Typ:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


class Ch:
    id = CH
    def typing(self): return Typ()
    async def send(self, content=None, **kw): pass


async def say(text):
    msgs = []

    async def out(**k): msgs.append(k)
    out.interaction = None
    await B.handle_prompt(Ch(), 1, text, out)
    job = B._last_job.get(CH)
    while job and job.status != "done":
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    reply = msgs[-1]["content"]
    print("     ", repr(reply[-230:]))
    return reply


def unit():
    print("== thresholds")
    check(B.compact_at(32768) == 24576 and B.compact_at(None) == B.CC_COMPACT_HINT_TOKENS, "compact hint at 75% of a 32k window")
    job = B.CCJob(channel=Ch(), task="x", snap=B.CCSnap("ollama", "qwen3.5:9b", "read", "default"), user_id=1)
    job.parser.context_tokens, job.ctx_limit, job.ctx_approx = 30000, 32768, False
    line = B.chat_stats(job, [])
    check("30k/32k ctx" in line and "almost full" in line, f"near the limit: {line}")
    job.parser.context_tokens = 26000
    check("long chat, send /compact" in B.chat_stats(job, []), "75%: suggest /compact")


async def live():
    B.http = httpx.AsyncClient()
    print("== Claude Code on Ollama: memory across messages")
    B.update_settings(CH, engine="claude", cc_backend="ollama", cc_model="qwen3.5:9b", cc_perm="read", style="chat")
    r1 = await say("Remember this: my favourite colour is teal. Just say OK.")
    r2 = await say("What is my favourite colour? One word.")
    s1, s2 = re.search(r"session (\w+)", r1).group(1), re.search(r"session (\w+)", r2).group(1)
    check(s1 == s2 and "teal" in r2.lower(), f"same session {s1}, remembers teal")
    c1, c2 = (int(re.search(r"(\d+)k/32k ctx", r).group(1)) for r in (r1, r2))
    check(3 <= c1 <= c2 <= 20, f"context {c1}k -> {c2}k of 32k")
    print("== plain local engine: memory counter")
    B.update_settings(CH, engine="local")
    B._history.pop(CH, None)
    r1 = await say("Remember this: my dog is called Biscuit. Just say OK.")
    r2 = await say("What is my dog called? One word.")
    check("memory 1/6" in r1 and "memory 2/6" in r2 and "biscuit" in r2.lower(), "memory 1/6 -> 2/6, remembers Biscuit")
    check(re.search(r"~\d+k/32k ctx", r2) and "session" not in r2, "local engine: ctx shown, no session (it has none)")
    await B.unload_all()


async def main():
    unit()
    if LIVE:  # needs Ollama running
        await live()
    print("\nALL V22 CHECKS PASSED")

asyncio.run(main())
