<#
    Restart the syslab web app so it picks up code and .env changes.

        powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1
        powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1 -IncludeOllama

    Why this is more than Stop-ScheduledTask + Start-ScheduledTask:

    The task runs run_server.bat, which launches python.exe as a child process.
    Stopping the task kills the batch file, but Windows does not always take the
    child with it. The orphaned python keeps holding port 8000, the new instance
    cannot bind, and it exits. Everything looks restarted and nothing has
    changed. So this stops the task, then makes sure whatever is actually
    listening on the port is gone, then starts it and proves a new process is
    listening.

    Ollama is left alone unless you pass -IncludeOllama, because restarting it
    drops the model out of VRAM and the next question waits for it to reload.
#>

param(
    [switch]$IncludeOllama,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

function Say([string]$text, [string]$colour = "Gray") { Write-Host "  $text" -ForegroundColor $colour }

# The port comes from .env unless one was passed in.
if ($Port -eq 0) {
    $Port = 8000
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        $line = Select-String -Path $envFile -Pattern '^\s*APP_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue |
                Select-Object -First 1
        if ($line) { $Port = [int]$line.Matches[0].Groups[1].Value }
    }
}

function Get-Listeners([int]$port) {
    try {
        @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        @()
    }
}

function Describe([int]$processId) {
    $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($p) { "$($p.ProcessName) (pid $processId)" } else { "pid $processId" }
}

Write-Host "`nRestarting syslab-server on port $Port" -ForegroundColor Cyan
Write-Host ("-" * 62)

$before = Get-Listeners $Port
if ($before.Count -gt 0) {
    Say ("Currently listening: " + (($before | ForEach-Object { Describe $_ }) -join ", "))
} else {
    Say "Nothing is listening on port $Port yet"
}

# ---- stop -----------------------------------------------------------------
$tasks = @("syslab-server")
if ($IncludeOllama) { $tasks += "syslab-ollama" }

foreach ($name in $tasks) {
    if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {
        Say "$name is not registered. Run install_windows.ps1 first." "Red"
        exit 1
    }
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Say "Stopped task $name"
}
Start-Sleep -Seconds 2

# ---- clear the port, whatever is holding it -------------------------------
$stubborn = Get-Listeners $Port
foreach ($processId in $stubborn) {
    $p = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    if ($p.ProcessName -notin @("python", "pythonw", "cmd", "conhost", "uvicorn")) {
        Say "Port $Port is held by $($p.ProcessName) (pid $processId), which is not ours. Leaving it alone." "Yellow"
        Say "Stop that program, or change APP_PORT in .env, then run this again." "Yellow"
        exit 1
    }
    Stop-Process -Id $processId -Force
    Say "Killed orphaned $(Describe $processId) that survived the task stop" "Yellow"
}

Start-Sleep -Seconds 2
if ((Get-Listeners $Port).Count -gt 0) {
    Say "Port $Port is still held after stopping everything. Reboot, or investigate with:" "Red"
    Say "  Get-NetTCPConnection -LocalPort $Port -State Listen | Select OwningProcess" "Red"
    exit 1
}
Say "Port $Port is clear" "Green"

# ---- start ----------------------------------------------------------------
foreach ($name in $tasks) {
    Start-ScheduledTask -TaskName $name
    Say "Started task $name"
}

$deadline = (Get-Date).AddSeconds(30)
$after = @()
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $after = Get-Listeners $Port
    if ($after.Count -gt 0) { break }
}

Write-Host ("-" * 62)
if ($after.Count -eq 0) {
    Say "Nothing came up on port $Port within 30 seconds." "Red"
    Say "Read the log: $root\logs\server.log" "Red"
    exit 1
}

$isNew = ($after | Where-Object { $before -notcontains $_ }).Count -gt 0
Say ("Now listening: " + (($after | ForEach-Object { Describe $_ }) -join ", ")) "Green"
if ($isNew -or $before.Count -eq 0) {
    Say "This is a new process, so your changes are live." "Green"
} else {
    Say "Same process as before. Something is wrong; read logs\server.log." "Red"
    exit 1
}

Write-Host "`n  Now run:  .\run.cmd scripts\check_remote.py`n"
