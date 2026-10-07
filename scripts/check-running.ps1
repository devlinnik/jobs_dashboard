param([Parameter(Mandatory = $true)][string]$TargetDirectory)
$ErrorActionPreference = "Stop"
$expected = [IO.Path]::GetFullPath((Join-Path $TargetDirectory "DevlinDesk.exe"))
foreach ($process in @(Get-Process -Name DevlinDesk -ErrorAction SilentlyContinue)) {
    $path = $null
    try { $path = $process.Path } catch { }
    if (-not $path -or [string]::Equals($path, $expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Close Devlin Desk before installation/update and try again. Processes are not terminated automatically."
    }
}
