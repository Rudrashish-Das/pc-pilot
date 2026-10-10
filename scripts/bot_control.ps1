<#
  pc-pilot control.
    .\scripts\bot_control.ps1 install   Add "pc-pilot" to Startup apps (toggle it later in Task Manager > Startup apps)
    .\scripts\bot_control.ps1 remove    Remove it from Startup apps
    .\scripts\bot_control.ps1 boot      Also start it when Windows boots, before anyone signs in (run as administrator, once)
    .\scripts\bot_control.ps1 unboot    Undo boot (run as administrator)
    .\scripts\bot_control.ps1 start     Start the bot in the background (no console window)
    .\scripts\bot_control.ps1 stop      Stop the running bot
    .\scripts\bot_control.ps1 restart   Stop, then start (after pulling updates)
    .\scripts\bot_control.ps1 restart-idle  Restart once no Claude Code job is running (restarts the bot schedules itself)
    .\scripts\bot_control.ps1 status    Show whether it's running / in startup
    .\scripts\bot_control.ps1 log       Tail data\bot.log
    .\scripts\bot_control.ps1 dashboard Print the web dashboard's link (with its access key) and open it here
    .\scripts\bot_control.ps1 firewall  Let phones on your Wi-Fi reach the dashboard (run as administrator, once)
