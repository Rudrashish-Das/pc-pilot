"""Security fixes: DNS-rebinding-safe fetch, sandboxed read/edit Claude Code jobs, MCP helper not importing from the
workspace, DMs with no allow-list, private task/reminder lists, voice decode cap, Telegram button scoping."""
import asyncio, json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ["TELEGRAM_ALLOWED_USER_IDS"] = "111,222"
os.environ["TELEGRAM_OWNER_IDS"] = "111"
from _setup import B, FIXTURES, TMP  # noqa  (first: isolates the bot)
import llmbot.telegram as T

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "s.json"; B.TASKS_FILE = tmp / "t.json"; B.REMINDERS_FILE = tmp / "r.json"
B._settings.clear(); B._tasks.clear(); B._reminders.clear()


def check(c, label):
    assert c, label
    print("  ok", label)


print("== fetch_page connects to the IP that was checked (no second DNS lookup)")
seen = {}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        seen["host"] = self.headers.get("Host")
        body = b"hello from the checked address"
        self.send_response(200); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


async def pinned():
    real = B.assert_public_url
    B.assert_public_url = lambda url: asyncio.sleep(0, "127.0.0.1")  # "rebind.invalid" can't resolve: IP must be used
    try:
        text = await B.fetch_page(f"http://rebind.invalid:{port}/x")
    finally:
        B.assert_public_url = real
    check("hello from the checked address" in text, "request went to the checked IP")
    check(seen.get("host") == f"rebind.invalid:{port}", f"Host header is the real name ({seen.get('host')})")
asyncio.run(pinned())
srv.shutdown()

u, h, ext = B.pinned_request("https://user:pw@Example.com:8443/a?b=1#frag", "93.184.215.14")
check(u == "https://93.184.215.14:8443/a?b=1" and h == {"Host": "example.com:8443"} and ext == {"sni_hostname": "example.com"},
      "https: IP in URL, Host + SNI = hostname, no userinfo/fragment")
u, h, ext = B.pinned_request("http://example.com", "2606:2800:21f:cb07::1")
check(u == "http://[2606:2800:21f:cb07::1]/" and ext == {}, "IPv6 bracketed, http has no SNI")

print("== Claude Code read/edit jobs can't plant hooks, MCP servers or CLAUDE.md")
for perm in ("read", "edit"):
    for backend in ("anthropic", "ollama"):
        cmd = B.build_cc_command("claude", B.CCSnap(backend, "m", perm, "default"))
        i = cmd.index("--setting-sources")
        check(cmd[i + 1] == "user" and "Edit(./.claude/**)" in cmd and "Edit(./CLAUDE.md)" in cmd,
              f"{backend}/{perm}: user settings only, .claude/ and CLAUDE.md not editable")
        check(cmd.index("--allowedTools") > cmd.index("--disallowedTools"), f"{backend}/{perm}: allowedTools stays last")
full = B.build_cc_command("claude", B.CCSnap("anthropic", "m", "full", "default"))
check("--setting-sources" not in full, "full access unchanged")

print("== web-tools helper doesn't import from the workspace")
cfg = json.loads(Path(B.mcp_web_config()).read_text())["mcpServers"]["bot"]
check(cfg["args"][:3] == ["-P", "-m", "llmbot"] and cfg["env"].get("PYTHONSAFEPATH") == "1", "python -P (safe path)")

print("== Discord DMs with an empty ALLOWED_USER_IDS are owners-only")
B.ALLOWED_USER_IDS.clear(); B.OWNER_IDS.clear(); B.OWNER_IDS.add(900000000000000001)
guild, stranger, owner = object(), 900000000000000009, 900000000000000001
check(B.is_allowed_in(stranger, guild), "server member: allowed (empty list = everyone in the server)")
check(not B.is_allowed_in(stranger, None), "stranger's DM: ignored")
check(B.is_allowed_in(owner, None), "owner's DM: allowed")
B.ALLOWED_USER_IDS.add(stranger)
check(B.is_allowed_in(stranger, None), "listed user's DM: allowed")
B.ALLOWED_USER_IDS.clear()
check(B.is_allowed_in(111, None) and not B.is_allowed_in(333, None), "Telegram ids keep their own list")

print("== reminders and tasks are private to their creator (owners see all)")
mine = B.add_reminder("in 2 hours", "take my meds", 1, owner)
theirs = B.add_reminder("in 3 hours", "call mum", 2, stranger)
t = B.add_task("0 9 * * *", "news digest", "news", 1, owner)
check("take my meds" not in B.reminders_text(stranger) and "call mum" in B.reminders_text(stranger), "local tool: own reminders")
check(B.tasks_text(stranger) == "No scheduled tasks." and "news" in B.tasks_text(owner), "local tool: own tasks")
names = [f["name"] for f in B.tasks_embed(stranger).to_dict().get("fields", [])]
check(len(names) == 1 and "call mum" in names[0], "/tasks for a member: only theirs")
check(len(B.tasks_embed(owner).to_dict()["fields"]) == 3, "/tasks for an owner: everything")
opts = [o.value for o in B.TasksView(stranger).children[0].options]
check(opts == [theirs["id"]], "cancel menu: only theirs")
check("Only" in B.cancel_reminder(mine["id"], stranger), "can't cancel someone else's")

print("== voice notes are cut off at the limit while decoding")
B.VOICE_MAX_SECONDS = 0
try:
    B._decode_16k((FIXTURES / "voice.ogg").read_bytes()); check(False, "should stop")
except ValueError as e:
    check("longer than" in str(e), "stops decoding past VOICE_MAX_SECONDS")


print("== Telegram buttons only work in the chat they were posted in")
class FakeTg(T.Telegram):
    def __init__(self):
        super().__init__(B, "123456789:" + "A" * 35)
        self.calls = []

    async def api(self, method, *, files=None, _timeout=None, **params):
        self.calls.append((method, params))
        return {"message_id": 7} if method == "sendMessage" else True


async def tg():
    tg = FakeTg()
    msg = await tg.send(111, "panel", view=B.ReminderView({"id": "r1", "user_id": 111, "text": "x", "channel_id": 111}))
    tok = next(iter(tg._views))
    hits = []
    tg._views[tok].view.children[0].callback = lambda fi: asyncio.sleep(0, hits.append(fi))
    q = lambda chat: {"id": "cb", "from": {"id": 111, "first_name": "R"}, "data": f"{tok}:0",
                      "message": {"message_id": msg.message_id, "chat": {"id": chat, "type": "private"}}}
    await tg._on_callback(q(-100555))
    check(not hits, "tap from another chat is refused")
    await tg._on_callback(q(111))
    check(len(hits) == 1, "tap in the right chat works")
    tg._views[tok].view.children[0].disabled = True
    await tg._on_callback(q(111))
    check(len(hits) == 1, "disabled button can't be triggered")
asyncio.run(tg())
print("all security checks passed")
