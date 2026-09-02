<#
    Turn syslab-server on and off, for now or for good.

        powershell -ExecutionPolicy Bypass -File scripts\service\control_windows.ps1 -Action status
        powershell -ExecutionPolicy Bypass -File scripts\service\control_windows.ps1 -Action stop
        powershell -ExecutionPolicy Bypass -File scripts\service\control_windows.ps1 -Action start
        powershell -ExecutionPolicy Bypass -File scripts\service\control_windows.ps1 -Action disable
        powershell -ExecutionPolicy Bypass -File scripts\service\control_windows.ps1 -Action enable

    status    what is running, what starts at boot, and on which port
    stop      stop it now. It comes back at the next restart.
    start     start it now, without waiting for a restart.
    disable   stop it now AND stop it starting at boot. Everything stays
              installed; -Action enable puts it back in one command.
    enable    start at boot again, and start it now.

    To remove it altogether, use uninstall_windows.ps1 instead. Disabling is
    almost always what you want: nothing is deleted, and turning it back on
    does not mean redoing Phase 05.

    Only 'disable' and 'enable' need an Administrator PowerShell, because they
    change how the machine starts up. status, stop and start do not.
#>

param(
    [ValidateSet("status", "stop", "start", "disable", "enable")]
    [string]$Action = "status",
    [switch]$IncludeOllama
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Say([string]$text, [string]$colour = "Gray") { Write-Host "  $text" -ForegroundColor $colour }
function Head([string]$text) { Write-Host "`n$text" -ForegroundColor Cyan; Write-Host ("-" * 62) }

$tasks = @("syslab-server")
if ($IncludeOllama -or $Action -in @("status", "disable", "enable")) { $tasks += "syslab-ollama" }

function Need-Admin([string]$why) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Say "$why needs an Administrator PowerShell." "Red"
        Say "Right-click PowerShell, Run as administrator, then run this again." "Red"
        exit 1
    }
}

# The port comes from .env so status can tell you what is actually listening.
$port = 8000
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($line) { $port = [int]$line.Matches[0].Groups[1].Value }
}

function Listeners([int]$p) {
    try {
        @(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { @() }
}

function Show-Status {
    Head "syslab-server"
    foreach ($name in $tasks) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $task) {
            Say "$name  not installed" "Yellow"
            continue
        }
        $atBoot = if ($task.State -eq "Disabled") { "will NOT start at boot" } else { "starts at boot" }
        $colour = if ($task.State -eq "Disabled") { "Yellow" } else { "Green" }
        Say ("{0,-16} {1,-9} {2}" -f $name, $task.State, $atBoot) $colour
    }

    $pids = Listeners $port
    if ($pids.Count -gt 0) {
        $names = $pids | ForEach-Object {
            $p = Get-Process -Id $_ -ErrorAction SilentlyContinue
            if ($p) { "$($p.ProcessName) (pid $_)" } else { "pid $_" }
        }
        Say ("Port {0}:        answering, {1}" -f $port, ($names -join ", ")) "Green"
    } else {
        Say ("Port {0}:        nothing listening" -f $port) "Yellow"
    }

    Write-Host ""
    Say "stop     stop it now, back at the next restart"
    Say "disable  stop it now and stop it starting at boot"
    Say "enable   put it back"
}

switch ($Action) {

    "status" { Show-Status }

    "stop" {
        Head "Stopping"
        foreach ($name in $tasks) {
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                Say "Stopped $name"
            }
        }
        Start-Sleep -Seconds 2
        # The task can leave an orphaned python holding the port. Same problem
        # restart_windows.ps1 solves; solved the same way here.
        foreach ($processId in (Listeners $port)) {
            $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -in @("python", "pythonw", "cmd", "conhost")) {
                Stop-Process -Id $processId -Force
                Say "Killed leftover $($p.ProcessName) (pid $processId)" "Yellow"
            }
        }
        if ((Listeners $port).Count -eq 0) { Say "Port $port is clear" "Green" }
        Say ""
        Say "It will start again at your next restart. To prevent that: -Action disable" "Yellow"
    }

    "start" {
        Head "Starting"
        foreach ($name in $tasks) {
            if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
                Say "$name is not installed. Run install_windows.ps1 first." "Red"
                exit 1
            }
            Start-ScheduledTask -TaskName $name
            Say "Started $name"
        }
        Start-Sleep -Seconds 6
        if ((Listeners $port).Count -gt 0) {
            Say "Port $port is answering" "Green"
        } else {
            Say "Nothing on port $port yet. Check $root\logs\server.log" "Yellow"
        }
    }

    "disable" {
        Need-Admin "Disabling a scheduled task"
        Head "Disabling"
        foreach ($name in $tasks) {
            if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                Disable-ScheduledTask -TaskName $name | Out-Null
                Say "Disabled $name" "Yellow"
            }
        }
        Start-Sleep -Seconds 2
        foreach ($processId in (Listeners $port)) {
            $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -in @("python", "pythonw", "cmd", "conhost")) {
                Stop-Process -Id $processId -Force
                Say "Killed leftover $($p.ProcessName) (pid $processId)" "Yellow"
            }
        }
        Write-Host ""
        Say "Nothing is deleted. Your token, files and settings are untouched." "Green"
        Say "Your laptop can no longer reach it until you run -Action enable."
        Say ""
        Say "If you also want the machine to sleep again:"
        Say "  powercfg /change standby-timeout-ac 30"
    }

    "enable" {
        Need-Admin "Enabling a scheduled task"
        Head "Enabling"
        foreach ($name in $tasks) {
            if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
                Say "$name is not installed. Run install_windows.ps1 first." "Red"
                exit 1
            }
            Enable-ScheduledTask -TaskName $name | Out-Null
            Start-ScheduledTask -TaskName $name
            Say "Enabled and started $name" "Green"
        }
        Start-Sleep -Seconds 6
        if ((Listeners $port).Count -gt 0) {
            Say "Port $port is answering" "Green"
        } else {
            Say "Nothing on port $port yet. Check $root\logs\server.log" "Yellow"
        }
        Say ""
        Say "It will start on its own at every restart from now on."
    }
}

Write-Host ""
