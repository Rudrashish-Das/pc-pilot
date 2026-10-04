"""The /ping window, run as its own process by pcstatus.PingWindow. Each line on stdin is a ping as JSON
{"from", "message"}; the window shows the latest one (a new ping replaces the text, it doesn't open another window)
and stays on top of everything. It prints {"reply": ...} once answered ("" when closed without a reply) and exits.
It doesn't take the keyboard from whatever is in front (a game would lose its fullscreen); click the box to reply."""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

KEEP_ON_TOP_MS = 500  # other always-on-top windows (a game's overlay, Task Manager) can't cover it for long
CHIME_GAP = 10  # seconds: a burst of pings sounds once
ICONS = [Path(__file__).with_name(n) for n in ("dashboard_logo.png", "dashboard_favicon.png")]  # 192 and 64 px
CHIME = Path(os.getenv("SystemRoot", r"C:\Windows")) / "Media" / "Windows Message Nudge.wav"


def chime(root: tk.Tk) -> None:
    """A sound so a ping is noticed with the screen off to one side (or a game in front)."""
    try:
        import winsound
        if CHIME.exists():
            winsound.PlaySound(str(CHIME), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except (ImportError, RuntimeError):
        root.bell()


APPS = {"discord": "Discord", "telegram": "Telegram", "dashboard": "the dashboard"}


def draw_badge(c: tk.Canvas, source: str) -> None:
    """A small drawn badge for the app a ping came from (no image files to ship or download)."""
    c.delete("all")
    if source == "discord":  # blurple, with a white face and two eyes
        c.create_oval(0, 0, 21, 21, fill="#5865F2", outline="")
        c.create_polygon(5, 7, 8, 5.5, 13, 5.5, 16, 7, 17.5, 13, 15, 15.5, 6, 15.5, 3.5, 13,
                         fill="white", outline="", smooth=True)
        c.create_oval(7, 9, 9.5, 11.5, fill="#5865F2", outline="")
        c.create_oval(11.5, 9, 14, 11.5, fill="#5865F2", outline="")
    elif source == "telegram":  # blue, with a white paper plane
        c.create_oval(0, 0, 21, 21, fill="#2AABEE", outline="")
        c.create_polygon(4.5, 10.5, 16, 5.5, 14, 16, 10.5, 13, 9, 15.5, 8.5, 12, fill="white", outline="")
        c.create_line(8.5, 12, 14, 7.5, fill="#C8DAEA", width=1)
    else:  # the dashboard: the bot's own colours
        c.create_oval(0, 0, 21, 21, fill="#F5B800", outline="")
        c.create_text(11, 11, text="M", fill="#111111", font=("Segoe UI", 9, "bold"))


def read_pings(pings: queue.Queue) -> None:
    for line in sys.stdin.buffer:  # not the console code page: the bot writes UTF-8
        try:
            pings.put(json.loads(line.decode("utf-8-sig")))
        except ValueError:
            pass


def main() -> None:
    pings: queue.Queue = queue.Queue()
    first = sys.stdin.buffer.readline()
    pings.put(json.loads(first.decode("utf-8-sig") or "{}"))
    threading.Thread(target=read_pings, args=(pings,), daemon=True).start()
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled screens
            # Its own taskbar button with the Mavis icon, not grouped under Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pc-pilot.ping")
        except (AttributeError, OSError):
            pass
    result = {"reply": ""}
    state = {"count": 0, "chimed": 0.0}
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(padx=16, pady=14)
    icons = []  # the Mavis logo in the title bar and on the taskbar (Tk drops images nothing references)
    try:
        icons = [tk.PhotoImage(file=str(f)) for f in ICONS]
        root.iconphoto(True, *icons)
    except tk.TclError:
        pass
    top = tk.Frame(root)
    top.pack(fill="x")
    badge = tk.Canvas(top, width=22, height=22, highlightthickness=0, bd=0, bg=root.cget("bg"))
    head = tk.Label(top, font=("Segoe UI", 10, "bold"), anchor="w")
    head.pack(side="left", fill="x")
    body = tk.Label(root, font=("Segoe UI", 12), wraplength=420, justify="left", anchor="w")
    body.pack(fill="x", pady=(6, 12))
    box = tk.Entry(root, font=("Segoe UI", 11), width=42)
    box.pack(fill="x")
    row = tk.Frame(root)
    row.pack(fill="x", pady=(10, 0))

    def send(_=None):
        text = box.get().strip()
        if text:
            result["reply"] = text
            root.destroy()

    tk.Button(row, text="Reply", width=10, command=send, default="active").pack(side="right")
    tk.Button(row, text="Close", width=10, command=root.destroy).pack(side="right", padx=(0, 8))
    box.bind("<Return>", send)
    root.bind("<Escape>", lambda _: root.destroy())

    def show(ping: dict) -> None:
        who = ping.get("from") or "Someone"
        state["count"] += 1
        root.title(f"Ping from {who}")
        source = ping.get("source") or ""
        app = APPS.get(source)
        badge.pack_forget()
        if app:
            draw_badge(badge, source)
            badge.pack(side="left", padx=(0, 8), before=head)
        head.configure(text=f"{'' if app else '📨 '}{who} pinged you" + (f" on {app}" if app else "")
                       + (f" ({state['count']} pings)" if state["count"] > 1 else ""))
        body.configure(text=ping.get("message") or "")
        if time.monotonic() - state["chimed"] > CHIME_GAP:
            state["chimed"] = time.monotonic()
            chime(root)

    def poll():
        while not pings.empty():
            show(pings.get())
        root.after(200, poll)

    show(pings.get())
    root.update_idletasks()  # centre it on the screen (hidden until placed, or Windows picks a spot first)
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.deiconify()

    def keep_on_top():
        root.attributes("-topmost", True)
        root.lift()
        root.after(KEEP_ON_TOP_MS, keep_on_top)
    keep_on_top()
    poll()
    root.mainloop()
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    os._exit(0)  # not a normal exit: the stdin thread, blocked reading, crashes Python's shutdown


if __name__ == "__main__":
    main()
