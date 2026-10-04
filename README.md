<p align="center"><img src="assets/logo.png" alt="pc-pilot logo" width="160"></p>

# pc-pilot

Talk to AI models running on your own PC from Discord or Telegram, on any device, with no port forwarding.

One bot process, two front ends. Both share one backend: engines, Claude Code sessions, reminders, scheduled prompts, budgets and the GPU queue.

| Engine | What it is | Cost |
|---|---|---|
| 💻 **Local** | Any OpenAI-compatible server (Ollama, LM Studio, llama.cpp…) with tools: web search, page fetch, reminders, scheduled tasks | Free |
| 🤖 **Claude Code** | The `claude` CLI in headless mode, working in a sandboxed workspace folder. It runs on Anthropic's models or on your own Ollama model | Anthropic pricing, or free on Ollama |
| ✨ **Auto** | The local model answers. When a task needs a computer, it proposes a Claude Code job and the owner confirms | Mostly free |

## Contents
- [Features](#features)
- [Security model](#security-model)
- [Setup](#setup): step by step, about 20 minutes
- [Using the bot](#using-the-bot)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout) · [Development](#development)

## Features

- **Chat or cards:**
  - Chat replies read like a person's messages, with one small grey stats line.
  - Card replies show live progress and have Stop, Follow up, Retry, Full log and Compact buttons.
- **`/panel`** for per-channel settings: engine, model, Claude Code backend (Anthropic, Ollama or a custom one), permission profile, reply style, voice mode and workspace.
- **Persistent Claude Code sessions** per channel or chat, with context and cost shown, idle reset and compacting.
- **Files both ways.**
  - Send images, PDFs and code to Claude Code.
  - It can send files back as attachments, and delete workspace files when asked.
- **Voice notes**, transcribed locally with faster-whisper on the CPU.
- **Reminders** ("remind me at 6pm…") and **scheduled prompts**:
  - "every weekday at 9 give me a news brief".
  - Scheduled prompts run as fresh, read-only Claude Code jobs.
  - Both survive restarts.
- **Self-hosted web tools:** on non-Anthropic backends, the bot serves Claude Code its own `web_search` and `fetch_page` over MCP.
- **Cost controls:**
  - a budget per job and per day;
  - lean CLI flags (about 10k tokens per call instead of 34k);
  - a per-message cost breakdown.
- **Frees the GPU:** idle models are unloaded (`/unload` does it right away).
- **Storage:** JSON files in `data/` with no setup, or a Postgres database if you set `DATABASE_URL`. Either way it holds settings, tasks, reminders, spend, the local model's chat memory and a history of every Claude Code job with its cost.
- **Web dashboard** for your phone: what the bot is doing right now (live Claude Code progress, local replies, voice notes), a searchable history of everything it did, Claude Code jobs with their cost, today's spend against the cap, upcoming reminders and scheduled prompts, models in GPU memory, the PC's RAM/GPU/battery, and the log. Served by the bot itself on your local network, behind an access key. `/dashboard` gives owners the link. Its **Chat** tab talks to the bot over the same Wi-Fi, so the local model still answers when the internet is down.
- **PC power from chat** (`/power`, owners only): lock, sleep, hibernate, restart or shut down, with a confirmation step. The bot posts in the chat when it's back up.
- **Guests** (`GUEST_IDS`): someone who can only check on the PC. `/status` says whether a game is being played and for how long, and how long the PC has been on. `/ping <message>` pops up on the PC's screen, on top of everything, with a sound; the reply typed there goes back to them. Nothing else works for them.

## Security model

- **Allow-lists:** only listed Discord and Telegram user ids get answers, and only owners can use Claude Code. Every button and menu checks this again.
- **No shell for the local model**, ever. `fetch_page` has SSRF protection that blocks private, loopback and link-local addresses, including after redirects.
- **Claude Code's permission profiles:**
  - `read` and `edit` can only reach the workspace, with no shell.
  - `full` asks for confirmation for every job, unless an owner turns on ☠️ **Full access without asking** for that chat. That's opt-in, time-limited or until turned off, and never available for the local model.
- **Secrets stay out of output:**
  - Tokens are removed from Claude Code's environment.
  - Everything posted to chat is redacted first: known secret values, common token formats and your home path.
  - Bot output can't ping @everyone or roles.

Details: [docs/SETUP.md › Security](docs/SETUP.md#security).

---

## Setup

The steps are written for **Windows** (the bot is developed on Windows 11). Linux and macOS should work too: use `source .venv/bin/activate` and `python3`, and skip the startup script.

### What you need

| | Needed for | Get it |
|---|---|---|
| Python 3.11+ | always | [python.org](https://www.python.org/downloads/): tick **Add python.exe to PATH** |
| Git | cloning the repo | [git-scm.com](https://git-scm.com/) |
| A Discord **or** Telegram bot token (or both) | always | steps 2 and 3 below |
| [Ollama](https://ollama.com) + a model | the 💻 Local and ✨ Auto engines | step 5 |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) + an Anthropic account | the 🤖 Claude Code engine (it can also run on Ollama for free) | step 6 |

You need at least one engine. The easiest free start is Ollama. The most capable is Claude Code.

**Discord, Telegram, or both:** set up only the platform(s) you want:
- Discord only: do step 2 and skip step 3.
- Telegram only: skip step 2, do step 3, and leave `DISCORD_TOKEN` empty.

The bot starts whatever has a token, and every feature works on either platform.

### 1. Download and install

```powershell
git clone https://github.com/Rudrashish-Das/pc-pilot.git
cd pc-pilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

- If `Activate.ps1` is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then try again.
- From now on, all settings go in **`.env`**. Open it with `notepad .env`. Every setting is explained in the file, and it's git-ignored, so your tokens never get committed.

### 2. Create the Discord bot (skip if you only want Telegram)

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application** → give it a name.
2. **Bot** tab:
   - Click **Reset Token** and copy the token into `.env` as `DISCORD_TOKEN=...`.
   - Under **Privileged Gateway Intents**, turn on **Message Content Intent**.
3. **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`.
   - Bot permissions: View Channels, Send Messages, Read Message History, Embed Links, Attach Files.
   - Open the generated URL and add the bot to your server.
4. In Discord, turn on **User Settings → Advanced → Developer Mode**. Then right-click to copy ids into `.env`:
   - the server icon, which gives you **Copy Server ID** → `GUILD_ID=...` (commands then show up instantly);
   - your own name, which gives you **Copy User ID** → `OWNER_IDS=...` and `ALLOWED_USER_IDS=...`.

   ```ini
   DISCORD_TOKEN=your-token
   GUILD_ID=123456789012345678
   ALLOWED_USER_IDS=111111111111111111
   OWNER_IDS=111111111111111111
   ```
   Owners can use Claude Code. Users who are only allowed get just the local model. **Always set `ALLOWED_USER_IDS`:** if it's empty, everyone in the server can use the local model (DMs are then owners-only).

### 3. Create the Telegram bot (optional)

1. In Telegram, message [@BotFather](https://t.me/BotFather) and send `/newbot`. Pick a name and a username that ends in `bot`.
2. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=...`.
3. You fill in your Telegram id in step 7. Until then the bot answers nobody on Telegram.

Want only Telegram? Leave `DISCORD_TOKEN` empty.

### 4. Set your timezone

Reminders and schedules use this timezone:

```ini
TIMEZONE=Asia/Kolkata      # any IANA name: Europe/London, America/New_York, …
```

### 5. Local engine: Ollama (optional, free)

```powershell
winget install Ollama.Ollama
ollama pull qwen3.5:9b
setx OLLAMA_CONTEXT_LENGTH 32768
```

- Quit Ollama from the system tray and start it again, so it picks up the context length.
- The defaults in `.env` already point at it:
  ```ini
  LLM_URL=http://localhost:11434/v1
  LLM_MODEL=qwen3.5:9b
  ```
- Pick a model that fits your GPU. `qwen3.5:9b` needs about 8 GB of VRAM with a 32k context; on less, try a 4b model. `ollama ps` shows whether it runs **100% GPU**. A CPU/GPU split works, but it's slower.
- Optional, to fit more of the model on the GPU: [docs/SETUP.md](docs/SETUP.md#ollama-tuning-optional) (flash attention and a smaller KV cache). `ollama stop qwen3.5:9b` unloads the model by hand.
- Any other OpenAI-compatible server works as well: point `LLM_URL` at its `/v1` URL.

### 6. Claude Code engine (optional)

```powershell
irm https://claude.ai/install.ps1 | iex
claude
```

- Log in once in that interactive session, then quit. Check that `claude --version` works in a **new** terminal.
- If the bot can't find it, set `CLAUDE_BIN` to the full path, for example `C:\Users\<you>\.local\bin\claude.exe`.
- Claude Code only works inside the folders listed in `WORKSPACES`. The default is `claude_workspace` inside the repo.
- Useful settings:
  ```ini
  DEFAULT_ENGINE=local        # local | claude | auto
  CC_MODEL=sonnet             # sonnet | opus | haiku
  CC_PERMISSION=edit          # read | edit | full (full asks before every job)
  CC_MAX_BUDGET_USD=1.0       # per job
  CC_DAILY_BUDGET_USD=5.0     # per day
  ```

### Optional: Postgres instead of files

By default everything the bot keeps goes into JSON files in `data/`. To use a local Postgres database instead, create one called `llmbot`, then set:

```ini
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/llmbot
```

On the next start the bot creates its tables and imports any existing `data/` files. See [docs/SETUP.md](docs/SETUP.md#notes) for details.
- **No Anthropic account?** Claude Code can run on your Ollama model: in `/panel` → ⚙️ Settings, set the backend to **🦙 Ollama**. See [docs/SETUP.md](docs/SETUP.md#claude-code-on-your-own-model-no-anthropic-account).

### 7. First run

```powershell
python -m llmbot
```

You should see `Logged in as YourBot#1234`, and `Telegram: logged in as @yourbot` if you set that up. Logs also go to `data\bot.log`.

- **Discord:** type `/help` in your server, then `/panel`. Mention the bot (`@YourBot hi`) or use `/ask`.
- **Telegram:** open your bot and send `/start`. It replies with **your Telegram user id**. Put it in `.env`:
  ```ini
  TELEGRAM_ALLOWED_USER_IDS=123456789
  ```
  Stop the bot with `Ctrl+C` and start it again. From then on, just message it.

### 8. Run it in the background, at logon and at boot (Windows)

```powershell
.\scripts\bot_control.ps1 install   # adds "pc-pilot" to Startup apps
.\scripts\bot_control.ps1 start     # starts it now, with no window
```

Startup apps only run once someone signs in. So after `/power` → Restart, the PC would sit at the lock screen with the bot offline. To have it start at boot instead, run this once in PowerShell **as administrator**:

```powershell
.\scripts\bot_control.ps1 boot
```

This registers a scheduled task that starts the bot as you, before anyone signs in. It asks for your Windows password (for a Microsoft account, the account password, not your PIN), which Windows stores encrypted for that task; run `boot` again after changing your password. Without a stored password ("S4U" tasks), Windows' per-user encryption breaks: Chrome can't read its cookies and signs you out of every site after each restart. If Ollama isn't running then (its own app also waits for sign-in), the bot starts `ollama serve` itself. Keep the Startup apps entry as well: at sign-in it sees the bot already running and does nothing. `unboot` removes the task.

`install` builds a small launcher, `bin\pc-pilot.exe`, using the C# compiler that comes with Windows. Task Manager names each startup entry after the program it runs, so the launcher is what makes the entry show as **pc-pilot** with the logo. Without it, the entry would show as "Python". Run `install` again if you move the folder.

| Command | Does |
|---|---|
| `status` | Shows whether it's running, in Startup apps, and set to start at boot |
| `stop` / `start` / `restart` | Stops, starts or restarts the background bot. Use `restart` after changing `.env` or pulling updates |
| `log` | Follows `data\bot.log` |
| `remove` | Takes it out of Startup apps |
| `boot` / `unboot` | Starts it at boot, before sign-in, or stops doing that (as administrator) |

Only one copy can run at a time. A second one exits on its own, so stop the background copy before running `python -m llmbot` in a terminal.

### Updating

```powershell
git pull
pip install -r requirements.txt
.\scripts\bot_control.ps1 restart
```

Your settings, tasks, reminders and logs are in `data/` (or your Postgres database, see below) and `.env`. Git never touches either.

---

## Using the bot

| | Discord | Telegram |
|---|---|---|
| Ask something | `@YourBot …`, `/ask …`, reply to the bot, or DM it | Just message it (in groups, @mention it or reply to it) |
| Settings | `/panel` | `/panel` |
| Switch engine | `/panel` → engine, or `/claude …` / `/local …` for one message | `/claude …`, `/local …` |
| Stop / full log | `/stop`, `/log` | `/stop`, `/log` |
| New Claude Code session | `/panel` → 🆕 | `/new` |
| Several conversations at once | a channel or a thread: each thread starts with its channel's settings, own session | `/topic <name>`: each topic has its own session and settings |
| What Claude Code learned from earlier tasks | `/skills` | `/skills` |
| Context window, Claude plan limits (5-hour, weekly), spend | `/usage` | `/usage` |
| Run one of Claude Code's skills (docx, pdf, xlsx, deep-research, …) | `/skill` (name autocompletes) | `/skill <name> [request]` |
| Reminders | "remind me in 10 min to…" or `/remind` | same, or `/remind in 10 min \| take meds` |
| Scheduled prompts | "every weekday at 9 give me a news brief" or `/schedule` | same, or `/schedule 0 9 * * 1-5 \| prompt` |
| List and cancel them, or choose the model a task runs on and what it may do | `/tasks` | `/tasks` |
| Free the GPU | `/unload` | `/unload` |
| Lock / sleep / hibernate / restart / shut down the PC (owners) | `/power` | `/power` |
| Game being played, PC on-time (owners, `GUEST_IDS`) | `/status` | `/status` |
| Pop a message up on the PC's screen, get the reply (owners, `GUEST_IDS`) | `/ping` | `/ping <message>` |
| Web dashboard link (owners) | `/dashboard` | `/dashboard` (private chat) |

Voice notes, images and files work on both. See [docs/SETUP.md](docs/SETUP.md) for how each feature behaves and what it costs.

## Troubleshooting

| Problem | Fix |
|---|---|
| Slash commands don't show up | Set `GUILD_ID` for instant sync (`GUILD_ID=all` covers every server the bot is in; a server missing from a list gets none). Global commands can take up to an hour. Restart Discord (`Ctrl+R`). |
| The bot ignores @mentions | Turn on **Message Content Intent** (step 2), and check that your id is in `ALLOWED_USER_IDS`. |
| "Missing Access" in a private channel | Add the bot to the channel: Edit Channel → Permissions. `/panel` warns you about this. |
| Telegram answers nothing | Your id must be in `TELEGRAM_ALLOWED_USER_IDS` (send `/start` to get it), then restart. |
| The Claude Code engine is missing | You're not an owner: set `OWNER_IDS` (with no owners at all, Claude Code is off). Or `claude` isn't found: set `CLAUDE_BIN`. |
| The local model is slow | Run `ollama ps`. If it isn't 100% GPU, try the [Ollama tuning](docs/SETUP.md#ollama-tuning-optional) settings, or use a smaller model or context. |
| The dashboard doesn't load on the phone | Same Wi-Fi as the PC? Run `.\scripts\bot_control.ps1 firewall` once as administrator, and set the Wi-Fi to Private in Windows settings. If `llmbot.local` doesn't open but the IP link does, your phone doesn't support `.local` names (older Android): use the IP link. |
| "Another copy of the bot is already running" | Run `.\scripts\bot_control.ps1 stop` first. |
| Anything else | Check `data\bot.log`, or run `.\scripts\bot_control.ps1 log`. |

---

## Project layout

```
llmbot/
  core.py         backend: engines, Claude Code runner, tools, reminders, scheduler, settings + the Discord front end
  telegram.py     Telegram front end (Bot API over httpx): renders Discord-style output, maps buttons/menus
  dashboard.py    web dashboard (aiohttp, in the bot process); dashboard.html is the page
  webchat.py      the dashboard's Chat tab: a third front end (like telegram.py), works without internet
  store.py        storage: JSON files in data/ or Postgres
  skills.py       skills Claude Code saves after tasks that took work and gets back on similar ones
  __main__.py     python -m llmbot   (python -m llmbot --mcp-web = the web-tools MCP server Claude Code starts)
tests/
  test_suites.py  pytest entry point: runs each suite in its own process
  suites/         the suites (live_* and the live parts of others run only with LLMBOT_LIVE=1)
  fixtures/       sample Claude Code stream-json output, a short voice note
scripts/
  bot_control.ps1 install / start / stop / restart / status / log / dashboard / firewall
  launcher.cs     the Startup-apps launcher that install compiles to bin\pc-pilot.exe
assets/           logo.png, logo.ico
docs/SETUP.md     detailed guide: every feature, costs, security
.env.example      every setting, documented
data/             runtime state, created on first run (not in git)
```

## Development

```powershell
pip install -r requirements.txt pytest
pytest                            # offline: no tokens, network or cost
$env:LLMBOT_LIVE = "1"; pytest    # also real Claude Code (haiku, a few cents), Ollama and Whisper
```

- The suites load the bot with no `.env` and a temporary `data/` folder, so they never touch your real settings, tasks or reminders.
- The Postgres suite runs only if you point `LLMBOT_TEST_DATABASE_URL` at a throwaway database. It drops and recreates its tables there.
- CI runs the offline suites on Windows on every push.
- Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md). In short, this summary isn't the license, and the license text is what counts:

- **Allowed for free:**
  - personal use, study, hobby projects, and running it for yourself, your family and friends;
  - changing it;
  - sharing it, as long as you pass on the license and the `Required Notice` line;
  - use by charities, schools, public research and government bodies.
- **Not allowed:** any commercial use, such as using it in or for a business, selling it, or offering it as a paid service.
- **Commercial licence:** contact the author.

This isn't an OSI "open source" license, because those can't restrict commercial use. The libraries this project uses keep their own licenses.
