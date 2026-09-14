#Requires -Version 5.1
<#
.SYNOPSIS
    Launch Conduction
.DESCRIPTION
    Starts the Conduction server if not already running and opens the browser.
.PARAMETER NoBrowser
    Do not open the browser after starting the server
#>
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Determine port from environment or default
$port = $env:CONDUCTION_PORT
if (-not $port) {
    $port = 8000
}

$baseUrl = "http://127.0.0.1:$port"
$pingUrl = "$baseUrl/api/ping"

Write-Host "=== Conduction Launcher ===" -ForegroundColor Cyan
Write-Host ""

# Check if server is already running
$alreadyRunning = $false
try {
    Write-Host "Checking if server is already running..." -ForegroundColor Gray
    $response = Invoke-WebRequest -Uri $pingUrl -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
    if ($response.StatusCode -eq 200) {
        $alreadyRunning = $true
        Write-Host "  Server is already running at $baseUrl" -ForegroundColor Green
    }
} catch {
    # Server not running, which is fine
    Write-Host "  Server not running, will start it..." -ForegroundColor Gray
}

if (-not $alreadyRunning) {
    # Ensure .agentgraph directory exists
    $agentgraphDir = Join-Path $PSScriptRoot ".agentgraph"
    if (-not (Test-Path $agentgraphDir)) {
        New-Item -ItemType Directory -Path $agentgraphDir | Out-Null
    }

    # Start the server
    $pythonExe = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
    $appScript = Join-Path $PSScriptRoot "app.py"
    $logPath = Join-Path $agentgraphDir "app.log"
    $pidPath = Join-Path $agentgraphDir "app.pid"

    if (-not (Test-Path $pythonExe)) {
        Write-Host "Error: Virtual environment not found. Please run install.ps1 first." -ForegroundColor Red
        exit 1
    }

    Write-Host "Starting Conduction server..." -ForegroundColor Yellow
    Write-Host "  Port: $port" -ForegroundColor Gray
    Write-Host "  Logs: $logPath" -ForegroundColor Gray

    # Start the server process
    $startParams = @{
        FilePath = $pythonExe
        ArgumentList = @($appScript)
        WorkingDirectory = $PSScriptRoot
        RedirectStandardOutput = $logPath
        RedirectStandardError = $logPath
        PassThru = $true
        WindowStyle = "Hidden"
    }

    if ($env:CONDUCTION_PORT) {
        $startParams.Environment = @{CONDUCTION_PORT = $env:CONDUCTION_PORT}
    }

    $process = Start-Process @startParams

    # Record PID
    $process.Id | Set-Content -Path $pidPath -NoNewline
    Write-Host "  Process ID: $($process.Id)" -ForegroundColor Gray

    # Wait for server to be ready (max 30 seconds)
    Write-Host "  Waiting for server to be ready..." -ForegroundColor Gray
    $maxWait = 30
    $waited = 0
    $ready = $false

    while ($waited -lt $maxWait) {
        Start-Sleep -Seconds 1
        $waited++

        try {
            $response = Invoke-WebRequest -Uri $pingUrl -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # Not ready yet, continue waiting
        }
    }

    if (-not $ready) {
        Write-Host ""
        Write-Host "Error: Server did not start within $maxWait seconds" -ForegroundColor Red
        Write-Host "Check logs at: $logPath" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Server is ready!" -ForegroundColor Green
}

# Open browser unless -NoBrowser
if (-not $NoBrowser) {
    Write-Host ""
    Write-Host "Opening browser at $baseUrl ..." -ForegroundColor Yellow
    Start-Process $baseUrl
}

Write-Host ""
Write-Host "=== Conduction is running ===" -ForegroundColor Green
Write-Host ""
Write-Host "Access the interface at: $baseUrl" -ForegroundColor Cyan
Write-Host ""
