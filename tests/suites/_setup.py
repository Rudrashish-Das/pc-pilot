"""Shared setup for the suites. Import it before anything else: it loads the bot with no .env and a throwaway data
folder, so tests never touch your real settings, tasks or reminders.

LLMBOT_LIVE=1 also runs the parts that call real services (Claude Code on Anthropic = billed, Ollama, Whisper)."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
TMP = Path(tempfile.mkdtemp(prefix="llmbot-test-"))
LIVE = os.getenv("LLMBOT_LIVE") == "1"

os.environ["LLMBOT_ENV_FILE"] = str(TMP / "none.env")  # doesn't exist: only the defaults and os.environ apply
os.environ["LLMBOT_DATA_DIR"] = str(TMP / "data")
os.environ.setdefault("TIMEZONE", "Asia/Kolkata")  # the suites' expected times are written in IST
# Never used to connect; makes Discord the front end for its ids. A suite can set DISCORD_TOKEN="" first (Telegram only).
os.environ.setdefault("DISCORD_TOKEN", "fake-token-for-tests")
sys.path.insert(0, str(ROOT))

import llmbot.core as B  # noqa: E402
