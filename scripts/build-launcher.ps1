# Rebuild the standalone launcher using the .NET Framework compiler shipped with Windows.
param([string]$Output)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Output) { $Output = Join-Path $root 'DevlinLauncher.exe' }
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework 4.8 compiler was not found.' }
$arguments = @('/nologo', '/target:winexe', '/platform:anycpu', '/optimize+', '/langversion:5', '/codepage:65001', "/out:$Output", "/win32icon:$(Join-Path $root 'dashboard\assets\devlin-desk.ico')", '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll')
foreach ($name in @('install-windows.ps1', 'package_windows.py', 'check-running.ps1', 'refresh-icons.ps1')) {
    $arguments += "/resource:$(Join-Path $PSScriptRoot $name),DevlinInstaller.$name"
}
$arguments += "/resource:$(Join-Path $root 'dashboard\assets\devlin-desk.ico'),DevlinInstaller.devlin-desk.ico"
$arguments += Join-Path $PSScriptRoot 'DevlinLauncher.cs'
& $csc @arguments
if ($LASTEXITCODE -ne 0) { throw "Launcher build failed ($LASTEXITCODE)." }
Write-Host "Built $Output"
