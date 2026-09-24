<#
.SYNOPSIS
    Install the garage API as a Windows startup task.

.DESCRIPTION
    Registers a Scheduled Task that starts the API at boot and restarts it if
    it stops. Scheduled Tasks rather than a real Windows service because a
    service needs a Service Control Manager handshake that a plain Python
    script does not perform - the usual answer to that is NSSM or WinSW, and
    a third-party wrapper is a poor trade for one background process. If you
    already run NSSM, `nssm install GarageApi <python> <path>\run.py` works
    and gives you `sc.exe query` for free.

    Run from an elevated PowerShell prompt.

.EXAMPLE
    Set-ExecutionPolicy -Scope Process Bypass
    .\install-windows.ps1 -EnvFile C:\garage-api\garage-api.env

.EXAMPLE
    .\install-windows.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$InstallDir = "C:\garage-api",
    [string]$EnvFile    = "",
    [string]$Python     = "",
    [string]$TaskName   = "GarageApi",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $admin     = [Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not $principal.IsInRole($admin)) {
        throw "Run this from an elevated PowerShell prompt (Run as administrator)."
    }
}

Assert-Admin

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask   -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    Write-Host "$InstallDir was left in place; delete it by hand if you are done."
    return
}

$source = Split-Path -Parent $PSScriptRoot

# pythonw.exe, not python.exe: it has no console window, so the task runs
# without a black box appearing at logon. Its stdout goes nowhere, which is
# why the task redirects it to a file below.
if (-not $Python) {
    $candidate = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $candidate) { $candidate = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $candidate) {
        throw "No Python found on PATH. Install it from python.org (tick 'Add python.exe to PATH') or pass -Python."
    }
    $Python = $candidate.Source
}
Write-Host "Python:      $Python"

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item (Join-Path $source "app.py") $InstallDir -Force
Copy-Item (Join-Path $source "run.py") $InstallDir -Force
Write-Host "Installed:   $InstallDir"

if (-not $EnvFile) { $EnvFile = Join-Path $InstallDir "garage-api.env" }
if (-not (Test-Path $EnvFile)) {
    $example = Join-Path $source "garage-api.env.example"
    Copy-Item $example $EnvFile -Force
    Write-Warning "Created $EnvFile from the example. Fill it in before starting; the service will refuse to boot without API_SCOPES."
}

# The env file holds GARAGE_TOKEN, which is the key to the door. Strip
# inherited permissions and grant only SYSTEM and Administrators.
$acl = Get-Acl $EnvFile
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
foreach ($who in @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $who, "FullControl", "Allow")))
}
Set-Acl $EnvFile $acl
Write-Host "Env file:    $EnvFile (SYSTEM and Administrators only)"

# Access mode needs PyJWT. Token mode does not, so a failure here is a
# warning rather than a stop.
if (Select-String -Path $EnvFile -Pattern '^\s*AUTH_MODE\s*=\s*access' -Quiet) {
    Write-Host "Installing PyJWT for AUTH_MODE=access..."
    $pip = $Python -replace 'pythonw\.exe$', 'python.exe'
    & $pip -m pip install --quiet "PyJWT[crypto]==2.10.1"
    if ($LASTEXITCODE -ne 0) { Write-Warning "pip failed. Install PyJWT by hand, or the service will exit on the first request." }
}

$logFile = Join-Path $InstallDir "garage-api.log"
$runner  = Join-Path $InstallDir "run-service.cmd"

# A .cmd wrapper so stdout lands in a file. Without it the JSON event log -
# every toggle, every rejected caller - goes to a handle nobody is holding.
@"
@echo off
cd /d "$InstallDir"
"$Python" "$InstallDir\run.py" --env "$EnvFile" >> "$logFile" 2>&1
"@ | Set-Content -Path $runner -Encoding ASCII

$action    = New-ScheduledTaskAction -Execute $runner
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" `
                                        -LogonType ServiceAccount `
                                        -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -RestartCount 999 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Register-ScheduledTask -TaskName $TaskName `
                       -Description "Garage door API" `
                       -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings | Out-Null

Write-Host ""
Write-Host "Registered scheduled task '$TaskName' (starts at boot, as SYSTEM)."
Write-Host ""
Write-Host "  Start now      Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Stop           Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "  Status         Get-ScheduledTask   -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Logs           Get-Content '$logFile' -Wait -Tail 20"
Write-Host "  Remove         .\install-windows.ps1 -Uninstall"
Write-Host ""
Write-Host "Check the config before starting:"
Write-Host "  & '$Python' '$InstallDir\run.py' --env '$EnvFile' --check"
