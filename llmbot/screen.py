"""The dashboard's Screen tab: the PC's screen (and sound, see the end) streamed to a phone, and touches sent back as
mouse and keyboard.

Made for casual games and quick remote control over Wi-Fi, not fast action games (that's Sunshine + Moonlight's job).
Frames: the chosen monitor, captured with DXGI Desktop Duplication (dxcam: a few ms a frame, free while the screen is
still, and it sees many fullscreen games) or GDI as a fallback (mss: ~35 ms at 1440p), then scaled and JPEG-encoded
with Pillow, all on one worker thread. The page keeps two frame requests ("next") in flight: the Wi-Fi's round trip is
hidden, yet a slow phone or connection gets fewer frames instead of a growing delay. Only the 64 px tiles that changed
are sent (a casual game rarely changes much of the screen at once), drawn by the page over what it has; nothing when
nothing changed. The mouse pointer is drawn into the frames as it really looks (hand, text cursor, a game's own), or
sent as a position for the page to draw an arrow (moves at once, without a frame), or both, like AnyDesk.
Input: SendInput (absolute mouse on the virtual desktop; keys as scan codes, so games that read raw input see them;
typed text as Unicode). One device at a time; another one can take over.

Windows only. What it can't see or reach: the sign-in/lock screen and UAC prompts (secure desktop), windows of programs
running as administrator when the bot isn't (UIPI), and a few exclusive-fullscreen games (black: use borderless).
"""
from __future__ import annotations

import asyncio
import ctypes
import io
import json
import logging
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from aiohttp import WSMsgType, web

log = logging.getLogger("llmbot.screen")

# (max width, JPEG quality) per quality setting the page offers
QUALITY = {"low": (960, 50), "medium": (1280, 62), "high": (1920, 75)}
FPS_MAX = 60
IN_FLIGHT_MAX = 4  # frame requests the page may have outstanding (it uses 2)
REFRESH = 10.0  # resend the whole frame this often even when little changed (a safety net, ~60 KB)

_pool = ThreadPoolExecutor(1, thread_name_prefix="screen")  # capture objects belong to the thread that made them
_cap: Any = None  # Capture, used only on _pool
_viewer: web.WebSocketResponse | None = None
_session: dict | None = None  # who's watching, for status() (the Now tab, a second device's Screen tab)


def _dpi_aware() -> None:
    """Physical pixels everywhere (monitors, capture, pointer, SendInput), whatever the display scaling. Process-wide,
    but nothing else in the bot process draws windows (the /ping window is its own process)."""
    try:
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # per-monitor v2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


