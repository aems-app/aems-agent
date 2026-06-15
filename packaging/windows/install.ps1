# AEMS Agent — Windows install / upgrade script
#
# Mirrors the kill-then-replace flow the NSIS installer uses
# (packaging/windows/installer.nsi). Intended for two audiences:
#
#   1. End users who downloaded the raw PyInstaller bundle (e.g. the
#      `aems-agent-windows-portable.zip` from a GitHub release) instead
#      of `aems-agent-setup.exe`. They extract the zip, then double-click
#      `install.ps1` (right-click -> "Run with PowerShell") to drop the
#      files into `%LOCALAPPDATA%\AEMS Agent` and start the tray.
#   2. Developers / admins iterating on the agent locally. After
#      `python packaging/build.py` they run this script to refresh the
#      installed copy without hand-killing the tray and copying files.
#
# Doing the swap by hand reliably trips `_preflight_port_or_die`'s
# "Another AEMS Agent is already running" dialog because the old tray
# stays bound to 127.0.0.1:61234 the entire time. This script removes
# that surprise: stop the old process, wait for the port to free, copy
# files atomically, then launch the new tray once.
#
# Usage:
#   .\install.ps1                  # install/upgrade and start the tray
#   .\install.ps1 -NoStart         # install/upgrade only, do not start
#   .\install.ps1 -Source <path>   # use <path>\aems-agent\ as source
#                                  # (defaults to ".\aems-agent" next to
#                                  #  the script, then ".\dist\aems-agent")

[CmdletBinding()]
param(
    [string]$Source,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDir = Join-Path $env:LOCALAPPDATA 'AEMS Agent'

function Resolve-Source {
    param([string]$Hint)
    $candidates = @()
    if ($Hint) {
        if (Test-Path (Join-Path $Hint 'aems-agent.exe')) { $candidates += $Hint }
        if (Test-Path (Join-Path $Hint 'aems-agent\aems-agent.exe')) {
            $candidates += (Join-Path $Hint 'aems-agent')
        }
    }
    $candidates += (Join-Path $ScriptDir 'aems-agent')
    $candidates += $ScriptDir
    $candidates += (Join-Path $ScriptDir '..\dist\aems-agent')
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c 'aems-agent.exe')) {
            return (Resolve-Path $c).Path
        }
    }
    throw "Could not locate aems-agent.exe. Pass -Source <path-to-bundle>."
}

function Stop-RunningAgent {
    $running = Get-Process -Name aems-agent -ErrorAction SilentlyContinue
    if (-not $running) { return }
    Write-Host "Stopping running AEMS Agent (PID $($running.Id -join ', '))..."
    $running | Stop-Process -Force -ErrorAction SilentlyContinue
    # Wait up to 10s for the listener to release :61234 so file copy and the
    # next launch's preflight both succeed. TCPListener is the
    # authoritative check; Test-NetConnection on a free port emits
    # WARNINGs that clutter the script output.
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new(
                [System.Net.IPAddress]::Loopback, 61234)
            $listener.Start()
            $listener.Stop()
            return
        } catch {
            # still bound; try again
        } finally {
            if ($listener) { try { $listener.Stop() } catch {} }
        }
    }
    Write-Warning "Port 61234 still held after stopping aems-agent.exe; continuing anyway."
}

function Sync-InstallDir {
    param([string]$From)
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir | Out-Null
    }
    # Wipe the previous _internal/ wholesale so PyInstaller version drift
    # (different DLL set between releases) can't leave orphaned files that
    # crash the new binary at import time.
    $oldInternal = Join-Path $InstallDir '_internal'
    if (Test-Path $oldInternal) { Remove-Item -Recurse -Force $oldInternal }
    Remove-Item -Force (Join-Path $InstallDir 'aems-agent.exe') -ErrorAction SilentlyContinue

    Write-Host "Copying $From -> $InstallDir"
    Copy-Item -Recurse (Join-Path $From '_internal') (Join-Path $InstallDir '_internal')
    Copy-Item (Join-Path $From 'aems-agent.exe') (Join-Path $InstallDir 'aems-agent.exe')
}

function Register-Autostart {
    # Mirror what packaging/windows/installer.nsi writes to
    # HKCU\Software\Microsoft\Windows\CurrentVersion\Run so users who install
    # from the portable bundle get the same auto-relaunch-at-sign-in behaviour
    # as users who run aems-agent-setup.exe. Without this, the web UI's
    # "Does not auto-start" warning would still be true for portable-bundle
    # users even though it is false for everyone who went through the .exe.
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    $exe = Join-Path $InstallDir 'aems-agent.exe'
    $value = '"' + $exe + '" run --tray'
    try {
        if (-not (Test-Path $runKey)) {
            New-Item -Path $runKey -Force | Out-Null
        }
        New-ItemProperty -Path $runKey -Name 'AEMS Agent' -PropertyType String -Value $value -Force | Out-Null
        Write-Host "Autostart registered: $runKey\AEMS Agent"
    } catch {
        Write-Warning "Could not register autostart ($_); the tray will still run now but won't relaunch after sign-out."
    }
}

function Start-Tray {
    $exe = Join-Path $InstallDir 'aems-agent.exe'
    Write-Host "Starting $exe run --tray"
    Start-Process -FilePath $exe -ArgumentList 'run','--tray' `
        -WorkingDirectory $InstallDir | Out-Null
}

$src = Resolve-Source -Hint $Source
Write-Host "Source:   $src"
Write-Host "Install:  $InstallDir"

Stop-RunningAgent
Sync-InstallDir -From $src
$installedExe = Join-Path $InstallDir 'aems-agent.exe'
$version = (& $installedExe --version) -replace '^aems-agent\s+',''
Write-Host "Installed aems-agent $version"
Register-Autostart

if (-not $NoStart) {
    Start-Tray
}
