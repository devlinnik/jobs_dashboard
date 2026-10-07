param([Parameter(Mandatory = $true)][string]$TargetDirectory)
$ErrorActionPreference = "Stop"
$expected = [IO.Path]::GetFullPath((Join-Path $TargetDirectory "DevlinDesk.exe"))
foreach ($process in @(Get-Process -Name DevlinDesk -ErrorAction SilentlyContinue)) {
    $path = $null
    try { $path = $process.Path } catch { }
    if (-not $path -or [string]::Equals($path, $expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Exit Devlin Desk using the tray menu before installation/update and try again. Closing the window keeps monitoring in the background. Processes are not terminated automatically."
    }
}
