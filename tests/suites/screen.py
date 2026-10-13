"""Dashboard Screen tab (llmbot/screen.py): input turned into SendInput events (captured, not sent: the test doesn't
move the real mouse) on the chosen screen, the real pointer drawn into a frame (no GDI objects left behind), the
WebSocket's sign-in/origin checks, frames only on request (at most IN_FLIGHT_MAX ahead) and only when something
changed, the arrow / real / both pointer modes, switching screens, held keys and buttons let go when the viewer leaves,
one viewer at a time, and the reasons it refuses. Real capture of each screen is tried too when there is a desktop."""
import asyncio, ctypes, os, sys

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


PHONE = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile "
         "Safari/537.36")
LAPTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 Edg/131.0"
MON0 = {"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True}
MON1 = {"left": -1280, "top": -200, "width": 1280, "height": 1024, "primary": False}


async def main():
    if sys.platform != "win32":
        print("Windows only: skipped")
        return
    print("== input")
    inp = SCR.Input(MON0)
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
    inp = SCR.Input(MON1)
    inp.handle({"t": "move", "x": 0, "y": 0})
    check(kinds()[0][2:4] == SCR.to_absolute(0, 0, MON1) != SCR.to_absolute(0, 0, MON0),
          "a point on another screen maps to that screen")
    check(SCR.on_monitor((1, 1919, 1079), MON0) == (1, 1) and SCR.on_monitor((1, -1280, -200), MON1) == (0, 0) and SCR.on_monitor((1, -5, 3), MON0) is None
          and SCR.on_monitor(None, MON0) is None, "pointer position per screen; elsewhere or hidden = none")

    print("== the real pointer drawn into a frame")
    from PIL import Image
    user32 = ctypes.WinDLL("user32")
    user32.LoadCursorW.restype = ctypes.c_void_p
    arrow = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    me = kernel32.GetCurrentProcess()
    img = Image.new("RGB", (200, 100), (40, 90, 160))
    SCR.draw_pointer(img, MON0, (arrow, 100, 50))
    gdi0 = user32.GetGuiResources(me, 0)
    for _ in range(300):
        SCR.draw_pointer(img, MON0, (arrow, 100, 50))
    check(user32.GetGuiResources(me, 0) == gdi0, f"300 draws, no GDI objects left behind ({gdi0})")
    check(img.getpixel((101, 52)) != (40, 90, 160) and img.getpixel((150, 10)) == (40, 90, 160),
          "drawn at the pointer, nowhere else")
    edge = Image.new("RGB", (50, 50), (40, 90, 160))
    SCR.draw_pointer(edge, MON0, (arrow, 48, 48))
    SCR.draw_pointer(edge, MON0, (arrow, -40, -40))
    check(True, "half off the edge, or off it entirely: no error")

    print("== capturing the real screens")
    try:
        for i, mon in enumerate(SCR.monitors()):
            jpeg, w, h = SCR.grab(mon, 960, 50, False, SCR.pointer())
            assert jpeg[:2] == b"\xff\xd8" and (w, h) == (mon["width"], mon["height"]), mon
            print(f"  ok screen {i + 1}: {w}x{h} via {SCR._cap.how}, {len(jpeg) // 1024} KB")
        SCR._reset_capture()
        cap = SCR.Capture(SCR.monitors()[0])
        if cap.image(False) is not None and cap.how == "dxgi":
            import time
            cap.cam._duplicator.update_frame = lambda wait_for_frame=False: False  # as Windows does at a UAC prompt
            t0 = time.monotonic()
            img = cap.image(True)
            check(img is not None and cap.how == "gdi" and time.monotonic() - t0 < 1,
                  f"GPU capture lost the screen: a GDI frame at once, no endless retry ({time.monotonic() - t0:.2f} s)")
            cap.dxgi_at = 0  # as if DXGI_RETRY had passed
            check(cap.image(False) is not None and cap.how == "dxgi", "and back on the GPU capture later")
        cap.close()
    except Exception as e:  # e.g. CI without a desktop: the WebSocket checks below use a stand-in
        print(f"  (skipped: {type(e).__name__}: {e})")

    print("== WebSocket")
    G = {"static": False, "n": 0, "calls": []}

    def fake_grab(mon, width, quality, fresh_only, draw):
        G["calls"].append((mon, fresh_only, draw))
        if G["static"] and fresh_only:
            return None, mon["width"], mon["height"]
        G["n"] += 1
        return f"F{G['n']}".encode(), mon["width"], mon["height"]

    PTR = [(7, 480, 810)]
    SCR.grab = fake_grab
    SCR.monitors = lambda: [MON0, MON1]
    SCR.pointer = lambda: PTR[0]
    SCR.KEEPALIVE = 60
    B.DASHBOARD_SCREEN = True
    D.core, D._key = B, "k" * 24
    runner = web.AppRunner(D.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/api/screen"
    with_timeout = lambda c: asyncio.wait_for(c, 3)

    async def quiet(ws, seconds=0.4):
        try:
            return await asyncio.wait_for(ws.receive(), seconds)
        except asyncio.TimeoutError:
            return None

    async def frame(ws):
        await ws.send_json({"t": "next"})
        head = await with_timeout(ws.receive_json())
        body = await with_timeout(ws.receive_bytes()) if head.get("frame") else None
        return head, body

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

        check(SCR.device_name(PHONE) == "Android phone · Chrome" and SCR.device_name(LAPTOP) == "Windows PC · Edge"
              and SCR.device_name("") == "a device", "device names from the browser")
        check(D.screen_status() is None, "nobody watching: no status")
        ws = await s.ws_connect(url, headers={**auth, "User-Agent": PHONE})
        check(any(e["text"] == "Screen control opened from Android phone · Chrome (127.0.0.1)" for e in B._events),
              "opening is in the activity feed, with the device")
        m = await with_timeout(ws.receive_json())
        check(m == {"monitors": [{"w": 1920, "h": 1080, "primary": True}, {"w": 1280, "h": 1024, "primary": False}],
                    "monitor": 0}, "the screens are listed first")
        check(await quiet(ws) is None, "no frame until the page asks")
        head, body = await frame(ws)
        check(head == {"screen": [1920, 1080], "cursor": list(SCR.on_monitor(PTR[0], MON0)), "frame": True} and body == b"F1"
              and G["calls"][-1][2] is None, "arrow mode: frame, pointer as a position, not drawn in")
        G["static"] = True
        await ws.send_json({"t": "next"})
        check(await quiet(ws) is None, "still screen, still pointer: nothing sent")
        PTR[0] = (7, 960, 540)
        head = await with_timeout(ws.receive_json())
        check(head == {"screen": [1920, 1080], "cursor": list(SCR.on_monitor(PTR[0], MON0)), "frame": False}, "pointer moved: its position only")
        PTR[0] = None
        head, _ = await frame(ws)
        check(head["cursor"] is None and not head["frame"], "pointer hidden (a game hid it): the arrow goes")

        await ws.send_json({"t": "opts", "cursor": "real"})
        PTR[0] = (7, 480, 810)
        head, body = await frame(ws)
        check(head["frame"] and head["cursor"] is None and G["calls"][-1][2] == (7, 480, 810),
              "real pointer: drawn into the frame, no arrow")
        n = G["n"]
        PTR[0] = (8, 481, 810)  # moved a pixel and changed shape, screen still
        head, body = await frame(ws)
        check(head["frame"] and G["n"] == n + 1 and G["calls"][-1][1] is False, "pointer moved: a new frame drawn")
        await ws.send_json({"t": "next"})
        check(await quiet(ws) is None, "real pointer still: nothing sent")
        await ws.send_json({"t": "opts", "cursor": "both"})
        head = await with_timeout(ws.receive_json())
        await with_timeout(ws.receive_bytes())
        check(head["cursor"] == list(SCR.on_monitor(PTR[0], MON0)) and head["frame"] and G["calls"][-1][2] == (8, 481, 810),
              "both: drawn in, and the arrow's position")

        await ws.send_json({"t": "opts", "cursor": "arrow", "monitor": 1})
        m = await with_timeout(ws.receive_json())
        check(m["monitor"] == 1, "switched to screen 2")
        head, body = await frame(ws)
        check(head["screen"] == [1280, 1024] and G["calls"][-1][0] == MON1 and head["cursor"] is None,
              "its frames, and the pointer isn't on it")
        await ws.send_json({"t": "move", "x": 0, "y": 0})
        await asyncio.sleep(0.1)
        check(kinds()[0][2:4] == SCR.to_absolute(0, 0, MON1), "touches land on screen 2")
        await ws.send_json({"t": "opts", "monitor": 9})
        m = await with_timeout(ws.receive_json())
        check(m["monitor"] == 0, "a screen that's gone: back to the main one")
        head, _ = await frame(ws)

        G["static"] = False
        for _ in range(10):
            await ws.send_json({"t": "next"})
        got = 0
        while (msg := await quiet(ws, 0.5)) is not None:
            got += msg.type == aiohttp.WSMsgType.BINARY
        check(got == SCR.IN_FLIGHT_MAX, f"10 requests at once: only {SCR.IN_FLIGHT_MAX} frames ahead ({got})")
        await ws.send_json({"t": "opts", "fps": "lots", "quality": "ultra"})
        await ws.send_json({"t": "next"})
        check(await quiet(ws) is not None, "bad settings ignored, still streaming")
        live = D.screen_status()
        check(live and live["device"] == "Android phone · Chrome" and live["who"] == "127.0.0.1" and live["fps"] > 0
              and live["kbps"] >= 0 and live["monitor"] == 1 and live["monitors"] == 2 and live["cursor"] == "arrow",
              f"status while streaming: {live}")
        await ws.send_json({"t": "key", "k": "Space", "a": "down"})
        await asyncio.sleep(0.1)
        kinds()

        ws2 = await s.ws_connect(url, headers={**auth, "User-Agent": LAPTOP})
        m = await with_timeout(ws2.receive_json())
        check(m["busy"]["device"] == "Android phone · Chrome" and (await with_timeout(ws2.receive())).type
              == aiohttp.WSMsgType.CLOSE, "another device is told who's watching, and not connected")
        await ws.send_json({"t": "next"})
        check(await quiet(ws) is not None and not kinds(), "the first keeps streaming, Space still held")

        ws2 = await s.ws_connect(url + "?take=1", headers={**auth, "User-Agent": LAPTOP})
        while True:  # past the frames still on their way
            raw = await with_timeout(ws.receive())
            if raw.type == aiohttp.WSMsgType.TEXT and "error" in (msg := raw.json()):
                break
        check(msg == {"error": "Taken over by Windows PC · Edge (127.0.0.1).", "taken": True},
              "Take over: the first is told by whom")
        await asyncio.sleep(0.2)
        k = kinds()
        check(any(x[0] == "key" and x[3] & SCR.KEY_UP for x in k), "the first viewer's held Space was let go")
        check(D.screen_status()["device"] == "Windows PC · Edge", "status follows the new viewer")
        ws3 = await s.ws_connect(url, headers={**auth, "User-Agent": LAPTOP})
        while True:
            raw = await with_timeout(ws2.receive())
            if raw.type == aiohttp.WSMsgType.TEXT and "error" in (msg := raw.json()):
                break
        check(msg["error"] == "This device was disconnected.", "the same device reconnecting (a reload) needn't ask")
        await ws3.close()
        await asyncio.sleep(0.2)
        check(D.screen_status() is None, "closed: no status")

        # A phone that dropped off the Wi-Fi: connected, then silent (a raw socket: aiohttp's client would answer)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        lines = ["GET /api/screen HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13",
                 f"Authorization: Bearer {D._key}", f"User-Agent: {PHONE}", "", ""]
        writer.write("\r\n".join(lines).encode())
        await writer.drain()
        check(b" 101 " in await with_timeout(reader.readline()), "(a silent viewer connected)")
        await asyncio.sleep(0.2)
        t0 = asyncio.get_running_loop().time()
        ws5 = await s.ws_connect(url, headers={**auth, "User-Agent": PHONE})
        await with_timeout(ws5.receive_json())
        head, body = await frame(ws5)
        waited = asyncio.get_running_loop().time() - t0
        check(body and waited < 1, f"a viewer that went silent doesn't hold up the next one ({waited:.2f} s)")
        await ws5.close()
        writer.transport.abort()

        print("== sound")
        import threading, time
        stop, failed = threading.Event(), []
        real = threading.Thread(target=SCR._record, args=(asyncio.get_running_loop(), lambda pcm: None, stop, failed))
        real.start()
        await asyncio.sleep(0.6)
        stop.set()
        await asyncio.get_running_loop().run_in_executor(None, real.join, 3)
        check(not real.is_alive() and not failed, f"the real loopback recording starts and stops {failed or ''}")

        def fake_record(loop, put, stop, failed):
            while not stop.is_set():
                loop.call_soon_threadsafe(put, b"\x01\x00" * 1920)  # 20 ms of stereo 16-bit
                time.sleep(0.02)

        SCR._record = fake_record
        aurl = url + "/audio"
        a = await s.ws_connect(aurl, headers={**auth, "User-Agent": PHONE})
        msg = await with_timeout(a.receive_json())
        check("Open the Screen tab" in msg["error"], "nobody watching: no sound")
        v = await s.ws_connect(url, headers={**auth, "User-Agent": PHONE})
        await with_timeout(v.receive_json())
        a = await s.ws_connect(aurl, headers={**auth, "User-Agent": LAPTOP})
        msg = await with_timeout(a.receive_json())
        check("Open the Screen tab" in msg["error"], "another device can't listen in")
        a = await s.ws_connect(aurl, headers={**auth, "User-Agent": PHONE})
        check(await with_timeout(a.receive_json()) == {"rate": 48000, "channels": 2}
              and len(await with_timeout(a.receive_bytes())) == 3840, "the watching device gets the sound")
        check(D.screen_status()["audio"] is True, "status: sound on")
        await v.close()
        while (await with_timeout(a.receive())).type not in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            pass
        await asyncio.sleep(0.3)
        check(not any(t.name == "screen-audio" for t in threading.enumerate()), "the screen closed: sound stops, its thread ends")

        B.DASHBOARD_SCREEN = False
        ws3 = await s.ws_connect(url, headers=auth)
        msg = await with_timeout(ws3.receive_json())
        check(D.screen_status() is None and "DASHBOARD_SCREEN" in msg["error"], "turned off: says how to turn it on")
        B.DASHBOARD_SCREEN = True
        B._background = True
        ws4 = await s.ws_connect(url, headers=auth)
        msg = await with_timeout(ws4.receive_json())
        check("signed in" in msg["error"], "session 0 after a boot: asks to sign in")
        B._background = None
    await runner.cleanup()


asyncio.run(main())
print("\nALL SCREEN CHECKS PASSED")
