<#
    Removes what install_windows.ps1 created. Run as Administrator.
    Power settings are left alone: they are a machine preference, not ours to
    guess at. Restore them yourself with, for example:
        powercfg /change standby-timeout-ac 30
#>

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "  This needs an Administrator PowerShell." -ForegroundColor Red
    exit 1
}

foreach ($name in @("syslab-server", "syslab-ollama")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "  Removed $name" -ForegroundColor Green
    } else {
        Write-Host "  $name was not registered"
    }
}
Write-Host "`n  Power settings left as they are. Restore with e.g. powercfg /change standby-timeout-ac 30`n"
