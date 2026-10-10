"""Failures come with a "💡" suggestion from fixed rules (llmbot/hints.py, no model involved): the rule table against
error texts shaped like the real ones, then the local engine, Claude Code results and /power end to end."""
import asyncio, json

from _setup import B  # noqa  (first: isolates the bot)
import httpx
from llmbot.hints import suggest


def check(c, label):
    assert c, label
    print("  ok", label)


def has(text, *words):
    return text is not None and all(w.lower() in text.lower() for w in words)


print("== rules")
cases = [  # (error text, where, words the suggestion must contain)
    ("ConnectError: All connection attempts failed", "local", ["ollama serve", "http://x:1/v1"]),
    ("LLM server returned HTTP 404: {\"error\":\"model 'qwen9:x' not found\"}", "local", ["ollama pull qwen9:x"]),
    ("HTTP 500: model requires more system memory (9.1 GiB) than is available", "local", ["/unload", "smaller"]),
    ("HTTP 500: llama runner process has terminated: exit status 2", "local", ["Restart Ollama"]),
    ("ReadTimeout", "local", ["took too long"]),
    ("Invalid API key · Please run /login", "claude", ["run `claude`", "/login"]),
    ("Failed to authenticate: OAuth session expired and could not be refreshed", "claude", ["run `claude`", "/login"]),
    ("Credit balance is too low", "claude", ["console.anthropic.com", "Ollama"]),
    ('API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}', "claude", ["Retry"]),
    ("Claude AI usage limit reached|1790780000", "claude", ["usage limit"]),
    ("Prompt is too long", "claude", ["/compact"]),
    ("Timed out after 900s.", "claude", ["CLAUDE_TIMEOUT=900s"]),
    ("[WinError 2] The system cannot find the file specified", "claude", ["CLAUDE_BIN"]),
    ("Claude Code exited (code 1) without a result.", "claude", ["/log"]),
    ("Unable to abort the system shutdown ... (1190)", "power", ["Cancel"]),
    ("Access is denied.(5)", "power", ["signed-in user"]),
    ("[Errno 11001] getaddrinfo failed", "local", ["offline"]),
    ("403 Forbidden (error code: 50013): Missing Permissions", "", ["Edit Channel"]),
    ("InvalidDataError: Invalid data found when processing input", "voice", ["voice note"]),
]
for text, where, words in cases:
    got = suggest(text, where=where, model="qwen9:x", llm_url="http://x:1/v1", claude_timeout=900)
    assert has(got, *words), (text, where, got)
print(f"  ok {len(cases)} error texts -> the right suggestion")
check(suggest("ConnectTimeout: connect timed out", where="local", llm_url="u") is not None
      and "took too long" not in suggest("httpx.ConnectTimeout", where="local", llm_url="u"),
      "a connect timeout says 'start Ollama', not 'took too long'")
check(suggest("everything went fine, 401 files processed", where="claude") is None, "numbers inside normal text don't match")
check(suggest("something nobody has seen before", where="claude") is None, "unknown errors: no made-up advice")
check(suggest("Credit balance is too low", where="local") is None, "rules only fire where they apply")


async def main():
    print("== local engine: server down, model missing")
    sent = []

    async def out(**kw): sent.append(kw["content"])

    class Ch:
        id = 4242
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("All connection attempts failed"))))
    await B.answer_local(Ch(), 1, "hi", out, model="qwen3.5:9b")
    check("💡" in sent[-1] and "ollama serve" in sent[-1], f"server down: {sent[-1]!r}")
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, json={"error": "model 'nope:1b' not found"})))
    await B.answer_local(Ch(), 1, "hi", out, model="nope:1b")
    check("ollama pull nope:1b" in sent[-1], f"model missing: {sent[-1]!r}")

    print("== Claude Code results")
    def job(result=None, error=None, stderr=""):
        j = B.CCJob(channel=None, task="t", snap=B.snapshot(42), user_id=1)
        j.parser.result, j.error, j.stderr_tail = result, error, stderr
        return j
    text, _, _ = B.result_text(job({"type": "result", "is_error": True, "result": "Credit balance is too low"}))
    check("💡" in text and "console.anthropic.com" in text, "card/chat text: out of credit")
    text, _, _ = B.result_text(job(error="Timed out after 900s."))
    check(f"CLAUDE_TIMEOUT={B.CLAUDE_TIMEOUT}s" in text, "timeout names the real setting")
    text, _, _ = B.result_text(job(error="Claude Code exited (code 1) without a result.",
                                   stderr="Invalid API key · Please run /login"))
    check("run `claude`" in text, "reads the CLI's stderr too")
    text, _, _ = B.result_text(job({"type": "result", "is_error": False, "result": "Rate limits are 429 per hour."}))
    check("💡" not in text, "a successful answer never gets a hint")

    print("== /power")
    B.power_supported = lambda: True

    async def refuse(*cmd): return "Access is denied.(5)"
    B._run_cmd = refuse
    err = await B.do_power("restart", 42, 1)
    msg = B.with_hint(f"⚠️ Couldn't restart: {err}", where="power")
    check("signed-in user" in msg, "power failure explained")


asyncio.run(main())
print("\nALL FAILURE HINT CHECKS PASSED")
