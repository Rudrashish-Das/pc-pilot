"""The dashboard's Screen tab: the PC's screen streamed to a phone, and touches sent back as mouse and keyboard.

Made for casual games and quick remote control over Wi-Fi, not fast action games (that's Sunshine + Moonlight's job).
Frames: the primary monitor, captured with mss (GDI) and scaled and JPEG-encoded with Pillow on one worker thread. The
page asks for each frame ("next") after drawing the previous one, so a slow phone or Wi-Fi gets fewer frames instead of
a growing delay, and a frame identical to the last one isn't resent. Input: SendInput (absolute mouse on the virtual
desktop; keys as scan codes, so games that read raw input see them; typed text as Unicode). One viewer at a time.

Windows only. What it can't see or reach: the sign-in/lock screen and UAC prompts (secure desktop), windows of programs
running as administrator when the bot isn't (UIPI), and some exclusive-fullscreen games (captured black: use
borderless or windowed mode).
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from aiohttp import WSMsgType, web

log = logging.getLogger("llmbot.screen")

# (max width, JPEG quality) per quality setting the page offers
QUALITY = {"low": (960, 50), "medium": (1280, 62), "high": (1920, 75)}
FPS_MAX = 30
KEEPALIVE = 2.0  # resend an unchanged frame this often, so the page knows the stream is alive

_pool = ThreadPoolExecutor(1, thread_name_prefix="screen")  # mss objects belong to the thread that made them
_mss: Any = None
_viewer: web.WebSocketResponse | None = None


# ---- capture (runs on _pool) ----------------------------------------------------------------------------------


def _dpi_aware() -> None:
    """Physical pixels everywhere (capture, cursor position, SendInput), whatever the display scaling."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def grab(width: int, quality: int) -> tuple[bytes, int, int]:
    """(JPEG of the primary monitor at most `width` wide, its full width, its full height)."""
    global _mss
    import mss
    from PIL import Image

    if _mss is None:
        _dpi_aware()
        _mss = (mss.MSS if hasattr(mss, "MSS") else mss.mss)()
    mon = _mss.monitors[1]
    shot = _mss.grab(mon)
    img = Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)
    if img.width > width:
        factor = img.width // width
        if factor >= 2 and img.width / factor <= width * 1.15:  # integer reduce: several times faster than resize
            img = img.reduce(factor)
        if img.width > width * 1.15:
            img = img.resize((width, round(img.height * width / img.width)), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), shot.width, shot.height


def _reset_capture() -> None:
    global _mss
    try:
        if _mss is not None:
            _mss.close()
    except Exception:
        pass
    _mss = None


# ---- input (SendInput) ----------------------------------------------------------------------------------------

if sys.platform == "win32":
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

MOVE, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
WHEEL, HWHEEL = 0x0800, 0x1000
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
KEY_EXTENDED, KEY_UP, KEY_UNICODE, KEY_SCANCODE = 0x1, 0x2, 0x4, 0x8

KEYS = {"escape": 0x1B, "esc": 0x1B, "enter": 0x0D, "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
        "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
        "left": 0x25, "right": 0x27, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B,
        **{f"f{i}": 0x6F + i for i in range(1, 13)}}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B}  # need the E0 prefix as scan codes


def vk_of(name: str) -> int | None:
    """'Escape', 'Up', 'F5', 'w', '7' -> a virtual-key code (None if unknown)."""
    n = name.strip().lower()
    if n in KEYS:
        return KEYS[n]
    if len(n) == 1 and (n.isascii() and n.isalnum()):
        return ord(n.upper())
    return None


def _send(*inputs) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0):
    return INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


def _key(vk: int, up: bool):
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = (KEY_SCANCODE if scan else 0) | (KEY_EXTENDED if vk in EXTENDED else 0) | (KEY_UP if up else 0)
    return INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0 if scan else vk, scan, flags, 0, 0)))


