import asyncio, json, sys, tempfile
from pathlib import Path
import httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
B.is_telegram_id = lambda x: False  # these tests use small fake Discord ids

SP = TMP
tmp = Path(tempfile.mkdtemp())
B.TASKS_FILE = tmp / "tasks.json"; B._tasks.clear()
B.SETTINGS_FILE = tmp / "settings.json"
ok = 0
def check(cond, label):
    global ok
    if not cond:
        raise AssertionError(label)
    ok += 1
    print("  ok", label)

print("== text utils")
check(B.strip_think("<think>hmm</think>\nHello") == "Hello", "strip think block")
check(B.strip_think("reasoning...</think>Answer") == "Answer", "strip orphan close")
check(B.strip_think("Hi<think>unterminated") == "Hi", "strip unterminated")
long = "intro\n```python\n" + "\n".join(f"x = {i}  # " + "a" * 60 for i in range(80)) + "\n```\nbye"
chunks = B.split_message(long)
check(all(len(c) <= 2000 for c in chunks) and len(chunks) > 1, f"split sizes {[len(c) for c in chunks]}")
check(all(c.count("```") % 2 == 0 for c in chunks), "fences balanced per chunk")
check(len(B.split_message("y" * 5000)) == 3, "hard split long line")

print("== cron")
check(B.normalize_crontab("0 9 * * 0") == "0 9 * * sun", "dow 0 -> sun")
check(B.normalize_crontab("0 9 * * 1-5") == "0 9 * * mon-fri", "dow range")
check(B.normalize_crontab("*/15 * * * */2") == "*/15 * * * */2", "dow step untouched")
check(B.normalize_crontab("0 9 * * 7") == "0 9 * * sun", "dow 7 -> sun")
t = B.validate_cron("0 9 * * 0")
check(t.get_next_fire_time(None, B.now_local()).strftime("%a") == "Sun", "0 = Sunday fires on Sunday")
for bad in ["*/5 * * * *", "0,5 9 * * *", "* * * * *", "0 9 * *", "61 * * * *"]:
    try:
        B.validate_cron(bad); check(False, f"should reject {bad}")
    except ValueError as e:
        check(True, f"rejects '{bad}': {e}")
for good in ["*/15 * * * *", "0 9 * * 1-5", "30 8 1 * *", "0 */2 * * *"]:
    B.validate_cron(good); check(True, f"accepts '{good}'")

print("== ssrf")
async def ssrf():
    for u in ["http://localhost/", "http://127.0.0.1:8080/", "http://10.0.0.5/", "http://192.168.1.1/",
              "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://foo.localhost/",
              "file:///etc/passwd", "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"]:
        try:
            await B.assert_public_url(u); check(False, f"should block {u}")
        except B.UnsafeURL as e:
            check(True, f"blocks {u} ({e})")
    await B.assert_public_url("https://example.com/"); check(True, "allows example.com")
asyncio.run(ssrf())

print("== stream-json parser (real CLI sample)")
p = B.StreamParser()
lines = []
for raw in (FIXTURES / "sample.jsonl").read_text(encoding="utf-8-sig").splitlines():
    lines += p.feed_line(raw)
for l in lines: print("    |", l)
check(p.session_id and p.result and p.result["type"] == "result", "session + result captured")
check(any(l.startswith("🟢") for l in lines) and any("Read" in l for l in lines), "init + tool lines")
check(any(l.startswith("⛔") for l in lines) and not any(l.startswith("⚠️") for l in lines), "denial shown once")
check(p.result["permission_denials"][0]["tool_name"] == "Write", "denials present")

