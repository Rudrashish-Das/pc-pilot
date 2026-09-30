# Changelog

Before 0.23.0, each version was a separate file (`bot.py`, then `bot_v2.py` to `bot_v22.py`). From 0.23.0 on, git history is the record.

## Unreleased
- Renamed to **pc-pilot** (github.com/Rudrashish-Das/pc-pilot):
  - What changes: the Startup apps entry (`pc-pilot`), the launcher (`bin\pc-pilot.exe`), the boot task (`pc-pilot (boot)`) and the web user-agent.
  - Updating: run `bot_control.ps1 install` again, and `boot` again (as administrator) if you use it.
  - Unchanged: the Python package and the run command (`python -m llmbot`).
- Dashboard theme button next to the status line: 🖥️ System (follows the phone or PC), ☀️ Light, 🌙 Dark. Tap to cycle; each browser remembers its choice.
- Chat from the dashboard: a **Chat** tab talks to the bot over your Wi-Fi, a third front end next to Discord and Telegram on the same backend. It works without internet (local model, Claude Code on Ollama, reminders). Several chats, each with its own engine, model and session; `/panel` and the other menus and buttons work in the page; file attachments both ways; Claude Code's steps shown live. `DASHBOARD_CHAT` = owner (default: the access key holder is an owner) / user (local model only) / off.
- Dashboard chat: `/` autocompletes commands (with what each does), and 🎙️ voice input turns speech into text with the PC's Whisper, into the message box to check before sending. The dashboard is also served over https (`DASHBOARD_HTTPS_PORT`, default 8766, self-signed certificate in `data/`), because phones only allow the microphone on https; `bot_control.ps1 firewall` opens that port too. New dependency: `cryptography`.
- With the dashboard chat on, the bot starts without internet: no more waiting for discord.com, or exiting after 5 minutes. Discord and Telegram keep retrying in the background.
- Scheduled tasks are pinned to the engine and model that created them, and each has its own permissions. `/tasks` → ⚙️ changes both:
  - the model a task runs on: the creator can pick among local models, and owners can also pick any Claude Code backend and model;
  - read-only, edit or full access for Claude Code tasks (owners only; full access asks you to confirm and isn't offered on Ollama);
  - whether the task may set reminders (owners only).

  Tasks still never create tasks. The approving owner is checked again on every run. Existing tasks take their chat's current settings.
- The dashboard has a name: the bot announces `llmbot.local` on your network (mDNS, `DASHBOARD_NAME`), pointing at the PC's current Wi-Fi address, so `http://llmbot.local:8765` keeps working when the IP changes. `/dashboard` links use the name, with the IP as a fallback; `bot_control.ps1 firewall` also opens UDP 5353 and warns when the Wi-Fi is set to Public.
- The bot can come back after `/power` → Restart without anyone signing in. Run `bot_control.ps1 boot` once, as administrator: it registers a scheduled task that starts the bot at boot, as you, with no stored password. Startup apps only ran at sign-in, so a restarted PC waited at the lock screen with the bot offline.
  - The bot starts `ollama serve` when nothing answers on this PC's Ollama port (`OLLAMA_AUTOSTART`, on by default).
  - Lock works from a bot started at boot.
  - The Startup apps launcher does nothing when the bot is already running.
  - `/power` says when the bot will be back (at boot, at sign-in, or not at all) and how to fix it.
- A logo (`assets/logo.png`, `assets/logo.ico`). `bot_control.ps1 install` now builds `bin\pc-pilot.exe`, a small launcher, so Task Manager › Startup apps lists the bot as "pc-pilot" with the logo. Before, the entry appeared as "Python". `status` also says when the entry is disabled in Task Manager.
- Web dashboard, for your phone on the same Wi-Fi: what the bot is doing now (live Claude Code steps, local replies, voice notes, queue, pending power action), a searchable activity history (replies, reminders, scheduled prompts, power, model unloads, sleep/resume, warnings and errors), Claude Code jobs with costs, today's spend, reminders and scheduled prompts, models in memory, the PC's RAM/GPU/battery/disk, and the log. It runs inside the bot on port 8765 (`DASHBOARD_PORT`, `DASHBOARD_HOST`), needs an access key (`DASHBOARD_TOKEN` or the generated `data/dashboard.key`), refuses public internet addresses, and is read-only. `/dashboard` gives owners the link; `scripts\bot_control.ps1 dashboard` prints it and `firewall` opens the port on Private networks. Activity is kept in `data/events.jsonl` (same size cap as the logs) or the `llmbot_events` table on Postgres.
- Failures come with a "💡" line saying what to try. It comes from fixed rules in `llmbot/hints.py`, not a model, and fills in your real settings. It covers the local model server being down, a missing model, out of memory, Claude Code login, credit, rate limits, usage limits, context overflow, timeouts, a missing CLI, `/power` errors, voice decoding, DNS, Discord permissions, file size and Postgres. Unknown errors get no made-up advice.
- Local engine is faster on follow-ups. Ollama now reuses its cached prompt instead of re-reading the whole chat every message (measured: 0.2s instead of 3s for 4k tokens). To make that possible:
  - the current time moved from the system prompt into each user message;
  - old exchanges are dropped in blocks, not one per message;
  - the model is kept loaded until the bot's own idle unload, instead of Ollama's 5-minute default.
- Local engine memory is 20 exchanges instead of 6 (`LLM_HISTORY_TURNS`), with a size cap (`LLM_HISTORY_CHARS`, 40k characters) so long answers can't overflow a 32k context window.
- Claude Code: the wait after a reply for a lingering CLI is 5 seconds instead of 15.
- Security fixes from an audit:
  - Web fetches connect to the exact address that passed the private-network check, so a DNS answer that changes between check and connect (DNS rebinding) can't reach the PC's local services.
  - Read and edit Claude Code jobs can't write `.claude/`, `.mcp.json` or `CLAUDE.md` in the workspace, and don't load its project settings. A prompt-injected edit could otherwise have planted hooks that run commands on the next job.
  - The web-tools helper for Ollama/custom backends no longer imports Python modules from the workspace (`python -P`), which an edit job could write.
  - With an empty `ALLOWED_USER_IDS`, Discord DMs are owners-only. Before, anyone sharing any server with the bot could DM it.
  - `/tasks` and the local model's list tools show only your own reminders and tasks (owners see all). On Discord, `/tasks` is visible only to you.
  - `/local model:` accepts only the server's listed models from non-owners, since on a paid gateway any model name could cost money.
  - Voice notes stop decoding at `VOICE_MAX_SECONDS`, instead of decoding the whole file first (a small file can hold hours of audio).
  - Telegram buttons only act in the chat they were posted in, and disabled buttons can't be triggered.
- `/power` (owners, on Discord and Telegram): lock, sleep, hibernate, restart or shut down the PC, with a confirmation step. Restart and shutdown wait 30 seconds and can be cancelled. The bot posts "back online" in the chat that asked, after a restart (from a note in `data/`) or after waking up (it notices the time jump).

- Optional Postgres storage (`DATABASE_URL`): settings, tasks, reminders, usage, local chat memory and the power note go in `llmbot_state`, and every Claude Code job is a row in `llmbot_jobs`. Existing `data/` files are imported once. Without `DATABASE_URL`, the same data stays in JSON files in `data/`. That now includes `history.json`, so the local model's memory survives restarts, and `jobs.jsonl`.

- Log rotation with a total cap: `bot.log` rolls over into dated gzip archives, and the oldest are deleted to keep all logs under `LOG_MAX_TOTAL_MB` (default 2048). This replaces the old 2 MB × 3 backups. Also new: `LOG_FILE_MB`, `LOG_COMPRESS`, `LOG_KEEP_DAYS` and `LOG_LEVEL`. `jobs.jsonl` follows the same limits.

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
