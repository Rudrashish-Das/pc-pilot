"""Started at boot and nobody signed in (session 0, no DPAPI keys): Claude Code jobs get no browser, so a Chrome
started from there can't sign the user out of every site. In a desktop session, nothing changes."""
from _setup import B  # noqa  (first: isolates the bot)

fails = 0


def check(ok, what):
    global fails
    print(("PASS " if ok else "FAIL ") + what)
    fails += not ok


def job(perm, backend="anthropic"):
    return B.CCJob(channel=None, task="open my gmail", snap=B.CCSnap(backend, "haiku", perm, "default"), user_id=1)


for background in (False, True):
    B._background = background
    where = "before sign-in" if background else "signed in"
    for perm in ("read", "edit", "full"):
        for backend in ("anthropic", "ollama"):
            cmd = B.build_cc_command("claude", job(perm, backend).snap)
            has = "--no-chrome" in cmd and "mcp__claude-in-chrome" in cmd and "mcp__computer-use" in cmd
            check(has == background, f"{where}, {perm}/{backend}: browser tools {'off' if background else 'left alone'}")
            i = cmd.index("--no-chrome") if has else -1
            check(not has or cmd[i + 4].startswith("--"), f"{where}, {perm}/{backend}: the disallowed list ends at a flag")
    prompt = B.cc_prompt(job("full"))
    check(("[Browser: not available" in prompt) == background, f"{where}: prompt says whether a browser is available")

raise SystemExit(1 if fails else 0)
