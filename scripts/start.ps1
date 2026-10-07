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
Invoke-Checked -Program ".venv\Scripts\python.exe" -Arguments @("run.py")
