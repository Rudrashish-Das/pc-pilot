import asyncio, sys, tempfile
from dataclasses import replace
from pathlib import Path
import discord, httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.USAGE_FILE = tmp / "usage.json"; B.REMINDERS_FILE = tmp / "rem.json"
B._usage.clear(); B._reminders.clear()
SP = TMP; WS = SP / "ws_v16"; WS.mkdir(exist_ok=True)
for f in WS.iterdir():
    if f.is_file(): f.unlink()
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
OWNER, CH = 1, 1616
B.OWNER_IDS.add(OWNER); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.add(OWNER)


def check(c, label):
    assert c, label
    print("  ok", label)


def unit():
    print("== [[delete:]] rules")
    (WS / "a.txt").write_text("x"); (WS / "sub").mkdir(exist_ok=True); (WS / "sub" / "b.txt").write_text("y")
    (SP / "outside.txt").write_text("keep")
    snap = replace(B.snapshot(CH), perm="edit")
    text, paths, lines = B.plan_deletes("ok\n[[delete: a.txt]]\n[[delete: sub/b.txt]]\n[[delete: a.txt]]", snap)
    check(text == "ok" and [p.name for p in paths] == ["a.txt", "b.txt"], "edit mode: files queued, markers removed, dupes skipped")
    check(lines[1] == "🗑️ deleted `sub/b.txt` from the workspace", "notice names the relative path")
    _, paths, lines = B.plan_deletes("[[delete: ../outside.txt]][[delete: " + str(SP / "outside.txt") + "]]", snap)
    check(not paths and all("outside the workspace" in l for l in lines), "../ and absolute paths outside refused")
    _, paths, lines = B.plan_deletes("[[delete: sub]][[delete: nope.txt]][[delete: .]]", snap)
    check(not paths and "folders" in lines[0] and "not found" in lines[1] and "outside" in lines[2], "folders / missing / workspace root refused")
    _, paths, lines = B.plan_deletes("[[delete: a.txt]]", replace(snap, perm="read"))
    check(not paths and "read-only" in lines[0], "read mode refuses")
    check((WS / "a.txt").exists() and (SP / "outside.txt").exists(), "planning deletes nothing by itself")


class Typ:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


async def run(prompt, perm="edit", fail_send=False):
    B.update_settings(CH, engine="claude", cc_model="haiku", cc_perm=perm, style="chat")
    msgs = []

    async def out(**kw):
        if fail_send:
            raise discord.HTTPException(type("R", (), {"status": 500, "reason": "x"})(), "boom")
        msgs.append(kw)

    class Ch:
        id = CH
        def typing(self): return Typ()
        async def send(self, content=None, **kw): msgs.append({"content": content, **kw})
    job = await B.start_cc_job(Ch(), prompt, B.snapshot(CH, resume=B.CC_CONTINUE), OWNER, out=out, chat=True)
    while job.status != "done":
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    return job, msgs


async def live():
    print("== live (haiku, edit mode): the request from Discord")
    B.http = httpx.AsyncClient()
    job, msgs = await run("Create a file named random and add 10 random numbers from 1000-5000 in the file, new line "
                          "separated, then send me the file then delete it from your WD.")
    last = msgs[-1]
    print("     reply:", repr(last["content"]))
    files = last.get("files") or []
    data = files[0].fp.read().decode() if files else ""
    nums = [int(x) for x in data.split()]
    check(files and len(nums) == 10 and all(1000 <= n <= 5000 for n in nums), f"file attached with 10 numbers: {nums}")
    check(not any(WS.glob("random*")), "file deleted from the workspace after sending")
    check("🗑️ deleted" in last["content"] and "[[delete" not in last["content"], "shows 🗑️ notice, marker hidden")
    check("can't delete" not in last["content"].lower() and "cannot delete" not in last["content"].lower(), "no 'can't delete' complaint")

    print("== live: failed send keeps the file")
    B.update_settings(CH, cc_session=None)
    job, msgs = await run("Create keep.txt containing hi, attach it, then delete it.", fail_send=True)
    check((WS / "keep.txt").exists(), "reply not delivered -> file not deleted")

    print("== live: knows why it ignores someone")
    B.update_settings(CH, cc_session=None)
    job, msgs = await run("Why can't you reply to <@841691078494650389>? Answer in one sentence, don't search files.")
    print("     reply:", repr(msgs[-1]["content"]))
    check("allow" in msgs[-1]["content"].lower(), "points at the allow-list")


async def main():
    unit()
    if LIVE:
        await live()

asyncio.run(main())
print("\nALL V16 CHECKS PASSED")
