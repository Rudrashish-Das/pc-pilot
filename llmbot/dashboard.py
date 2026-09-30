"""Web dashboard: what the bot is doing now and what it has done, viewable from a phone on the same network.

Runs inside the bot process (aiohttp, which discord.py already uses) on DASHBOARD_HOST:DASHBOARD_PORT. Read-only.
Access needs the key (DASHBOARD_TOKEN, or the one generated in data/dashboard.key): open /?key=<key> once and a
cookie remembers it. Owners get the link with /dashboard in Discord or Telegram. Requests from public internet
addresses are refused even with the key; only this PC, the LAN and Tailscale-style (100.64/10) addresses get in.
"""
from __future__ import annotations

import asyncio
import ctypes
import hmac
import ipaddress
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

log = logging.getLogger("llmbot.dashboard")

COOKIE = "llmbot_dashboard"
PAGE = Path(__file__).with_name("dashboard.html")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and carrier NAT
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' "
                               "'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
}

core: Any = None
_runner: web.AppRunner | None = None
_key = ""
_cache: dict[str, tuple[float, Any]] = {}


# ---- access ---------------------------------------------------------------------------------------------------


def access_key(data_dir: Path, configured: str) -> str:
    """DASHBOARD_TOKEN, else a random key kept in data/dashboard.key (created on first start)."""
    if configured:
        return configured
    path = data_dir / "dashboard.key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    except FileNotFoundError:
        pass
    key = secrets.token_urlsafe(24)
    path.write_text(key + "\n", encoding="utf-8")
    return key


def local_address(ip: str | None) -> bool:
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a.is_link_local or (a.version == 4 and a in _CGNAT)


def lan_ips() -> list[str]:
    """This PC's addresses a phone on the same network can use, best first."""
    ips: list[str] = []
    try:  # the address of the default route (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    good = [ip for ip in dict.fromkeys(ips) if not ip.startswith(("127.", "169.254."))]
    return good or ["127.0.0.1"]


def links() -> list[str]:
    host = core.DASHBOARD_HOST
    ips = ["127.0.0.1"] if host in ("127.0.0.1", "localhost", "::1") else (lan_ips() if host in ("0.0.0.0", "::", "") else [host])
    return [f"http://{ip}:{core.DASHBOARD_PORT}/?key={_key}" for ip in ips]


def _authed(request: web.Request) -> bool:
    given = request.cookies.get(COOKIE) or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(given) and hmac.compare_digest(given.encode(), _key.encode())


@web.middleware
async def guard(request: web.Request, handler):
    if not local_address(request.remote):
        log.warning("Dashboard: refused a request from %s (not a local network address)", request.remote)
        raise web.HTTPForbidden(text="The dashboard only answers this PC and its local network.")
    resp = await handler(request)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _home() -> web.Response:
    """Redirect to the page itself, with the cookie that remembers the key (and the key gone from the address)."""
    resp = web.Response(status=302, headers={"Location": "/"})
    resp.set_cookie(COOKIE, _key, max_age=365 * 86400, httponly=True, samesite="Strict", path="/")
    return resp


async def page(request: web.Request) -> web.StreamResponse:
    given = request.query.get("key")
    if given is not None:
        if hmac.compare_digest(given.encode(), _key.encode()):
            return _home()
        await asyncio.sleep(1)
        return web.Response(text=LOGIN.replace("{error}", "Wrong key."), content_type="text/html", status=401)
    if not _authed(request):
        return web.Response(text=LOGIN.replace("{error}", ""), content_type="text/html", status=401)
    return web.FileResponse(PAGE, headers={"Content-Type": "text/html; charset=utf-8"})


async def login(request: web.Request) -> web.StreamResponse:
    form = await request.post()
    if hmac.compare_digest(str(form.get("key", "")).strip().encode(), _key.encode()):
        return _home()
    await asyncio.sleep(1)
    return web.Response(text=LOGIN.replace("{error}", "Wrong key."), content_type="text/html", status=401)


def api(fn):
    async def wrapper(request: web.Request):
        if not _authed(request):
            return web.json_response({"error": "not signed in"}, status=401)
        return web.json_response(await fn(request), dumps=_dumps)
    return wrapper


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---- what's shown ---------------------------------------------------------------------------------------------


async def cached(name: str, seconds: float, fn):
    hit = _cache.get(name)
    if hit and time.monotonic() - hit[0] < seconds:
        return hit[1]
    try:
        value = await fn()
    except Exception as e:
        log.debug("dashboard %s: %s", name, e)
        value = None
    _cache[name] = (time.monotonic(), value)
    return value


