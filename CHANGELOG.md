# Changelog

Before 0.23.0, each version was a separate file (`bot.py`, then `bot_v2.py` to `bot_v22.py`). From 0.23.0 on, git history is the record.

## Unreleased
- Fixed: Chrome signed you out of every site after each restart while `bot_control.ps1 boot` was set up. The boot task logged on without your password (S4U), and Windows' per-user encryption (DPAPI) then made a master key your real session couldn't open, and switched your account to it. Chrome couldn't decrypt or save cookies. `boot` now asks for your Windows password and stores it with the task (run it again after a password change), and `status` warns about an old S4U task. If you had one: `unboot`, sign out, and sign in with your password, not your PIN, once.
  - When Windows refuses the boot task's stored password (changed or expired), the bot says so: `status` warns, the bot logs it and adds it to the dashboard's activity when it starts at sign-in instead, `/power` shows it, and the "back online" message after a restart includes it.
- 📨 **Guests: `/status` and `/ping`** for someone who should only check on the PC (`GUEST_IDS`, Discord or Telegram ids). Owners can use them too; guests get nothing else.
  - `/status`: the game being played and for how long, how long the laptop has been on (since it last started or woke up: with Fast Startup, "shut down" is a hibernation, so the boot time alone can be days old), and whether the screen is locked or nobody is signed in. Games are what Windows' Game Bar has recognised on the PC, plus anything running from a Steam, Epic, Riot, Xbox, GOG, EA, Ubisoft or `Games` folder (launchers, anti-cheat and crash reporters left out).
  - `/ping <message>`: a small window on the PC's screen that stays on top of everything, with a notification sound, and a box to reply in; the reply goes back to whoever pinged (a Discord DM, or their Telegram chat), and closing it tells them it was seen. It shows a Discord or Telegram badge for where the ping came from, and the Mavis logo as its icon on the title bar and taskbar. It doesn't take the keyboard from a game in front. Pings while it's open go into the same window (the latest one shown, with a count) instead of opening more, and a burst sounds once; one answer goes back to each person who pinged. Nobody signed in: they're told it can't pop up. Spam guard: a guest gets `PING_LIMIT` pings (default 5) per 10 minutes, at least 15 s apart, and is told when to try again; owners aren't limited.
  - New dependency: `psutil` (`pip install -r requirements.txt`).
- Fixed: scheduled tasks and reminders in a deleted Discord thread, Telegram topic or dashboard chat. Before, a Telegram topic's task still ran every time (Claude Code spend included) and its reply was lost; a Discord thread's task silently never ran; a dashboard chat came back as "Reminders". Now:
  - They post in the chat the thread or topic belonged to, with a note saying why, and a repeating task moves there for good. A Discord channel with no parent gets a DM instead, as reminders already did.
  - Telegram sends bots no notice when a topic is deleted, so before a task or reminder runs for a topic, the bot checks it still exists: a reply to a message that doesn't exist, which always fails, so nothing is posted, and whose error says whether the topic is still there. (A typing indicator or an unchanged topic edit succeed even for deleted topics: tested against the real API.) Nothing is run for a reply nobody would get. A reply that finishes after its topic was deleted goes to the group with a note instead of being lost, and so does anything sent there later.
  - Deleting a dashboard chat that has tasks or reminders asks first: cancel them, or keep them in a chat named "Scheduled".
- Fixed: reminders and one-shot scheduled prompts that came due while the bot was off (or the PC asleep) could be lost: they fired 10 s after start, before Discord had logged in, found no channel and were dropped. They now wait (up to 15 minutes, still saved) for Discord or Telegram to connect.
- Fixed: a scheduled Claude Code run replaced the chat's own session with its fresh one, so your next message continued the scheduled run instead of your conversation.
- Fixed: cron weekday ranges starting at 0 (`0 9 * * 0-6`, `0-3`) were rejected, and steps counted from Monday (`*/2` ran Mon/Wed/Fri/Sun instead of Sun/Tue/Thu/Sat).
- Fixed: a reply that set a reminder, task or delete lost its other notes ("learned skill …", the small-context-window warning).
- Fixed: a long Discord chat reply plus its notes could pass 2000 characters, so Discord refused it, part of the reply went missing and requested file deletes were skipped. Notes that don't fit now go in their own message.
- Fixed: continuing a session the bot had no cost record for (opened from the dashboard after running in a terminal) counted its whole lifetime cost as today's spend, tripping the daily cap at once.
- Fixed: the sign-in handoff could exit in the middle of a local-model reply (between its web searches) or a voice transcription.
- Fixed: a deleted dashboard chat's id was reused by the next new chat, which then continued the old chat's Claude Code session and memory.
- Fixed: "in 99999999999 days" crashed the reply instead of saying it's too far away; a failure while posting a Claude Code reply now says so instead of leaving no answer.
- Fixed: a failed `bot.log` rollover (file locked) recursed thousands of times on every log line until the file was free.
- Cancelling someone else's task or reminder now says "no task/reminder with that id" instead of confirming it exists. Bad numbers sent to the dashboard's API get a normal answer instead of a server error.
- Fixed: with `bot_control.ps1 boot`, Claude Code using Chrome signed you out of Google and every other site. The boot copy runs in session 0 under an S4U logon, which has no password and so no DPAPI keys: Chrome started from there can't decrypt the profile's cookies. It also stayed in session 0 after sign-in, because the Startup apps launcher saw it running and did nothing. Now the launcher asks it to hand over at sign-in. It exits once no job is running, and the launcher starts the bot again in your desktop session. Run `bot_control.ps1 install` once to rebuild the launcher.
  - Before sign-in (the boot copy, session 0), Claude Code jobs get no browser: `--no-chrome`, no Claude in Chrome or computer-use tools, and a `[Browser: not available …]` line telling them not to start Chrome. They use WebSearch/WebFetch, or say the task needs someone signed in to the PC.
