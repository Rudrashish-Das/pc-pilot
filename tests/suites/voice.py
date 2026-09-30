import asyncio, sys, tempfile, time
from pathlib import Path
import discord, httpx
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)
B.is_telegram_id = lambda x: False  # these tests use small fake Discord ids

SP = TMP
OGG = (FIXTURES / "voice.ogg").read_bytes()  # SAPI speech: "What is two plus two? Answer in one word."
tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "settings.json"; B.USAGE_FILE = tmp / "usage.json"; B._usage.clear()
WS = SP / "ws_v14"; WS.mkdir(exist_ok=True)
B.WORKSPACES.clear(); B.WORKSPACES["default"] = WS
BOT_ID, OWNER, CH = 1111, 1, 1414
B.OWNER_IDS.add(OWNER); B.ALLOWED_USER_IDS.clear(); B.ALLOWED_USER_IDS.add(OWNER)
def check(c, label):
    assert c, label
    print("  ok", label)

class Obj:
    def __init__(self, **kw): self.__dict__.update(kw)
bot_user = Obj(id=BOT_ID); B.bot._connection.user = bot_user
class Typ:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass
class Att:
    def __init__(self, voice=True, duration=3.0, data=OGG): self._v, self.duration, self.size, self._d = voice, duration, len(data), data
    def is_voice_message(self): return self._v
    async def read(self): return self._d
replies = []
class FakeMsg:
    def __init__(self, content="", att=None, author=OWNER, guild=True, ref_author=None, ch=CH):
        self.author = Obj(id=author, bot=False); self.content = content; self.mentions = []; self.role_mentions = []
        self.attachments = [att] if att else []
        self.guild = Obj(me=Obj(roles=[])) if guild else None
        self.channel = Obj(id=ch, typing=lambda: Typ())
        resolved = None
        if ref_author is not None:
            resolved = discord.Message.__new__(discord.Message); resolved.author = Obj(id=ref_author)
        self.reference = Obj(resolved=resolved) if ref_author is not None else None
    async def reply(self, text, **kw): replies.append(text)

calls = []
real_handle = B.handle_prompt
async def fake_handle(channel, user_id, prompt, out, **kw): calls.append((prompt, out.prefix))
B.handle_prompt = fake_handle

async def routing():
    print("== which voice notes get answered")
    heard = []
    async def fake_hear(self, message, att): heard.append(1); return "what is two plus two"
    orig_hear = B.LLMBot._hear; B.LLMBot._hear = fake_hear
    cases = [
        ("mode all: voice note", "all", FakeMsg(att=Att()), True),
        ("mode replies: plain voice note", "replies", FakeMsg(att=Att()), False),
        ("mode replies: voice reply to bot", "replies", FakeMsg(att=Att(), ref_author=BOT_ID), True),
        ("mode off: voice note", "off", FakeMsg(att=Att()), False),
        ("mode off: even as reply to bot", "off", FakeMsg(att=Att(), ref_author=BOT_ID), False),
        ("DM voice note (mode off elsewhere)", "off", FakeMsg(att=Att(), guild=False), True),
        ("not an allowed user", "all", FakeMsg(att=Att(), author=999), False),
        ("normal audio file (not a voice note)", "all", FakeMsg(att=Att(voice=False)), False),
        ("plain text, no mention", "all", FakeMsg(content="hello"), False),
        ("plain text in a DM, no mention", "all", FakeMsg(content="hello", guild=False), True),
        ("plain text in a DM from someone not allowed", "all", FakeMsg(content="hello", guild=False, author=999), False),
        ("plain text replying to the bot", "all", FakeMsg(content="hello", ref_author=BOT_ID), True),
    ]
    for label, mode, msg, expect in cases:
        B.update_settings(CH, voice=mode); calls.clear()
        await B.bot.on_message(msg)
        got = bool(calls)
        assert got == expect, (label, calls)
        print(f"  ok {label}: {'answered' if got else 'ignored'}")
    B.update_settings(CH, voice="all"); calls.clear()
    await B.bot.on_message(FakeMsg(att=Att()))
    check(calls[0] == ("what is two plus two", '-# heard: "what is two plus two"'), "prompt = transcript; reply gets the 🎙️ heard line")
    B.LLMBot._hear = orig_hear

async def real_voice():
    print("== real transcription through the bot")
    t = time.time()
    text, secs = await B.transcribe(OGG)
    print(f"     heard {text!r} ({secs:.1f}s audio, {time.time() - t:.1f}s incl. model load)")
    check("plus" in text.lower() and ("2" in text or "two" in text.lower()), "Whisper transcribes the voice note")
    replies.clear()
    await B.bot.on_message(FakeMsg(att=Att(duration=999)))
    check(replies and "only listen to voice notes up to" in replies[0], f"too long -> short explanation: {replies[0]!r}")
    silent = B._decode_16k(OGG)[:0]
    import numpy as np
    replies.clear()
    orig = B.transcribe
    async def empty(data): return "", 2.0
    B.transcribe = empty
    await B.bot.on_message(FakeMsg(att=Att()))
    check(replies and "couldn't make out" in replies[0], "no speech -> says so")
    B.transcribe = orig
    B.LLM_IDLE_UNLOAD = 1; B._whisper_last = time.monotonic() - 5
    check(B._whisper is not None, "model loaded after use"); B._unload_whisper_if_idle()
    check(B._whisper is None, "Whisper unloaded after idle")

async def out_prefix():
    print("== heard line placement")
    sent = []
    class Ch:
        id = 5
        async def send(self, **kw): sent.append(kw["content"])
    o = B.Out(Ch(), prefix='-# heard: "hi"'); await o(content="Hello!"); await o(content="second")
    check(sent == ['-# heard: "hi"\nHello!', "second"], "prefix goes on the first reply only")
    sent.clear(); o = B.Out(Ch(), prefix='-# heard: "hi"'); await o(content="x" * 1995)
    check(sent[0] == '-# heard: "hi"' and len(sent[1]) == 1995, "near the 2000 limit -> heard line sent separately")

async def live():
    print("== live: voice note -> Whisper -> Claude (haiku) -> chat reply")
    B.handle_prompt = real_handle
    B.update_settings(CH, engine="claude", cc_model="haiku", cc_perm="read", style="chat", voice="all")
    B.http = httpx.AsyncClient()
    sent = []
    class LiveMsg(FakeMsg):
        async def reply(self, **kw): sent.append(kw.get("content"))
    m = LiveMsg(att=Att())
    async def send(**kw): sent.append(kw.get("content"))
    m.channel = Obj(id=CH, typing=lambda: Typ(), send=send)
    await B.bot.on_message(m)
    job = B._last_job[CH]
    while job.status != "done":
        await asyncio.sleep(0.3)
    await asyncio.sleep(0.3)
    print("     reply:", repr(sent[-1]))
    lines = sent[-1].splitlines()
    check(lines[0].startswith('-# heard: "') and "plus" in lines[0].lower(), "first line shows what was heard")
    check(any("4" in l or "four" in l.lower() for l in lines[1:-1]), "answered the spoken question")
    check(lines[-1].startswith("-# haiku"), "stats line at the bottom")

async def main():
    await routing(); await out_prefix()
    if LIVE:  # downloads the Whisper model, then calls Claude (billed)
        await real_voice(); await live()
asyncio.run(main())
print("\nALL V14 CHECKS PASSED")