def _names(channel_id: int | None, user_id: int | None) -> dict:
    out = {}
    try:
        if channel_id:
            out["where"] = core.chat_label(channel_id)
        if user_id:
            out["who"] = core.user_label(user_id)
    except Exception:
        pass
    return out


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def ollama_models() -> list[dict]:
    """What Ollama has in memory right now (/api/ps on each Ollama the bot uses)."""
    out = []
    for root in {core._llm_root(), core.OLLAMA_URL}:
        try:
            r = await core.http.get(f"{root}/api/ps", timeout=2)
            r.raise_for_status()
        except Exception:
            continue
        for m in r.json().get("models", []):
            out.append({"name": m.get("name") or m.get("model"), "server": root,
                        "size_mb": round((m.get("size") or 0) / 2**20), "vram_mb": round((m.get("size_vram") or 0) / 2**20),
                        "context": m.get("context_length"), "until": m.get("expires_at")})
    return out


async def ollama_up() -> bool:
    try:
        r = await core.http.get(f"{core._llm_root()}/api/version", timeout=2)
        return r.status_code < 400
    except Exception:
        return False


async def gpu() -> list[dict] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    p = await asyncio.create_subprocess_exec(
        exe, "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kw)
    out, _ = await asyncio.wait_for(p.communicate(), 5)
    gpus = []
    for line in out.decode(errors="replace").strip().splitlines():
        name, used, total, util, temp = [x.strip() for x in line.split(",")][:5]
        num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
        gpus.append({"name": name, "mem_used_mb": num(used), "mem_total_mb": num(total), "util": num(util),
                     "temp_c": num(temp)})
    return gpus


def machine() -> dict:
    """Memory, battery and disk. Windows only for memory/battery (no extra packages)."""
    out: dict[str, Any] = {"host": socket.gethostname()}
    try:
        du = shutil.disk_usage(core.DATA_DIR)
        out["disk"] = {"free_gb": round(du.free / 2**30, 1), "total_gb": round(du.total / 2**30, 1)}
    except OSError:
        pass
    if sys.platform != "win32":
        return out

    class MEM(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    class POWER(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]

    try:
        m = MEM()
        m.dwLength = ctypes.sizeof(MEM)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            out["ram"] = {"used_gb": round((m.ullTotalPhys - m.ullAvailPhys) / 2**30, 1),
                          "total_gb": round(m.ullTotalPhys / 2**30, 1)}
        p = POWER()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)) and (p.BatteryFlag & 0xFF) != 128:
            pct = p.BatteryLifePercent & 0xFF
            out["battery"] = {"percent": None if pct == 255 else pct, "plugged_in": p.ACLineStatus == 1}
    except Exception:
        pass
    return out


def current_job() -> dict | None:
    job = core._cc_current
    if job is None:
        return None
    ch = getattr(job.channel, "id", None)
    return {"id": job.id, "task": core.clip(core.redact(job.task), 1500), "status": job.status,
            "backend": job.snap.backend, "model": job.snap.model, "perm": job.snap.perm, "workspace": job.snap.workspace,
            "seconds": job.elapsed(), "scheduled": job.scheduled, "stopping": job.stop_requested,
            "lines": [core.redact(x) for x in job.lines], "context": job.parser.context_tokens,
            **_names(ch, job.user_id)}


def schedule() -> dict:
    tasks = []
    for t in core._tasks.values():
        tasks.append({"id": t["id"], "description": t["description"], "prompt": core.clip(t["prompt"], 600),
                      "engine": t.get("engine", "local"), "cron": t.get("cron") or None, "at": t.get("at"),
                      "next": _iso(core.next_run(t["id"])), **_names(t["channel_id"], t["created_by"])})
    tasks.sort(key=lambda t: t["next"] or "~")
    rems = [{"id": r["id"], "text": r["text"], "when": r["when"], **_names(r["channel_id"], r["user_id"])}
            for r in core.my_reminders(None)]
    return {"tasks": tasks, "reminders": rems}


def frontends() -> list[dict]:
    out = []
    if core.DISCORD_TOKEN:
        b = core.bot
        ready = b.is_ready() and not b.is_closed()
        out.append({"name": "Discord", "ok": ready,
                    "detail": (f"{b.user} · {len(b.guilds)} server(s) · {b.latency * 1000:.0f} ms" if ready
                               else "connecting")})
    tg = core._telegram
    if tg is not None:
        out.append({"name": "Telegram", "ok": bool(tg.username) and not tg._closed,
                    "detail": f"@{tg.username}" if tg.username else "connecting"})
    return out


