"""Dashboard Screen tab (llmbot/screen.py): input turned into SendInput events (captured, not sent: the test doesn't
move the real mouse), the WebSocket's sign-in/origin checks, frames only on request and only when something changed,
held keys and buttons let go when the viewer leaves, one viewer at a time, and the reasons it refuses."""
import asyncio, os, sys

os.environ["DASHBOARD_PORT"] = "0"
from _setup import B, TMP  # noqa  (first: isolates the bot)
import aiohttp
from aiohttp import web
from llmbot import dashboard as D, screen as SCR


def check(c, label):
    assert c, label
    print("  ok", label)


sent = []  # every INPUT that would have gone to SendInput
SCR._send = lambda *inputs: sent.extend(inputs)


def kinds():
    out = []
    for i in sent:
        if i.type == 0:
            out.append(("mouse", i.u.mi.dwFlags, i.u.mi.dx, i.u.mi.dy, ctypes_signed(i.u.mi.mouseData)))
        else:
            out.append(("key", i.u.ki.wVk, i.u.ki.wScan, i.u.ki.dwFlags))
    sent.clear()
    return out


def ctypes_signed(v):
    return v - 2**32 if v >= 2**31 else v


async def main():
    if sys.platform != "win32":
        print("Windows only: skipped")
        return
    print("== input")
    inp = SCR.Input()
    inp.handle({"t": "click", "b": "left", "x": 0.5, "y": 0.5})
    k = kinds()
    check(k[0][0] == "mouse" and k[0][1] == SCR.MOVE | SCR.ABSOLUTE | SCR.VIRTUALDESK and 0 < k[0][2] < 65535
          and [x[1] for x in k[1:]] == [0x2, 0x4], "tap: move there, then left down + up")
    inp.handle({"t": "click", "b": "right"})
    check([x[1] for x in kinds()] == [0x8, 0x10], "right click where the pointer is")
    inp.handle({"t": "down", "b": "left", "x": 0, "y": 0})
    k = kinds()
    check(k[-1][1] == 0x2 and inp.buttons == {"left"}, "drag start: button remembered")
    inp.handle({"t": "rel", "dx": 5, "dy": -99999})
    check(kinds() == [("mouse", SCR.MOVE, 5, -2000, 0)], "trackpad move: relative, clamped")
    inp.handle({"t": "wheel", "dy": -120})
    check(kinds() == [("mouse", SCR.WHEEL, 0, 0, -120)], "wheel down (negative delta survives the DWORD)")
    inp.handle({"t": "key", "k": "Up", "a": "down"})
    k = kinds()
    check(k[0][0] == "key" and k[0][2] and k[0][3] & SCR.KEY_SCANCODE and k[0][3] & SCR.KEY_EXTENDED
          and inp.keys == {0x26}, "arrow down: scan code, extended, remembered")
    inp.handle({"t": "key", "k": "Alt+Tab"})
    k = kinds()
    check(len(k) == 4 and not k[0][3] & SCR.KEY_UP and k[3][3] & SCR.KEY_UP and inp.keys == {0x26},
          "combination tapped: both down, released in reverse")
    inp.handle({"t": "key", "k": "Nope+A"})
    check(kinds() == [], "unknown key: nothing sent")
    inp.handle({"t": "text", "s": "hi😀\n"})
    k = kinds()
    units = [x[2] for x in k if x[3] & SCR.KEY_UNICODE and not x[3] & SCR.KEY_UP]
    check(units == [ord("h"), ord("i"), 0xD83D, 0xDE00] and len(k) == 10, "text as Unicode (emoji = 2 units), newline = Enter")
    inp.release()
    k = kinds()
    check({x[1] for x in k if x[0] == "mouse"} == {0x4} and any(x[0] == "key" and x[3] & SCR.KEY_UP for x in k)
          and not inp.buttons and not inp.keys, "release: held button and key let go")
    check(SCR.vk_of("w") == 0x57 and SCR.vk_of("F5") == 0x74 and SCR.vk_of("esc") == 0x1B and SCR.vk_of("é") is None,
          "key names")

    print("== WebSocket")
    frames = iter([b"A", b"A", b"B"])
    SCR.grab = lambda w, q: (next(frames, b"B"), 1920, 1080)
    SCR.cursor = lambda: (0.25, 0.75)
    SCR.KEEPALIVE = 60
    B.DASHBOARD_SCREEN = True
    D.core, D._key = B, "k" * 24
    runner = web.AppRunner(D.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/api/screen"
    async with aiohttp.ClientSession() as s:
        try:
            await s.ws_connect(url)
            check(False, "unauthenticated refused")
        except aiohttp.WSServerHandshakeError as e:
            check(e.status == 401, "no key: refused before the upgrade")
        auth = {"Authorization": f"Bearer {D._key}"}
        try:
            await s.ws_connect(url, headers={**auth, "Origin": "http://evil.example"})
            check(False, "cross-site refused")
        except aiohttp.WSServerHandshakeError as e:
            check(e.status == 403, "another site's page can't open it")

        ws = await s.ws_connect(url, headers=auth)
        check(any(e["text"].startswith("Screen control opened") for e in B._events), "opening is in the activity feed")
        with_timeout = lambda c: asyncio.wait_for(c, 3)
        try:
            await asyncio.wait_for(ws.receive(), 0.4)
            check(False, "nothing before asked")
        except asyncio.TimeoutError:
            check(True, "no frame until the page asks")
        await ws.send_json({"t": "next"})
        head = await with_timeout(ws.receive_json())
        body = await with_timeout(ws.receive_bytes())
        check(head == {"screen": [1920, 1080], "cursor": [0.25, 0.75], "frame": True} and body == b"A", "frame A")
        await ws.send_json({"t": "opts", "fps": 30})
        await ws.send_json({"t": "next"})
        head = await with_timeout(ws.receive_json())
        body = await with_timeout(ws.receive_bytes())
        check(body == b"B", "the identical frame was skipped; the next change sent")
        await ws.send_json({"t": "key", "k": "Space", "a": "down"})
        await asyncio.sleep(0.1)
        kinds()

        ws2 = await s.ws_connect(url, headers=auth)
        msg = await with_timeout(ws.receive_json())
        check("another device" in msg["error"], "a second viewer takes over; the first is told")
        await asyncio.sleep(0.2)
        k = kinds()
        check(any(x[0] == "key" and x[3] & SCR.KEY_UP for x in k), "the first viewer's held Space was let go")
        await ws2.close()

        B.DASHBOARD_SCREEN = False
        ws3 = await s.ws_connect(url, headers=auth)
        msg = await with_timeout(ws3.receive_json())
        check("DASHBOARD_SCREEN" in msg["error"], "turned off: says how to turn it on")
        B.DASHBOARD_SCREEN = True
        B._background = True
        ws4 = await s.ws_connect(url, headers=auth)
        msg = await with_timeout(ws4.receive_json())
        check("signed in" in msg["error"], "session 0 after a boot: asks to sign in")
        B._background = None
    await runner.cleanup()


asyncio.run(main())
print("\nALL SCREEN CHECKS PASSED")
