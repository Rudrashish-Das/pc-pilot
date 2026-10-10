"""What /status reports and /ping shows: the game being played (and since when), how long the PC has been on, and a
dialog on the PC's screen for a ping. Windows only; elsewhere every answer is "unknown"."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

GAMES_REFRESH = 600  # seconds between re-reading the list of games Windows knows
# Where game stores install games: the folder after these is the game's own (and a good name for it)
LIBRARY = re.compile(r"\\(?:steamapps\\common|epic games|riot games|xboxgames|gog games|ea games|"
                     r"ubisoft game launcher\\games|games)\\([^\\]+)\\", re.I)
NOT_GAMES = re.compile(r"^(?:riot client|steamworks shared|steam controller configs|gamesave|launcher|epic online services|"
                       r"directxredist|_commonredist)$", re.I)
# Launchers, crash reporters, anti-cheat and installers that live next to games
HELPER = re.compile(r"crash|report|launcher|setup|unins|redist|anticheat|^eac|beservice|battleye|cef|"
                    r"webhelper|updater|install|prereq|riotclient|socialclub|overlay|service|helper", re.I)
GENERIC_HOSTS = {"javaw.exe", "java.exe", "python.exe", "pythonw.exe"}  # run games, but mostly other things
JUNK_PRODUCT = re.compile(r"java|unreal|unity|microsoft|bootstrap", re.I)
SUFFIXES = re.compile(r"(?:-win64-shipping|_x64|x64|64|_dx1[12]|_vulkan|vk|_f|_rwdi)$", re.I)

_known: tuple[float, set[str]] = (-1e9, set())
_names: dict[str, str] = {}


def known_game_exes() -> set[str]:
    """Executables Windows' Game Bar has recognised as games on this PC (lowercase full paths)."""
    global _known
    if time.monotonic() - _known[0] < GAMES_REFRESH:
        return _known[1]
    found = set()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                            found.add(str(winreg.QueryValueEx(k, "MatchedExeFullPath")[0]).lower())
                    except OSError:
                        pass
        except OSError:
            pass
    _known = (time.monotonic(), {p for p in found if Path(p).name not in GENERIC_HOSTS})
    return _known[1]


def _product_name(exe: str) -> str:
    """The ProductName in an executable's version info, or ''."""
    import ctypes
    from ctypes import wintypes
    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(exe, None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(exe, 0, size, buf):
        return ""
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(n)) or n.value < 4:
        return ""
    lang, cp = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
    if not ver.VerQueryValueW(buf, rf"\StringFileInfo\{lang:04x}{cp:04x}\ProductName", ctypes.byref(ptr),
                              ctypes.byref(n)) or not n.value:
        return ""
    return ctypes.wstring_at(ptr, n.value).rstrip("\0").strip()


def game_name(exe: str) -> str:
    if exe in _names:
        return _names[exe]
    name = ""
    try:
        name = _product_name(exe) if sys.platform == "win32" else ""
    except OSError:
        pass
    if not name or JUNK_PRODUCT.search(name):
        m = LIBRARY.search(exe)
        name = m.group(1) if m else SUFFIXES.sub("", Path(exe).stem).strip(" -_") or Path(exe).stem
    _names[exe] = name
    return name


def is_game(exe: str, known: set[str]) -> bool:
    low = exe.lower()
    if low in known:
        return True
    m = LIBRARY.search(exe)
    return bool(m) and not NOT_GAMES.match(m.group(1)) and not HELPER.search(Path(exe).stem)


def running_games() -> list[tuple[str, float]]:
    """(name, started at) for each game running now, the longest-running first."""
    try:
        import psutil
    except ImportError:
        return []
    known, games = known_game_exes(), {}
    by_name = {Path(k).name: k for k in known}
    for p in psutil.process_iter(["exe", "name", "create_time"]):
        exe, started = p.info.get("exe"), p.info.get("create_time")
        if not exe:  # path hidden (an anti-cheat protecting the game): the file name still matches a known game
            exe = by_name.get((p.info.get("name") or "").lower())
        if not exe or not started or not is_game(exe, known):
            continue
        name = game_name(exe)
        games[name] = min(started, games.get(name, started))
    return sorted(games.items(), key=lambda g: g[1])


_EVENT_QUERY = ("*[System[(Provider[@Name='Microsoft-Windows-Kernel-General'] and EventID=12) or "
                "(Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1)]]")


