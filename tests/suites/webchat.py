"""Dashboard chat (llmbot/webchat.py) over HTTP: ids that can't clash with Discord/Telegram, owner rights from the
access key, messages and replies (local model faked), /panel menus that change settings, a reminder with live
buttons, a form (modal) round trip, files served safely, the same-origin check, and chats surviving a restart."""
import asyncio, io, os

os.environ["DASHBOARD_PORT"] = "0"  # the suite runs the app itself
from _setup import B, TMP  # noqa  (first: isolates the bot)
from aiohttp import web
import discord, httpx
from llmbot import dashboard as D, store as store_mod, webchat as W

B.OWNER_IDS.add(900000000000000001)  # Claude Code enabled at all (the web user is an owner by DASHBOARD_CHAT)
B.CC_ENABLED = True


def check(c, label):
    assert c, label
    print("  ok", label)


async def fake_run_local(prompt, model, ctx, history):
    return B.LocalResult(f"echo: {prompt.split(chr(10))[-1]}", [], None, prompt)


async def wait_for(fn, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        v = await fn()
        if v:
            return v
        await asyncio.sleep(0.05)
    return await fn()


async def main():
    B.http = httpx.AsyncClient()
    B.STORE = store_mod.FileStore(B.DATA_DIR)
    B.scheduler.start()
    B.run_local = fake_run_local
    B.ctx_limit = lambda *a, **k: asyncio.sleep(0, None)

    print("== ids and rights")
    uid = B.WEB_USER_ID
    check(B.is_web_id(uid) and not B.is_telegram_id(uid) and not B.is_web_id(900000000000000001)
          and not B.is_web_id(-1001234567890), "web ids can't be Discord or Telegram ids")
    check(B.is_allowed(uid) and B.is_owner(uid) and B.is_allowed_in(uid, None), "DASHBOARD_CHAT=owner: allowed, owner")
    B.DASHBOARD_CHAT = "user"
    check(B.is_allowed(uid) and not B.is_owner(uid), "DASHBOARD_CHAT=user: local model only")
    B.DASHBOARD_CHAT = "off"
    check(not B.is_allowed(uid), "DASHBOARD_CHAT=off: nothing")
    B.DASHBOARD_CHAT = "owner"
    check(not B.is_owner(uid - 5), "only the one web user counts")

    fe = W.start(B)
    D.core, D._key, D.webchat = B, "k" * 24, W
    check(B.frontend_for(B.WEB_ID_BASE * -1 - 1) is fe and not B._frontends[-1].owns(-B.WEB_ID_BASE - 1),
          "web chats belong to the web front end, not Discord")

    runner = web.AppRunner(D.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    async with httpx.AsyncClient(base_url=base, cookies={D.COOKIE: D._key}, timeout=20) as c:
        check((await httpx.AsyncClient(base_url=base).get("/api/chats")).status_code == 401, "needs the key")
        chat = (await c.post("/api/chats", json={})).json()["chat"]
        cid = chat["id"]
        check(B.is_web_id(int(cid)) and int(cid) != uid, f"new chat {cid}")
        check(B.get_settings(int(cid))["style"] == "chat", "new chats use plain chat replies")
        B.update_settings(int(cid), engine="local")

        print("== messages")
        r = await c.post(f"/api/chats/{cid}/send", files={"text": (None, "hello there")})
        check(r.json() == {"ok": True}, "sent")

        async def replied():
            d = (await c.get(f"/api/chats/{cid}?since=0")).json()
            return d if any(m["role"] == "bot" for m in d["messages"]) else None
        d = await wait_for(replied)
        roles = [m["role"] for m in d["messages"]]
        bot = [m for m in d["messages"] if m["role"] == "bot"][0]
        check(roles[:2] == ["user", "bot"] and "echo: hello there" in bot["text"], f"reply: {bot['text'][:60]!r}")
        check(bot["reply_to"] == d["messages"][0]["id"], "reply points at the question")
        check(d["chat"]["title"] == "hello there", "chat named after its first message")
        v = d["v"]
        check((await c.get(f"/api/chats/{cid}?since={v}")).json()["messages"] == [], "since= returns only changes")
        ev = [e for e in B._events if e["kind"] == "local"][-1]
        check(ev["where"].startswith("Web:") and ev["who"] == "You (dashboard)", "activity feed names the web chat")

        print("== /panel and its menus")
        await c.post(f"/api/chats/{cid}/send", files={"text": (None, "/panel")})

        async def panel():
            d = (await c.get(f"/api/chats/{cid}?since={v}")).json()
            return next((m for m in d["messages"] if m.get("embed") and m["controls"]), None)
        pm = await wait_for(panel)
        check(pm and pm["live"], "panel posted with live controls")
        sel = next(x for x in pm["controls"] if x["type"] == "select" and any("Auto" in o["label"] for o in x["options"]))
        opt = next(j for j, o in enumerate(sel["options"]) if "Auto" in o["label"])
        res = (await c.post(f"/api/chats/{cid}/press", json={"mid": pm["id"], "i": sel["i"], "opt": opt})).json()
        check(B.get_settings(int(cid))["engine"] == "auto", f"engine changed from the web menu ({res})")
        d = (await c.get(f"/api/chats/{cid}?since=0")).json()
        pm2 = next(m for m in d["messages"] if m["id"] == pm["id"])
        check(pm2["v"] > pm["v"], "the panel message was edited in place")

        print("== /usage and /skills in the web chat")
        B._usage["plan"] = {"at": 1, "status": "allowed", "windows": {"five_hour": {"utilization": 0.42, "resets_at": None}}}
        checked = []

        async def fake_check():
            checked.append(1)
            B._usage["plan"]["windows"]["five_hour"]["utilization"] = 0.5
        B.check_plan_now = fake_check
        v2 = (await c.get(f"/api/chats/{cid}?since=0")).json()["v"]
        await c.post(f"/api/chats/{cid}/send", files={"text": (None, "/usage")})

        async def usage_msg():
            d = (await c.get(f"/api/chats/{cid}?since={v2}")).json()
            return next((m for m in d["messages"] if m.get("embed") and "Usage" in str(m["embed"])), None)
        um = await wait_for(usage_msg)
        check(um and "5-hour limit" in str(um["embed"]) and "42%" in str(um["embed"]), "usage panel posted")
        btn = next(x for x in um["controls"] if x["type"] == "button")
        await c.post(f"/api/chats/{cid}/press", json={"mid": um["id"], "i": btn["i"]})
        d = (await c.get(f"/api/chats/{cid}?since=0")).json()
        check(checked and "50%" in str(next(m for m in d["messages"] if m["id"] == um["id"])["embed"]),
              "🔄 check now works from the web (owner by access key)")
        await c.post(f"/api/chats/{cid}/send", files={"text": (None, "/skills")})

        async def skills_msg():
            d = (await c.get(f"/api/chats/{cid}?since={v2}")).json()
            return next((m for m in d["messages"] if m["role"] == "bot" and "skill" in m["text"].lower()), None)
        check(await wait_for(skills_msg), "/skills answers")
        cmds = {x["name"] for x in (await c.get("/api/chats")).json()["commands"]}
        check({"usage", "skills"} <= cmds, "both in the page's / autocomplete")

        print("== reminder with buttons")
        rem = B.add_reminder("in 5 min", "stretch", int(cid), uid)
        await B.fire_reminder(rem["id"])
        d = (await c.get(f"/api/chats/{cid}?since=0")).json()
        rm = [m for m in d["messages"] if "stretch" in m["text"]][-1]
        check(rm["live"] and rm["controls"] and "@You (dashboard)" in rm["text"], f"reminder posted: {rm['text'][:50]!r}")
        done = next(x for x in rm["controls"] if x["type"] == "button")
        res = (await c.post(f"/api/chats/{cid}/press", json={"mid": rm["id"], "i": done["i"]})).json()
        check("toast" in res, f"reminder button works: {res}")

        print("== form (modal)")
        got = []

        class V(discord.ui.View):
            @discord.ui.button(label="Edit")
            async def edit(self, inter, button):
                async def on_done(i2, text):
                    got.append(text)
                    await i2.response.send_message("saved", ephemeral=True)
                await inter.response.send_modal(B.TaskModal("Edit it", "old text", on_done))
        ch = await fe.get_channel(int(cid))
        m = await ch.send("form test", view=V())
        res = (await c.post(f"/api/chats/{cid}/press", json={"mid": m.id, "i": 0})).json()
        check(res.get("modal") and res["modal"]["default"] == "old text" and res["modal"]["long"], "button opens a form")
        res = (await c.post(f"/api/chats/{cid}/modal", json={"token": res["modal"]["token"], "text": "new text"})).json()
        check(got == ["new text"] and res["toast"] == "saved", "form submitted to the same handler")
        res = (await c.post(f"/api/chats/{cid}/modal", json={"token": "nope", "text": "x"})).json()
        check("expired" in res["toast"], "unknown form token refused")

        print("== files")
        fm = await ch.send("here", file=discord.File(io.BytesIO(b"<script>alert(1)</script>"), "x.html"))
        d = (await c.get(f"/api/chats/{cid}?since=0")).json()
        f = next(m for m in d["messages"] if m["id"] == fm.id)["files"][0]
        r = await c.get(f["url"])
        check(r.status_code == 200 and "attachment" in r.headers["content-disposition"]
              and r.headers["content-security-policy"] == "sandbox", "bot files download (never rendered), sandboxed")
        check((await c.get("/api/chat-file/..%2F..%2F.env")).status_code == 404
              and (await httpx.AsyncClient(base_url=base).get(f["url"])).status_code == 404, "no traversal, needs the key")
        r = await c.post(f"/api/chats/{cid}/send", files={"text": (None, "look"), "file": ("a.txt", b"hi", "text/plain")})
        check(r.json() == {"ok": True}, "upload accepted")

        print("== safety")
        r = await c.post(f"/api/chats/{cid}/send", files={"text": (None, "x")}, headers={"Origin": "http://evil.example"})
        check(r.status_code == 403, "cross-site POST refused")
        check((await c.get("/api/chats/123")).status_code == 404, "unknown chat id refused")
        big = b"x" * 5000
        check((await httpx.AsyncClient(base_url=base).post("/login", content=big,
               headers={"Content-Type": "application/x-www-form-urlencoded"})).status_code == 413, "login body capped")

        print("== autocomplete and voice")
        lst = (await c.get("/api/chats")).json()
        names = [x["name"] for x in lst["commands"]]
        check({"panel", "claude", "remind", "help"} <= set(names) and all(x["desc"] for x in lst["commands"]),
              f"command list for autocomplete ({len(names)})")
        check(all(f"/{n}" in W.HELP for n in names), "/help lists the same commands")
        heard = []

        async def fake_transcribe(data):
            heard.append(data)
            return " remind me to call mom ", 2.0
        real, B.transcribe = B.transcribe, fake_transcribe
        r = (await c.post("/api/transcribe", files={"audio": ("voice.webm", b"OGGfake", "audio/webm")})).json()
        check(r == {"text": "remind me to call mom", "seconds": 2.0} and heard == [b"OGGfake"], f"transcribe: {r}")
        check("error" in (await c.post("/api/transcribe", files={"x": ("a", b"", "text/plain")})).json(), "no audio -> error")
        B.VOICE_ENABLED = False
        check("off" in (await c.post("/api/transcribe", files={"audio": ("v.webm", b"x", "audio/webm")})).json()["error"],
              "VOICE_ENABLED=false -> refused")
        B.VOICE_ENABLED, B.transcribe = True, real

        print("== https (for the microphone)")
        B.DASHBOARD_NAME, B.DASHBOARD_HOST = "llmbot", "0.0.0.0"
        ctx = D.tls_context()
        from cryptography import x509
        cert = x509.load_pem_x509_certificate((B.DATA_DIR / "dashboard-cert.pem").read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        check("llmbot.local" in san.get_values_for_type(x509.DNSName)
              and any(str(i) == "127.0.0.1" for i in san.get_values_for_type(x509.IPAddress)), "self-signed cert covers llmbot.local and the IPs")
        mtime = (B.DATA_DIR / "dashboard-cert.pem").stat().st_mtime
        D.tls_context()
        check((B.DATA_DIR / "dashboard-cert.pem").stat().st_mtime == mtime, "reused on the next start")
        s2 = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await s2.start()
        port2 = s2._server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(verify=False, cookies={D.COOKIE: D._key}) as sc:
            r = await sc.get(f"https://127.0.0.1:{port2}/api/chats")
        check(r.status_code == 200, "dashboard served over https, same cookie")
        B.DASHBOARD_HOST = "127.0.0.1"

        print("== restart")
        await asyncio.sleep(2.2)  # the debounced save
        fe2 = W.WebFrontend(B)
        c2 = fe2.chats[int(cid)]
        check(len(c2.messages) == len(fe.chats[int(cid)].messages) and not c2.live, "chats and messages reloaded; buttons expire")
        r = (await c.post(f"/api/chats/{cid}/rename", json={"title": "Renamed"})).json()
        check(r["chat"]["title"] == "Renamed", "rename")
        check((await c.post(f"/api/chats/{cid}/delete", json={})).json() == {"ok": True} and int(cid) not in fe.chats, "delete")
    await runner.cleanup()
    B.scheduler.shutdown(wait=False)
    await B.http.aclose()


asyncio.run(main())
print("\nALL WEBCHAT CHECKS PASSED")
