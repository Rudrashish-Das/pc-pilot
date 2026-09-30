"""Scheduled tasks run on a pinned engine + model, with per-task permissions: pinning at creation, who may change
what (update_task), what a run actually gets (tools, snapshot, reminders), and the /tasks screens end to end through
the Telegram front end. Nothing real runs: the model server and Claude Code are faked."""
import asyncio, json, os

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111,222"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B  # noqa  (first: isolates the bot)
from telegram_frontend import FakeTg, msg, tap, check  # noqa  (importing runs nothing: its main() is guarded)
import httpx

OWNER, GUEST, CHAT = 111, 222, 111
LOCAL_MODELS = ["qwen3.5:9b", "gemma3:4b"]
sent_models, cc_runs = [], []


def llm(request):
    if request.url.path.endswith("/api/tags"):
        return httpx.Response(200, json={"models": [{"name": m} for m in LOCAL_MODELS]})
    body = json.loads(request.content)
    sent_models.append((body["model"], sorted(t["function"]["name"] for t in body.get("tools") or [])))
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "done"}}]})


async def fake_start_cc_job(channel, task, snap, user_id, **kw):
    cc_runs.append((snap, kw))


B.http = httpx.AsyncClient(transport=httpx.MockTransport(llm))
B.start_cc_job = fake_start_cc_job
B.CC_ENABLED = True


def new_task(uid=OWNER, **kw):
    return B.add_task("0 9 * * *", "news brief", "news", CHAT, uid, **kw)


def markup(tg):
    last = next(p for m, p, _ in reversed(tg.calls) if m in ("sendMessage", "editMessageText") and p.get("reply_markup"))
    return [b for row in last["reply_markup"]["inline_keyboard"] for b in row]


def button(tg, starts):
    return next(b["callback_data"] for b in markup(tg) if b["text"].lstrip("✓ ").startswith(starts))


def option(tg, menu_starts, value):
    """Tap target for one option of a select: <tok>:<i>:<j>."""
    tok, i, _ = button(tg, menu_starts).split(":")
    sel = tg._views[tok].view.children[int(i)]
    return f"{tok}:{i}:{[o.value for o in sel.options].index(value)}"


def text(tg):
    return next(p.get("text", "") for m, p, _ in reversed(tg.calls) if m in ("sendMessage", "editMessageText"))


