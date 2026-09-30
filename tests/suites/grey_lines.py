import sys
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)

src = ("Four. Here you go 👋\n"
       '-# 🎙️ "hi"\n'
       "-# ⏰ Reminder `r1` set for <t:1:t> (<t:1:R>): Take your meds 💊\n"
       "-# 🗓️ Task `a` scheduled: 🤖 once, <t:2:f> (<t:2:R>): x\n"
       "-# 🗑️ deleted `a.txt` from the workspace\n"
       "-# ⚠️ not done: it says the file was deleted, but nothing was deleted\n"
       "-# qwen3.5:9b self-hosted · 10s · 5k ctx · session 15f58e02 · ⛔ blocked Read (read mode, change in /panel)")
out = B.quiet_grey(src)
print(out)
lines = out.split("\n")
assert lines[0] == "Four. Here you go 👋", "normal text keeps its emoji"
assert all(not B._EMOJI_RE.search(l) for l in lines[1:]), "no emoji left in grey lines"
assert all(l.startswith("-# ") and "  " not in l for l in lines[1:]), "clean spacing"
assert "<t:1:t> (<t:1:R>)" in lines[2] and "·" in lines[-1] and "→" in B.quiet_grey("-# a → b"), "timestamps, dots, arrows kept"
assert B.host_label("http://localhost:11434", "m") == "m self-hosted"
print("\nALL V21 CHECKS PASSED")
