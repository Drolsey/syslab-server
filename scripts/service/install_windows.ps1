<#
    Phase 05: make syslab-server survive a reboot and a logout.

    Run from an ADMINISTRATOR PowerShell:
        powershell -ExecutionPolicy Bypass -File scripts\service\install_windows.ps1

    It registers two scheduled tasks that start at boot, and stops the machine
    sleeping. Safe to run more than once: existing tasks are replaced.

    Why scheduled tasks rather than NSSM or a real Windows service:
    a service runs as LocalSystem, which has a different user profile, so Ollama
    would look for your pulled models in the wrong place and your virtualenv
    would not be on its path. These tasks run as YOU, without needing your
    password stored, using a logon type called S4U. Same profile, same models,
    no login session required.
#>

[CmdletBinding()]
param(
    [switch]$SkipPowerSettings
)

$ErrorActionPreference = "Stop"

function Say([string]$text, [string]$colour = "Gray") { Write-Host "  $text" -ForegroundColor $colour }
function Head([string]$text) { Write-Host "`n$text" -ForegroundColor Cyan; Write-Host ("-" * 62) }

# ---------------------------------------------------------------- checks
Head "Checks"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say "This needs an Administrator PowerShell. Right-click PowerShell, Run as administrator." "Red"
    exit 1
}
Say "Running as Administrator" "Green"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Say "Project: $root"

$launcher = Join-Path $root "scripts\service\run_server.bat"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
foreach ($required in @($launcher, $venvPython)) {
    if (-not (Test-Path $required)) {
        Say "Missing: $required" "Red"
        Say "Create the virtualenv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" "Red"
        exit 1
    }
}
Say "Launcher and virtualenv found" "Green"

# The Microsoft Store build of Python is per-user and sandboxed. A venv made
# from it can fail in a non-interactive task, and the failure is obscure.
$base = & $venvPython -c "import sys; print(sys.base_prefix)" 2>$null
if ($base -like "*WindowsApps*" -or $base -like "*Microsoft\WindowsApps*") {
    Say "WARNING: this virtualenv is built on the Microsoft Store Python." "Yellow"
    Say "         It usually works under a scheduled task, but when it fails it fails" "Yellow"
    Say "         confusingly. If the gate check does not pass after a reboot, install" "Yellow"
    Say "         Python from python.org and rebuild .venv from it." "Yellow"
} else {
    Say "Virtualenv base: $base" "Green"
}

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $guess) { $ollama = $guess }
}
if (-not $ollama) {
    Say "Could not find ollama.exe. Install Ollama, or add it to PATH." "Red"
    exit 1
}
Say "Ollama: $ollama" "Green"

# ---------------------------------------------------------------- tasks
Head "Scheduled tasks"

$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

function Register([string]$name, $action, [string]$description) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Say "Replaced existing task $name"
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description $description | Out-Null
    Say "Registered $name" "Green"
}

Register "syslab-ollama" `
    (New-ScheduledTaskAction -Execute $ollama -Argument "serve") `
    "Runs the Ollama model server for syslab-server, at boot, without a login."

Register "syslab-server" `
    (New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $root) `
    "Runs the syslab-server web app at boot, without a login."

# ---------------------------------------------------------------- power
if (-not $SkipPowerSettings) {
    Head "Power"
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change monitor-timeout-ac 15
    Say "Sleep and hibernate disabled on mains power. The screen still turns off after 15 minutes." "Green"
    Say "A sleeping desktop cannot answer your laptop, which is the whole point of Phase 06."
}

# ---------------------------------------------------------------- start
Head "Starting both tasks now"
# Deliberately goes through restart_windows.ps1 rather than Start-ScheduledTask,
# so the orphaned-process handling lives in exactly one file.
& (Join-Path $PSScriptRoot "restart_windows.ps1") -IncludeOllama
Get-ScheduledTask -TaskName "syslab-*" |
    Select-Object TaskName, State |
    Format-Table -AutoSize | Out-String | Write-Host

Head "Next"
Say "1.  python scripts\check_services.py"
Say "2.  Reboot, log out rather than logging back in, then run the check again"
Say "    from any machine that can reach this one, or log in and re-run it here."
Say ""
Say "Logs:      $root\logs\server.log"
Say "Uninstall: powershell -ExecutionPolicy Bypass -File scripts\service\uninstall_windows.ps1"
Write-Host ""
