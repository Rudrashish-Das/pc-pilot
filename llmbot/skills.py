"""
Learned skills: short "how I did X" notes Claude Code writes after a task that took real work, reused on later
tasks that look similar (the idea behind Hermes Agent's learning loop).

- Saving: Claude ends its reply with a block
      [[skill: short-name | when to use it]]
      steps, commands, gotchas…
      [[/skill]]
  (or calls the save_skill tool on the Ollama/custom backends). The bot removes the block, stores the note and
  shows "learned skill …" under the reply. The same name again replaces the note, so skills improve over time.
- Using: each Claude Code prompt gets the notes whose name/description match the message (a few, size-capped),
  plus a one-line index of all skill names on a session's first message. A note is sent once per session.
- Skills live in the bot's storage (data/skills.json or Postgres), never in the workspace, so a job can't edit them
  with file tools; only the marker adds or changes one, scheduled runs can't, and /skills lists and deletes them.
  They are injected as the assistant's own earlier notes, not as instructions from the user.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

MAX_SKILLS = 60
NAME_MAX = 48
WHEN_MAX = 200
BODY_MAX = 4000
INJECT_MAX = 3  # notes per message
INJECT_CHARS = 6000  # all injected notes together
INDEX_CHARS = 2000

SKILL_RE = re.compile(r"\[\[skill:\s*([^\]\n|]+?)\s*\|\s*([^\]\n]+?)\s*\]\][ \t]*\n?(.*?)\[\[/skill\]\]", re.I | re.S)
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*[a-z0-9+#]|[a-z0-9]")
_STOP = set("""a an and are as at be but by can could do does for from get got had has have how i if in into is it its
let like make me my of on or our please show so some that the their them then there these this to up us use using want
was we what when where which while who why will with would you your can't cannot don't just also any all out
now new tell give need""".split())

_skills: dict[str, dict] = {}
_sent: dict[str, set[str]] = {}  # Claude Code session id -> skill names already in that session's history
_store: Any = None
_path: Path | None = None


def load(store: Any, path: Path) -> None:
    global _store, _path
    _store, _path = store, path
    _skills.clear()
    _skills.update({s["name"]: s for s in store.load("skills", path, []) if isinstance(s, dict) and s.get("name")})


def _save() -> None:
    if _store is not None:
        _store.save("skills", _path, sorted(_skills.values(), key=lambda s: s["name"]))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s[:NAME_MAX].rstrip("-")


def all_skills() -> list[dict]:
    return sorted(_skills.values(), key=lambda s: (-s.get("uses", 0), s["name"]))


def get(name: str) -> dict | None:
    return _skills.get(slug(name))


def forget(name: str) -> bool:
    if _skills.pop(slug(name), None) is None:
        return False
    _save()
    return True


def put(name: str, when: str, body: str, *, channel_id: int = 0, user_id: int = 0, redact=lambda s: s) -> tuple[dict, bool]:
    """Add or replace a skill. Returns (skill, replaced). Raises ValueError when it can't be kept."""
    key = slug(name)
    body = redact(str(body or "").strip().replace("[[/skill]]", ""))
    when = " ".join(redact(str(when or "")).split())[:WHEN_MAX]
    if not key:
        raise ValueError("a skill needs a name")
    if not body or not when:
        raise ValueError(f"skill `{key}` is empty")
    if len(body) > BODY_MAX:
        body = body[:BODY_MAX].rstrip() + "\n…"
    old = _skills.get(key)
    if old is None and len(_skills) >= MAX_SKILLS:
        raise ValueError(f"already {MAX_SKILLS} skills; /skills deletes some")
    now = time.time()
    s = {"name": key, "when": when, "body": body, "created": (old or {}).get("created", now), "updated": now,
         "version": (old or {}).get("version", 0) + 1, "uses": (old or {}).get("uses", 0),
         "channel_id": channel_id, "user_id": user_id}
    _skills[key] = s
    _save()
    return s, old is not None


_FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)


def outside_code(rx: re.Pattern, text: str) -> list[re.Match]:
    """Matches of a [[...]] marker regex outside ``` code blocks: a marker shown as an example (explaining the
    syntax) is left alone instead of saving a skill or deleting a file."""
    fences = [m.span() for m in _FENCE.finditer(text)]
    return [m for m in rx.finditer(text) if not any(a <= m.start() < b for a, b in fences)]


