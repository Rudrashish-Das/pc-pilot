# Setup (Windows)

## 1. Discord application
1. Go to https://discord.com/developers/applications, click **New Application**, then open **Bot**.
2. **Reset Token**, then copy it into `DISCORD_TOKEN` in `.env`.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent** (needed for @mention replies).
4. Open **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **Send Messages**, **Read Message History** (also **Attach Files** and **Embed Links** for result files and cards)
   - Open the generated URL and add the bot to your server.
5. In Discord, go to **User Settings → Advanced** and turn on **Developer Mode**. Then right-click to copy:
   - the server icon, which gives you `GUILD_ID`
   - your own name, which gives you `OWNER_IDS` (and `ALLOWED_USER_IDS` for friends)

## 2. Ollama (local engine)
```powershell
winget install Ollama.Ollama
ollama pull qwen3.5:9b
setx OLLAMA_CONTEXT_LENGTH 32768
```
Quit Ollama from the tray and start it again so it picks up the context length. `ollama ps` should show `100% GPU`; if it shows a CPU/GPU split, the model is spilling into RAM and will be much slower.

### Claude Code on your own model (no Anthropic account)
`/panel` → ⚙️ Settings → backend **🦙 Ollama**, then pick the model. Claude Code keeps all its abilities (files, attachments, reminders, scheduled tasks), but your model does the thinking, for free.
- Claude Code needs a large context window: its instructions alone are ~11k tokens. Keep `OLLAMA_CONTEXT_LENGTH` at 32768 or more; Ollama's default on an 8 GB GPU is only 4096.
- Web search: Claude Code's own WebSearch only works with Anthropic, so on Ollama the bot gives Claude Code its own `web_search` / `fetch_page` tools (DuckDuckGo or SearXNG, same SSRF protection). No setup needed.
- Images only work if the model can see images.
- Expect it to be slower and less reliable with tools than Sonnet.

## 3. Claude Code
```powershell
irm https://claude.ai/install.ps1 | iex
claude
```
Log in once interactively, then quit. `claude --version` should work in a new terminal. If `claude` isn't on PATH for the bot, set `CLAUDE_BIN` to the full path (for example `C:\Users\<you>\.local\bin\claude.exe`).

## 4. Bot
```powershell
git clone https://github.com/Rudrashish-Das/pc-pilot.git
cd pc-pilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python -m llmbot
```
In Discord, run `/help` for a quick guide, or `/panel` for settings. Only you can see either.

### More than one server
Invite the bot to the other server, using the Discord Developer Portal → your app → OAuth2 → URL Generator: scopes **bot** + **applications.commands**, and permissions View Channels, Send Messages, Embed Links, Attach Files, Read Message History. Then either:
- list them all: `GUILD_ID=111111,222222` — commands appear instantly in each; or
- leave `GUILD_ID` empty — global commands, in every server the bot is in and in DMs (new/changed commands can take a while to show).

Restart the bot after changing it. It removes leftover copies so commands never show twice. Access is still controlled by `ALLOWED_USER_IDS` / `OWNER_IDS`, not by the server: people in other servers are ignored unless they're listed, and only owners get Claude Code (which runs on this PC, in your workspace).

## Telegram (optional)
Telegram is a second front end on the same bot, in the same process: engines, models, Claude Code sessions, reminders, scheduled prompts, budgets and the GPU queue are shared. Each Telegram chat has its own settings and session, like a Discord channel.
1. In Telegram, message **@BotFather** → `/newbot`, pick a name and a username ending in `bot`. Put the token in `TELEGRAM_BOT_TOKEN` in `.env`.
2. Restart the bot, open your new bot in Telegram and send `/start`. While `TELEGRAM_ALLOWED_USER_IDS` is empty it replies with your Telegram user id and does nothing else.
3. Put that id in `TELEGRAM_ALLOWED_USER_IDS` and restart. (`TELEGRAM_OWNER_IDS` is only needed if you allow other people: they then get the local model but not Claude Code.)