print("== cc command/env")
os_env = B.os.environ
os_env["DISCORD_TOKEN"] = "secret"; os_env["LLM_API_KEY"] = "secret2"
snap = B.CCSnap("ollama", "qwen3.5:9b", "read", "default", resume="abc")
cmd = B.build_cc_command("claude", snap)
print("   ", cmd)
check(cmd[cmd.index("--resume") + 1] == "abc" and cmd[-1] == "mcp__bot__save_skill", "resume + allowedTools last")
env = B.build_cc_env(snap)
check("DISCORD_TOKEN" not in env and "LLM_API_KEY" not in env, "secrets scrubbed")
check(env["ANTHROPIC_BASE_URL"] == "http://localhost:11434" and env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "qwen3.5:9b", "ollama env")
check("--permission-mode" in B.build_cc_command("c", B.CCSnap("anthropic", "sonnet", "full", "default")) and "bypassPermissions" in B.build_cc_command("c", B.CCSnap("anthropic", "sonnet", "full", "default")), "full profile")

print("== local engine tool loop (mock LLM server)")
calls = []
def handler(req: httpx.Request):
    body = json.loads(req.content)
    calls.append(body)
    n = len(calls)
    if n == 1:
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "schedule_task", "arguments": json.dumps(
                {"cron": "0 8 * * *", "prompt": "Summarize AI news", "description": "AI news"})}},
            {"id": "c2", "type": "function", "function": {"name": "propose_claude_code", "arguments": {"task": "fix tests", "reason": "needs repo"}}},
        ]}}]})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "<think>ok</think>Done!"}}]})

