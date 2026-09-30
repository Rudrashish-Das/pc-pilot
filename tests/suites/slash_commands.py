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


async def run(guild_ids, joined, global_existing, guild_existing, all_servers=False):
    calls.clear()
    B.scheduler = B.AsyncIOScheduler(timezone=B.TZ)
    B.GUILD_IDS = guild_ids
    B.GUILD_ALL = all_servers
    bot = B.LLMBot()
    bot._connection.application_id = 99
    tree = bot.tree

    async def cb(inter): pass
    tree.add_command(discord.app_commands.Command(name="usage", description="d", callback=cb))
    tree.copy_global_to = lambda guild: calls.append(("copy", guild.id))

    async def sync(guild=None):
        calls.append(("sync", guild.id if guild else "global"))
        if guild is None:
            calls.append(("contexts", {tuple(tree.allowed_contexts._merge_to_array(c.allowed_contexts) or [])
                                       for c in tree.get_commands()}))
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
    before = {c: c.allowed_contexts for c in tree.get_commands()}
    await bot.setup_hook()
    calls.append(("restored", all(c.allowed_contexts is before[c] for c in tree.get_commands())))
    await bot.on_ready()
    await bot.on_ready()  # second READY (reconnect) must not redo the cleanup
    return calls


async def main():
    print("== parsing")
    check(B.GUILD_IDS == [111, 222], "GUILD_ID=111, 222 -> two servers")
    print("== several servers, coming from global mode")
    c = await run([111, 222], [G(111, "A"), G(222, "B"), G(444, "Old")], ["x"], {444: ["x"]})
    check(("sync", 111) in c and ("sync", 222) in c, "synced to both servers")
    check(("sync", "global") in c and ("contexts", {(1,)}) in c, "global copies for DMs only (contexts = bot DM)")
    check(("restored", True) in c, "DM limit taken off again (per-server syncs of new servers must not get it)")
    check(not any(x[0] == "clear-global" for x in c), "global commands kept (they're the DM copies now)")
    check(("clear-guild", 444, []) in c and c.count(("clear-guild", 444, [])) == 1, "leftovers in a server no longer listed removed once")
    check(not any(x[0] == "clear-guild" and x[1] in (111, 222) for x in c), "listed servers untouched")
    print("== server the bot isn't in / can't access")
    c = await run([111, 333], [G(111, "A")], [], {})
    check(("sync", 333) in c and ("sync", 111) in c, "a failing server doesn't stop the others")
    print("== GUILD_ID=all")
    c = await run([], [G(111, "A"), G(555, "New")], [], {}, all_servers=True)
    check(("sync", 111) in c and ("sync", 555) in c and ("contexts", {(1,)}) in c,
          "every joined server + DM copies")
    check(not any(x[0] == "clear-guild" for x in c), "no server treated as 'not listed'")
    print("== global mode")
    c = await run([], [G(111, "A"), G(222, "B")], [], {111: ["x"], 222: []})
    check(("sync", "global") in c and ("clear-guild", 111, []) in c and ("clear-guild", 222, []) not in c,
          "global sync; old per-server copies cleared only where they exist")
    check(("contexts", {()}) in c, "global mode: commands everywhere (no DM-only limit)")
    print("\nALL V20 CHECKS PASSED")

asyncio.run(main())
