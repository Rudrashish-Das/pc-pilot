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
import re
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
IMAGES = {"/logo.png": Path(__file__).with_name("dashboard_logo.png"),  # also the phone home-screen icon
          "/favicon.png": Path(__file__).with_name("dashboard_favicon.png")}
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
    names = [mdns_host()] if _mdns_ip and mdns_host() else []  # the name first, while it's announced
    return [f"http://{h}:{core.DASHBOARD_PORT}/?key={_key}" for h in names + ips]


# ---- local name (mDNS) ----------------------------------------------------------------------------------------
# Announces <DASHBOARD_NAME>.local with only this PC's Wi-Fi/LAN address (Windows' own <hostname>.local also
# lists link-local 169.254.x addresses, which phones sometimes pick). Re-announced when the address changes.

_zc: Any = None  # zeroconf.asyncio.AsyncZeroconf while announcing
_mdns_ip: str | None = None
_mdns_task: asyncio.Task | None = None


def mdns_host() -> str | None:
    """'llmbot.local', or None when there's nothing to announce (no name, invalid name, or not on the LAN)."""
    name = (core.DASHBOARD_NAME or "").strip().lower().removesuffix(".local")
    if not name or core.DASHBOARD_HOST not in ("0.0.0.0", "::", ""):
        return None
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
        log.warning("DASHBOARD_NAME %r isn't a valid name (letters, digits, hyphens); not announcing it", name)
        return None
    return f"{name}.local"


def _primary_ip() -> str | None:
    ip = lan_ips()[0]
    return None if ip.startswith("127.") else ip


async def _mdns_register(ip: str) -> None:
    global _zc, _mdns_ip
    from zeroconf import IPVersion, ServiceInfo
    from zeroconf.asyncio import AsyncZeroconf

    host = mdns_host()
    info = ServiceInfo("_http._tcp.local.", f"{host.removesuffix('.local')} bot dashboard._http._tcp.local.",
                       addresses=[socket.inet_aton(ip)], port=core.DASHBOARD_PORT, server=f"{host}.",
                       properties={"path": "/"})
    zc = AsyncZeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)  # only the network the phone is on
    await zc.async_register_service(info, allow_name_change=True)
    _zc, _mdns_ip = zc, ip
    log.info("Dashboard announced as http://%s:%s (%s)", host, core.DASHBOARD_PORT, ip)


async def _mdns_unregister() -> None:
    global _zc, _mdns_ip
    zc, _zc, _mdns_ip = _zc, None, None
    if zc is not None:
        try:
            await zc.async_unregister_all_services()
        finally:
            await zc.async_close()


async def _mdns_loop() -> None:
    """Announce, then check every minute that the address is still right (new Wi-Fi, new DHCP lease, waking up)."""
    while True:
        try:
            ip = _primary_ip()
            if ip != _mdns_ip:
                await _mdns_unregister()
                if ip:
                    await _mdns_register(ip)
        except Exception as e:  # e.g. UDP 5353 unavailable: the IP links keep working
            log.warning("Couldn't announce the dashboard name %s: %s", mdns_host(), e)
            await _mdns_unregister()
        await asyncio.sleep(60)


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


_cpu_last: tuple[int, int] | None = None  # (idle, total) 100-ns ticks at the previous reading


def _cpu_times() -> tuple[int, int] | None:
    """Cumulative (idle, total) CPU time across all cores. Windows: GetSystemTimes (kernel time includes idle)."""
    if sys.platform == "win32":
        idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return idle.value, kernel.value + user.value
    try:  # Linux
        f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
        return f[3] + f[4], sum(f[:8])
    except (OSError, ValueError, IndexError):
        return None


async def cpu() -> dict | None:
    """CPU load in % since the previous reading (the dashboard polls every few seconds); the first reading samples
    over a quarter second."""
    global _cpu_last
    now = _cpu_times()
    if now is None:
        return None
    if _cpu_last is None:
        _cpu_last = now
        await asyncio.sleep(0.25)
        now = _cpu_times()
    (idle0, total0), (idle1, total1) = _cpu_last, now
    _cpu_last = now
    busy = 100.0 * (1 - (idle1 - idle0) / (total1 - total0)) if total1 > total0 else 0.0
    return {"percent": round(max(0.0, min(100.0, busy)), 1), "cores": os.cpu_count()}


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
                      "model": t.get("model"), "backend": t.get("backend"), "workspace": t.get("workspace"),
                      "perm": t.get("perm", "read"), "reminders": bool(t.get("reminders")),
                      "approved_by": core.user_label(t["approved_by"]) if t.get("approved_by") else None,
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
        "machine": {**machine(), "cpu": await cached("cpu", 2, cpu), "gpu": await cached("gpu", 2, gpu)},
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
    for route, path in IMAGES.items():  # no key needed: the login page shows them too
        app.router.add_get(route, lambda request, path=path: web.FileResponse(path, headers={"Cache-Control": "max-age=86400"}))
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
    if mdns_host():
        global _mdns_task
        _mdns_task = asyncio.create_task(_mdns_loop())


async def stop() -> None:
    global _runner, _mdns_task
    if _mdns_task is not None:
        _mdns_task.cancel()
        _mdns_task = None
    try:
        await asyncio.wait_for(_mdns_unregister(), 5)  # says goodbye, so phones forget the name right away
    except Exception:
        pass
    if _runner is not None:
        await _runner.cleanup()
        _runner = None


def link_text(angle: bool = True) -> str:
    """What /dashboard replies (owners only; the link holds the access key). angle: Discord's <url> (no preview)."""
    if _runner is None:
        return "The dashboard isn't running (DASHBOARD_PORT=0, or it couldn't start: see the log)."
    urls = links()[:3]
    fmt = (lambda u: f"<{u}>") if angle else (lambda u: u)
    host = mdns_host() if _mdns_ip else None
    if host:
        body = (f"{fmt(urls[0])}\nIf that name doesn't open (some older Android phones), use the address instead:\n"
                + "\n".join(fmt(u) for u in urls[1:]))
        after = f"after that, just `http://{host}:{core.DASHBOARD_PORT}` works (bookmark it)"
    else:
        body = "\n".join(fmt(u) for u in urls)
        after = "after that the plain address works"
    return ("📊 **Dashboard**: open this on a phone or PC on the same Wi-Fi. It holds the access key, so don't "
            f"share it.\n{body}\n-# The first visit stores a cookie; {after}. If it doesn't load, set your Wi-Fi to "
            "Private in Windows and run `scripts\\bot_control.ps1 firewall` once (as admin).")


LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bot dashboard</title><link rel="icon" type="image/png" href="/favicon.png"><link rel="apple-touch-icon" href="/logo.png">
<meta name="color-scheme" content="light dark">
<style>body{font:16px system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;
background:Canvas;color:CanvasText}form{width:min(92vw,360px);display:grid;gap:12px}
input,button{font:inherit;padding:12px;border-radius:10px;border:1px solid #8886}
button{background:#5865f2;color:#fff;border:0}p{margin:0;opacity:.75;font-size:14px}.e{color:#e5484d;opacity:1}</style>
</head><body><form method="post" action="/login"><img src="/logo.png" alt="" width="88" height="88" style="justify-self:center"><h2 style="margin:0;text-align:center">Bot dashboard</h2>
<p>Paste the access key, or open the link from <b>/dashboard</b> in Discord or Telegram.</p>
<p class="e">{error}</p><input name="key" type="password" autocomplete="current-password" placeholder="Access key" required autofocus>
<button>Open</button></form></body></html>"""
