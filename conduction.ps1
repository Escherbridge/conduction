#Requires -Version 5.1
<#
.SYNOPSIS
    Launch Conduction
.DESCRIPTION
    Starts the Conduction server if not already running and opens the browser.
    Defaults: host 127.0.0.1, port 8000, 30 s startup timeout, browser opened.
    Every default can be overridden by a parameter or the matching CONDUCTION_* env var.
.PARAMETER Port
    TCP port to serve on. Default: $env:CONDUCTION_PORT, else 8000.
.PARAMETER BindHost
    Interface to bind. Default: $env:CONDUCTION_HOST, else 127.0.0.1 (loopback only).
.PARAMETER TimeoutSeconds
    How long to wait for the server to answer /api/ping. Default: $env:CONDUCTION_START_TIMEOUT, else 30.
.PARAMETER NoBrowser
    Do not open the browser after starting the server. Also honours CONDUCTION_NO_BROWSER=1.
.PARAMETER Stop
    Stop a server previously started by this script, then exit.
#>
param(
    [int]$Port = 0,
    [string]$BindHost = "",
    [int]$TimeoutSeconds = 0,
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

function Get-Default([string]$explicit, [string]$envName, [string]$fallback) {
    if ($explicit) { return $explicit }
    $fromEnv = [Environment]::GetEnvironmentVariable($envName)
    if ($fromEnv) { return $fromEnv }
    return $fallback
}

$portValue = Get-Default ($(if ($Port -gt 0) { "$Port" } else { "" })) "CONDUCTION_PORT" "8000"
$hostValue = Get-Default $BindHost "CONDUCTION_HOST" "127.0.0.1"
$maxWait = [int](Get-Default ($(if ($TimeoutSeconds -gt 0) { "$TimeoutSeconds" } else { "" })) "CONDUCTION_START_TIMEOUT" "30")
if ($env:CONDUCTION_NO_BROWSER -eq "1") { $NoBrowser = $true }

# Bind host 0.0.0.0 is not a reachable address; probe and browse over loopback.
$probeHost = if ($hostValue -eq "0.0.0.0" -or $hostValue -eq "::") { "127.0.0.1" } else { $hostValue }
$baseUrl = "http://${probeHost}:$portValue"
$pingUrl = "$baseUrl/api/ping"

$agentgraphDir = Join-Path $PSScriptRoot ".agentgraph"
$logPath = Join-Path $agentgraphDir "app.log"
$pidPath = Join-Path $agentgraphDir "app.pid"

function Test-ServerUp {
    try {
        $response = Invoke-WebRequest -Uri $pingUrl -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Host "=== Conduction Launcher ===" -ForegroundColor Cyan
Write-Host ""

if ($Stop) {
    if (-not (Test-Path $pidPath)) {
        Write-Host "No PID file at $pidPath - nothing to stop." -ForegroundColor Yellow
        exit 0
    }
    $recordedPid = (Get-Content -Path $pidPath -Raw).Trim()
    $process = Get-Process -Id $recordedPid -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $recordedPid -Force
        Write-Host "Stopped Conduction (PID $recordedPid)." -ForegroundColor Green
    } else {
        Write-Host "PID $recordedPid is not running; clearing stale PID file." -ForegroundColor Yellow
    }
    Remove-Item -Path $pidPath -Force
    exit 0
}

# Check if server is already running
Write-Host "Checking if server is already running..." -ForegroundColor Gray
if (Test-ServerUp) {
    Write-Host "  Server is already running at $baseUrl" -ForegroundColor Green
} else {
    Write-Host "  Server not running, will start it..." -ForegroundColor Gray

    if (-not (Test-Path $agentgraphDir)) {
        New-Item -ItemType Directory -Path $agentgraphDir | Out-Null
    }

    # Clear a stale PID file so -Stop never targets a recycled PID.
    if (Test-Path $pidPath) {
        $stalePid = (Get-Content -Path $pidPath -Raw).Trim()
        if (-not (Get-Process -Id $stalePid -ErrorAction SilentlyContinue)) {
            Remove-Item -Path $pidPath -Force
        }
    }

    $pythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
    $appScript = Join-Path $PSScriptRoot "app.py"

    if (-not (Test-Path $pythonExe)) {
        Write-Host "Error: Virtual environment not found at $pythonExe" -ForegroundColor Red
        Write-Host "Please run install.ps1 first." -ForegroundColor Yellow
        exit 1
    }

    # Keep one previous run's log for post-mortem; the redirect below truncates.
    if (Test-Path $logPath) {
        Move-Item -Path $logPath -Destination "$logPath.prev" -Force
    }

    Write-Host "Starting Conduction server..." -ForegroundColor Yellow
    Write-Host "  Host: $hostValue" -ForegroundColor Gray
    Write-Host "  Port: $portValue" -ForegroundColor Gray
    Write-Host "  Logs: $logPath" -ForegroundColor Gray

    # Start-Process has no -Environment parameter on PS 5.1; the child inherits
    # this process's environment, so export the settings here instead.
    $env:CONDUCTION_PORT = $portValue
    $env:CONDUCTION_HOST = $hostValue

    $process = Start-Process -FilePath $pythonExe `
        -ArgumentList @($appScript) `
        -WorkingDirectory $PSScriptRoot `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError "$logPath.err" `
        -PassThru `
        -WindowStyle Hidden

    $process.Id | Set-Content -Path $pidPath -NoNewline
    Write-Host "  Process ID: $($process.Id)" -ForegroundColor Gray

    Write-Host "  Waiting up to $maxWait s for server to be ready..." -ForegroundColor Gray
    $deadline = (Get-Date).AddSeconds($maxWait)
    $ready = $false

    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            Write-Host ""
            Write-Host "Error: Server process exited immediately (exit code $($process.ExitCode))" -ForegroundColor Red
            Write-Host "Check logs at: $logPath and $logPath.err" -ForegroundColor Yellow
            exit 1
        }
        Start-Sleep -Milliseconds 500
        if (Test-ServerUp) { $ready = $true; break }
    }

    if (-not $ready) {
        Write-Host ""
        Write-Host "Error: Server did not start within $maxWait seconds" -ForegroundColor Red
        Write-Host "Check logs at: $logPath and $logPath.err" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Server is ready!" -ForegroundColor Green
}

if (-not $NoBrowser) {
    Write-Host ""
    Write-Host "Opening browser at $baseUrl ..." -ForegroundColor Yellow
    Start-Process $baseUrl
}

Write-Host ""
Write-Host "=== Conduction is running ===" -ForegroundColor Green
Write-Host ""
Write-Host "Access the interface at: $baseUrl" -ForegroundColor Cyan
Write-Host "Stop it with: .\conduction.ps1 -Stop" -ForegroundColor Gray
Write-Host ""
