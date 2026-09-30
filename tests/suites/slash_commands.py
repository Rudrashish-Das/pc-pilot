import asyncio, os, sys, tempfile
from pathlib import Path
os.environ["GUILD_ID"] = "111, 222"
import discord
from _setup import B, FIXTURES, LIVE, TMP  # noqa  (first: isolates the bot)

tmp = Path(tempfile.mkdtemp())
B.SETTINGS_FILE = tmp / "s.json"; B.TASKS_FILE = tmp / "t.json"; B.REMINDERS_FILE = tmp / "r.json"; B.USAGE_FILE = tmp / "u.json"
B._tasks.clear(); B._reminders.clear()


def check(c, label):
    assert c, label
    print("  ok", label)


calls = []


class G:
    def __init__(self, gid, name): self.id, self.name = gid, name


async def run(guild_ids, joined, global_existing, guild_existing):
    calls.clear()
    B.scheduler = B.AsyncIOScheduler(timezone=B.TZ)
    B.GUILD_IDS = guild_ids
    bot = B.LLMBot()
    bot._connection.application_id = 99
    tree = bot.tree
    tree.copy_global_to = lambda guild: calls.append(("copy", guild.id))

    async def sync(guild=None):
        calls.append(("sync", guild.id if guild else "global"))
        if guild is not None and guild.id == 333:
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "Missing Access")
    tree.sync = sync

    async def fetch_commands(guild=None):
        return guild_existing.get(guild.id, []) if guild else global_existing
    tree.fetch_commands = fetch_commands

    async def upsert_global(app, payload): calls.append(("clear-global", app, payload))
    async def upsert_guild(app, gid, payload): calls.append(("clear-guild", gid, payload))
    bot.http.bulk_upsert_global_commands = upsert_global
    bot.http.bulk_upsert_guild_commands = upsert_guild
    B.claude_bin = lambda: None
    B._spawn = lambda coro: coro.close()
    type(bot).guilds = property(lambda self: joined)
    await bot.setup_hook()
    await bot.on_ready()
    await bot.on_ready()  # second READY (reconnect) must not redo the cleanup
    return calls


async def main():
    print("== parsing")
    check(B.GUILD_IDS == [111, 222], "GUILD_ID=111, 222 -> two servers")
    print("== several servers, coming from global mode")
    c = await run([111, 222], [G(111, "A"), G(222, "B"), G(444, "Old")], ["x"], {444: ["x"]})
    check(("sync", 111) in c and ("sync", 222) in c and ("sync", "global") not in c, "synced to both servers, not globally")
    check(("clear-global", 99, []) in c, "old global commands removed (no duplicates)")
    check(("clear-guild", 444, []) in c and c.count(("clear-guild", 444, [])) == 1, "leftovers in a server no longer listed removed once")
    check(not any(x[0] == "clear-guild" and x[1] in (111, 222) for x in c), "listed servers untouched")
    print("== server the bot isn't in / can't access")
    c = await run([111, 333], [G(111, "A")], [], {})
    check(("sync", 333) in c and ("sync", 111) in c, "a failing server doesn't stop the others")
    print("== global mode")
    c = await run([], [G(111, "A"), G(222, "B")], [], {111: ["x"], 222: []})
    check(("sync", "global") in c and ("clear-guild", 111, []) in c and ("clear-guild", 222, []) not in c,
          "global sync; old per-server copies cleared only where they exist")
    print("\nALL V20 CHECKS PASSED")

asyncio.run(main())