- Fixed: a failed lookup of Claude Code's skills (CLI missing, timeout) made every `/skills` or `/skill` typo start another one (up to 60 s each), and an error starting the CLI left Discord's `/skills` "thinking" forever. Failures are now caught and not retried for 5 minutes.
- Fixed: a message starting with a path (`/etc/hosts what is this?`) was sent to Claude Code as a slash command, without the time and access lines. Skill names now match regardless of case (`/skill mytool` finds `MyTool`).
- Fixed: `[[skill:]]`, `[[attach:]]` and `[[delete:]]` shown as examples inside a ``` code block were acted on (saving a skill or deleting a file); only markers outside code blocks count now.
- Fixed: a Claude Code skill made in chat ("make a skill called test…") wasn't found by `/skill test` until some later message ran. The bot's skill list was only refreshed when a job started. Now a name it doesn't know makes it ask Claude Code again, and `/skills` (and `/skill` with no name) shows a list at most 30 seconds old.
- Fixed: in a Discord DM the bot now answers every message, with no @mention or slash command needed (it only did for voice notes).
- 🧰 **Claude Code's skills** (docx, pdf, pptx, xlsx, deep-research, dataviz, code-review, skill-creator, … and your own in `~/.claude/skills`) now work from Discord, Telegram and the dashboard chat. Claude uses them by itself when a request fits; `/skill <name> [request]` runs one (Discord autocompletes the name and takes a file; on Telegram and the web, attach files to the command). `/skills` lists them next to the learned ones. `CC_SKILLS=false` turns them off again (they add ~2.5k cached tokens per message; MCP servers stay off either way). A skill runs with the chat's own permissions: skills that run scripts need full access.
- Claude Code on a small local model (seen with qwen3.5:9b):
  - A run that ends by announcing a step ("I'll create the file.") without calling any tool now shows as ⚠️ **Not done** (an error), not a success.
  - The first message of a session warns when Claude Code's setup fills most of the model's context window (~26k of 32k), with the fix: raise `OLLAMA_CONTEXT_LENGTH` to 65536 or more.
- ✨ **Auto** engine: finished Claude Code runs go into the local model's chat memory, and it's told that Claude Code can do what it can't. Before, it forgot the run it had just proposed and went on telling the user it had no file access.
- Slash commands work in DMs with the bot when `GUILD_ID` is set: global copies limited to DMs (Discord command contexts), so servers still get their instant per-server copies without seeing each command twice.
- `GUILD_ID=all`: slash commands in every server the bot is in, and in servers it joins later, instantly. Before, a server missing from `GUILD_ID` had no slash commands at all.
- Plan limits show the clock time they reset, not only how long until then: "resets at 14:30 (in 1 hour)", "on Sunday, 4 Oct, 14:30 (in 3 days)".
- The dashboard chat refreshes its `/` command list when the bot restarts (a page left open kept offering the old list).
- 🧵 **Discord threads** work like Telegram topics: a new thread (or forum post) starts with its channel's settings (engine, models, permissions, "full access without asking") and its own Claude Code session; `/panel` in a thread changes only that thread; "without asking" turned on or off in the channel also applies to its threads. The permission check asks for "Send Messages in Threads" there.
- 📊 **`/usage`** (Discord, Telegram, dashboard chat), like the Claude app's usage panel: this chat's context window (tokens used of the model's real window, e.g. 153.5k / 200k), your Claude plan's limits (5-hour and weekly, % used and when each resets) and today's API-equivalent spend. Plan limits come from Claude Code itself after every Anthropic-backend job; 🔄 **Check plan now** asks with a tiny Haiku call (~$0.003 API-equivalent). The stats line warns once a limit passes 80%, `/panel` shows a one-line summary, and the dashboard's usage card has the bars.
- The Claude models' context window is now read from Claude Code (`modelUsage`), so stats lines show e.g. `41k/200k ctx` on the Anthropic backend too, and warn when it's almost full.
- 📘 **Skills**: after a task that took real work, Claude Code saves what worked (name, when to use it, steps, gotchas). Later messages that look like that task get the note in their prompt, so it starts from what worked. Same name = improved version. `/skills` lists, shows and forgets them; scheduled runs can't save any; `SKILLS_ENABLED=false` turns it off. Idea from Hermes Agent's learning loop.
- 🧵 **Telegram topics**: each topic (private chat with threaded mode on, or a group with Topics) is its own conversation, with its own Claude Code session, settings and memory. `/topic <name>` creates one. New topics copy the chat's settings (not its session). "Full access without asking" set in the main chat applies to all its topics; set in a topic, to that topic only.
- Claude Code is told the chat's current permission in every message (`[Access: …]`). Before, a session that started read-only or edit kept saying it couldn't run commands after the chat was switched to full access.
- ☠️ **Full access without asking**, per chat (`/panel` → ⚙️ Settings, owners): full-access Claude Code messages run with no confirmation card, for 12 hours or until turned off.
  - Jobs the local model proposes still ask.
  - It's never available on the Ollama backend.
  - It turns off when the chat leaves full access.
- `bot_control.ps1 start` (and `restart`) launch the bot through Explorer, like Startup apps.
  - From an administrator window it no longer runs as administrator, so its Claude Code jobs can't get admin rights.
  - Closing the terminal that started it no longer stops it.
  - `status` and `stop` also find a copy that was started as administrator; `stop` says that it needs an administrator window.
  - `stop` and `restart` also stop a copy the boot task started, from a normal window (by ending the task).
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
