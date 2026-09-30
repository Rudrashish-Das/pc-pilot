"""Runs each script in tests/suites in its own process (the bot keeps module-level state, so suites don't share one).

    pytest                     offline suites (no tokens, no network, no cost)
    LLMBOT_LIVE=1 pytest       also the live parts: Claude Code on Anthropic (billed, haiku), Ollama, Whisper
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SUITES = Path(__file__).parent / "suites"
LIVE = os.getenv("LLMBOT_LIVE") == "1"


@pytest.mark.parametrize("suite", sorted(p.name for p in SUITES.glob("*.py") if not p.name.startswith("_")))
def test_suite(suite):
    if suite.startswith("live_") and not LIVE:
        pytest.skip("live suite: set LLMBOT_LIVE=1")
    if suite.startswith("pg_") and not os.getenv("LLMBOT_TEST_DATABASE_URL"):
        pytest.skip("Postgres suite: set LLMBOT_TEST_DATABASE_URL to a throwaway database")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, suite], cwd=SUITES, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    assert r.returncode == 0, f"{suite} failed\n--- stdout\n{r.stdout[-4000:]}\n--- stderr\n{r.stderr[-4000:]}"
