"""Postgres storage against a real, throwaway database (its llmbot_* tables are dropped first):
LLMBOT_TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/llmbot_test

Imports old data/ files once, saves every kind of bookkeeping, survives a restart and a dropped connection, records
Claude Code jobs, keeps the password out of errors, and never hands DATABASE_URL to Claude Code."""
import asyncio, json, os, time

URL = os.environ["LLMBOT_TEST_DATABASE_URL"]
from _setup import B, TMP  # noqa  (first: isolates the bot)
import psycopg
from llmbot import store

with psycopg.connect(URL, autocommit=True) as c:
    c.execute("drop table if exists llmbot_state, llmbot_jobs")


def check(c, label):
    assert c, label
    print("  ok", label)


def db(sql, *params):
    with psycopg.connect(URL, autocommit=True) as c:
        return c.execute(sql, params).fetchall()


async def main():
    print("== first start: old files are imported")
    data = TMP / "data"
    (data / "settings.json").write_text(json.dumps({"42": {"engine": "claude", "style": "cards"}}), encoding="utf-8")
    (data / "tasks.json").write_text(json.dumps([{"id": "t1", "cron": "0 9 * * *", "prompt": "news", "description": "news",
                                                  "channel_id": 42, "user_id": 1, "engine": "local"}]), encoding="utf-8")
    B.DATABASE_URL = URL
    B.load_state()
    check(B.STORE.kind == "postgres" and "llmbot_test" in B.STORE.describe() and "@" not in B.STORE.describe(),
          f"store: {B.STORE.describe()}")
    check(B.get_settings(42)["engine"] == "claude" and "t1" in B._tasks, "settings and tasks loaded from the files")
    check(not (data / "settings.json").exists() and (data / "settings.json.imported").exists(), "file renamed *.imported")
    check(db("select data->'42'->>'style' from llmbot_state where name = 'settings'") == [("cards",)], "now in llmbot_state")

    print("== every kind of bookkeeping is saved")
    B.scheduler.start()
    B.update_settings(42, engine="local")
    r = B.add_reminder("in 2 hours", "stretch", 42, 1)
    B.remember(42, "hi", "hello!")
    B.add_spend(0.25)
    B.STORE.save("power", B.POWER_FILE, {"action": "restart", "channel_id": 42, "user_id": 1, "at": time.time()})
    names = {n for (n,) in db("select name from llmbot_state")}
    check({"settings", "tasks", "reminders", "history", "usage", "power"} <= names, f"documents: {sorted(names)}")
    check(B.power_pending()["action"] == "restart", "power note readable")
    B.STORE.delete("power", B.POWER_FILE)
    check(B.power_pending() is None, "power note deleted")
    check(not list(data.glob("*.json")), "no JSON files written while on Postgres")

    print("== restart: everything comes back")
    for d in (B._settings, B._tasks, B._reminders, B._usage, B._history):
        d.clear()
    B.load_state()
    check(B.get_settings(42)["engine"] == "local" and r["id"] in B._reminders and B._history[42][-1]["content"] == "hello!"
          and B.spent_today() == 0.25, "settings, reminders, chat memory and spend reloaded")
    B.forget_history(42)
    B._history.clear(); B.load_state()
    check(42 not in B._history, "/reset is saved too")

    print("== dropped connection")
    B.STORE._conn.close()
    B.update_settings(42, style="chat")
    check(db("select data->'42'->>'style' from llmbot_state where name = 'settings'") == [("chat",)], "reconnects and saves")

    print("== job history")
    B.STORE.record_job({"job_id": "abc", "frontend": "discord", "channel_id": 42, "user_id": 1, "backend": "anthropic",
                        "model": "haiku", "perm": "read", "outcome": "success", "turns": 2, "cost_usd": 0.0123,
                        "session_cost_usd": 0.05, "context_tokens": 11000, "seconds": 6, "session_id": "s1", "prompt": "hi"})
    rows = db("select model, cost_usd::float, outcome from llmbot_jobs where job_id = 'abc'")
    check(rows == [("haiku", 0.0123, "success")], f"llmbot_jobs row: {rows}")

    print("== secrets")
    bad = URL.replace("://", "://nobody:wrong-pass-123@", 1) if "@" not in URL else \
        URL.split("://")[0] + "://nobody:wrong-pass-123@" + URL.split("@", 1)[1]
    try:
        store.PgStore(bad, wait=0)
        check(False, "bad password should fail")
    except RuntimeError as e:
        check("wrong-pass-123" not in str(e) and "Can't reach Postgres" in str(e), f"error hides the password: {e}")
    os.environ["DATABASE_URL"] = URL
    env = B.build_cc_env(B.CCSnap("anthropic", "haiku", "read", "default"))
    check("DATABASE_URL" not in env, "DATABASE_URL is not passed to Claude Code")
    B.scheduler.shutdown(wait=False)
    B.STORE.close()
    print("\nALL POSTGRES CHECKS PASSED")


asyncio.run(main())