def screen_size() -> tuple[int, int]:
    """The primary monitor in physical pixels (it's always at 0,0 of the virtual desktop)."""
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def to_absolute(nx: float, ny: float) -> tuple[int, int]:
    """A point on the primary monitor as fractions (0..1) -> SendInput's 0..65535 over the whole virtual desktop."""
    u = ctypes.windll.user32
    w, h = screen_size()
    vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    x = min(max(nx, 0.0), 1.0) * (w - 1)
    y = min(max(ny, 0.0), 1.0) * (h - 1)
    return round((x - vx) * 65535 / max(vw - 1, 1)), round((y - vy) * 65535 / max(vh - 1, 1))


def cursor() -> tuple[float, float] | None:
    """Where the mouse pointer is, as fractions of the primary monitor (None when it's on another one)."""
    pt = wintypes.POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
        return None
    w, h = screen_size()
    if not (0 <= pt.x < w and 0 <= pt.y < h):
        return None
    return round(pt.x / max(w - 1, 1), 4), round(pt.y / max(h - 1, 1), 4)


class Input:
    """One viewer's input. Remembers what it's holding down, to let go of it all when the viewer leaves."""

    def __init__(self) -> None:
        self.buttons: set[str] = set()
        self.keys: set[int] = set()

    def handle(self, m: dict) -> None:
        t = m.get("t")
        if t in ("move", "down", "up", "click") and "x" in m and "y" in m:
            ax, ay = to_absolute(float(m["x"]), float(m["y"]))
            _send(_mouse(MOVE | ABSOLUTE | VIRTUALDESK, ax, ay))
        if t == "rel":
            dx, dy = (max(-2000, min(2000, int(m.get(k) or 0))) for k in ("dx", "dy"))
            _send(_mouse(MOVE, dx, dy))
        elif t in ("down", "up", "click"):
            b = m.get("b") if m.get("b") in BUTTONS else "left"
            down, up = BUTTONS[b]
            if t == "down":
                self.buttons.add(b)
                _send(_mouse(down))
            elif t == "up":
                self.buttons.discard(b)
                _send(_mouse(up))
            else:
                for _ in range(2 if m.get("double") else 1):
                    _send(_mouse(down), _mouse(up))
        elif t == "wheel":
            dy, dx = (max(-1200, min(1200, int(m.get(k) or 0))) for k in ("dy", "dx"))
            if dy:
                _send(_mouse(WHEEL, data=dy))
            if dx:
                _send(_mouse(HWHEEL, data=dx))
        elif t == "key":
            vks = [vk_of(k) for k in str(m.get("k") or "").split("+")]
            if not vks or None in vks:
                return
            a = m.get("a", "tap")
            if a in ("down", "tap"):
                self.keys.update(vks)
                _send(*(_key(vk, False) for vk in vks))
            if a in ("up", "tap"):
                self.keys.difference_update(vks)
                _send(*(_key(vk, True) for vk in reversed(vks)))
        elif t == "text":
            text = str(m.get("s") or "")[:500]
            events = []
            for ch in text.replace("\r\n", "\n"):
                if ch == "\n":
                    events += [_key(0x0D, False), _key(0x0D, True)]
                    continue
                units = ch.encode("utf-16-le")  # emoji etc. are two units (a surrogate pair)
                for i in range(0, len(units), 2):
                    code = int.from_bytes(units[i:i + 2], "little")
                    events += [INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEY_UNICODE, 0, 0))),
                               INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEY_UNICODE | KEY_UP, 0, 0)))]
            if events:
                _send(*events)

    def release(self) -> None:
        for b in list(self.buttons):
            _send(_mouse(BUTTONS[b][1]))
        if self.keys:
            _send(*(_key(vk, True) for vk in self.keys))
        self.buttons.clear()
        self.keys.clear()


# ---- the WebSocket ---------------------------------------------------------------------------------------------


