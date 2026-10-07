param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$testCount = 0
function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
    $script:testCount++
}
foreach ($file in @(Get-ChildItem $PSScriptRoot -Filter *.ps1)) {
    $tokens = $null
    $parseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Assert-True ($parseErrors.Count -eq 0) ("PowerShell parse failed: " + $file.Name + " " + $parseErrors)
}
# Load only function definitions, never the real installation entry point.
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot "install-windows.ps1"), [ref]$tokens, [ref]$parseErrors)
foreach ($function in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $function.Extent.Text
}
$script:started = 0
$script:signatureStatus = "Valid"
$script:signerName = "Microsoft Corporation"
$script:processExit = 0
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
    [IO.File]::WriteAllText($OutFile, "fixture, not an executable")
}
function Get-AuthenticodeSignature {
    param($FilePath)
    $certificate = New-Object PSObject
    $certificate | Add-Member -MemberType ScriptMethod -Name GetNameInfo -Value { param($Type, $Issuer) return $script:signerName }
    return [pscustomobject]@{ Status = $script:signatureStatus; SignerCertificate = $certificate }
}
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, [switch]$Wait)
    $script:started++
    return [pscustomobject]@{ ExitCode = $script:processExit }
}
Install-SignedPrerequisite -Url "https://example.test/signed.exe" -Publisher "Microsoft Corporation" -Arguments @("/test")
Assert-True ($script:started -eq 1) "Valid matching publisher was not accepted"
foreach ($case in @(@("NotSigned", "Microsoft Corporation"), @("Valid", "Fake Microsoft Corporation"), @("Valid", "Other publisher"))) {
    $script:signatureStatus = $case[0]
    $script:signerName = $case[1]
    $rejected = $false
    try {
        Install-SignedPrerequisite -Url "https://example.test/blocked.exe" -Publisher "Microsoft Corporation" -Arguments @("/test")
    } catch { $rejected = $true }
    Assert-True $rejected "Invalid signature or publisher was accepted"
    Assert-True ($script:started -eq 1) "Untrusted prerequisite was executed"
}
$script:signatureStatus = "Valid"
$script:signerName = "Microsoft Corporation"
$script:processExit = 5
$rejected = $false
try { Install-SignedPrerequisite -Url "https://example.test/failed.exe" -Publisher "Microsoft Corporation" -Arguments @("/test") } catch { $rejected = $true }
Assert-True $rejected "Failed prerequisite installation reported success"
$rejected = $false
try { Invoke-Checked -Program $Python -Arguments @("-c", "import sys; sys.exit(7)") } catch { $rejected = $true }
Assert-True $rejected "Native command exit status was swallowed"
# Running instances are never killed, and a different installation is not blocked.
$global:devlinInstallerTestProcesses = @()
function Get-Process { param($Name, $ErrorAction) return $global:devlinInstallerTestProcesses }
$target = Join-Path ([IO.Path]::GetTempPath()) "DevlinInstallerTest"
& (Join-Path $PSScriptRoot "check-running.ps1") -TargetDirectory $target
Assert-True $true "Empty running-process check failed"
$global:devlinInstallerTestProcesses = @([pscustomobject]@{ Path = (Join-Path ([IO.Path]::GetTempPath()) "OtherApp/DevlinDesk.exe") })
& (Join-Path $PSScriptRoot "check-running.ps1") -TargetDirectory $target
Assert-True $true "Other installation blocked"
$global:devlinInstallerTestProcesses = @([pscustomobject]@{ Path = (Join-Path $target "DevlinDesk.exe") })
$rejected = $false
try { & (Join-Path $PSScriptRoot "check-running.ps1") -TargetDirectory $target } catch { $rejected = $true }
Assert-True $rejected "Running target installation was not blocked"
# Check Shell notifications without calling native Shell APIs or changing real caches.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
namespace Devlin {
    public static class ShellIcons {
        public static List<string> Calls = new List<string>();
        public static void SHChangeNotify(uint eventId, uint flags, string path, IntPtr other) {
            Calls.Add(eventId.ToString() + ":" + flags.ToString() + ":" + path);
        }
    }
}
'@
$iconTestFolder = Join-Path ([IO.Path]::GetTempPath()) ("DevlinIconTest-" + [guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $iconTestFolder | Out-Null
    [IO.File]::WriteAllText((Join-Path $iconTestFolder "DevlinDesk.exe"), "not an executable")
    & (Join-Path $PSScriptRoot "refresh-icons.ps1") -TargetDirectory $iconTestFolder
    Assert-True ([Devlin.ShellIcons]::Calls[0] -eq ("8192:8197:" + (Join-Path $iconTestFolder "DevlinDesk.exe"))) "EXE change notification did not use the correct Unicode path"
    Assert-True ([Devlin.ShellIcons]::Calls[[Devlin.ShellIcons]::Calls.Count - 1] -eq "134217728:8192:") "Shell icon cache invalidation was not requested"
} finally {
    Remove-Item -LiteralPath $iconTestFolder -Recurse -Force
}
Write-Host "Installer PowerShell checks passed: $testCount"
