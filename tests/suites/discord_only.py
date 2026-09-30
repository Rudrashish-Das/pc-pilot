"""Discord only (no TELEGRAM_BOT_TOKEN): the backend starts without loading the Telegram front end at all."""
import asyncio, os, sys

os.environ.pop("TELEGRAM_BOT_TOKEN", None)
from _setup import B  # noqa  (first: isolates the bot)


def check(c, label):
    assert c, label
    print("  ok", label)


async def main():
    check(B.DISCORD_TOKEN and not B.TELEGRAM_BOT_TOKEN, "config: Discord token only")
    await B.core_start()
    check("llmbot.telegram" not in sys.modules and B._telegram is None, "Telegram module never imported")
    check([type(f).__name__ for f in B._frontends] == ["DiscordFrontend"], "only the Discord front end")
    check(B.frontend_for(900000000000000001) is not None, "Discord ids routed to Discord")
    check(not B.is_allowed(111), "Telegram-range ids get nothing")
    await B.core_stop()
    print("\nALL DISCORD-ONLY CHECKS PASSED")


asyncio.run(main())
