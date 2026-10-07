param(
    [ValidateSet("Install", "Update", "Build")][string]$Action = "Install",
    [string]$SourceDirectory,
    [string]$TargetDirectory,
    [switch]$Local,
    [switch]$NoShortcut
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
# Process-local execution policy is set by the .cmd wrappers; global policy is never changed.
# Downloaded prerequisite installers must have valid publisher signatures before execution.
function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
function Find-Python312 {
    $candidates = New-Object System.Collections.Generic.List[string]
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $found = & $launcher.Source -3.12 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates.Add(([string]$found).Trim()) }
        } catch { }
    }
    $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))
    foreach ($key in @("HKCU:\Software\Python\PythonCore\3.12\InstallPath", "HKLM:\Software\Python\PythonCore\3.12\InstallPath")) {
        if (Test-Path $key) {
            $value = (Get-ItemProperty $key -ErrorAction SilentlyContinue).ExecutablePath
            if ($value) { $candidates.Add($value) }
        }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notmatch "WindowsApps") { $candidates.Add($pythonCommand.Source) }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            try {
                & $candidate -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return $candidate }
            } catch { }
        }
    }
    return $null
}
function Install-SignedPrerequisite {
    param([string]$Url, [string]$Publisher, [string[]]$Arguments)
    $downloadFolder = Join-Path ([IO.Path]::GetTempPath()) ("DevlinPrerequisite-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $downloadFolder | Out-Null
    $installer = Join-Path $downloadFolder "setup.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Url -OutFile $installer -UseBasicParsing -TimeoutSec 120
        $signature = Get-AuthenticodeSignature -FilePath $installer
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -cne $Publisher) {
            throw "Installer publisher verification failed. The downloaded file was not executed."
        }
        $process = Start-Process -FilePath $installer -ArgumentList $Arguments -PassThru -Wait
        if ($process.ExitCode -eq 3010) { Write-Warning "Windows restart is required by the prerequisite installer." }
        if ($process.ExitCode -notin @(0, 3010)) { throw "Prerequisite installation failed (exit $($process.ExitCode))." }
    } finally {
        Remove-Item -LiteralPath $downloadFolder -Recurse -Force -ErrorAction SilentlyContinue
    }
}
function Ensure-Python312 {
    $python = Find-Python312
    if (-not $python) {
        Write-Host "Installing Python 3.12 x64 from python.org (signed by Python Software Foundation)..."
        Install-SignedPrerequisite -Url "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -Publisher "Python Software Foundation" -Arguments @("/quiet", "InstallAllUsers=0", "Include_launcher=1", "InstallLauncherAllUsers=0", "PrependPath=0", "Include_test=0")
        $python = Find-Python312
    }
    if (-not $python) { throw "Python 3.12 x64 was not found after installation. Install it from python.org and retry." }
    return $python
}
function Test-WebView2 {
    foreach ($base in @("HKCU:\Software\Microsoft\EdgeUpdate\Clients", "HKLM:\Software\Microsoft\EdgeUpdate\Clients", "HKLM:\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients")) {
        if (Test-Path $base) {
            foreach ($key in @(Get-ChildItem $base -ErrorAction SilentlyContinue)) {
                $entry = Get-ItemProperty $key.PSPath -ErrorAction SilentlyContinue
                if ($entry.name -match "WebView2" -and $entry.pv -and $entry.pv -ne "0.0.0.0") { return $true }
            }
        }
    }
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($base) {
            $webview = Join-Path $base "Microsoft\EdgeWebView\Application"
            if (Test-Path $webview) {
                if (@(Get-ChildItem $webview -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName "msedgewebview2.exe") }).Count -gt 0) { return $true }
            }
        }
    }
    return $false
}
function Ensure-WebView2 {
    if (-not (Test-WebView2)) {
        Write-Host "Installing the signed Microsoft Edge WebView2 Runtime..."
        Install-SignedPrerequisite -Url "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -Publisher "Microsoft Corporation" -Arguments @("/silent", "/install")
        if (-not (Test-WebView2)) { throw "WebView2 Runtime was not detected. Restart Windows if requested, then retry." }
    }
}
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw "Devlin Desk requires 64-bit Windows 10/11." }
    $packager = Join-Path $PSScriptRoot "package_windows.py"
    if (-not $SourceDirectory) {
        $sourceCandidate = Split-Path -Parent $PSScriptRoot
        if (Test-Path (Join-Path $sourceCandidate "run.py")) {
            $SourceDirectory = $sourceCandidate
        } else {
            $manifest = Join-Path $sourceCandidate "install-info.json"
            if (Test-Path $manifest) {
                $SourceDirectory = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).source_directory
            }
            if (-not $SourceDirectory -or -not (Test-Path (Join-Path $SourceDirectory "run.py"))) {
                $SourceDirectory = Join-Path $env:LOCALAPPDATA "DevlinDeskUpdater"
            }
        }
    }
    $SourceDirectory = [IO.Path]::GetFullPath($SourceDirectory)
    if (-not $TargetDirectory) { $TargetDirectory = Join-Path $SourceDirectory "dist\DevlinDesk" }
    $TargetDirectory = [IO.Path]::GetFullPath($TargetDirectory)
    & (Join-Path $PSScriptRoot "check-running.ps1") -TargetDirectory $TargetDirectory
    Write-Host "Devlin Desk - $Action"
    Write-Host "Application folder: $TargetDirectory"
    $python = Ensure-Python312
    if ($Action -ne "Build") { Ensure-WebView2 }
    New-Item -ItemType Directory -Path $SourceDirectory -Force | Out-Null
    $resultPath = Join-Path ([IO.Path]::GetTempPath()) ("DevlinInstall-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        $arguments = @($packager, "--source-root", $SourceDirectory, "--target-folder", $TargetDirectory, "--result", $resultPath)
        if ($Action -eq "Update" -and -not $Local) { $arguments += "--refresh" }
        Invoke-Checked -Program $python -Arguments $arguments
        $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        if (-not $result.ok) { throw "Installation did not report success." }
    } finally {
        Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    }
    if ($Action -ne "Build" -and -not $NoShortcut) {
        try {
            $desktop = [Environment]::GetFolderPath("DesktopDirectory")
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut((Join-Path $desktop "Devlin Desk.lnk"))
            $shortcut.TargetPath = $result.executable
            $shortcut.WorkingDirectory = $TargetDirectory
            $shortcut.IconLocation = $result.executable + ",0"
            $shortcut.Description = "Devlin Desk - project monitoring"
            $shortcut.Save()
            Write-Host "Desktop shortcut created: Devlin Desk"
        } catch {
            Write-Warning "The application is installed, but the desktop shortcut could not be created. Run DevlinDesk.exe from the application folder."
        }
    }
    Write-Host "Done. Run $($result.executable)"
    if ($result.backup) { Write-Host "Rollback copy: $($result.backup)" }
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The previous installed version and user settings are preserved. Correct the problem and retry."
    exit 1
}
