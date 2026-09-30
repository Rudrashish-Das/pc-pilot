# Changelog

Before 0.23.0, each version was a separate file (`bot.py`, then `bot_v2.py` to `bot_v22.py`). From 0.23.0 on, git history is the record.

## Unreleased
- Local engine is faster on follow-ups. Ollama now reuses its cached prompt instead of re-reading the whole chat every message (measured: 0.2s instead of 3s for 4k tokens). To make that possible:
  - the current time moved from the system prompt into each user message;
  - old exchanges are dropped in blocks, not one per message;
  - the model is kept loaded until the bot's own idle unload, instead of Ollama's 5-minute default.
- Local engine memory is 20 exchanges instead of 6 (`LLM_HISTORY_TURNS`), with a size cap (`LLM_HISTORY_CHARS`, 40k characters) so long answers can't overflow a 32k context window.
- Claude Code: the wait after a reply for a lingering CLI is 5 seconds instead of 15.
- Security fixes from an audit:
  - Web fetches connect to the exact address that passed the private-network check, so a DNS answer that changes between check and connect (DNS rebinding) can't reach the laptop's local services.
  - Read and edit Claude Code jobs can't write `.claude/`, `.mcp.json` or `CLAUDE.md` in the workspace, and don't load its project settings. A prompt-injected edit could otherwise have planted hooks that run commands on the next job.
  - The web-tools helper for Ollama/custom backends no longer imports Python modules from the workspace (`python -P`), which an edit job could write.
  - With an empty `ALLOWED_USER_IDS`, Discord DMs are owners-only. Before, anyone sharing any server with the bot could DM it.
  - `/tasks` and the local model's list tools show only your own reminders and tasks (owners see all). On Discord, `/tasks` is visible only to you.
  - `/local model:` accepts only the server's listed models from non-owners, since on a paid gateway any model name could cost money.
  - Voice notes stop decoding at `VOICE_MAX_SECONDS`, instead of decoding the whole file first (a small file can hold hours of audio).
  - Telegram buttons only act in the chat they were posted in, and disabled buttons can't be triggered.

## 0.24.0
- Telegram: the stats line is a tap-to-reveal spoiler, because Telegram has no small grey text. It can be turned off per chat in `/panel` → ⚙️ Settings. Warnings (compact hints, blocked tools, unbacked claims) stay visible either way.
- Tests: Telegram-only and Discord-only setups.

## 0.23.0 — repository layout
- The code is now the `llmbot` package: `llmbot/core.py` (was `bot_v23.py`) and `llmbot/telegram.py` (was `telegram_v1.py`). Run it with `python -m llmbot`.
- Runtime files moved to `data/`: `settings.json`, `tasks.json`, `reminders.json`, `usage.json`, `mcp_web.json` and `bot.log`. `LLMBOT_DATA_DIR` and `LLMBOT_ENV_FILE` override the locations.
- `TIMEZONE` now defaults to `UTC` when it isn't set.
- New files and folders:
  - `scripts/bot_control.ps1`, which gains `restart`;
  - `docs/SETUP.md`;
  - a pytest suite that runs offline;
  - CI.

## Earlier versions
- **v23** Telegram front end on the same backend and in the same process. Chats are told apart by id range.
- **v22** The grey line shows context used against the model's real limit for Ollama models.
- **v21** Grey `-#` lines have no emoji.
- **v20** `GUILD_ID` takes a list of servers, and stale command copies are removed.
- **v19** Claude Code on Ollama: the bot serves its own web and action tools over MCP.
- **v18** Attachments go to Claude Code: images, PDFs and code.
- **v17** Scheduled prompts on Claude Code, one-shot or cron. Every prompt carries the local date and time.
- **v16** `[[delete:]]`: workspace files are deleted after the reply is posted.
- **v15** One-shot reminders, with Done and Snooze buttons.
- **v14** Voice notes, transcribed by local Whisper.
- **v13** Session id shown in the stats line.
- **v12** Reply styles: chat or cards. Added `/stop`, `/log` and `/help`.
- **v11** Per-message cost, a context meter and Compact.
- **v10** @mentions of the bot's role count as addressing the bot.
- **v9** Attachments from Claude Code.
- **v8** Claude Code is told it's in a chat and uses web search.
- **v7** Sessions per workspace, idle reset, and per-model cost in the log.
- **v6** Redaction, secret scrubbing, path-scoped read/edit profiles, lean flags and budgets.
- **v5** Channel-permission preflight.
- **v4** `/panel` is only visible to you, and permissions are changed in place.
- **v3** `/<BOT_COMMAND>` alias and continued sessions.
- **v2** Idle model unload, `/unload`, startup script and single-instance lock.
- **v1** Local engine with tools, Claude Code runner, Auto mode, `/panel` and allow-lists.
