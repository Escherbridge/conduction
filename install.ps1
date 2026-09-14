#Requires -Version 5.1
<#
.SYNOPSIS
    Install Conduction dependencies
.DESCRIPTION
    Creates a virtual environment and installs all required dependencies.
    Idempotent - safe to run multiple times.
.PARAMETER Shortcut
    Create desktop and start menu shortcuts after installation
.PARAMETER WhatIf
    Show what would be done without making changes
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Shortcut
)

$ErrorActionPreference = "Stop"

Write-Host "=== Conduction Installation ===" -ForegroundColor Cyan
Write-Host ""

# Check if venv exists
$venvPath = Join-Path $PSScriptRoot "venv"
$venvExists = Test-Path $venvPath

if (-not $venvExists) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow

    # Try uv first
    $uvAvailable = $false
    try {
        $null = Get-Command uv -ErrorAction Stop
        $uvAvailable = $true
    } catch {
        # uv not available
    }

    if ($uvAvailable) {
        Write-Host "  Using uv..." -ForegroundColor Gray
        if ($PSCmdlet.ShouldProcess("venv", "Create with uv venv")) {
            & uv venv $venvPath
            if ($LASTEXITCODE -ne 0) {
                throw "uv venv failed with exit code $LASTEXITCODE"
            }
        }
    } else {
        Write-Host "  Using python -m venv..." -ForegroundColor Gray
        if ($PSCmdlet.ShouldProcess("venv", "Create with python -m venv")) {
            & python -m venv $venvPath
            if ($LASTEXITCODE -ne 0) {
                throw "python -m venv failed with exit code $LASTEXITCODE"
            }
        }
    }
} else {
    Write-Host "Virtual environment already exists at: $venvPath" -ForegroundColor Green
}

# Determine pip command
$pipCmd = Join-Path $venvPath "Scripts\pip.exe"
if ($uvAvailable) {
    $pipCmd = "uv pip"
}

# Install requirements
Write-Host ""
Write-Host "Installing dependencies..." -ForegroundColor Yellow

$reqFiles = @("requirements.txt", "requirements-dev.txt")
foreach ($reqFile in $reqFiles) {
    $reqPath = Join-Path $PSScriptRoot $reqFile
    if (Test-Path $reqPath) {
        Write-Host "  Installing from $reqFile..." -ForegroundColor Gray
        if ($PSCmdlet.ShouldProcess($reqFile, "Install with $pipCmd")) {
            if ($uvAvailable) {
                & uv pip install -r $reqPath
            } else {
                & $pipCmd install -r $reqPath
            }
            if ($LASTEXITCODE -ne 0) {
                throw "Installation from $reqFile failed with exit code $LASTEXITCODE"
            }
        }
    } else {
        Write-Host "  Warning: $reqFile not found, skipping" -ForegroundColor DarkYellow
    }
}

# Verify installation
Write-Host ""
Write-Host "Verifying installation..." -ForegroundColor Yellow
$pythonExe = Join-Path $venvPath "Scripts\python.exe"

if ($PSCmdlet.ShouldProcess("app module", "Import verification")) {
    $verifyScript = "import app; print('OK')"
    $result = & $pythonExe -c $verifyScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Error: Could not import app module" -ForegroundColor Red
        Write-Host "  $result" -ForegroundColor Red
        throw "Verification failed"
    } else {
        Write-Host "  app module imports successfully" -ForegroundColor Green
    }
}

# Create shortcut if requested
if ($Shortcut -and -not $WhatIfPreference) {
    Write-Host ""
    $shortcutScript = Join-Path $PSScriptRoot "scripts\create-shortcut.ps1"
    if (Test-Path $shortcutScript) {
        Write-Host "Creating shortcuts..." -ForegroundColor Yellow
        & $shortcutScript
    } else {
        Write-Host "  Warning: scripts/create-shortcut.ps1 not found, skipping" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Launch Conduction:"
Write-Host "       Windows (PowerShell): .\conduction.ps1" -ForegroundColor White
Write-Host "       Windows (cmd):        conduction.cmd" -ForegroundColor White
Write-Host "       Linux/macOS:          ./conduction.sh" -ForegroundColor White
Write-Host ""
Write-Host "  2. (Optional) Create desktop shortcut:"
Write-Host "       .\scripts\create-shortcut.ps1" -ForegroundColor White
Write-Host ""