def _iso(s: str) -> float:
    s = re.sub(r"(\.\d{6})\d*", r"\1", s.strip()).replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def on_since() -> float | None:
    """When the PC last started or woke from sleep/hibernation (Fast Startup's "shut down" is a hibernation, so the
    boot time alone can be days old). From the System event log; falls back to the boot time."""
    if sys.platform == "win32":
        try:
            r = subprocess.run(["wevtutil", "qe", "System", f"/q:{_EVENT_QUERY}", "/c:1", "/rd:true", "/f:xml"],
                               capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            m = re.search(r"Name='(?:StartTime|WakeTime)'>([^<]+)<", r.stdout)
            if m:
                return _iso(m.group(1))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
    try:
        import psutil
        return psutil.boot_time()
    except ImportError:
        return None


def desktop() -> str:
    """'ok' when a dialog from this process shows on the screen, 'locked' when it will once someone unlocks it,
    'away' when nobody is signed in, 'background' when someone is but this copy runs in Windows' background session
    (started at boot: no screen to show it on until the copy on the desktop takes over, about a minute)."""
    if sys.platform != "win32":
        return "away"
    import ctypes
    k32, wts = ctypes.windll.kernel32, ctypes.windll.wtsapi32
    screen, mine = k32.WTSGetActiveConsoleSessionId(), ctypes.c_ulong()
    if screen == 0xFFFFFFFF:
        return "away"
    buf, size = ctypes.c_void_p(), ctypes.c_ulong()
    # WTSUserName (5): empty at the sign-in screen. Asked by session id, so it works from the background session too
    user = ""
    if wts.WTSQuerySessionInformationW(None, screen, 5, ctypes.byref(buf), ctypes.byref(size)):
        try:
            user = ctypes.wstring_at(buf.value) if buf.value else ""
        finally:
            wts.WTSFreeMemory(buf)
    if not user:
        return "away"
    if not k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(mine)) or mine.value != screen:
        return "background"
    # WTSSessionInfoEx (25): its SessionFlags say whether the session is locked
    if wts.WTSQuerySessionInformationW(None, screen, 25, ctypes.byref(buf), ctypes.byref(size)):
        try:
            # WTSINFOEXW: Level (DWORD), padding (the union holds 8-byte times), then WTSINFOEX_LEVEL1_W { SessionId,
            # SessionState, SessionFlags, ... }
            flags = ctypes.cast(buf, ctypes.POINTER(ctypes.c_long * 5)).contents[4]
        finally:
            wts.WTSFreeMemory(buf)
        if flags == 0:  # WTS_SESSIONSTATE_LOCK
            return "locked"
    return "ok"


class PingWindow:
    """One window on the screen for every ping (llmbot/pingbox.py, its own process): a ping while it's open is added
    to it instead of opening another. Answering it answers them all."""

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.waiting: list[tuple[object, str, asyncio.Future]] = []  # (who to answer, message, answer) for self.proc

    def is_open(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _start(self) -> None:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        self.proc = await asyncio.create_subprocess_exec(
            str(pythonw if pythonw.exists() else exe), "-m", "llmbot.pingbox", cwd=str(Path(__file__).parent.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.waiting = []
        asyncio.ensure_future(self._watch(self.proc, self.waiting))

    async def show(self, key: object, sender: str, message: str, source: str = "") -> str | None:
        """Show a ping and wait for the answer: the reply, '' when the window was closed without one, or None when
        a later ping with the same key (the same chat) takes the answer instead. Raises RuntimeError when the window
        couldn't be shown."""
        line = (json.dumps({"from": sender, "message": message, "source": source}) + "\n").encode()
        fut = asyncio.get_running_loop().create_future()
        for _ in range(2):
            if not self.is_open():
                await self._start()
            waiting = self.waiting
            waiting.append((key, message, fut))  # before writing: the window may answer right away
            try:
                self.proc.stdin.write(line)
                await self.proc.stdin.drain()
                break
            except (BrokenPipeError, ConnectionResetError):  # closed just now: open a new one
                waiting.remove((key, message, fut))
                self.proc = None
        return await fut

    async def _watch(self, proc: asyncio.subprocess.Process, waiting: list) -> None:
        out, err = await asyncio.gather(proc.stdout.read(), proc.stderr.read())
        await proc.wait()
        if self.proc is proc:
            self.proc = None
        try:  # an answer it printed counts even if the process then exited badly
            reply = str(json.loads(out.decode())["reply"] or "")
        except (ValueError, KeyError, TypeError):
            reply = None
        if reply is None:
            error = RuntimeError(err.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}")
            for _, _, fut in waiting:
                if not fut.done():
                    fut.set_exception(error)
            return
        last = {key: fut for key, _, fut in waiting}
        for key, _, fut in waiting:
            if not fut.done():
                fut.set_result(reply if last[key] is fut else None)


WINDOW = PingWindow()


async def show_ping(key: object, sender: str, message: str, source: str = "") -> str | None:
    """source: "discord", "telegram" or "dashboard" (the window shows that app's badge)."""
    return await WINDOW.show(key, sender, message, source)
