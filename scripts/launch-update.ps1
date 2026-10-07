param([Parameter(Mandatory = $true)][string]$TargetDirectory, [switch]$Local, [switch]$NoShortcut)
$ErrorActionPreference = "Stop"
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("DevlinUpdate-" + [guid]::NewGuid().ToString("N"))
$devlinExit = 1
try {
    New-Item -ItemType Directory -Path $temporary | Out-Null
    # Run maintenance from TEMP: no .cmd, Python code or PowerShell file is held open
    # in the installation folder while that folder is replaced.
    foreach ($name in @("install-windows.ps1", "package_windows.py", "check-running.ps1")) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $temporary $name)
    }
    $arguments = @{ Action = "Update"; TargetDirectory = $TargetDirectory }
    $manifest = Join-Path $TargetDirectory "install-info.json"
    if (Test-Path $manifest) {
        $source = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).source_directory
        if ($source -and (Test-Path (Join-Path $source "run.py"))) { $arguments.SourceDirectory = $source }
    }
    if ($Local) { $arguments.Local = $true }
    if ($NoShortcut) { $arguments.NoShortcut = $true }
    & (Join-Path $temporary "install-windows.ps1") @arguments
    $devlinExit = $LASTEXITCODE
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
}
if ($devlinExit -ne 0) { Write-Host "Update failed. The previous application is preserved." }
Read-Host "Press Enter to close"
exit $devlinExit
