"""Web dashboard: access key and cookie, the local-network-only guard, the state/events/jobs/log APIs, and the
activity feed (events from reminders, tasks and warnings; persisted, so they come back after a restart)."""
import asyncio, json, logging, os

os.environ["DASHBOARD_PORT"] = "0"  # core_start isn't used here; the suite starts the server on a free port itself
from _setup import B, TMP  # noqa  (first: isolates the bot)
from aiohttp import web
import httpx
from llmbot import dashboard as D, store as store_mod

B.is_telegram_id = lambda x: False  # small fake Discord ids
OWNER, CH = 1, 1515


def check(c, label):
    assert c, label
    print("  ok", label)


async def main():
    B.http = httpx.AsyncClient()
    B.STORE = store_mod.FileStore(B.DATA_DIR)
    B.scheduler.start()
    logging.getLogger().addHandler(B.EventLogHandler())

    print("== access key")
    key = D.access_key(B.DATA_DIR, "")
    check(len(key) >= 20 and (B.DATA_DIR / "dashboard.key").exists(), "generated and saved in data/dashboard.key")
    check(D.access_key(B.DATA_DIR, "") == key, "same key on the next start")
    check(D.access_key(B.DATA_DIR, "from-env-1234567890") == "from-env-1234567890", "DASHBOARD_TOKEN wins")

    print("== local-network guard")
    for ip in ["127.0.0.1", "192.168.1.20", "10.0.0.5", "172.20.1.1", "100.101.102.103", "::1", "fe80::1%12",
               "::ffff:192.168.1.9", "fd12::1"]:
        assert D.local_address(ip), ip
    for ip in ["8.8.8.8", "1.2.3.4", "2606:4700::1111", "::ffff:8.8.8.8", "", None, "junk"]:
        assert not D.local_address(ip), ip
    check(True, "LAN / loopback / Tailscale allowed; public addresses refused")

    print("== local name (mDNS)")
    D.core, D._key = B, key
    B.DASHBOARD_HOST = "0.0.0.0"
    for name, want in [("llmbot", "llmbot.local"), ("My-Bot", "my-bot.local"), ("llmbot.local", "llmbot.local"),
                       ("", None), ("bad name", None), ("-x", None)]:
        B.DASHBOARD_NAME = name
        assert D.mdns_host() == want, (name, D.mdns_host())
    B.DASHBOARD_NAME, B.DASHBOARD_HOST = "llmbot", "127.0.0.1"
    check(D.mdns_host() is None, "names validated; not announced when the dashboard is this-PC-only")
    B.DASHBOARD_HOST = "0.0.0.0"
    check(not D.links()[0].startswith("http://llmbot.local"), "links use the IP while the name isn't announced")
    D._mdns_ip, D._runner = "192.168.1.5", object()  # as if announced and serving
    text = D.link_text()
    check(D.links()[0].startswith("http://llmbot.local:") and "older Android" in text and "http://llmbot.local:8765`"
          not in text.split("\n")[1], "announced: the name comes first, IP as fallback")
    D._mdns_ip = D._runner = None

    print("== activity events")
    r = B.add_reminder("in 2 hours", "take my meds", CH, OWNER)
    t = B.add_task("0 9 * * *", "morning news", "Morning news", CH, OWNER)
    B.cancel_reminder(r["id"], OWNER)
    logging.getLogger("llmbot").warning("disk nearly full, token sk-ant-api03-abcdefghijklmnopqrstuv")
    kinds = [(e["kind"], e.get("status")) for e in B._events]
    check(("reminder", "set") in kinds and ("reminder", "cancelled") in kinds and ("task", "scheduled") in kinds,
          f"reminder/task events: {kinds}")
    warn = [e for e in B._events if e["kind"] == "warning"]
    check(warn and "sk-ant" not in warn[-1]["text"] and warn[-1]["level"] == "warning", "warnings recorded, redacted")
    ids = [e["id"] for e in B._events]
    check(ids == sorted(ids) and len(set(ids)) == len(ids), "event ids unique and increasing")
    rows = B.STORE.recent_events(10)
    check([e["id"] for e in rows] == ids[::-1], "persisted to events.jsonl, newest first")
    check(B.STORE.recent_events(10, before=ids[-1])[0]["id"] == ids[-2], "paging with before=")
    with B.activity("local", "what's the weather", CH, OWNER, model="qwen"):
        check(len(B._inflight) == 1, "in-flight work visible while running")
    check(not B._inflight, "and gone after")

    print("== names survive restarts")
    class FakeTg:
        _names = {5550001: "Rudrashish"}
        _chats = {5550001: "Rudrashish (private)"}
    discord_only = B.is_telegram_id
    B.is_telegram_id = lambda x: abs(int(x)) < 10 ** 16  # the real rule, for this part
    B._telegram = FakeTg()
    check(B.user_label(5550001) == "Rudrashish" and B.chat_label(5550001) == "Telegram: Rudrashish (private)", "live names")
    B._telegram = None  # as after a restart, before the person writes again
    B._known_names.clear()
    B._known_names.update(B.STORE.load("names", B.NAMES_FILE, {}))
    check(B.user_label(5550001) == "Rudrashish" and B.chat_label(5550001) == "Telegram: Rudrashish (private)",
          "names come back from data/names.json")
    check(B.user_label(5550002) == "Telegram user 5550002", "unknown people still get the fallback")
    B._known_names.clear()
    B.NAMES_FILE.unlink()
    for i, (uid, who) in enumerate([(5550003, "Asha"), (5550004, "Telegram user 5550004")]):  # older history rows
        B._event_last_id += 1
        B.STORE.record_event({"id": B._event_last_id, "ts": 1.0, "kind": "local", "level": "info", "text": "hi",
                              "user_id": uid, "channel_id": uid, "who": who, "where": f"Telegram: {who} (private)"})
    B._events.clear()
    B.load_state()
    check(B.user_label(5550003) == "Asha" and "u:5550004" not in B._known_names,
          "names backfilled from the activity history (fallback labels skipped)")
    B.is_telegram_id = discord_only

    print("== server")
    B.DASHBOARD_TOKEN, B.DASHBOARD_HOST = "", "127.0.0.1"
    D.core, D._key = B, key
    runner = web.AppRunner(D.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base, follow_redirects=False) as c:
        r0 = await c.get("/")
        check(r0.status_code == 401 and "Access key" in r0.text, "no key: login page")
        check((await c.get("/api/state")).status_code == 401, "no key: API refused")
        check((await c.get("/?key=wrong")).status_code == 401, "wrong key refused")
        for img in ("/logo.png", "/favicon.png"):
            ri = await c.get(img)
            assert ri.status_code == 200 and ri.content[:4] == b"\x89PNG", img
        check('href="/favicon.png"' in r0.text, "logo and favicon served (no key needed), linked from the login page")
        r1 = await c.get(f"/?key={key}")
        check(r1.status_code == 302 and r1.headers["location"] == "/" and D.COOKIE in r1.headers.get("set-cookie", ""),
              "right key: cookie set, key dropped from the address")
        check("httponly" in r1.headers["set-cookie"].lower() and "samesite=strict" in r1.headers["set-cookie"].lower(),
              "cookie is HttpOnly + SameSite=Strict")
        c.cookies.set(D.COOKIE, key)
        r2 = await c.get("/")
        check(r2.status_code == 200 and "Bot dashboard" in r2.text and "frame-ancestors 'none'" in
              r2.headers["content-security-policy"], "page served with security headers")
        r3 = await c.post("/login", data={"key": key}, cookies={})
        check(r3.status_code == 302, "login form works")

        st = (await c.get("/api/state")).json()
        check(st["bot"]["version"] == B.__version__ and "frontends" in st["bot"], "state: bot info")
        check(any(x["id"] == t["id"] and x["next"] for x in st["tasks"]), "state: tasks with next run")
        tt = next(x for x in st["tasks"] if x["id"] == t["id"])
        check(tt["perm"] == "read" and tt["model"] and tt["reminders"] is False, f"state: task runs on {tt['model']}, read-only")
        check(st["busy"]["claude"] == [] and st["busy"]["inflight"] == [], "state: idle")
        check(len(st["events"]) == len(B._events), "state: events")
        since = st["events"][-1]["id"]
        check((await c.get(f"/api/state?since={since}")).json()["events"] == [], "state: since= returns only new events")
        check(isinstance(st["machine"], dict) and "spend" in st, "state: machine and spend")
        lite = (await c.get("/api/state?lite=1")).json()
        check(not {"machine", "models", "installed", "ollama_up"} & set(lite) and "busy" in lite and "tasks" in lite,
              "lite refresh skips machine/model stats (no nvidia-smi off the Now tab)")
        inst = st["installed"]
        check(isinstance(inst["local"], list) and inst["default_local"] == B.LLM_MODEL and "whisper" in inst,
              f"state: installed models ({len(inst['local'])} local; none offline is fine)")
        cc_was, B.CC_ENABLED = B.CC_ENABLED, True
        cc = (await D.installed_models())["claude"]
        B.CC_ENABLED = cc_was
        check(cc and cc[0]["backend"] == "Anthropic" and "sonnet" in cc[0]["models"], "installed: Claude Code models")
        c1 = st["machine"]["cpu"]
        check(c1 is not None and 0 <= c1["percent"] <= 100 and c1["cores"], f"state: CPU load {c1}")

        B.STORE.record_job({"job_id": "abc", "outcome": "success", "cost_usd": 0.05, "prompt": "hello", "model": "haiku",
                            "channel_id": CH, "user_id": OWNER})
        jobs = (await c.get("/api/jobs")).json()["jobs"]
        check(jobs and jobs[0]["job_id"] == "abc" and jobs[0]["cost_usd"] == 0.05, "jobs history")
        ev = (await c.get("/api/events?limit=2")).json()["events"]
        check(len(ev) == 2 and ev[0]["id"] > ev[1]["id"], "older events endpoint")

        (B.DATA_DIR / "bot.log").write_text("2026-09-30 10:00:00,000 INFO llmbot: hi\n"
                                           "2026-09-30 10:00:01,000 WARNING llmbot: token sk-ant-api03-zzzzzzzzzzzzzzzzzzzzzz\n",
                                           encoding="utf-8")
        lines = (await c.get("/api/log?lines=50")).json()["lines"]
        check(len(lines) == 2 and "sk-ant" not in lines[1], "log tail, redacted")

    print("== restart: history comes back")
    before = len(B._events)
    B._events.clear()
    B._events.extend(reversed(B.STORE.recent_events(B.EVENTS_KEEP)))
    check(len(B._events) == before, "events reloaded from the store")
    await runner.cleanup()
    B.scheduler.shutdown(wait=False)
    await B.http.aclose()


asyncio.run(main())
print("\nALL DASHBOARD CHECKS PASSED")
