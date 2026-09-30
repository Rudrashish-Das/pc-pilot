<#
  pc-pilot control.
    .\scripts\bot_control.ps1 install   Add "pc-pilot" to Startup apps (toggle it later in Task Manager > Startup apps)
    .\scripts\bot_control.ps1 remove    Remove it from Startup apps
    .\scripts\bot_control.ps1 boot      Also start it when Windows boots, before anyone signs in (run as administrator, once)
    .\scripts\bot_control.ps1 unboot    Undo boot (run as administrator)
    .\scripts\bot_control.ps1 start     Start the bot in the background (no console window)
    .\scripts\bot_control.ps1 stop      Stop the running bot
    .\scripts\bot_control.ps1 restart   Stop, then start (after pulling updates)
    .\scripts\bot_control.ps1 status    Show whether it's running / in startup
    .\scripts\bot_control.ps1 log       Tail data\bot.log
    .\scripts\bot_control.ps1 dashboard Print the web dashboard's link (with its access key) and open it here
    .\scripts\bot_control.ps1 firewall  Let phones on your Wi-Fi reach the dashboard (run as administrator, once)
#>
param([Parameter(Position = 0)][ValidateSet("install", "remove", "boot", "unboot", "start", "stop", "restart", "status", "log", "dashboard", "firewall")][string]$Action = "status")

$Dir      = Split-Path $PSScriptRoot -Parent  # the repo root
$Pythonw  = Join-Path $Dir ".venv\Scripts\pythonw.exe"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "pc-pilot.lnk"
$LogFile  = Join-Path $Dir "data\bot.log"
$Launcher = Join-Path $Dir "bin\pc-pilot.exe"  # built from scripts\launcher.cs so Startup apps shows our name and logo
$BootTask = "pc-pilot (boot)"  # llmbot/core.py BOOT_TASK looks for this name

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Build-Launcher {
    # csc.exe ships with Windows (.NET Framework 4), so there's nothing to install
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe" }
    if (-not (Test-Path $csc)) { return $false }
    New-Item -ItemType Directory -Force (Split-Path $Launcher) | Out-Null
    $out = & $csc /nologo /target:winexe /optimize "/out:$Launcher" "/win32icon:$(Join-Path $Dir 'assets\logo.ico')" `
        /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot "launcher.cs")
    if ($LASTEXITCODE -ne 0) { $out | Write-Warning; return $false }
    return $true
}

function Get-BotProcess {
    # "-m llmbot" but not the "--mcp-web" helper that Claude Code starts
    $p = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "-m\s+llmbot" -and $_.CommandLine -notmatch "--mcp-web" }
    if ($p) { return $p }
    # A copy started as administrator hides its command line from a normal window; it still holds the bot's port
    $owner = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 47823 -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    if ($owner) { Get-CimInstance Win32_Process -Filter "ProcessId = $owner" }
}

function Stop-Bot {
    $p = Get-BotProcess
    if (-not $p) { "Not running."; return }
    try {
        $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
        "Stopped."
    } catch {
        Write-Error "Couldn't stop it (PID $($p.ProcessId -join ', ')): it was started as administrator. Run this once in an administrator PowerShell."
        exit 1
    }
}

function Get-EnvValue($name) {
    $envFile = Join-Path $Dir ".env"
    if (-not (Test-Path $envFile)) { return "" }
    $line = Get-Content $envFile | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -Last 1
    if ($line) { return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'") }
    return ""
}

function Get-DashboardPort {
    $p = Get-EnvValue "DASHBOARD_PORT"
    if ($p -match "^\d+$") { return [int]$p }
    return 8765
}

function Get-DashboardHttpsPort {
    $p = Get-EnvValue "DASHBOARD_HTTPS_PORT"
    if ($p -match "^\d+$") { return [int]$p }
    return 8766
}

function Start-Bot {
    if (Get-BotProcess) { "Already running."; return }
    if (Test-Path $Launcher) {
        # Through Explorer, like Startup apps: the bot gets normal rights even from an administrator window (so Claude
        # Code jobs never run as admin), and it doesn't belong to this terminal (closing it can't take the bot along)
        Start-Process explorer.exe -ArgumentList "`"$Launcher`""
    } else {
        Start-Process -FilePath $Pythonw -ArgumentList "-m", "llmbot" -WorkingDirectory $Dir
    }
    "Started. Logs: $LogFile"
}

if (-not (Test-Path $Pythonw)) { Write-Error "venv not found at $Pythonw. Create it first (see docs\SETUP.md)."; exit 1 }

