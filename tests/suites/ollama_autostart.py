"""At startup the bot starts `ollama serve` when nothing answers on this PC's Ollama port (after a boot, Ollama's own
app isn't running until someone signs in). Nothing is really started: Popen and the HTTP client are faked."""
import asyncio, subprocess

from _setup import B  # noqa  (first: isolates the bot)
import httpx


def check(c, label):
    assert c, label
    print("  ok", label)


started = []
state = {"up": False}


def fake_popen(args, **kw):
    started.append((args, kw))
    state["up"] = True  # the server comes up once started


def handler(request):
    if not state["up"]:
        raise httpx.ConnectError("refused")
    return httpx.Response(200, json={"version": "0.12.0"})


B.subprocess.Popen = fake_popen
B._ollama_exe = lambda: r"C:\Ollama\ollama.exe"
B.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
B.os.environ["DISCORD_TOKEN"] = "secret-token-for-the-test"
B.SCRUB_ENV.add("DISCORD_TOKEN")


async def run(url, ollama_url="http://example.com:11434", up=False, enabled=True):
    started.clear()
    state["up"] = up
    B.LLM_URL, B.OLLAMA_URL, B.OLLAMA_AUTOSTART = url, ollama_url, enabled
    await B.ensure_ollama()
    return started


async def main():
    s = await run("http://localhost:11434/v1")
    check(len(s) == 1 and s[0][0] == [r"C:\Ollama\ollama.exe", "serve"], "local Ollama down: starts `ollama serve`")
    check("DISCORD_TOKEN" not in s[0][1]["env"], "without the bot's secrets in its environment")
    check(s[0][1]["stdout"] is subprocess.DEVNULL, "detached from the bot's output")
    check(not await run("http://localhost:11434/v1", up=True), "already running: left alone")
    check(not await run("http://localhost:1234/v1", ollama_url="http://192.168.1.5:11434"),
          "LM Studio port / remote Ollama: not ours to start")
    check(len(await run("http://example.com/v1", ollama_url="http://127.0.0.1:11434")) == 1,
          "local Ollama for the Claude Code backend counts too")
    check(not await run("http://localhost:11434/v1", enabled=False), "OLLAMA_AUTOSTART=false: never")
    B._ollama_exe = lambda: None
    check(not await run("http://localhost:11434/v1"), "not installed: nothing to start, no crash")


asyncio.run(main())
print("\nALL OLLAMA AUTOSTART CHECKS PASSED")
