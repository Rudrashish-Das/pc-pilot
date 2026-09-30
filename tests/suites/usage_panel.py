"""/usage: plan limits from Claude Code's rate_limit_event, the model's context window from modelUsage, the stats
line warning, and the panel on Discord/Telegram. LLMBOT_LIVE=1 also runs a real "check now" (tiny Haiku call)."""
import asyncio, json, time
from types import SimpleNamespace

from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)

B.USAGE_FILE = TMP / "usage.json"
B._usage.clear()
CHAT = 111


def check(c, label):
    assert c, label
    print("  ok", label)


print("== the stream's rate_limit_event and modelUsage")
p = B.StreamParser()
for line in (FIXTURES / "sample.jsonl").read_text(encoding="utf-8").splitlines():
    p.feed_line(line)
check(p.rate_limit and p.rate_limit["unifiedWindows"]["seven_day"]["utilization"] == 0.2, "event captured")
check(B.context_window(p.result, p.model) == 200000, "context window from modelUsage")
check(B.context_window({"modelUsage": {"claude-sonnet-5-5": {"contextWindow": 1000000},
                                       "claude-haiku-4-5": {"contextWindow": 200000}}}, "claude-sonnet-5-5") == 1000000,
      "the main model's window, not a helper model's")
check(B.context_window(None, None) is None, "unknown -> None")

print("== plan windows")
now = time.time()
B.note_plan({"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.91, "isUsingOverage": False,
             "unifiedWindows": {"five_hour": {"utilization": 0.91, "resetsAt": now + 7560},
                                "seven_day": {"utilization": 0.44, "resetsAt": now + 3 * 86400}}})
w = B.plan_windows()
check([x[0] for x in w] == ["5-hour limit", "Weekly · all models"] and w[0][1] == 0.91, f"labels and order: {w}")
check(B.plan_warning().startswith("5-hour limit 91% used, resets <t:"), f"warning past 80%: {B.plan_warning()}")
B.note_plan({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": now - 5}}})
check(B.plan_windows()[0][1] == 0.0 and B.plan_windows()[1][1] == 0.44, "a window past its reset reads 0; others kept")
check(B.plan_warning() is None, "no warning under 80%")
B.note_plan({"status": "rejected", "rateLimitType": "seven_day", "utilization": 1.0, "resetsAt": now + 600})
check(B.plan_warning().startswith("⛔ plan limit reached (Weekly · all models)"), f"limit hit: {B.plan_warning()}")

print("== record_cost keeps it (Anthropic backend only)")
B._usage.pop("plan", None)
snap = B.CCSnap("anthropic", "haiku", "read", next(iter(B.WORKSPACES)))
job = B.CCJob(channel=SimpleNamespace(id=CHAT), task="x", snap=snap, user_id=1)
job.parser = p
B.record_cost(job)
check(B.plan_windows() and job.ctx_limit == 200000, "plan + context window from a real job's stream")
B._usage.pop("plan", None)
job = B.CCJob(channel=SimpleNamespace(id=CHAT), task="x", snap=B.replace(snap, backend="ollama"), user_id=1)
job.parser = p
B.record_cost(job)
check(not B.plan_windows() and job.ctx_limit is None, "not from Ollama (its limits aren't Claude's)")
check(json.loads(B.USAGE_FILE.read_text()).get("sessions"), "usage saved")

print("== stats line and panels")
B.note_plan({"status": "allowed_warning", "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": now + 3600},
                                                             "seven_day": {"utilization": 0.3, "resetsAt": now + 86400}}})
job = B.CCJob(channel=SimpleNamespace(id=CHAT), task="x", snap=snap, user_id=1)
job.parser = p
line = B.chat_stats(job, [])
check("5-hour limit 85% used" in line.split(B.STATS_MARK)[1], "stats line warns (after the mark: visible on Telegram)")
line = B.chat_stats(job, [])
check("used" not in line and "5-hour 85%" in line.split(B.STATS_MARK)[0], "next reply: only in the routine part, no warning")
check("5-hour limit" in B.chat_stats(B.replace(job, channel=SimpleNamespace(id=CHAT + 1)), []).split(B.STATS_MARK)[1],
      "another chat gets its own first warning")
B.note_plan({"status": "allowed_warning", "unifiedWindows": {"five_hour": {"utilization": 0.87, "resetsAt": now + 3600}}})
check("used" not in B.chat_stats(job, []), "same step (80-89%): still quiet")
B.note_plan({"status": "allowed_warning", "unifiedWindows": {"five_hour": {"utilization": 0.92, "resetsAt": now + 3600}}})
check("5-hour limit 92% used" in B.chat_stats(job, []).split(B.STATS_MARK)[1], "crossing 90% warns once more")
check("used" not in B.chat_stats(job, []), "then quiet again")
B.note_plan({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": now + 5 * 3600}}})
check("5-hour limit 85% used" in B.chat_stats(job, []).split(B.STATS_MARK)[1], "a new 5-hour window warns afresh")
B.update_settings(CHAT, cc_session="abcdef1234", cc_session_at=time.time(), cc_session_path=str(B.WORKSPACES[snap.workspace]),
                  cc_session_setup=B.CC_SETUP_FINGERPRINT, cc_session_ctx=153500, cc_session_ctx_limit=200000)
e = B.usage_embed(CHAT)
f = {x.name.split(" · ")[0]: x.value for x in e.fields}
check("153.5k / 200k (77%)" in f["Context window"], f"context: {f['Context window']}")
check("**5-hour limit** 85%" in f["Plan usage limits"] and "█" in f["Plan usage limits"], "plan bars")
check("daily cap" in f["Spend today"], "spend")
check(any(x.name == "Plan usage" and "5-hour 85%" in x.value for x in B.panel_embed(CHAT).fields), "one line in /panel")
check(B.UsageView(CHAT).children[0].custom_id in B.UsageView.owner_custom_ids, "check-now is owner-only (runs Claude Code)")


async def live():
    print("== live: check now (tiny Haiku call)")
    B._usage.pop("plan", None)
    err = await B.check_plan_now()
    check(err is None and B.plan_windows(), f"plan read: {err or B.plan_windows()}")


if LIVE:
    asyncio.run(live())
print("all ok")