if sys.platform == "win32":
    from ctypes import wintypes

    _dpi_aware()
    _u32 = ctypes.WinDLL("user32", use_last_error=True)  # own instances: argtypes set here don't touch windll's
    _g32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE),
                    ("ptScreenPos", wintypes.POINT)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG), ("bmHeight", wintypes.LONG),
                    ("bmWidthBytes", wintypes.LONG), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    for fn, args, res in [
        (_u32.EnumDisplayMonitors, [wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM], wintypes.BOOL),
        (_u32.GetMonitorInfoW, [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)], wintypes.BOOL),
        (_u32.GetCursorInfo, [ctypes.POINTER(CURSORINFO)], wintypes.BOOL),
        (_u32.GetIconInfo, [wintypes.HANDLE, ctypes.POINTER(ICONINFO)], wintypes.BOOL),
        (_u32.DrawIconEx, [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
                           wintypes.UINT, wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        (_g32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
        (_g32.CreateDIBSection, [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                 ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD], wintypes.HBITMAP),
        (_g32.SelectObject, [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        (_g32.DeleteObject, [wintypes.HANDLE], wintypes.BOOL),
        (_g32.DeleteDC, [wintypes.HDC], wintypes.BOOL),
        (_g32.GetObjectW, [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p], ctypes.c_int),
        (_g32.GdiFlush, [], wintypes.BOOL),
    ]:
        fn.argtypes, fn.restype = args, res


# ---- screens and the pointer -----------------------------------------------------------------------------------


def monitors() -> list[dict]:
    """The PC's screens in physical pixels: the main one first, then left to right."""
    out: list[dict] = []

    def each(hmon, hdc, rect, data):
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if _u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            out.append({"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top,
                        "primary": bool(mi.dwFlags & 1)})
        return True

    _u32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(each), 0)
    out.sort(key=lambda m: (not m["primary"], m["left"], m["top"]))
    return out or [{"left": 0, "top": 0, "width": _u32.GetSystemMetrics(0), "height": _u32.GetSystemMetrics(1),
                    "primary": True}]


def pointer() -> tuple[int, int, int] | None:
    """(cursor handle, x, y) on the virtual desktop, or None while Windows hides the pointer (many games do)."""
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if not _u32.GetCursorInfo(ctypes.byref(ci)) or not ci.flags & 1 or not ci.hCursor:
        return None
    return ci.hCursor, ci.ptScreenPos.x, ci.ptScreenPos.y


def on_monitor(p: tuple[int, int, int] | None, mon: dict) -> tuple[float, float] | None:
    """A pointer() as fractions of the monitor (None when hidden or on another screen)."""
    if p is None:
        return None
    x, y = p[1] - mon["left"], p[2] - mon["top"]
    if not (0 <= x < mon["width"] and 0 <= y < mon["height"]):
        return None
    return round(x / max(mon["width"] - 1, 1), 4), round(y / max(mon["height"] - 1, 1), 4)


_shapes: dict[int, tuple[int, int, int, int]] = {}  # cursor handle -> (hotspot x, y, width, height)


def _cursor_shape(hcur: int) -> tuple[int, int, int, int]:
    hit = _shapes.get(hcur)
    if hit:
        return hit
    ii = ICONINFO()
    if not _u32.GetIconInfo(hcur, ctypes.byref(ii)):
        raise OSError("GetIconInfo failed")
    try:  # GetIconInfo makes copies of the cursor's bitmaps: always deleted
        bm = BITMAP()
        _g32.GetObjectW(ii.hbmMask, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        shape = (ii.xHotspot, ii.yHotspot, bm.bmWidth, bm.bmHeight if ii.hbmColor else bm.bmHeight // 2)
    finally:
        _g32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            _g32.DeleteObject(ii.hbmColor)
    if len(_shapes) > 64:  # handles of programs' own cursors can be freed and reused
        _shapes.clear()
    _shapes[hcur] = shape
    return shape


def draw_pointer(img, mon: dict, p: tuple[int, int, int]) -> None:
    """Draw the real pointer into a full-size frame. Windows draws it (DrawIconEx) onto a copy of the patch under it,
    so every kind looks right: colour, animated (first frame), and inverting ones like the text cursor."""
    from PIL import Image

    hcur, px, py = p
    hx, hy, cw, ch = _cursor_shape(hcur)
    left, top = px - mon["left"] - hx, py - mon["top"] - hy
    box = (max(0, left), max(0, top), min(img.width, left + cw), min(img.height, top + ch))
    if cw <= 0 or ch <= 0 or box[0] >= box[2] or box[1] >= box[3]:
        return
    patch = Image.new("RGB", (cw, ch))
    patch.paste(img.crop(box), (box[0] - left, box[1] - top))
    raw = patch.tobytes("raw", "BGRX")
    bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=cw, biHeight=-ch, biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    hdc = _g32.CreateCompatibleDC(None)
    if not hdc:
        return
    hbm = _g32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    if not hbm:
        _g32.DeleteDC(hdc)
        return
    old = _g32.SelectObject(hdc, hbm)
    try:
        ctypes.memmove(bits, raw, len(raw))
        _u32.DrawIconEx(hdc, 0, 0, hcur, cw, ch, 0, None, 0x3)  # DI_NORMAL
        _g32.GdiFlush()
        drawn = Image.frombuffer("RGB", (cw, ch), ctypes.string_at(bits, len(raw)), "raw", "BGRX", 0, 1)
        img.paste(drawn.crop((box[0] - left, box[1] - top, box[2] - left, box[3] - top)), box[:2])
    finally:
        _g32.SelectObject(hdc, old)
        _g32.DeleteObject(hbm)
        _g32.DeleteDC(hdc)


# ---- capture (runs on _pool) ----------------------------------------------------------------------------------


def _dxgi_camera(mon: dict):
    """dxcam's camera for the GPU output showing this monitor (laptops: the screens can be on different GPUs)."""
    import dxcam

    factory = getattr(dxcam, "__factory")  # outputs and their desktop positions aren't in dxcam's public API
    for d, outputs in enumerate(factory.outputs):
        for o, out in enumerate(outputs):
            out.update_desc()
            r = out.desc.DesktopCoordinates
            if (r.left, r.top, r.right - r.left, r.bottom - r.top) == (mon["left"], mon["top"], mon["width"],
                                                                        mon["height"]):
                return dxcam.create(device_idx=d, output_idx=o, output_color="BGRA", processor_backend="numpy",
                                    max_buffer_len=2)
    raise LookupError("no GPU output at that position (a screen plugged in after the bot started?)")


class AccessLost(Exception):
    """DXGI lost the screen: the lock screen or a UAC prompt, a display mode switch, a game going exclusive
    fullscreen, or another program capturing it."""


def _no_recovery() -> None:
    # dxcam's own recovery retries forever, sleeping, on the one capture thread: nothing else (a "can't see the
    # screen" message, the next viewer, closing) gets through until the screen comes back. Fail fast instead.
    raise AccessLost("the GPU capture lost access to the screen")


DXGI_RETRY = 5.0  # seconds on GDI after DXGI failed, before trying DXGI again


class Capture:
    """One monitor's frames: DXGI when it works, else GDI (and DXGI tried again every DXGI_RETRY seconds)."""

    def __init__(self, mon: dict) -> None:
        self.mon, self.cam, self.gdi = mon, None, None
        self.dxgi_at = 0.0  # when to try DXGI (again); inf = never (no GPU output for this screen)
        self.how = "gdi"

    def _try_dxgi(self) -> None:
        try:
            self.cam = _dxgi_camera(self.mon)
            self.cam._recover_output = _no_recovery
            self.how = "dxgi"
        except Exception as e:
            log.info("Screen: DXGI capture not available (%s: %s); using GDI", type(e).__name__, e)
            self.cam = None
            self.dxgi_at = float("inf") if isinstance(e, LookupError) else time.monotonic() + DXGI_RETRY

    def _drop_dxgi(self, why: Exception) -> None:
        log.info("Screen: %s; GDI for now", why)
        try:
            self.cam.release()
        except Exception:
            pass
        self.cam, self.how, self.dxgi_at = None, "gdi", time.monotonic() + DXGI_RETRY

    def _gdi_image(self):
        import mss
        from PIL import Image

        if self.gdi is None:
            self.gdi = (mss.MSS if hasattr(mss, "MSS") else mss.mss)()
        m = self.mon
        shot = self.gdi.grab({"left": m["left"], "top": m["top"], "width": m["width"], "height": m["height"]})
        return Image.frombuffer("RGB", shot.size, shot.bgra, "raw", "BGRX", 0, 1)

    def image(self, fresh_only: bool):
        """The screen as a PIL image (a copy, safe to draw on); None when it hasn't changed and fresh_only. Raises
        when neither way can see it (the lock screen, a UAC prompt)."""
        from PIL import Image

        if self.cam is None and time.monotonic() >= self.dxgi_at:
            self._try_dxgi()
        if self.cam is not None:
            import numpy as np

            try:
                frame = self.cam.grab(copy=False, new_frame_only=fresh_only)
            except Exception as e:  # AccessLost, or a COM error from a device that went away
                self._drop_dxgi(e)
                return self._gdi_image()
            if frame is not None:
                frame = np.ascontiguousarray(frame)
                return Image.frombuffer("RGB", (frame.shape[1], frame.shape[0]), frame, "raw", "BGRX", 0, 1)
            if fresh_only:
                return None
            # a picture is needed now but DXGI has none yet (just opened, nothing has moved): GDI's
        return self._gdi_image()

    def close(self) -> None:
        for obj, how in ((self.cam, "release"), (self.gdi, "close")):
            try:
                if obj is not None:
                    getattr(obj, how)()
            except Exception:
                pass
        self.cam = self.gdi = None


TILE = 64  # px of the scaled frame; a multiple of JPEG's 16 px blocks, so tiles join without seams
FULL_ABOVE = 0.5  # more than this much of the picture changed: one whole frame is smaller than many tiles


def changed_rects(prev, cur) -> list[tuple[int, int, int, int]]:
    """Where two frames (H x W x 3 arrays) differ, as (x, y, w, h) rectangles on the TILE grid: changed tiles joined
    into runs along each row, and identical runs in the rows below joined into taller rectangles."""
    import numpy as np

    h, w = cur.shape[:2]
    rows, cols = -(-h // TILE), -(-w // TILE)
    a, b, per = prev.reshape(h, w * 3), cur.reshape(h, w * 3), TILE * 3
    if (w * 3) % 8 == 0 and a.flags.c_contiguous and b.flags.c_contiguous:
        a, b, per = a.view(np.uint64), b.view(np.uint64), TILE * 3 // 8  # 8 bytes at a time: a tile is 24 words
    diff = a != b
    diff = np.logical_or.reduceat(diff, np.arange(0, diff.shape[1], per), axis=1)
    tiles = np.logical_or.reduceat(diff, np.arange(0, h, TILE), axis=0)
    rects: list[list[int]] = []
    open_runs: dict[tuple[int, int], list[int]] = {}  # (first col, last col) -> the rectangle growing downwards
    for r in range(rows):
        runs, c = [], 0
        while c < cols:
            if tiles[r, c]:
                start = c
                while c + 1 < cols and tiles[r, c + 1]:
                    c += 1
                runs.append((start, c))
            c += 1
        grown = {}
        for run in runs:
            rect = open_runs.get(run)
            if rect is not None and rect[1] + rect[3] == r * TILE:
                rect[3] += TILE
            else:
                rect = [run[0] * TILE, r * TILE, (run[1] - run[0] + 1) * TILE, TILE]
                rects.append(rect)
            grown[run] = rect
        open_runs = grown
    return [(x, y, min(rw, w - x), min(rh, h - y)) for x, y, rw, rh in rects]


def grab(mon: dict, width: int, quality: int, fresh_only: bool, draw: tuple[int, int, int] | None,
         ref: dict, full: bool) -> tuple[list | None, int, int, tuple[int, int], float]:
    """(parts, the monitor's full width and height, the frame's size, ms spent). parts: [(x, y, w, h, JPEG)] of what
    changed since the frame in ref["prev"] (what this viewer already has), or the whole frame when full or when most
    of it changed; [] or None when nothing did. draw: the pointer to draw in (pointer()), or None."""
    global _cap
    import numpy as np
    from PIL import Image

    t0 = time.perf_counter()
    if _cap is None or _cap.mon != mon:
        _reset_capture()
        _cap = Capture(mon)
    img = _cap.image(fresh_only and not full)
    if img is None:
        return None, mon["width"], mon["height"], (0, 0), 0.0
    if draw is not None:
        try:
            draw_pointer(img, mon, draw)
        except OSError:
            pass  # the cursor went away in between
    if img.width > width:
        factor = img.width // width
        if factor >= 2 and img.width / factor <= width * 1.15:  # integer reduce: several times faster than resize
            img = img.reduce(factor)
        if img.width > width * 1.15:
            img = img.resize((width, round(img.height * width / img.width)), Image.BILINEAR)
    cur = np.asarray(img)
    prev = ref.get("prev")
    if full or prev is None or prev.shape != cur.shape:
        rects = [(0, 0, img.width, img.height)]
    else:
        rects = changed_rects(prev, cur)
        if sum(w * h for _, _, w, h in rects) > FULL_ABOVE * img.width * img.height:
            rects = [(0, 0, img.width, img.height)]
    parts = []
    for x, y, w, h in rects:
        buf = io.BytesIO()
        (img if (w, h) == img.size else img.crop((x, y, x + w, y + h))).save(buf, "JPEG", quality=quality)
        parts.append((x, y, w, h, buf.getvalue()))
    ref["prev"] = cur
    return parts, mon["width"], mon["height"], img.size, (time.perf_counter() - t0) * 1000


def _reset_capture() -> None:
    global _cap
    if _cap is not None:
        _cap.close()
    _cap = None


# ---- input (SendInput) ----------------------------------------------------------------------------------------

if sys.platform == "win32":
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


def to_absolute(nx: float, ny: float, mon: dict) -> tuple[int, int]:
    """A point on a monitor as fractions (0..1) -> SendInput's 0..65535 over the whole virtual desktop."""
    u = ctypes.windll.user32
    vx, vy, vw, vh = (u.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    x = mon["left"] + min(max(nx, 0.0), 1.0) * (mon["width"] - 1)
    y = mon["top"] + min(max(ny, 0.0), 1.0) * (mon["height"] - 1)
    return round((x - vx) * 65535 / max(vw - 1, 1)), round((y - vy) * 65535 / max(vh - 1, 1))


class Input:
    """One viewer's input on the monitor it's watching. Remembers what it's holding down, to let go of it all when
    the viewer leaves."""

    def __init__(self, mon: dict) -> None:
        self.mon = mon
        self.buttons: set[str] = set()
        self.keys: set[int] = set()

    def handle(self, m: dict) -> None:
        t = m.get("t")
        if t in ("move", "down", "up", "click") and "x" in m and "y" in m:
            ax, ay = to_absolute(float(m["x"]), float(m["y"]), self.mon)
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
        return "Screen control needs more packages: run  pip install -r requirements.txt  and restart the bot."
    return None


def device_name(ua: str) -> str:
    """A User-Agent -> 'Android phone · Chrome', 'iPhone · Safari', 'Windows PC · Edge'..."""
    ua = ua or ""
    kind = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua
            else "Android phone" if "Android" in ua and "Mobile" in ua else "Android tablet" if "Android" in ua
            else "Windows PC" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux PC" if "Linux" in ua
            else "a device")
    browser = next((name for key, name in (("EdgA/", "Edge"), ("Edg/", "Edge"), ("SamsungBrowser", "Samsung Internet"),
                                           ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
                                           ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
                    if key in ua), "")
    return f"{kind} · {browser}" if browser else kind


def status() -> dict | None:
    """The stream right now, or None: {who (IP), device, since (epoch s), monitor (1-based), monitors, fps, kbps,
    ms (the PC's time per frame), quality, cursor, audio}."""
    if _session is None or _viewer is None or _viewer.closed:
        return None
    now = time.monotonic()
    recent = [(t, n, ms) for t, n, ms in _session["sent"] if now - t <= 2.0]
    return {**{k: v for k, v in _session.items() if k not in ("sent", "st", "ua")},
            "monitor": _session["st"]["mon_i"] + 1, "quality": _session["st"]["quality"],
            "cursor": _session["st"]["cursor"], "fps": round(len(recent) / 2, 1),
            "kbps": round(sum(n for _, n, _ in recent) * 8 / 2 / 1000),
            "ms": round(sum(ms for *_, ms in recent) / len(recent), 1) if recent else None}


def _monitor_list(mons: list[dict]) -> list[dict]:
    return [{"w": m["width"], "h": m["height"], "primary": m["primary"]} for m in mons]


async def serve(request: web.Request, core) -> web.WebSocketResponse:
    """Messages from the page (JSON): next (one more frame wanted), opts {quality, fps, monitor, cursor: "arrow" |
    "real" | "both"}, and input (see Input.handle). To the page: {monitors: [{w, h, primary}], monitor} at the
    start and when they change; {screen: [w, h], cursor: [x, y] | null, frame} when the picture or the pointer
    changed. With frame true it also has size [w, h] (the scaled frame), parts [[x, y, w, h, bytes]] (what changed,
    or the whole frame) and ms (the PC's time for it), and one binary message follows: the parts' JPEGs, one after
    another. {status} and {error}.
    Someone else already watching: {busy: status()} and closed, unless ?take=1 (the page's Take over button) or it's
    the same device coming back (a reload, a dropped Wi-Fi the server hasn't noticed yet)."""
    global _viewer, _session
    ws = web.WebSocketResponse(heartbeat=20, compress=False, max_msg_size=64 * 1024)
    await ws.prepare(request)
    why = unavailable(core)
    if why:
        await ws.send_json({"error": why})
        await ws.close()
        return ws
    who, ua = request.remote, request.headers.get("User-Agent", "")
    device = device_name(ua)
    if _viewer is not None and not _viewer.closed:
        same = _session is not None and (_session["who"], _session["ua"]) == (who, ua)
        if request.query.get("take") != "1" and not same:
            await ws.send_json({"busy": status()})
            await ws.close()
            return ws
        try:
            await _viewer.send_json({"error": "This device was disconnected." if same else
                                     f"Taken over by {device} ({who}).", "taken": True})
        except Exception:  # already gone
            pass
        await _viewer.close()
    _viewer = ws
    started = time.monotonic()
    core.note_event("bot", f"Screen control opened from {device} ({who})", level="warning")
    log.info("Screen control opened from %s (%s)", device, who)

    loop = asyncio.get_running_loop()
    mons = monitors()
    st = {"quality": "medium", "fps": 30, "mon": mons[0], "mon_i": 0, "cursor": "arrow", "credits": 0, "force": True}
    sent: deque = deque(maxlen=FPS_MAX * 3)  # (time, bytes, ms) of recent frames, for status()
    _session = {"who": who, "ua": ua, "device": device, "since": time.time(), "monitors": len(mons), "st": st,
                "sent": sent, "audio": False}
    inp = Input(st["mon"])
    wake = asyncio.Event()
    await ws.send_json({"monitors": _monitor_list(mons), "monitor": 0})

    async def frames() -> None:
        last_full, last_ptr, last_pos, failing = time.monotonic(), None, None, False
        ref: dict = {}  # the frame this page has, to send only what changed
        while not ws.closed:
            while st["credits"] <= 0:
                wake.clear()
                await wake.wait()
            t0 = time.monotonic()
            mon = st["mon"]
            real, arrow = st["cursor"] in ("real", "both"), st["cursor"] in ("arrow", "both")
            width, quality = QUALITY.get(st["quality"], QUALITY["medium"])
            ptr = pointer()
            pos = on_monitor(ptr, mon)
            redraw = real and ptr != last_ptr  # the drawn pointer moved or changed shape
            full = st["force"] or time.monotonic() - last_full > REFRESH
            try:
                parts, w, h, size, ms = await loop.run_in_executor(
                    _pool, grab, mon, width, quality, not (full or redraw), ptr if real and pos is not None else None,
                    ref, full)
            except Exception as e:  # locked, UAC prompt, display mode change
                if not failing:
                    log.info("Screen capture failed: %s: %s", type(e).__name__, e)
                    await ws.send_json({"status": "Can't see the screen right now (locked, a UAC prompt, or the "
                                                  "display changing). Retrying…"})
                failing = True
                await loop.run_in_executor(_pool, _reset_capture)
                await asyncio.sleep(1)
                continue
            if failing:
                failing = False
                await ws.send_json({"status": ""})
            if parts is not None:
                last_ptr = ptr
                if full:
                    st["force"], last_full = False, time.monotonic()
            changed = bool(parts)
            moved = arrow and pos != last_pos
            if changed or moved:  # the arrow alone moving is just its position, no frame
                st["credits"] -= 1
                last_pos = pos
                head = {"screen": [w, h], "cursor": pos if arrow else None, "frame": changed}
                if changed:  # the parts' places and lengths; their JPEGs follow in one binary message
                    head.update(size=list(size), parts=[[x, y, pw, ph, len(j)] for x, y, pw, ph, j in parts],
                                ms=round(ms, 1))
                await ws.send_json(head)
                if changed:
                    data = b"".join(j for *_, j in parts)
                    await ws.send_bytes(data)
                    sent.append((time.monotonic(), len(data), ms))
            await asyncio.sleep(max(0.0, 1 / st["fps"] - (time.monotonic() - t0)))

    async def frames_or_close() -> None:
        try:
            await frames()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # the connection dropped mid-send, or a bug: don't leave a page with a frozen picture
            log.info("Screen stream stopped: %s: %s", type(e).__name__, e)
            await ws.close()

    sender = asyncio.create_task(frames_or_close())
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
                st["credits"] = min(IN_FLIGHT_MAX, st["credits"] + 1)
                wake.set()
            elif m.get("t") == "opts":
                if m.get("quality") in QUALITY:
                    st["quality"] = m["quality"]
                if isinstance(m.get("fps"), (int, float)):
                    st["fps"] = max(1, min(FPS_MAX, int(m["fps"])))
                if m.get("cursor") in ("arrow", "real", "both"):
                    st["cursor"] = m["cursor"]
                if isinstance(m.get("monitor"), int):
                    mons = monitors()  # screens plugged in or out since the page opened
                    i = m["monitor"] if 0 <= m["monitor"] < len(mons) else 0
                    if mons[i] != st["mon"]:
                        inp.release()  # don't carry a held button over to another screen
                        st["mon"] = inp.mon = mons[i]
                    st["mon_i"], _session["monitors"] = i, len(mons)
                    await ws.send_json({"monitors": _monitor_list(mons), "monitor": i})
                st["force"] = True  # re-send now with the new settings, even if the screen is still
            else:
                try:
                    inp.handle(m)
                except (TypeError, ValueError) as e:
                    log.debug("bad screen input %r: %s", m, e)
    finally:
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, Exception):
            pass
        inp.release()
        if _viewer is ws:  # nobody took over: free the capture (GPU duplication, screen-sized buffers)
            _viewer = _session = None
            await loop.run_in_executor(_pool, _reset_capture)
        minutes = (time.monotonic() - started) / 60
        core.note_event("bot", f"Screen control closed ({device}, {who}, {minutes:.0f} min)")
        log.info("Screen control closed (%s, %s, %.1f min)", device, who, minutes)
    return ws


# ---- sound ------------------------------------------------------------------------------------------------------
# What the PC is playing (WASAPI loopback of the default speakers, via soundcard), to the device that's watching, on
# its own WebSocket: frames are pulled, sound is pushed. 16-bit PCM, 48 kHz stereo, 20 ms per message (~1.5 Mbit/s
# while something plays; silence isn't sent). The page plays it with Web Audio, which works on plain http.

AUDIO_RATE, AUDIO_CHANNELS, AUDIO_BLOCK = 48000, 2, 960  # 960 frames = 20 ms
AUDIO_QUEUE = 15  # 300 ms: when the Wi-Fi can't keep up, the oldest sound is dropped instead of falling behind
_audio_ws: web.WebSocketResponse | None = None


def _record(loop: asyncio.AbstractEventLoop, put, stop: threading.Event, failed: list) -> None:
    """The loopback recording, on its own thread until stop is set."""
    import warnings

    import numpy as np
    import soundcard as sc

    warnings.filterwarnings("ignore", message="data discontinuity")  # the gaps when sound starts and stops
    try:
        speaker = sc.default_speaker()
        mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        with mic.recorder(samplerate=AUDIO_RATE, channels=AUDIO_CHANNELS, blocksize=AUDIO_BLOCK) as rec:
            while not stop.is_set():
                data = rec.record(numframes=AUDIO_BLOCK)
                if np.abs(data).max() < 1e-4:  # silence
                    continue
                pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                loop.call_soon_threadsafe(put, pcm)
    except Exception as e:
        failed.append(e)
        loop.call_soon_threadsafe(put, None)


def _is_viewer(request: web.Request) -> bool:
    return (_session is not None and _viewer is not None and not _viewer.closed
            and (_session["who"], _session["ua"]) == (request.remote, request.headers.get("User-Agent", "")))


async def serve_audio(request: web.Request, core) -> web.WebSocketResponse:
    """The sound for the device that's watching the screen: {rate, channels} first, then binary 16-bit PCM
    (interleaved, little-endian); {error} when it can't. Ends when that device stops watching."""
    global _audio_ws
    ws = web.WebSocketResponse(heartbeat=20, compress=False, max_msg_size=4096)
    await ws.prepare(request)
    why = unavailable(core)
    if not why:
        try:
            import soundcard  # noqa: F401
        except Exception as e:  # not installed, or no audio service
            why = (f"Sound isn't available: {type(e).__name__}: {e}. Run  pip install -r requirements.txt  and "
                   "restart the bot.")
    if not why and not _is_viewer(request):
        why = "Open the Screen tab on this device first: sound goes to the device that's watching."
    if why:
        await ws.send_json({"error": why})
        await ws.close()
        return ws
    if _audio_ws is not None and not _audio_ws.closed:
        await _audio_ws.close()
    _audio_ws = ws
    viewer = _viewer
    if _session is not None:
        _session["audio"] = True

    loop = asyncio.get_running_loop()
    queue: deque = deque(maxlen=AUDIO_QUEUE)
    ready = asyncio.Event()
    stop, failed = threading.Event(), []

    def put(pcm: bytes | None) -> None:
        queue.append(pcm)
        ready.set()

    recorder = threading.Thread(target=_record, args=(loop, put, stop, failed), name="screen-audio", daemon=True)
    recorder.start()
    log.info("Screen sound on for %s", request.remote)

    async def send() -> None:
        await ws.send_json({"rate": AUDIO_RATE, "channels": AUDIO_CHANNELS})
        while not ws.closed:
            if viewer is not _viewer or viewer.closed:  # the screen stopped (tab closed, taken over)
                await ws.close()
                return
            try:
                await asyncio.wait_for(ready.wait(), 1)
            except asyncio.TimeoutError:
                continue
            ready.clear()
            while queue:
                pcm = queue.popleft()
                if pcm is None:
                    e = failed[0] if failed else None
                    await ws.send_json({"error": f"Sound stopped: {type(e).__name__}: {e}"})
                    await ws.close()
                    return
                await ws.send_bytes(pcm)

    sender = asyncio.create_task(send())
    try:
        async for _ in ws:  # nothing comes from the page; this ends when it closes
            pass
    finally:
        sender.cancel()
        try:
            await sender
        except (asyncio.CancelledError, Exception):
            pass
        stop.set()
        await loop.run_in_executor(None, recorder.join, 2)
        if _audio_ws is ws:
            _audio_ws = None
            if _session is not None and _viewer is viewer:
                _session["audio"] = False
        log.info("Screen sound off for %s", request.remote)
    return ws
