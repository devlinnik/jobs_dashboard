param([string]$TargetDirectory)
$arguments = @{ Action = "Build" }
if ($TargetDirectory) { $arguments.TargetDirectory = $TargetDirectory }
& (Join-Path $PSScriptRoot "install-windows.ps1") @arguments
exit $LASTEXITCODE