async def local():
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    B.scheduler.start()
    ctx = B.ToolCtx(42, 1, B.CHAT_TOOLS + ["propose_claude_code"])
    r = await B.run_local("remind me daily", "m", ctx, [])
    check(r.text == "Done!" and r.tools_used == ["schedule_task", "propose_claude_code"], f"result {r}")
    check(r.proposal == {"task": "fix tests", "reason": "needs repo"}, "proposal captured")
    check(any(m["role"] == "tool" for m in calls[1]["messages"]), "tool messages sent back")
    check("Asia/Kolkata" in calls[0]["messages"][0]["content"] and calls[0]["messages"][-1]["content"].startswith("[Now: ")
          and r.sent == calls[0]["messages"][-1]["content"], "timezone in system prompt, time in the user message")
    saved = json.loads(B.TASKS_FILE.read_text())
    check(len(saved) == 1 and saved[0]["channel_id"] == 42, "task persisted")
    # Read-only ctx cannot schedule
    msg = await B.execute_tool("schedule_task", {"cron": "0 8 * * *", "prompt": "x"}, B.ToolCtx(1, 1, B.READONLY_TOOLS))
    check("not available" in msg, "scheduled runs can't create tasks")
    # tool cap
    calls.clear()
    def always_tool(req):
        calls.append(json.loads(req.content))
        if "tools" not in calls[-1]:
            return httpx.Response(200, json={"choices": [{"message": {"content": "forced final"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "x", "function": {"name": "list_tasks", "arguments": "{}"}}]}}]})
    B.http = httpx.AsyncClient(transport=httpx.MockTransport(always_tool))
    r = await B.run_local("loop", "m", B.ToolCtx(1, 1, B.CHAT_TOOLS), None)
    check(r.text == "forced final" and len(calls) == B.MAX_TOOL_ROUNDS + 1, f"tool cap -> forced answer ({len(calls)} calls)")
    # cancel permissions
    tid = saved[0]["id"]
    check("Only the task's creator" in B.cancel_task(tid, 999), "non-creator can't cancel")
    B.OWNER_IDS.add(7)
    check("Cancelled" in B.cancel_task(tid, 7), "owner can cancel")
    # min interval via tool
    msg = await B.execute_tool("schedule_task", {"cron": "*/5 * * * *", "prompt": "x"}, B.ToolCtx(1, 1, B.CHAT_TOOLS))
    check("more often" in msg, "tool rejects 5-min cron")

    print("== views")
    B.WORKSPACES["other"] = tmp
    B.add_task("0 9 * * 1-5", "News", "News", 42, 1)
    pv = B.PanelView(42, [f"model{i}" for i in range(30)], B.ANTHROPIC_MODELS)
    comps = pv.to_components()
    check(len(comps) == 5 and len(comps[1]["components"][0]["options"]) == 25, "panel: 5 rows, model list capped at 25")
    for v in [B.PermView(42), B.TasksView(), B.LocalReplyView(42, "hi", "m", True),
              B.CCConfirmView(42, "task", B.snapshot(42)), ]:
        v.to_components()
    job = B.CCJob(channel=None, task="t", snap=B.snapshot(42), user_id=1)
    B.CCProgressView(job).to_components(); B.CCResultView(job).to_components()
    check(True, "all views construct")
    e = B.panel_embed(42); check(len(e.fields) == 11, "panel embed fields")
    B.confirm_embed("t", B.CCSnap("anthropic", "opus", "full", "default"))
    B.tasks_embed(); B.perm_embed(42)
    job.parser = p; job.status = "done"
    emb, f = B.result_embed(job)
    print("   result embed:", emb.title, [x.name for x in emb.fields])
    check(any("Blocked" in x.name for x in emb.fields), "result embed shows denials")
    B.transcript_file(job)
    # settings: backend change resets session
    B.update_settings(42, cc_session="s1"); B.update_settings(42, cc_backend="ollama")
    check(B.get_settings(42)["cc_session"] is None, "backend change resets session")
    B.update_settings(42, cc_session="s2"); B.update_settings(42, cc_perm="read")
    check(B.get_settings(42)["cc_session"] == "s2", "perm change keeps session")

    print("== small local models: stopped early, small window, memory of runs")
    oj = B.CCJob(channel=None, task="Create Rudrashish_Das.txt", snap=B.CCSnap("ollama", "qwen3.5:9b", "full", "default"),
                 user_id=1)
    promise = "I'll create a simple, readable text file with the requested information in the workspace directory."
    check(B.stalled(oj, promise), "announced a step, no tool call: stalled")
    check(not B.stalled(oj, "Created the file. Let me know if you need anything else."), "sign-off is not a stall")
    check(not B.stalled(oj, "Done, I'll remind you at 9.\n[[remind: 09:00 | stretch]]"), "marker reply is not a stall")
    oj.parser.tool_calls = 1
    check(not B.stalled(oj, promise), "a run that used tools is not a stall")
    oj.parser.tool_calls = 0
    aj = B.CCJob(channel=None, task="t", snap=B.CCSnap("anthropic", "sonnet", "full", "default"), user_id=1)
    check(not B.stalled(aj, promise), "Anthropic models aren't checked")
    oj.parser.first_context, oj.ctx_limit = 26452, 32768
    check("OLLAMA_CONTEXT_LENGTH" in (B.small_window_note(oj) or "") and "26k of 32k" in B.small_window_note(oj),
          "warns when setup fills most of the window")
    oj.ctx_limit = 65536
    check(B.small_window_note(oj) is None, "no warning with room left")
    oj.snap, oj.ctx_limit = B.CCSnap("ollama", "qwen3.5:9b", "full", "default", resume="abc"), 32768
    check(B.small_window_note(oj) is None, "warns once per session, not on every message")

    class Ch: id = 77
    oj.channel, oj.parser.result = Ch(), {"result": promise}
    B._history.pop(77, None)
    B.update_settings(77, engine="claude"); B.remember_cc(oj, promise)
    check(77 not in B._history, "Claude Code engine: local memory untouched")
    B.update_settings(77, engine="auto"); B.remember_cc(oj, promise)
    h = B._history.get(77) or []
    check(len(h) == 2 and "Rudrashish_Das.txt" in h[0]["content"] and h[1]["content"].startswith("[Claude Code: success, no tool calls]"),
          "Auto engine: the run goes into the local model's memory")
    B.scheduler.shutdown(wait=False)

asyncio.run(local())
print(f"\nALL {ok} CHECKS PASSED")



