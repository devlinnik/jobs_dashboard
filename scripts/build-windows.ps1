$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Invoke-Checked -Program "py" -Arguments @("-3.12", "-m", "venv", ".venv")
}
Invoke-Checked -Program ".venv\Scripts\python.exe" -Arguments @("-m", "pip", "install", "-r", "requirements-desktop.txt")
Invoke-Checked -Program ".venv\Scripts\python.exe" -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed", "--name", "DevlinDesk", "--add-data", "dashboard/static;dashboard/static", "--collect-all", "webview", "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.asyncio", "--hidden-import", "uvicorn.protocols.http.h11_impl", "--hidden-import", "uvicorn.lifespan.on", "run.py")
Write-Host "Build: dist\DevlinDesk\DevlinDesk.exe. Keep the entire DevlinDesk folder."
