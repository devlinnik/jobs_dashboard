@echo off
setlocal
if not exist "%~dp0dist\DevlinDesk\DevlinDesk.exe" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" -Action Install
    if errorlevel 1 (
        echo Installation failed. See the message above.
        pause
        exit /b 1
    )
)
start "" /D "%~dp0dist\DevlinDesk" "%~dp0dist\DevlinDesk\DevlinDesk.exe"
