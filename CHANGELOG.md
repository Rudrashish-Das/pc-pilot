# Changelog

Before 0.23.0, each version was a separate file (`bot.py`, then `bot_v2.py` to `bot_v22.py`). From 0.23.0 on, git history is the record.

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