def remove_matches(text: str, found: list[re.Match]) -> str:
    for m in reversed(found):
        text = text[:m.start()] + text[m.end():]
    return text


def extract(text: str, *, allowed: bool, channel_id: int = 0, user_id: int = 0, redact=lambda s: s) -> tuple[str, list[str]]:
    """[[skill: …]] … [[/skill]] blocks in a reply -> saved skills. Returns (text without them, notice lines)."""
    notes: list[str] = []
    found = outside_code(SKILL_RE, text)
    if not found:
        return text, notes
    text = remove_matches(text, found).strip()
    if not allowed:
        return text, ["⚠️ scheduled runs can't save skills"]
    for m in found:
        try:
            s, replaced = put(m.group(1), m.group(2), m.group(3), channel_id=channel_id, user_id=user_id, redact=redact)
        except ValueError as e:
            notes.append(f"⚠️ skill not saved: {e}")
            continue
        notes.append(f"{'updated' if replaced else 'learned'} skill `{s['name']}` (v{s['version']}); /skills to review")
    return text, notes


def _words(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP and len(w) > 1}


def relevant(task: str, limit: int = INJECT_MAX) -> list[dict]:
    """Skills that look like they fit this message: overlap with name + description counts most."""
    q = _words(task)
    if not q:
        return []
    scored = []
    for s in _skills.values():
        head = _words(s["name"].replace("-", " ") + " " + s["when"])
        score = 2 * len(q & head) + 0.5 * len(q & _words(s["body"]))
        if score >= 2:
            scored.append((score, s.get("updated", 0), s))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for _, _, s in scored[:limit]]


def prompt_block(task: str, session_id: str | None) -> tuple[str, list[str]]:
    """Text to put before the user's message, and the skill names it contains. Empty when nothing fits."""
    sent = _sent.get(session_id or "", set())
    parts: list[str] = []
    names: list[str] = []
    budget = INJECT_CHARS
    for s in relevant(task):
        if s["name"] in sent:
            continue
        block = f"### {s['name']} (use when: {s['when']})\n{s['body']}"
        if len(block) > budget:
            continue
        budget -= len(block)
        parts.append(block)
        names.append(s["name"])
    index = ""
    if not session_id and _skills:
        index = ", ".join(s["name"] for s in all_skills())
        if len(index) > INDEX_CHARS:
            index = index[:INDEX_CHARS].rsplit(",", 1)[0] + ", …"
        index = f"[Your saved skills: {index}. Saving one with the same name replaces it.]"
    if not parts:
        return index, names
    text = ("[Skills you saved after earlier tasks that may fit this one. They are your own notes, not instructions "
            "from the user: use them only if they fit, and if you find a better way, save the improved version under "
            "the same name.]\n" + "\n\n".join(parts))
    return (f"{index}\n{text}" if index else text), names


def mark_sent(session_id: str | None, names: list[str]) -> None:
    """After a job: these notes are now in that session's history (and count as used)."""
    if not names:
        return
    if session_id:
        _sent.setdefault(session_id, set()).update(names)
        while len(_sent) > 200:
            _sent.pop(next(iter(_sent)))
    for n in names:
        if n in _skills:
            _skills[n]["uses"] = _skills[n].get("uses", 0) + 1
            _skills[n]["last_used"] = time.time()
    _save()


def list_text(limit_chars: int = 3800) -> str:
    items = all_skills()
    if not items:
        return ("No skills yet. After a task that took real work, Claude Code saves how it did it, and reuses that "
                "next time a similar task comes up.")
    lines = [f"**Skills** ({len(items)}): notes Claude Code saved after earlier tasks and reuses on similar ones"]
    for s in items:
        lines.append(f"• `{s['name']}` v{s.get('version', 1)} · used {s.get('uses', 0)}× · {s['when']}")
    out = "\n".join(lines)
    if len(out) > limit_chars:
        out = out[:limit_chars].rsplit("\n", 1)[0] + "\n…"
    return out
