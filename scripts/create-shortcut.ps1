#Requires -Version 5.1
<#
.SYNOPSIS
    Create or remove Conduction desktop shortcuts
.DESCRIPTION
    Creates shortcuts on the Desktop and in the Start Menu Programs folder
    that launch Conduction via PowerShell.
.PARAMETER Remove
    Remove existing shortcuts instead of creating them
#>
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

# Determine paths
$scriptRoot = Split-Path -Parent $PSScriptRoot
$conductionScript = Join-Path $scriptRoot "conduction.ps1"
$shortcutName = "Conduction.lnk"

# Desktop and Start Menu paths
$desktopPath = [Environment]::GetFolderPath("Desktop")
$startMenuPath = [Environment]::GetFolderPath("Programs")

$desktopShortcut = Join-Path $desktopPath $shortcutName
$startMenuShortcut = Join-Path $startMenuPath $shortcutName

if ($Remove) {
    Write-Host "=== Removing Conduction Shortcuts ===" -ForegroundColor Cyan
    Write-Host ""

    $removed = 0
    if (Test-Path $desktopShortcut) {
        Remove-Item $desktopShortcut -Force
        Write-Host "  Removed: $desktopShortcut" -ForegroundColor Yellow
        $removed++
    }

    if (Test-Path $startMenuShortcut) {
        Remove-Item $startMenuShortcut -Force
        Write-Host "  Removed: $startMenuShortcut" -ForegroundColor Yellow
        $removed++
    }

    if ($removed -eq 0) {
        Write-Host "  No shortcuts found" -ForegroundColor Gray
    } else {
        Write-Host ""
        Write-Host "Removed $removed shortcut(s)" -ForegroundColor Green
    }
} else {
    Write-Host "=== Creating Conduction Shortcuts ===" -ForegroundColor Cyan
    Write-Host ""

    # Create WScript.Shell COM object
    $shell = New-Object -ComObject WScript.Shell

    # PowerShell executable
    $powershellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

    # Target arguments
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$conductionScript`""

    # Icon from shell32.dll (index 165 is a gear/industry icon)
    $iconLocation = "$env:SystemRoot\System32\shell32.dll,165"

    # Create desktop shortcut
    Write-Host "Creating desktop shortcut..." -ForegroundColor Yellow
    $shortcut = $shell.CreateShortcut($desktopShortcut)
    $shortcut.TargetPath = $powershellExe
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $scriptRoot
    $shortcut.IconLocation = $iconLocation
    $shortcut.Description = "Launch Conduction"
    $shortcut.Save()
    Write-Host "  Created: $desktopShortcut" -ForegroundColor Gray

    # Create Start Menu shortcut
    Write-Host "Creating Start Menu shortcut..." -ForegroundColor Yellow
    $shortcut = $shell.CreateShortcut($startMenuShortcut)
    $shortcut.TargetPath = $powershellExe
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $scriptRoot
    $shortcut.IconLocation = $iconLocation
    $shortcut.Description = "Launch Conduction"
    $shortcut.Save()
    Write-Host "  Created: $startMenuShortcut" -ForegroundColor Gray

    # Release COM object
    [System.Runtime.Interopservices.Marshal]::ReleaseComObject($shell) | Out-Null

    Write-Host ""
    Write-Host "=== Shortcuts Created ===" -ForegroundColor Green
    Write-Host ""
    Write-Host "You can now launch Conduction from:" -ForegroundColor Cyan
    Write-Host "  - Desktop shortcut" -ForegroundColor White
    Write-Host "  - Start Menu > Programs > Conduction" -ForegroundColor White
    Write-Host ""
}
