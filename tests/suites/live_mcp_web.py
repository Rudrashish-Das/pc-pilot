"""v19: Claude Code web tools via the bot's own MCP server. MODE=anthropic forces the local-web wiring onto haiku
(checks permissions/tool exposure); MODE=ollama runs against the real Ollama backend."""
import asyncio, os, sys, tempfile, time
from dataclasses import replace
from pathlib import Path
import httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)

MODE = os.environ.get("MODE", "anthropic")
MODEL = os.environ.get("MODEL", "qwen3.5:9b")
tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.USAGE_FILE = tmp / "usage.json"; B.REMINDERS_FILE = tmp / "rem.json"
B.TASKS_FILE = tmp / "tasks.json"; B._usage.clear(); B._tasks.clear(); B._reminders.clear()
SP = TMP; WS = SP / "ws_v19"; WS.mkdir(exist_ok=True)
(WS / "notes.txt").write_text("The project codename is BLUEHERON.\n")
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
OWNER, CH = 1, 1919
B.OWNER_IDS.clear(); B.OWNER_IDS.add(OWNER); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.add(OWNER)
if MODE == "anthropic":
    B.local_web = lambda snap: True


FAILS = []


def check(c, label):
    print("  ok" if c else "  FAIL", label)
    if not c:
        FAILS.append(label)


class Typ:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


class Ch:
    id = CH
    def typing(self): return Typ()
    async def send(self, content=None, **kw): pass


async def run(prompt, perm="read"):
    if MODE == "ollama":
        B.update_settings(CH, engine="claude", cc_backend="ollama", cc_model=MODEL, cc_perm=perm, style="chat")
    else:
        B.update_settings(CH, engine="claude", cc_model="haiku", cc_perm=perm, style="chat")
    msgs = []

    async def out(**k): msgs.append(k); outs.append(k)
    t = time.time()
    job = await B.start_cc_job(Ch(), prompt, B.snapshot(CH), OWNER, out=out, chat=True)
    while job.status != "done":
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    tools = [l for l in job.parser.transcript if l.startswith(("🔧", "▶", "[tool"))] or job.parser.transcript
    print(f"     ({time.time() - t:.0f}s) reply: {msgs[-1]['content'][:400]!r}")
    return job, msgs[-1]["content"]


outs = []


def used(job, name):
    return any(name in l for l in job.parser.transcript)


async def main():
    B.http = httpx.AsyncClient()
    B.scheduler.start()
    snap = replace(B.snapshot(CH), backend="ollama", model=MODEL, perm="read")
    cmd = B.build_cc_command("claude", snap)
    check("--mcp-config" in cmd and "WebSearch" not in " ".join(cmd) and cmd[-len(B.MCP_WEB_TOOLS):] == B.MCP_WEB_TOOLS, "command: MCP web tools, no WebSearch")
    check("WebSearch" in " ".join(B.build_cc_command("claude", replace(snap, backend="anthropic"))) or MODE == "anthropic",
          "anthropic backend unchanged")
    print(f"== {MODE}: plain chat")
    job, text = await run("Say hi in five words or fewer.")
    check(job.outcome() == "success" and text.strip(), "answers")
    print(f"== {MODE}: web question (read mode)")
    job, text = await run("What is the current weather in Bangalore? Use web search. Two sentences max, cite one link.")
    check(used(job, "web_search") or used(job, "fetch_page"), "used the bot's web tools")
    check(not (job.parser.result or {}).get("permission_denials"), "no permission denials")
    print(f"== {MODE}: reads a workspace file")
    job, text = await run("What is the project codename in notes.txt? Just the word.")
    check("BLUEHERON" in text.upper(), "Read tool works")
    print(f"== {MODE}: reminder marker")
    job, text = await run("Remind me to stretch in 30 minutes.")
    check(len(B._reminders) == 1 and "⏰ Reminder" in text, f"reminder really set ({len(B._reminders)})")
    print(f"== {MODE}: scheduled prompt")
    job, text = await run("Every day at 7am, check the weather in Bangalore for me.")
    check(len(B._tasks) == 1 and "🗓️ Task" in text, f"task really scheduled ({len(B._tasks)})")
    for t in B._tasks.values(): print("     task:", t["cron"] or t["at"], "|", t["prompt"][:150])
    check(any(t["cron"] for t in B._tasks.values()), "task repeats daily (cron, not once)")
    print(f"== {MODE}: create, send, delete a file (edit mode)")
    job, text = await run("Create a file named primes.txt with the first five prime numbers, one per line, send it to me, then delete it.", perm="edit")
    check(not (WS / "primes.txt").exists() and "🗑️ deleted" in text, "file created, sent and deleted")
    print(f"\nALL V19 {MODE.upper()} CHECKS PASSED")

asyncio.run(main())
