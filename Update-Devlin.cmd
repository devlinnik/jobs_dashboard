@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1" -Action Update %*
set "devlin_exit=%errorlevel%"
if not "%devlin_exit%"=="0" echo Update failed. The previous application is preserved. See the message above.
pause
exit /b %devlin_exit%
