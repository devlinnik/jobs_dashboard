@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\refresh-icons.ps1" -TargetDirectory "%~dp0dist\DevlinDesk"
if errorlevel 1 (
    echo Icon refresh failed. See the message above.
    pause
    exit /b 1
)
pause
