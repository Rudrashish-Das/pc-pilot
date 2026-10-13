# pc-pilot for beginners

This guide sets up pc-pilot step by step, for people who have never used a terminal or written code. It takes about 30 minutes. You copy and paste every command, so you don't need to understand them.

When you're done, you'll have a chat bot in **Telegram** that answers questions, sets reminders, searches the web and, if you allow it, does things on your PC for you.

> The main [README](../README.md) has the same steps in short form, plus Discord. This guide uses Telegram because it's quicker to set up.

---

## Before you start

You need:
- A **Windows 10 or 11** PC that stays on while you want to use the bot.
- **Telegram** on your phone or PC.
- About **10 GB of free disk space** if you want a free AI model on your own PC.

Pick the "brain" the bot will use. You can change it later.

- **A. Claude (best).** Needs a paid Claude Pro or Max plan from [claude.ai](https://claude.ai). Best at doing real work on your PC.
- **B. Free, on your PC.** A free AI model runs on your own computer. Works best with a gaming graphics card (NVIDIA with 8 GB or more). Slower and less clever than Claude.
- **C. Free, in the cloud.** A free AI model runs on someone else's servers, so your graphics card doesn't matter. Has daily usage limits.

If you're not sure, start with **C** and switch later.

---

## Step 1: Open PowerShell

PowerShell is the window where you'll paste commands.

1. Press the **Windows key**, type `PowerShell`, and click **Windows PowerShell**.
2. A blue or black window opens. This is where you paste commands.

**To paste a command:** copy it from this guide, then **right-click** inside the PowerShell window and press **Enter**. Wait until the blinking cursor comes back before you paste the next one.

---

## Step 2: Install the tools

Paste these two commands one at a time. Each one downloads and installs a program. Click **Yes** if Windows asks for permission.

```powershell
winget install -e --id Python.Python.3.12
```

```powershell
winget install -e --id Git.Git
```

Now **close PowerShell and open it again** (Step 1). New programs only show up in a fresh window.

<details>
<summary>"winget is not recognized"?</summary>

Open the **Microsoft Store**, search for **App Installer**, install or update it, then try again.
</details>

---

## Step 3: Download pc-pilot

Paste these lines one at a time:

```powershell
cd $HOME\Documents
git clone https://github.com/Rudrashish-Das/pc-pilot.git
cd pc-pilot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

The `pip install` line takes a few minutes and prints a lot of text. That's normal.

You now have a folder **Documents\pc-pilot**. The file **.env** in it holds all your settings.

**To edit your settings** at any time, paste:

```powershell
notepad $HOME\Documents\pc-pilot\.env
```

In Notepad, each setting is a line like `NAME=value`. Change only the part after `=`, then press **Ctrl+S** to save.

---

## Step 4: Create your Telegram bot

1. In Telegram, search for **@BotFather** (it has a blue tick) and open it.
2. Send `/newbot`.
3. It asks for a **name**. Type anything, for example `My PC`.
4. It asks for a **username**. It must end in `bot`, for example `myname_pc_bot`.
5. BotFather replies with a **token**: a long code like `1234567890:AAH...`. Copy it.
6. Open your settings (`notepad` command from Step 3) and find these lines. Fill them in:

```ini
TELEGRAM_BOT_TOKEN=paste-your-token-here
TIMEZONE=Asia/Kolkata
```

Use your own timezone, for example `Europe/London` or `America/New_York`. Save with **Ctrl+S**.

> **Keep the token secret.** Anyone who has it can control your bot.

---

## Step 5: Set up the brain

Do only the option you picked.

### Option A: Claude (paid plan)

1. Paste:
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
2. Close PowerShell, open it again, and paste:
   ```powershell
   claude
   ```
3. A browser opens. Sign in with your Claude account. Back in PowerShell, answer any questions with **Enter**.
4. Type `/exit` and press Enter.
5. In your settings, set:
   ```ini
   DEFAULT_ENGINE=claude
   ```

### Option B: Free, on your PC

1. Paste these one at a time:
   ```powershell
   winget install -e --id Ollama.Ollama
   ```
   ```powershell
   ollama pull qwen3.5:9b
   ```
   The second one downloads about 6 GB. If your graphics card has less than 8 GB, use `qwen3.5:4b` instead, and change `LLM_MODEL` in your settings to match.
2. Leave `DEFAULT_ENGINE=local` in your settings. That's the default.

### Option C: Free, in the cloud

1. Install Ollama and Claude Code (Claude Code is free, it just needs a brain to talk to):
   ```powershell
   winget install -e --id Ollama.Ollama
   ```
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
2. Close PowerShell, open it again, and paste:
   ```powershell
   ollama signin
   ```
   A browser opens. Create a free Ollama account and sign in.
3. Download the cloud model (it's tiny, the model itself stays on Ollama's servers):
   ```powershell
   ollama pull gpt-oss:120b-cloud
   ```
4. In your settings, set:
   ```ini
   DEFAULT_ENGINE=claude
   CC_BACKEND=ollama
   CC_MODEL=gpt-oss:120b-cloud
   ```

You **don't** log in to Claude for this option. The README lists [other free cloud options](../README.md#claude-code-on-free-cloud-models-no-paid-plan-no-gpu), such as OpenRouter.

---

## Step 6: Start the bot and link your Telegram account

1. Paste:
   ```powershell
   cd $HOME\Documents\pc-pilot
   .\.venv\Scripts\python.exe -m llmbot
   ```
2. Wait for the line `Telegram: logged in as @yourbot`. **Leave this window open.**
3. In Telegram, open your new bot (search for its username) and send `/start`.
4. It replies with **your Telegram user id**, a number like `123456789`.
5. In your settings, set:
   ```ini
   TELEGRAM_ALLOWED_USER_IDS=123456789
   ```
   This makes the bot answer **only you**, and makes you its owner.
6. Go back to PowerShell, press **Ctrl+C** to stop the bot, and start it again:
   ```powershell
   .\.venv\Scripts\python.exe -m llmbot
   ```
7. Send your bot `hi`. It should answer. 🎉

---

## Step 7: Keep it running in the background

So far the bot stops when you close PowerShell. To run it in the background and start it every time you sign in to Windows:

1. Press **Ctrl+C** in PowerShell to stop the bot.
2. Paste:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 install
   powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 start
   ```
3. You can close PowerShell now. The bot keeps running.

**Optional: start even before anyone signs in** (useful if you restart the PC from your phone):

1. Press the **Windows key**, type `PowerShell`, **right-click** it and choose **Run as administrator**.
2. Paste:
   ```powershell
   cd $HOME\Documents\pc-pilot
   powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 boot
   ```
3. Type your **Windows password** when asked. If you sign in with a Microsoft account, that's the account password, **not your PIN**. Windows keeps it encrypted, and you need to run this again if you change your password.

---

## Using the bot

- Just **send a message**, like you would to a person.
- `/help` lists everything the bot can do.
- `/panel` opens the settings: which brain to use, how much it may do on your PC, voice replies and more.
- **Reminders:** "remind me in 20 minutes to call mum".
- **Doing things on your PC** (Claude brain only): in `/panel`, raise the access level. **Full access** lets the bot run any program and change any file, so turn it on only if you trust what you ask it to do.

---

## Updating

Every now and then, get the latest version. Open PowerShell and paste:

```powershell
cd $HOME\Documents\pc-pilot
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 restart
```

Your settings and reminders are kept.

---

## If something goes wrong

- **The bot doesn't answer at all.** Check that the PC is on and the bot is running:
  ```powershell
  cd $HOME\Documents\pc-pilot
  powershell -ExecutionPolicy Bypass -File .\scripts\bot_control.ps1 status
  ```
  If it isn't running, use `start` instead of `status`.
- **It answers someone else but not you,** or not at all after `/start`: your id in `TELEGRAM_ALLOWED_USER_IDS` is missing or wrong. Fix it and use `restart`.
- **"Claude Code isn't logged in" or "OAuth session expired".** Open PowerShell, run `claude`, type `/login`, sign in, then `/exit`.
- **The free model on your PC is very slow.** Your graphics card is probably too small for it. Try `qwen3.5:4b`, or switch to option C.
- **You changed a setting and nothing happened.** Settings in `.env` are only read at start. Use `restart`.
- **Still stuck?** The log is in `Documents\pc-pilot\data\bot.log`. The last lines usually say what went wrong. More fixes are in the README's [Troubleshooting](../README.md#troubleshooting) section.
