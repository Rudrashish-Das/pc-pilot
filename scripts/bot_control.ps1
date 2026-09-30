<#
  Discord LLM Bot control.
    .\scripts\bot_control.ps1 install   Add "Discord LLM Bot" to Startup apps (toggle it later in Task Manager > Startup apps)
    .\scripts\bot_control.ps1 remove    Remove it from Startup apps
    .\scripts\bot_control.ps1 start     Start the bot in the background (no console window)
    .\scripts\bot_control.ps1 stop      Stop the running bot
    .\scripts\bot_control.ps1 restart   Stop, then start (after pulling updates)
    .\scripts\bot_control.ps1 status    Show whether it's running / in startup
    .\scripts\bot_control.ps1 log       Tail data\bot.log
#>
param([Parameter(Position = 0)][ValidateSet("install", "remove", "start", "stop", "restart", "status", "log")][string]$Action = "status")

$Dir      = Split-Path $PSScriptRoot -Parent  # the repo root
$Pythonw  = Join-Path $Dir ".venv\Scripts\pythonw.exe"
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "Discord LLM Bot.lnk"
$LogFile  = Join-Path $Dir "data\bot.log"

function Get-BotProcess {
    # "-m llmbot" but not the "--mcp-web" helper that Claude Code starts
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "-m\s+llmbot" -and $_.CommandLine -notmatch "--mcp-web" }
}

function Stop-Bot {
    $p = Get-BotProcess
    if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "Stopped." } else { "Not running." }
}

function Start-Bot {
    if (Get-BotProcess) { "Already running."; return }
    Start-Process -FilePath $Pythonw -ArgumentList "-m", "llmbot" -WorkingDirectory $Dir
    "Started. Logs: $LogFile"
}

if (-not (Test-Path $Pythonw)) { Write-Error "venv not found at $Pythonw. Create it first (see docs\SETUP.md)."; exit 1 }

switch ($Action) {
    "install" {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($Shortcut)
        $lnk.TargetPath = $Pythonw
        $lnk.Arguments = "-m llmbot"
        $lnk.WorkingDirectory = $Dir
        $lnk.Description = "Discord LLM Bot"
        $lnk.Save()
        "Added to Startup apps: $Shortcut"
        "Enable/disable it in Task Manager > Startup apps ('Discord LLM Bot')."
    }
    "remove" {
        if (Test-Path $Shortcut) { Remove-Item $Shortcut; "Removed from Startup apps." } else { "Not in Startup apps." }
    }
    "start" { Start-Bot }
    "stop" { Stop-Bot }
    "restart" { Stop-Bot; Start-Sleep -Seconds 2; Start-Bot }
    "status" {
        $p = Get-BotProcess
        "Running:         " + $(if ($p) { "yes (PID $($p.ProcessId -join ', '))" } else { "no" })
        "In Startup apps: " + $(if (Test-Path $Shortcut) { "yes (enable/disable in Task Manager)" } else { "no" })
    }
    "log" { Get-Content $LogFile -Tail 40 -Wait }
}
