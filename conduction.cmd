@echo off
REM Thin wrapper to launch Conduction via PowerShell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0conduction.ps1" %*