Using it:
- In a private chat, just send messages, photos, files (up to 20 MB, Telegram's limit for bots) or voice notes. In a group, @mention the bot or reply to it.
- Commands: `/panel`, `/new` (new Claude Code session), `/compact`, `/stop`, `/tasks`, `/remind in 10 min | take meds`, `/schedule 0 9 * * 1-5 | prompt`, `/claude`, `/local`, `/reset`, `/log`, `/unload`, `/help`. They appear in Telegram's `/` menu.
- The buttons are the same as on Discord. Dropdowns open as a list of options (⬅️ Back returns). **Edit** and **Follow up** ask you to send the text as your next message.
- Telegram can't show small grey text. The stats line under each reply (model, cost, context, session) is hidden behind a spoiler: tap it to read it. To remove it completely in a chat, use `/panel` → ⚙️ Settings → **📊 Stats line: turn off**. Warnings, such as "long chat, send /compact", always stay visible. Other small notes (reminder set, heard: …) are shown in italics.
- Telegram has no "only you can see this" messages: `/panel` and similar replies go into the chat, and short notices pop up on the button you tapped. Only allowed users can press the buttons.
- Messages sent while the bot was off for more than 30 minutes are skipped instead of answered late.
- Telegram only: leave `DISCORD_TOKEN` empty.
- Anyone can find a Telegram bot by name, so there is no "everyone" setting: only listed ids get answers. Strangers are ignored and logged in `data/bot.log`.

## 5. Run at logon (Task Manager › Startup apps)
```powershell
.\scripts\bot_control.ps1 install
```
This adds a **pc-pilot** entry, with the logo, to Startup apps. You can enable or disable it in Task Manager. The entry runs `bin\pc-pilot.exe`, a small launcher that `install` compiles from `scripts\launcher.cs` using the C# compiler built into Windows. Task Manager lists a startup entry under the name of the program it runs, so without the launcher the entry would show as "Python". The bot runs with no window and logs to `data\bot.log`. Other commands: `start`, `stop`, `restart`, `status`, `log`, `remove`. After pulling updates, run `restart`. If PowerShell blocks the script, run `powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 install`.

### Start at boot, before anyone signs in
Startup apps wait for someone to sign in. For the bot to come back after `/power` → Restart, or after a power cut, while the PC sits at the lock screen, run this once in PowerShell as administrator:
```powershell
.\scripts\bot_control.ps1 boot
```
- **What it does:** registers the scheduled task **pc-pilot (boot)**. At startup it runs `pythonw -m llmbot --boot` as you, using S4U, so Windows doesn't store your password. The bot waits up to 30 minutes for the network. Saved Wi-Fi networks connect before sign-in.
- **Ollama:** its app also waits for sign-in. So if nothing answers on `localhost:11434` when the bot starts, the bot runs `ollama serve` itself, hidden, with your `OLLAMA_*` settings. `OLLAMA_AUTOSTART=false` turns this off.
- **Signing in later:** the bot keeps running in Windows' background session. The Startup apps entry sees it and does nothing, so you get no second copy and no restart.
- **Lock from a bot started at boot:** Lock disconnects your screen session instead. You see the sign-in screen, and your apps keep running.
- **Checking and undoing:** `status` shows whether the task is set up; `unboot` (as administrator) removes it.

## Talking to the bot
- **Reply style** (`/panel` → ⚙️ Settings, default `REPLY_STYLE=chat`): **💬 Chat** replies like a person: a plain message, *typing…* while it works, and one small grey line with model · cost · today's spend · context · session id. **🗂️ Cards** brings back the embeds with live progress and Stop / Follow up / Retry / Full log / Compact buttons. `/claude` always uses cards.
- In chat style, reply to one of the bot's messages to keep talking (no @ needed); `/stop` stops a running reply and `/log` shows its full log.
- `/ask <anything>`, `@YourBot <anything>`, or `/<BOT_COMMAND> <anything>` (for example `BOT_COMMAND=jarvis` gives `/jarvis`) all use the channel's engine.
- Claude Code is told it's answering in a chat (Discord or Telegram): it uses WebSearch/WebFetch for current information (weather, news, prices) and pastes file contents when you ask to see a file. Ask it to send a file as an attachment and it's uploaded to the result card. Only files inside the workspace, up to 9 per reply and 10 MB each. Credential files (`.env`, keys and similar) are refused, and text files are redacted first. Override the text with `CC_SYSTEM_APPEND`.
- On the Claude Code engine, each channel keeps one ongoing conversation (`CC_CONTINUE=true`). Use **🆕 New CC session** in `/panel` to start fresh.

## Voice notes
- Hold the mic button in Discord (phone or desktop) and send a voice note in the bot's channel. It's transcribed on this PC and answered like a typed message. The reply starts with a small grey `heard: "…"` line.
- `/panel` → ⚙️ Settings → **Voice notes**: *answer all* (default), *replies only* (only voice notes sent as a reply to the bot) or *off*. DMs to the bot are always answered.
- The first voice note downloads the Whisper model once (~460 MB). It runs on the CPU so Ollama keeps the GPU, and unloads after `LLM_IDLE_UNLOAD` seconds idle, like the local LLM. Notes longer than `VOICE_MAX_SECONDS` (180) are refused.

## Files from Claude Code

**Sending it images and files:** attach them to your message (or use `file:` in `/ask`). With Claude Code they're saved in the workspace's `discord_uploads` folder, and Claude opens them with its Read tool: it sees images and PDFs and reads text and code. Up to 10 files of 25 MB each; they're deleted after a day. The local model gets text only, so attachments are skipped there with a note.

Claude Code only sees its workspace folder (`WORKSPACES`), not the bot's own code. Ask it to "send me the file" and it attaches the file. In edit or full mode, "…then delete it" also works: the bot deletes the file after the reply (and attachment) has been posted, and adds a grey 🗑️ line. It only deletes single files inside the workspace, and never deletes one if the reply failed to post. Running code or commands (real random numbers, scripts) needs full access in `/panel` → ⚙️ Settings, which asks you to confirm each job.

## Reminders

Just ask: "remind me to take my meds in 10 min", "remind me at 6pm to call mum", "tomorrow 9am: standup". Both engines can set them (Claude Code writes a hidden marker that the bot turns into a reminder; the local model has a `set_reminder` tool). Or use `/remind when:in 10 min what:take meds`, which is instant and costs nothing.

- Under the reply you get a grey line with the confirmed time, shown in your own timezone. That line comes from the bot, not the model.
- When it's due, the bot posts in the same channel and pings **only you** (it never pings roles or @everyone). It has ✅ Done and 💤 10 min buttons.
- `/tasks` (or ⏰ in `/panel`) lists and cancels reminders and recurring tasks. Only you see the list, and it shows only your own (owners see everyone's). For repeating reminders use `/schedule` or ask for one ("every day at 9pm").
- Reminders are kept in `data/reminders.json` and survive restarts. One that was due while the bot was off or the PC was asleep is sent as soon as the bot is back, marked late.

## Scheduled prompts

Ask for something that has to be looked up or done later, and Claude Code schedules itself: "8am today tell me the latest tweets from X, Y and Z", "every weekday at 9 give me a news brief", "at 6pm check if the match started". A grey line under the reply shows the task and when it runs.

- At that time a **fresh** session runs the prompt in **read-only** mode (web search/fetch, reading workspace files), posts the answer in the same channel and pings you. The usual per-job and daily budgets apply; if the daily cap is reached, the run is skipped and you're told.
- It has no memory of the chat, so Claude writes the prompt self-contained (names, handles, what to report).
- One-time tasks run once and disappear; repeating ones use cron (at most every 15 minutes). A scheduled run can never create more tasks.
- `/tasks` lists and cancels them (🤖 = Claude Code, 💻 = local model). `/schedule` creates a repeating one directly and uses the channel's engine.

### Which model runs a task, and what it may do
Each task keeps the engine and model it was created with. A task the local model scheduled runs on that same model. One Claude Code scheduled runs on that job's backend, model and workspace. `/schedule` uses the chat's current choice. Changing a chat's model later doesn't move existing tasks.

`/tasks` → **⚙️ Change what a task runs on / may do** opens one task:

| Control | Who | What it does |
|---|---|---|
| **Runs on** | the task's creator, or an owner | Picks the local model it runs on. Non-owners can only pick models the server lists. Owners can also pick any Claude Code backend and model, e.g. a news brief on `haiku` and the weather on local `qwen3.5:9b` |
| **May** | owners, Claude Code tasks only | 🔍 Read-only (the default), ✏️ Edit (can write files in its workspace) or ⚠️ Full access (any command, with nobody watching; asks you to confirm first). Full access isn't offered on the Ollama backend: the local model never gets a shell |
| **Reminders** | owners | Lets the task set reminders when it runs, e.g. "check the forecast at 7; if it'll rain, remind me at 8 to take an umbrella". Off by default |

The local model always gets only web search and page fetch, plus reminders if they're allowed. The owner who allowed Claude Code, more than read-only, or reminders is recorded with the task and checked again on every run. If that person is no longer in `OWNER_IDS`, and the creator isn't an owner either, the task falls back to the local model, read-only, and says so.
- Every message to Claude Code starts with the current date and time in `TIMEZONE`, so "today", "tonight" and "8am" mean your time, not UTC.

## PC power (`/power`)

Owners only, on Windows. `/power` offers five actions:
- **🔒 Lock:** immediate. Everything keeps running.
- **😴 Sleep**, **🛌 Hibernate**, **🔁 Restart** and **🔌 Shut down:** each asks you to confirm first.

- **Restart and shut down** wait 30 seconds, like `shutdown /t 30`. Open `/power` again and press ✖️ **Cancel** to stop it.
- **Back-up notice:** the bot posts in the chat where you asked, and pings only you, when it's up again:
  - after sleep or hibernate, as soon as the PC wakes and the network is back;
  - after a restart, about a minute after Windows starts, if the bot starts at boot (`bot_control.ps1 boot`, see [Start at boot](#start-at-boot-before-anyone-signs-in)). With only the Startup apps entry, it's once someone signs in.

  `/power` says which of these applies, and tells you how to fix it if the bot won't come back on its own.
- **Turning the PC on** isn't possible from chat, because nothing is running to receive the message. The same goes for waking it from sleep: someone has to press a key, move the mouse or press the power button (or open a laptop's lid).
- **If a requested action never happened**, for example a restart that was cancelled on the PC itself, the bot says so after a few minutes.
- **It's never a model tool:** neither the local model nor Claude Code can trigger it. Only the `/power` buttons can, after the owner check.

## Web dashboard

A page served by the bot itself (on by default, port 8765) that shows:
- **Now:** the running Claude Code job with its live tool steps, local-model replies and voice notes in progress, jobs waiting in line, a pending power action, today's spend against `CC_DAILY_BUDGET_USD`, the models Ollama has in memory (size, % on GPU, when they unload), the PC's CPU load, GPU load, memory, GPU memory and temperature, battery and disk, whether Discord and Telegram are connected, and what's coming up next.
- **Activity:** everything the bot did, newest first and grouped by day: local and Claude Code replies (with the reply, time taken, tools, cost), reminders set/fired/cancelled, scheduled prompts created and run, power actions, model unloads, sleep/resume, start/stop, and every warning or error from the log. Filter by kind, search, tap an entry for details, and load older entries. Kept in `data/events.jsonl` (or the `llmbot_events` table on Postgres), so it survives restarts.
- **Claude jobs:** every Claude Code job with outcome, model, cost, duration and turns.
- **Scheduled:** reminders and scheduled prompts with countdowns.
- **Log:** the end of `bot.log`, newest first, filterable by level.

It refreshes every 3 seconds while open and pauses when the tab is in the background.

**Opening it on your phone**
1. Send `/dashboard` to the bot (owners only; on Telegram in a private chat). It replies with a link like `http://llmbot.local:8765/?key=…`, plus the same link by IP address as a fallback. On the PC, `.\scripts\bot_control.ps1 dashboard` prints the same link.
2. The phone must be on the same Wi-Fi. The first time, allow it through Windows Firewall: open PowerShell **as administrator** and run `.\scripts\bot_control.ps1 firewall`. It opens the dashboard port and mDNS (UDP 5353, for the name) on Private networks only, so Windows must treat your home Wi-Fi as Private (Settings › Network & internet › Wi-Fi › your network; the script tells you if it's Public).
3. After the first visit a cookie remembers the key, so you can bookmark `http://llmbot.local:8765/` or add it to your home screen.

**The name `llmbot.local`:** the bot announces it on the network over mDNS (like printers and Chromecasts), pointing at the PC's current Wi-Fi address, and re-announces within a minute if that address changes. So the bookmark keeps working when the router hands the PC a new IP. Change it with `DASHBOARD_NAME` (empty turns it off). iPhones, Macs, Windows, Linux and Android 12+ resolve `.local` names; if yours doesn't, use the IP link (and a DHCP reservation in your router keeps that IP fixed).

**Security:** the dashboard shows your prompts and the bot's replies, so it needs the access key (`DASHBOARD_TOKEN`, or the random one in `data/dashboard.key`; delete that file and restart to change it). It only answers this PC, private LAN addresses and Tailscale (100.64.0.0/10); requests from public internet addresses are refused even with the key. It's plain HTTP, so someone on the same Wi-Fi who can watch the traffic could see what's on it: fine at home, not on a shared network. The status tabs only read; the **Chat** tab (below) acts as you. Set `DASHBOARD_HOST=127.0.0.1` to keep it to the PC, or `DASHBOARD_PORT=0` to turn it off.

### Chat from the dashboard

The **Chat** tab is a third way to talk to the bot, next to Discord and Telegram, on the same backend. It goes over your Wi-Fi only, so it also works **without internet**: the local model through Ollama, Claude Code on the Ollama backend, reminders and scheduled prompts all keep working. (Web search, Claude Code on Anthropic, Discord and Telegram need the internet, of course.)

- Each chat has its own engine, model, Claude Code session and memory. ⚙️ opens `/panel` right in the chat: its menus and buttons work like on Discord, and so do confirmations, Stop, reminders' ✅/💤 and `/tasks`. Other commands: `/help`, `/claude`, `/local`, `/new`, `/compact`, `/stop`, `/remind`, `/reset`, `/unload`, `/power`.
- Type `/` to see the commands with what they do; tap one (or use the arrow keys and Tab/Enter). Commands without arguments run right away.
- 🎙️ **Voice input:** tap to record, tap again to stop. The recording is turned into text on the PC by the same local Whisper as voice notes (nothing goes to the internet), and lands in the message box so you can fix it before sending. Phones only allow the microphone on **https** pages, so for voice open the https link from `/dashboard` (`https://llmbot.local:8766`, `DASHBOARD_HTTPS_PORT`). Its certificate is made by the PC itself, so each device warns once ("not private"): choose Advanced → Continue (Android) or Show Details → visit this website (iPhone). On the plain http page, 🎙️ opens your phone's own voice recorder instead and uploads what you record.
- 📎 attaches images, PDFs and code for Claude Code. Files the bot sends back appear in the chat (images inline, others as downloads) and are kept 30 days in `data/webchat_files`.
- While Claude Code works, the chat shows its steps live. Chats and their last 300 messages are kept (in `data/webchat.json`, or Postgres), up to 50 chats. Buttons stop working when the bot restarts, as on Discord.
- **Who you are there:** `DASHBOARD_CHAT=owner` (default) makes whoever has the access key an owner: Claude Code (if you've set `OWNER_IDS`, which turns Claude Code on at all) and `/power`. `user` gives only the local model; `off` hides the chat.
- **Starting without internet:** with the chat on, the bot no longer waits for discord.com at startup or exits when it can't reach it. The dashboard, chat and local models start at once, and Discord and Telegram keep retrying in the background until the internet is back.

## Model memory
- A model loads only when the first message or scheduled run needs it. Starting the bot and opening `/panel` don't load one.
- After `LLM_IDLE_UNLOAD` seconds idle (default 600), the bot unloads the models it used, so your GPU is free for other things. `/unload` does it immediately, and `/panel` shows 🧠 loaded or 💤 not in memory.
- The plain local engine asks Ollama to keep the model 2 minutes longer than `LLM_IDLE_UNLOAD`, so it isn't reloaded from disk mid-conversation (a reload also throws away the cached prompt).
- For Claude Code on Ollama, Ollama's own timer (`OLLAMA_KEEP_ALIVE`, default 5m) applies, and whichever is shorter wins. To let the bot's setting decide, raise Ollama's timer above it. It then also works as a backstop if the bot is killed:
  ```powershell
  setx OLLAMA_KEEP_ALIVE 30m
  ```
  Restart Ollama afterwards.
- Outside the bot, `ollama ps` lists loaded models and `ollama stop <model>` unloads one right away.

## Ollama tuning (optional)
These are Ollama settings, not bot settings, so they go in Windows environment variables, not `.env`. Ollama is a separate program and reads them only when it starts.
```powershell
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_KV_CACHE_TYPE q8_0
```
Then quit Ollama from the system tray and start it again. Its log (`%LOCALAPPDATA%\Ollama\server.log`) should show `OLLAMA_FLASH_ATTENTION:true` and `OLLAMA_KV_CACHE_TYPE:q8_0`.

- **What they do:** the KV cache is the model's working memory for the prompt, and it grows with the context window. Flash attention computes attention with less memory, and usually faster. `q8_0` stores the KV cache at half size, and it only works with flash attention on. Together they leave more room on the GPU, so less of the model runs on the slower CPU.
- **Measured** with qwen3.5:9b, 32k context, on an 8 GB laptop GPU: 7.24 → 6.82 GB in total, CPU share 1.7 → 1.3 GB, reading a 3.9k-token prompt 3.0 → 2.7s. Tool use was unchanged. Models whose KV cache is a bigger share of their memory gain more.
- **Downsides:** both apply to every model Ollama runs. `q8_0` has a small quality cost, most likely to show in long chats; `q4_0` saves more but can hurt answers. On a few GPU and model combinations flash attention has caused garbled output. To undo either one, delete the variable and restart Ollama.

## Notes
- **Workspaces:** Claude Code can only run inside folders listed in `WORKSPACES`. Don't whitelist this bot's own folder, because its `.env` holds your Discord token. The bot removes its own secrets from Claude Code's environment, but it can't hide files on disk.
- **Permission profiles:**
  - `read` and `edit` deny anything outside their allow-list without prompting. Blocked actions appear on the result card.
  - `full` uses `bypassPermissions` and always asks for confirmation before running.
- **Cron:** schedules use 5 fields in `TIMEZONE`, with standard numbering (0 = Sunday). For example, `0 9 * * 1-5` runs at 09:00 on weekdays. Tasks must be at least 15 minutes apart, with a maximum of 20.
- **Where the bot keeps things:** by default, files in `data/` (not in git). That covers:
  - `settings.json` (per-channel settings), `tasks.json`, `reminders.json`, `usage.json` (daily spend);
  - `history.json` (the local model's chat memory, so it survives restarts; `/reset` clears it);
  - `jobs.jsonl` (one line per Claude Code job: model, cost, turns, context, outcome);
  - `mcp_web.json` and `bot.log`.

  **Logs** roll over at `LOG_FILE_MB` (50) into dated, gzipped archives such as `bot-20260930-151200123.log.gz`. The oldest archives are deleted so that all log files together never use more than `LOG_MAX_TOTAL_MB`, 2 GB by default. That total includes `jobs.jsonl` when you're not on Postgres. `LOG_KEEP_DAYS` adds an age limit, `LOG_COMPRESS=false` keeps archives as plain text, and `LOG_LEVEL=DEBUG` gives more detail. The live `bot.log` is never deleted. To read an archive, use `gzip -dc` or 7-Zip.

  They survive restarts and updates. Copy the folder along if you move the bot to another machine. `LLMBOT_DATA_DIR` puts it somewhere else.
- **Postgres instead of files (optional):** set `DATABASE_URL=postgresql://user:password@localhost:5432/llmbot` in `.env`. Create the database first (`createdb llmbot`, or in pgAdmin). On the next start:
  - the bot creates two tables: `llmbot_state`, with one jsonb row per kind (settings, tasks, reminders, usage, history, the pending power action), and `llmbot_jobs`, with one row per Claude Code job;
  - any `data/` files it finds are imported once and renamed to `*.imported`.

  `mcp_web.json` and `bot.log` stay files. If the database isn't reachable within a minute of starting, the bot logs why and exits rather than silently falling back to files. `DATABASE_URL` is never passed to Claude Code. Example query: `select date(finished_at), sum(cost_usd) from llmbot_jobs group by 1 order by 1;`
- **Private channels:** add the bot to the channel (Edit Channel → Permissions) with View Channel, Send Messages, Embed Links, Attach Files and Read Message History. Otherwise slash commands still arrive, but the bot can't post replies or cards. `/panel` warns you when this is the case.
- **Running twice:** a second copy exits on its own (it holds localhost port 47823), so starting it manually while the startup copy is running won't cause duplicate replies.

## Security
- **Redaction:** everything the bot posts to Discord or Telegram passes through a redaction filter first. That covers replies, progress and result cards, attachments, the Full log and error messages. It removes:
  - known secret values from the environment (Discord and Telegram tokens, API keys and so on)
  - common token formats: Discord, Anthropic/OpenAI, GitHub, AWS, Google, Stripe, Slack, Hugging Face, JWTs, private keys, bearer tokens, credentials in URLs and `?api_key=` parameters
  - `KEY=value` lines for secret-looking names, as in `.env` files
  - your home folder path, which shows as `~`
- **Claude Code's environment:** env vars with secret-looking names are removed from the process. `ANTHROPIC_*` and `CLAUDE_*` are kept, plus anything listed in `CC_ENV_PASSTHROUGH`.
- **`read` / `edit` profiles:** file access is limited to the workspace, and reads or writes elsewhere (absolute paths, `../`) are denied. There's no shell at all, because `cat`, `git diff --no-index` and `git log --output` can reach any file. Use `full` when you need commands.
- **Mentions:** bot output can never ping `@everyone`, roles or users.
- **Keep in mind:** progress and result cards are visible to everyone in the channel, including file contents Claude Code shows. Use a private channel for private code. Redaction is best-effort: it catches known values and common formats, not every possible secret.

## Cost
- **Lean jobs:** Claude Code jobs don't load your MCP servers, skills or plugins, and `read`/`edit` only load the tools they use. In tests this cut a small job from $0.038 to $0.006. Set `CC_EXTRAS=true` to load them again.
- **Budgets:** `CC_MAX_BUDGET_USD` caps a single job, and `CC_DAILY_BUDGET_USD` caps the day. `/panel` shows today's spend.
- **Cheaper settings for chat:** use `haiku` in `/panel`, or set `CC_EFFORT=low`.
- **Long sessions:** with `CC_CONTINUE=true`, each message re-sends the session history. It's cached for 1 hour, so after `CC_SESSION_IDLE_MINUTES` (default 60) of inactivity the bot starts a fresh session instead of re-sending an uncached history. A session also resets when the workspace folder changes. Press 🆕 **New CC session** when you switch topics.
- **Compacting:** each result card shows the conversation's **Context** size. Past `CC_COMPACT_HINT_TOKENS` (default 50k), the card suggests compacting and highlights 🗜️ **Compact**, and `/panel` shows the warning too. Compact replaces the conversation with a summary while keeping the session; you can also send `/compact` as a message. About 11k tokens of fixed instructions and tools can't be compacted, so it's only worth it on long conversations.
- **Per-message cost:** cards show what that message cost, plus the session total on follow-ups. The CLI only reports running totals, so the bot works out the difference.
- **Where the money went:** 📄 **Full log** ends with a per-model cost breakdown, including the Haiku call behind WebSearch, and `data/bot.log` records each job's cost.
- **Free options:** the local model and scheduled tasks cost nothing. With Ollama running, ✨ **Auto** answers small talk locally and only hands computer tasks to Claude Code.