async def state(request: web.Request) -> dict:
    since = int(request.query.get("since") or 0)
    events = [e for e in core._events if e["id"] > since][-200:]
    pending = core.power_pending()
    return {
        "now": time.time(), "timezone": core.TIMEZONE,
        "bot": {"version": core.__version__, "started": core.STARTED_AT, "pid": os.getpid(),
                "storage": core.STORE.describe(), "claude": core.CLAUDE_VERSION, "frontends": frontends(),
                "default_engine": core.DEFAULT_ENGINE, "local_model": core.LLM_MODEL,
                "cc": {"enabled": core.CC_ENABLED, "backend": core.CC_BACKEND, "model": core.CC_MODEL}},
        "busy": {"claude": current_job(), "claude_waiting": core._cc_waiting, "local_locked": core._llm_lock.locked(),
                 "inflight": [{**{k: v for k, v in a.items() if k not in ("channel_id", "user_id")},
                               **_names(a.get("channel_id"), a.get("user_id"))} for a in list(core._inflight.values())],
                 "whisper_loaded": core._whisper is not None,
                 "power": {**pending, **_names(pending.get("channel_id"), pending.get("user_id"))} if pending else None},
        "models": await cached("models", 4, ollama_models),
        "ollama_up": await cached("ollama_up", 10, ollama_up),
        "spend": {"today": core.spent_today(), "daily_budget": core.CC_DAILY_BUDGET_USD,
                  "job_budget": core.CC_MAX_BUDGET_USD,
                  "jobs_today": core._usage.get("jobs", 0) if core._usage.get("date") == core.now_local().date().isoformat() else 0},
        "machine": {**machine(), "gpu": await cached("gpu", 5, gpu)},
        **schedule(),
        "events": events,
    }


async def events(request: web.Request) -> dict:
    """Older activity, from the store: ?before=<event id>&limit=100"""
    before = int(request.query["before"]) if request.query.get("before") else None
    limit = max(1, min(int(request.query.get("limit") or 100), 500))
    rows = await asyncio.to_thread(core.STORE.recent_events, limit, before)
    return {"events": rows}


async def jobs(request: web.Request) -> dict:
    """Claude Code job history: ?before=<finished_at>&limit=50"""
    limit = max(1, min(int(request.query.get("limit") or 50), 200))
    rows = await asyncio.to_thread(core.STORE.recent_jobs, limit, request.query.get("before") or None)
    for r in rows:
        r.update(_names(r.get("channel_id"), r.get("user_id")))
    return {"jobs": rows}


async def logtail(request: web.Request) -> dict:
    """The end of bot.log, redacted."""
    n = max(10, min(int(request.query.get("lines") or 300), 2000))

    def read() -> list[str]:
        path = core.DATA_DIR / "bot.log"
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 400 * n))
                data = f.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return []
        lines = data.splitlines()[1 if size > 400 * n else 0:]
        return [core.redact(x) for x in lines[-n:]]

    return {"lines": await asyncio.to_thread(read)}


# ---- server ---------------------------------------------------------------------------------------------------


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app.router.add_get("/", page)
    app.router.add_post("/login", login)
    app.router.add_get("/api/state", api(state))
    app.router.add_get("/api/events", api(events))
    app.router.add_get("/api/jobs", api(jobs))
    app.router.add_get("/api/log", api(logtail))
    return app


async def start(core_module) -> None:
    global core, _runner, _key
    core = core_module
    _key = access_key(core.DATA_DIR, core.DASHBOARD_TOKEN)
    _runner = web.AppRunner(make_app(), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, core.DASHBOARD_HOST, core.DASHBOARD_PORT).start()
    log.info("Dashboard on http://%s:%s (%s); owners get the link with /dashboard",
             core.DASHBOARD_HOST, core.DASHBOARD_PORT, ", ".join(lan_ips()))


async def stop() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None


def link_text(angle: bool = True) -> str:
    """What /dashboard replies (owners only; the link holds the access key). angle: Discord's <url> (no preview)."""
    if _runner is None:
        return "The dashboard isn't running (DASHBOARD_PORT=0, or it couldn't start: see the log)."
    urls = links()
    return ("📊 **Dashboard**: open this on a phone or PC on the same Wi-Fi. It holds the access key, so don't "
            "share it.\n" + "\n".join(f"<{u}>" if angle else u for u in urls[:3]) +
            "\n-# The first visit stores a cookie; after that the plain address works. If it doesn't load, "
            "allow the port in Windows Firewall: `scripts\\bot_control.ps1 firewall` (as admin).")


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot dashboard</title>
<meta name="color-scheme" content="light dark">
<style>body{font:16px system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;
background:Canvas;color:CanvasText}form{width:min(92vw,360px);display:grid;gap:12px}
input,button{font:inherit;padding:12px;border-radius:10px;border:1px solid #8886}
button{background:#5865f2;color:#fff;border:0}p{margin:0;opacity:.75;font-size:14px}.e{color:#e5484d;opacity:1}</style>
</head><body><form method="post" action="/login"><h2 style="margin:0">Bot dashboard</h2>
<p>Paste the access key, or open the link from <b>/dashboard</b> in Discord or Telegram.</p>
<p class="e">{error}</p><input name="key" type="password" autocomplete="current-password" placeholder="Access key" required autofocus>
<button>Open</button></form></body></html>"""