def unavailable(core) -> str | None:
    """Why the screen can't be shared right now, or None."""
    if sys.platform != "win32":
        return "Screen control works on Windows only."
    if not core.DASHBOARD_SCREEN:
        return "Screen control is off (DASHBOARD_SCREEN=false in .env)."
    if core.in_background_session():
        return ("The PC restarted and nobody has signed in yet, so there's no desktop to show. Sign in to Windows "
                "once (the bot moves to your session by itself), then open this again.")
    try:
        import mss  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        return "Screen control needs two packages: run  pip install -r requirements.txt  and restart the bot."
    return None


async def serve(request: web.Request, core) -> web.WebSocketResponse:
    """Messages from the page (JSON): next, opts {quality, fps}, and input (see Input.handle). To the page: JSON
    {screen: [w, h], cursor: [x, y] | null, frame} when the screen or the pointer changed, then the frame (binary
    JPEG) when frame is true; {status} and {error}."""
    global _viewer
    ws = web.WebSocketResponse(heartbeat=20, compress=False, max_msg_size=64 * 1024)
    await ws.prepare(request)
    why = unavailable(core)
    if why:
        await ws.send_json({"error": why})
        await ws.close()
        return ws
    if _viewer is not None and not _viewer.closed:
        await _viewer.send_json({"error": "The screen was opened on another device."})
        await _viewer.close()
    _viewer = ws
    started, who = time.monotonic(), request.remote
    core.note_event("bot", f"Screen control opened from {who}", level="warning")
    log.info("Screen control opened from %s", who)

    loop = asyncio.get_running_loop()
    inp = Input()
    want = asyncio.Event()
    opts = {"quality": "medium", "fps": 15}

    async def frames() -> None:
        last, last_at, last_pointer, failing = b"", 0.0, None, False
        while not ws.closed:
            await want.wait()
            width, quality = QUALITY.get(opts["quality"], QUALITY["medium"])
            t0 = time.monotonic()
            try:
                jpeg, w, h = await loop.run_in_executor(_pool, grab, width, quality)
            except Exception as e:  # locked, UAC prompt, display mode change
                if not failing:
                    log.info("Screen capture failed: %s", e)
                    await ws.send_json({"status": "Can't see the screen right now (locked, a UAC prompt, or the "
                                                  "display changing). Retrying…"})
                failing = True
                await loop.run_in_executor(_pool, _reset_capture)
                await asyncio.sleep(1)
                continue
            if failing:
                failing = False
                await ws.send_json({"status": ""})
            # The pointer isn't in the capture (GDI leaves it out): when only it moved, send just its position
            changed = jpeg != last or time.monotonic() - last_at > KEEPALIVE
            pointer = cursor()
            if changed or pointer != last_pointer:
                want.clear()
                last_pointer = pointer
                await ws.send_json({"screen": [w, h], "cursor": pointer, "frame": changed})
                if changed:
                    last, last_at = jpeg, time.monotonic()
                    await ws.send_bytes(jpeg)
            await asyncio.sleep(max(0.0, 1 / max(1, min(FPS_MAX, opts["fps"])) - (time.monotonic() - t0)))

    sender = asyncio.create_task(frames())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            if m.get("t") == "next":
                want.set()
            elif m.get("t") == "opts":
                if m.get("quality") in QUALITY:
                    opts["quality"] = m["quality"]
                opts["fps"] = max(1, min(FPS_MAX, int(m.get("fps") or opts["fps"])))
            else:
                try:
                    inp.handle(m)
                except (TypeError, ValueError) as e:
                    log.debug("bad screen input %r: %s", m, e)
    finally:
        sender.cancel()
        inp.release()
        if _viewer is ws:
            _viewer = None
        minutes = (time.monotonic() - started) / 60
        core.note_event("bot", f"Screen control closed ({who}, {minutes:.0f} min)")
        log.info("Screen control closed (%s, %.1f min)", who, minutes)
    return ws