async def main():
    tg = FakeTg()
    B._frontends.insert(0, tg)
    B.scheduler.start()
    B.update_settings(CHAT, local_model="qwen3.5:9b", cc_backend="anthropic", cc_model="sonnet")

    print("== pinned when created")
    t = new_task()
    check(t["engine"] == "local" and t["model"] == "qwen3.5:9b" and t["perm"] == "read" and not t["reminders"],
          "defaults: the chat's local model, read-only, no reminders")
    B.update_settings(CHAT, local_model="gemma3:4b")
    check(B._tasks[t["id"]]["model"] == "qwen3.5:9b", "changing the chat's model doesn't move the task")
    ctx = B.ToolCtx(CHAT, OWNER, B.CHAT_TOOLS, model="llama3:8b")
    await B.execute_tool("schedule_task", {"cron": "0 8 * * *", "prompt": "weather", "description": "w"}, ctx)
    check(any(x["model"] == "llama3:8b" for x in B._tasks.values()), "the local model schedules tasks on itself")
    snap = B.CCSnap("ollama", "qwen3.5:9b", "edit", next(iter(B.WORKSPACES)))
    B.extract_tasks("ok [[task: cron 0 7 * * * | prices]]", CHAT, OWNER, snap)
    cc = next(x for x in B._tasks.values() if x["prompt"] == "prices")
    check((cc["engine"], cc["backend"], cc["model"], cc["perm"]) == ("claude", "ollama", "qwen3.5:9b", "read"),
          "Claude Code's [[task:]] pins its own backend and model, but starts read-only")
    old = {"id": "old001", "cron": "0 6 * * *", "at": None, "engine": "claude", "prompt": "p", "description": "d",
           "channel_id": CHAT, "created_by": OWNER, "created_at": "2026-01-01T00:00:00+05:30"}
    B._tasks["old001"] = old
    B.load_tasks()
    check(old["backend"] == "anthropic" and old["model"] == "sonnet" and old["perm"] == "read",
          "a task from before pinning takes the chat's current Claude Code setup")

    print("== who may change what")
    g = new_task(GUEST)
    check("No task" in await B.update_task(t["id"], GUEST, engine="local", model="gemma3:4b"),
          "a guest can't change (or even see) someone else's task")
    check("✅" in await B.update_task(g["id"], GUEST, engine="local", model="gemma3:4b") and g["model"] == "gemma3:4b",
          "the creator can move their task to another listed local model")
    check("isn't one of the server's models" in await B.update_task(g["id"], GUEST, engine="local", model="paid-xl"),
          "…but not to a model the server doesn't list")
    for kw, why in ((dict(engine="claude", backend="anthropic", model="opus"), "Claude Code"),
                    (dict(perm="edit"), "permissions"), (dict(reminders=True), "reminders")):
        check("⛔" in await B.update_task(g["id"], GUEST, **kw), f"only owners: {why}")
    check("only gets web search" in await B.update_task(g["id"], OWNER, perm="edit"), "the local model never gets more")
    check("✅" in await B.update_task(g["id"], OWNER, engine="claude", backend="anthropic", model="haiku")
          and g["approved_by"] == OWNER, "an owner puts a guest's task on Claude Code (and is recorded as approving)")
    check("✅" in await B.update_task(cc["id"], OWNER, perm="edit"), "edit on the Ollama backend is fine")
    check("never gets a shell" in await B.update_task(cc["id"], OWNER, perm="full") and cc["perm"] == "edit",
          "full access on the Ollama backend is refused")
    await B.update_task(old["id"], OWNER, perm="full")
    check("never gets a shell" in await B.update_task(old["id"], OWNER, engine="claude", backend="ollama", model="q")
          and old["backend"] == "anthropic", "…and a full-access task can't move onto Ollama")
    check("✅" in await B.update_task(old["id"], OWNER, engine="local", model="gemma3:4b")
          and old["perm"] == "read" and "backend" not in old, "moving to local drops Claude Code's permissions")

    print("== what a run gets")
    await B.update_task(t["id"], OWNER, reminders=True)
    await B.run_scheduled_task(t["id"])
    check(sent_models[-1] == ("qwen3.5:9b", ["fetch_page", "list_reminders", "set_reminder", "web_search"]),
          f"local: the pinned model, web tools + reminders, never schedule_task: {sent_models[-1]}")
    t["approved_by"], t["created_by"] = 999, GUEST  # nobody behind it is an owner any more
    await B.run_scheduled_task(t["id"])
    check(sent_models[-1][1] == ["fetch_page", "web_search"] and "without reminders" in text(tg),
          "owner approval re-checked every run")
    t["created_by"] = OWNER
    await B.run_scheduled_task(cc["id"])
    s, kw = cc_runs[-1]
    check((s.backend, s.model, s.perm, s.resume) == ("ollama", "qwen3.5:9b", "edit", None) and kw["scheduled"],
          "Claude Code: the task's backend, model and permission, fresh session")
    cc["perm"] = "full"  # even if it got into the file somehow
    await B.run_scheduled_task(cc["id"])
    check(cc_runs[-1][0].perm == "edit", "full never reaches Claude Code on Ollama at run time")
    await B.run_scheduled_task(g["id"])
    check(cc_runs[-1][0].model == "haiku", "a guest's task an owner moved to Claude Code runs there")
    g["approved_by"] = 999
    n = len(cc_runs)
    await B.run_scheduled_task(g["id"])
    check(len(cc_runs) == n and "needs an owner" in text(tg), "…until that approval stops counting: local instead")

    print("== a scheduled Claude Code job and reminders")
    chan = await B.resolve_channel(CHAT)
    job = B.CCJob(channel=chan, task="t", snap=snap, user_id=OWNER, scheduled=True, reminders_ok=False)
    job.parser.result = {"type": "result", "subtype": "success", "is_error": False,
                         "result": "Rain later. [[remind: in 2 hours | umbrella]] [[task: in 1 hour | again]]"}
    n_rem, n_task = len(B._reminders), len(B._tasks)
    await B._finalize(job)
    check(len(B._reminders) == n_rem and len(B._tasks) == n_task and any("isn't allowed to set reminders" in x
          for x in job.notices), "not allowed: no reminder, no task")
    job = B.CCJob(channel=chan, task="t", snap=snap, user_id=OWNER, scheduled=True, reminders_ok=True)
    job.parser.result = {"type": "result", "subtype": "success", "is_error": False,
                         "result": "Rain later. [[remind: in 2 hours | umbrella]] [[task: in 1 hour | again]]"}
    await B._finalize(job)
    check(len(B._reminders) == n_rem + 1 and len(B._tasks) == n_task and any("can't create tasks" in x
          for x in job.notices), "allowed: the reminder is set, the task still isn't")

    print("== /tasks screens (Telegram)")
    g.update(engine="local", model="qwen3.5:9b", perm="read", approved_by=None)
    g.pop("backend", None), g.pop("workspace", None)
    await tg._dispatch({"message": msg(GUEST, "/tasks")})
    await tg._dispatch(tap(GUEST, option(tg, "⚙️", g["id"])))
    check(g["id"] in text(tg) and "news brief" in text(tg), "a guest opens their own task")
    labels = [b["text"] for b in markup(tg)]
    check(not any("Reminders" in x or "May" in x for x in labels), f"guest: no permission or reminder controls: {labels}")
    tok, i, _ = button(tg, "💻").split(":")
    sel = tg._views[tok].view.children[int(i)]
    check(all(o.value.startswith("local|") for o in sel.options), "guest: only local models to pick from")
    sel.options.append(B.discord.SelectOption(label="x", value="claude|anthropic|opus"))  # a crafted choice
    await tg._dispatch(tap(GUEST, f"{tok}:{i}:{len(sel.options) - 1}"))
    check(g["engine"] == "local" and "Only owners" in text(tg), "a crafted Claude Code choice is refused by update_task")

    await tg._dispatch({"message": msg(OWNER, "/tasks")})
    await tg._dispatch(tap(OWNER, option(tg, "⚙️", t["id"])))
    await tg._dispatch(tap(OWNER, option(tg, "💻", "claude|anthropic|sonnet")))
    check(t["engine"] == "claude" and t["model"] == "sonnet", "owner moves it to Claude Code sonnet")
    await tg._dispatch(tap(OWNER, option(tg, "🔍", "full")))
    check(t["perm"] == "read" and "full access?" in text(tg), "full access asks first")
    await tg._dispatch(tap(GUEST, button(tg, "⚠️ Yes")))
    check(t["perm"] == "read", "a guest can't confirm it")
    await tg._dispatch(tap(OWNER, button(tg, "⚠️ Yes")))
    check(t["perm"] == "full" and "Full access" in text(tg), "owner confirms: full access")
    await tg._dispatch(tap(OWNER, button(tg, "🔕")))
    check(not t["reminders"], "reminders toggle")
    B.scheduler.shutdown(wait=False)
    print("\nALL TASK PERMISSION CHECKS PASSED")


asyncio.run(main())