#>
param([Parameter(Position = 0)][ValidateSet("install", "remove", "boot", "unboot", "start", "stop", "restart", "restart-idle", "status", "log", "dashboard", "firewall")][string]$Action = "status")

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
        return
    } catch {}
    # A copy the boot task started runs in the background session, where a normal window can't stop it;
    # ending the task can.
    $task = Get-ScheduledTask -TaskName $BootTask -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") {
        try { Stop-ScheduledTask -TaskName $BootTask -ErrorAction Stop } catch {}
        Start-Sleep -Seconds 2
        if (-not (Get-BotProcess)) { "Stopped (it was started at boot)."; return }
    }
    Write-Error "Couldn't stop it (PID $($p.ProcessId -join ', ')): it was started as administrator. Run this once in an administrator PowerShell."
    exit 1
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
        # From a background process (no desktop, e.g. a restart the bot scheduled for itself) Explorer silently
        # does nothing: check, and start it directly if it didn't come up
        foreach ($i in 1..10) { if (Get-BotProcess) { break }; Start-Sleep -Seconds 1 }
        if (-not (Get-BotProcess)) { Start-Process -FilePath $Launcher -WorkingDirectory $Dir }
    } else {
        Start-Process -FilePath $Pythonw -ArgumentList "-m", "llmbot" -WorkingDirectory $Dir
    }
    foreach ($i in 1..10) { if (Get-BotProcess) { break }; Start-Sleep -Seconds 1 }
    if (-not (Get-BotProcess)) { Write-Error "It didn't start. See $LogFile"; exit 1 }
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
        # With your password stored (Windows keeps it encrypted, for this task only). Not S4U ("don't store the
        # password"): an S4U logon has no password, so Windows' per-user encryption (DPAPI) can't open your master
        # key; it makes a new one it can't reopen later and switches your account to it. Chrome then can't decrypt its
        # cookies and signs you out of everything, at every restart (seen on Windows 11 with a Microsoft account).
        # Signed in with a Microsoft account: its password is what Windows checks (a PIN never works here), and some
        # PCs only accept it with the account's own name
        $msa = whoami /groups /fo csv | ConvertFrom-Csv | Where-Object { $_.'Group Name' -like "MicrosoftAccount\*" } |
            Select-Object -First 1 -ExpandProperty 'Group Name'
        "Starting at boot needs your Windows password. A PIN or Windows Hello doesn't work here."
        if ($msa) { "You sign in with a Microsoft account: use the password of $($msa.Split('\')[1]) (the one for account.microsoft.com)." }
        "Windows stores it encrypted for this task. If you change the password, run 'boot' again."
        $cred = Get-Credential -UserName $user -Message "Your Windows password (not your PIN), so the bot can start at boot"
        if (-not $cred) { "Cancelled."; return }
        # ($taskAction, not $action: PowerShell names ignore case, and $Action is this script's validated parameter)
        try {
            $taskAction = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m llmbot --boot" -WorkingDirectory $Dir
            # "At startup" only fires on a full boot. With Fast Startup (on by default) shutting down is a half-hibernate
            # and turning the PC on resumes that kernel, so the task never ran and the bot stayed off until someone
            # signed in. Kernel-Boot event 27 is written on every boot, Fast Startup ones included (boot type 0x1); on
            # a full boot both triggers fire and MultipleInstances IgnoreNew keeps it to one bot.
            $boot27 = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskEventTrigger `
                -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
            $boot27.Enabled = $true
            $boot27.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
                "*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and EventID=27]]</Select></Query></QueryList>"
            $trigger = @((New-ScheduledTaskTrigger -AtStartup), $boot27)
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 4  # default 7 = below normal
            $err = $null
            foreach ($name in @($user) + @($msa | Where-Object { $_ })) {
                try {
                    Register-ScheduledTask -TaskName $BootTask -Action $taskAction -Trigger $trigger -Settings $settings `
                        -User $name -Password $cred.GetNetworkCredential().Password -RunLevel Limited `
                        -Description "Starts pc-pilot at boot, before anyone signs in. Made by scripts\bot_control.ps1 boot." `
                        -Force -ErrorAction Stop | Out-Null
                    $err = $null
                    break
                } catch { $err = $_ }
            }
            if ($err) { throw $err }
        } catch {
            Write-Error ("Couldn't register the boot task: $($_.Exception.Message) Use your account password, not your PIN. " +
                "No password on your account (passwordless sign-in)? Then skip 'boot': the Startup apps entry starts the bot when you sign in.")
            exit 1
        }
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
    "restart-idle" {
        # The bot's Claude Code runs are `claude -p --output-format stream-json`. A restart scheduled from inside one
        # waits for it (and any job started after it) to finish and its reply to be posted, so no answer is cut off.
        $deadline = (Get-Date).AddMinutes(30)
        # Only the bot's own (its descendants): the Claude desktop app's sessions are stream-json runs too
        $jobs = {
            $all = @{}; Get-CimInstance Win32_Process | ForEach-Object { $all[[int]$_.ProcessId] = $_ }
            $bot = @(Get-BotProcess | ForEach-Object { [int]$_.ProcessId })
            @($all.Values | Where-Object { $_.CommandLine -like "*stream-json*" -and $_.Name -notlike "powershell*" } | Where-Object {
                $p = $_; $hops = 0
                while ($p -and $hops -lt 6) { if ($bot -contains [int]$p.ParentProcessId) { return $true }; $p = $all[[int]$p.ParentProcessId]; $hops++ }
                $false
            })
        }
        do {
            while ((& $jobs).Count -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 5 }
            Start-Sleep -Seconds 15  # time to post the reply; a message sent meanwhile starts a new job
        } while ((& $jobs).Count -and (Get-Date) -lt $deadline)
        "Idle at $(Get-Date -Format HH:mm:ss); restarting"
        Stop-Bot; Start-Sleep -Seconds 2; Start-Bot
    }
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
        # 0x8007052E wrong password, 0x8007052F account restriction, 0x80070532 password expired, 0x80070569 logon
        # type not granted (llmbot/core.py BOOT_LOGON_FAILURES)
        $last = if ($task) { (Get-ScheduledTaskInfo -TaskName $BootTask -ErrorAction SilentlyContinue).LastTaskResult }
        if ($null -ne $last -and [uint32]$last -in [uint32[]](2147943726, 2147943727, 2147943730, 2147943785)) {
            Write-Warning ("Windows refused the boot task's password at the last boot (changed or expired?), so the bot " +
                "didn't start before sign-in. Run 'boot' again as administrator with your current password.")
        }
        if ($task -and $task.Principal.LogonType -eq "S4U") {
            Write-Warning ("The boot task runs without your password (S4U), which breaks Windows' encryption for your " +
                "account and signs Chrome out at every restart. Run 'boot' again as administrator to fix it, or 'unboot'.")
        }
        if ($task -and -not ($task.Triggers | Where-Object { $_.Subscription -like "*Kernel-Boot*" })) {
            Write-Warning ("The boot task only starts the bot after a full boot, not after a Fast Startup one (turning " +
                "the PC on after 'Shut down'). Run 'boot' again as administrator to add that.")
        }
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
