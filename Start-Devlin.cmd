@echo off
setlocal
if not exist "%~dp0dist\DevlinDesk\DevlinDesk.exe" (
    start "" "%~dp0DevlinLauncher.exe"
    exit /b 0
)
start "" /D "%~dp0dist\DevlinDesk" "%~dp0dist\DevlinDesk\DevlinDesk.exe"