switch ($Action) {
    "install" {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($Shortcut)
        if (Build-Launcher) {
            # Task Manager names a startup entry after the program it runs, so this shows as "pc-pilot"
            $lnk.TargetPath = $Launcher
            $lnk.Arguments = ""
            $lnk.IconLocation = "$Launcher,0"
            $name = "pc-pilot"
        } else {
            $lnk.TargetPath = $Pythonw
            $lnk.Arguments = "-m llmbot"
            $name = "Python"
            Write-Warning "Couldn't build the launcher (no csc.exe), so Task Manager will list the bot as 'Python'."
        }
        $lnk.WorkingDirectory = $Dir
        $lnk.Description = "pc-pilot"
        $lnk.Save()
        "Added to Startup apps: $Shortcut"
        "Enable/disable it in Task Manager > Startup apps (listed as '$name')."
    }
    "remove" {
        if (Test-Path $Shortcut) { Remove-Item $Shortcut; "Removed from Startup apps." } else { "Not in Startup apps." }
    }
    "boot" {
        if (-not (Test-Admin)) { Write-Error "Run PowerShell as administrator for this one."; exit 1 }
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        # S4U: runs as you without storing your password, whether or not you're signed in. It can't use network
        # shares or saved Windows credentials, which the bot doesn't need.
        $action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m llmbot --boot" -WorkingDirectory $Dir
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 4  # default 7 = below normal
        Register-ScheduledTask -TaskName $BootTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
            -Description "Starts pc-pilot at boot, before anyone signs in. Made by scripts\bot_control.ps1 boot." `
            -Force | Out-Null
        "The bot now starts when Windows boots, as $user, with no sign-in needed. It also starts Ollama if it isn't running."
        "Keep the Startup apps entry too: if the bot is already running at sign-in, it does nothing."
        "Undo with: .\scripts\bot_control.ps1 unboot (as administrator)"
    }
    "unboot" {
        if (-not (Get-ScheduledTask -TaskName $BootTask -ErrorAction SilentlyContinue)) { "Not set to start at boot."; return }
        if (-not (Test-Admin)) { Write-Error "Run PowerShell as administrator for this one."; exit 1 }
        Unregister-ScheduledTask -TaskName $BootTask -Confirm:$false
        "Removed: the bot no longer starts at boot (Startup apps still starts it when you sign in, if installed)."
    }
    "start" { Start-Bot }
    "stop" { Stop-Bot }
    "restart" { Stop-Bot; Start-Sleep -Seconds 2; Start-Bot }
    "status" {
        $p = Get-BotProcess
        "Running:         " + $(if ($p) { "yes (PID $($p.ProcessId -join ', '))" } else { "no" })
        $approved = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder" `
            -ErrorAction SilentlyContinue).(Split-Path $Shortcut -Leaf)
        $state = if ($approved -and ($approved[0] -band 1)) { "yes, but disabled in Task Manager" } else { "yes (enable/disable in Task Manager)" }
        "In Startup apps: " + $(if (Test-Path $Shortcut) { $state } else { "no" })
        $task = Get-ScheduledTask -TaskName $BootTask -ErrorAction SilentlyContinue
        "Starts at boot:  " + $(if (-not $task) { "no (run 'boot' as administrator so it comes back after a restart without sign-in)" }
                                elseif ($task.State -eq "Disabled") { "disabled in Task Scheduler" } else { "yes" })
    }
    "log" { Get-Content $LogFile -Tail 40 -Wait }
    "dashboard" {
        $port = Get-DashboardPort
        if ($port -eq 0) { "The dashboard is off (DASHBOARD_PORT=0 in .env)."; return }
        $key = Get-EnvValue "DASHBOARD_TOKEN"
        $keyFile = Join-Path $Dir "data\dashboard.key"
        if (-not $key -and (Test-Path $keyFile)) { $key = (Get-Content $keyFile -Raw).Trim() }
        if (-not $key) { "No access key yet: start the bot once, then run this again."; return }
        $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notmatch "^(127\.|169\.254\.)" -and $_.PrefixOrigin -ne "WellKnown" } |
            Select-Object -ExpandProperty IPAddress
        $name = Get-EnvValue "DASHBOARD_NAME"
        if (-not (Select-String -Path (Join-Path $Dir ".env") -Pattern "^\s*DASHBOARD_NAME\s*=" -Quiet -ErrorAction SilentlyContinue)) { $name = "llmbot" }
        "On this PC:     http://127.0.0.1:$port/?key=$key"
        if ($name) { "On your Wi-Fi:  http://$($name.ToLower()).local:$port/?key=$key   (the name the bot announces)" }
        foreach ($ip in $ips) { "By address:     http://${ip}:$port/?key=$key" }
        $https = Get-DashboardHttpsPort
        if ($https -and $name) { "With voice:     https://$($name.ToLower()).local:$https/?key=$key   (phones allow the mic only on https; accept the warning once)" }
        "The link holds the access key; don't share it. Phones can't connect? Run: .\scripts\bot_control.ps1 firewall (as administrator)"
        Start-Process "http://127.0.0.1:$port/?key=$key"
    }
    "firewall" {
        $port = Get-DashboardPort
        if (-not (Test-Admin)) { Write-Error "Run PowerShell as administrator for this one."; exit 1 }
        Get-NetFirewallRule -DisplayName "LLM bot dashboard*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        # Private networks only (home Wi-Fi). On networks Windows calls Public (cafes, hotels), it stays blocked.
        $ports = @($port, (Get-DashboardHttpsPort)) | Where-Object { $_ -gt 0 }
        New-NetFirewallRule -DisplayName "LLM bot dashboard" -Direction Inbound -Protocol TCP -LocalPort $ports `
            -Profile Private -Action Allow | Out-Null
        # mDNS: phones asking "where is llmbot.local?" (DASHBOARD_NAME)
        New-NetFirewallRule -DisplayName "LLM bot dashboard (name)" -Direction Inbound -Protocol UDP -LocalPort 5353 `
            -Profile Private -Action Allow | Out-Null
        "Allowed inbound TCP $($ports -join ', ') (http, https) and UDP 5353 (the llmbot.local name) on Private networks."
        $public = Get-NetConnectionProfile | Where-Object NetworkCategory -eq "Public"
        foreach ($n in $public) {
            "Note: '$($n.Name)' is set to Public, so this doesn't apply there. If it's your home network, make it Private:"
            "  Set-NetConnectionProfile -InterfaceAlias '$($n.InterfaceAlias)' -NetworkCategory Private"
        }
    }
}
