param([string]$TargetDirectory)
$ErrorActionPreference = "Stop"
if (-not $TargetDirectory) { $TargetDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) "dist\DevlinDesk" }
$executable = Join-Path ([IO.Path]::GetFullPath($TargetDirectory)) "DevlinDesk.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "Installed application not found: $executable" }
if (-not ("Devlin.ShellIcons" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Devlin {
    public static class ShellIcons {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        public static extern void SHChangeNotify(uint eventId, uint flags, string path, IntPtr other);
    }
}
'@
}
# Notify Explorer through supported Shell APIs. No cache deletion or Explorer restart.
# SHCNE_UPDATEITEM, SHCNF_PATHW | SHCNF_FLUSHNOWAIT.
[Devlin.ShellIcons]::SHChangeNotify(0x00002000, 0x00002005, $executable, [IntPtr]::Zero)
$desktop = [Environment]::GetFolderPath("DesktopDirectory")
if ($desktop) {
    $shortcut = Join-Path $desktop "Devlin Desk.lnk"
    if (Test-Path -LiteralPath $shortcut) {
        [Devlin.ShellIcons]::SHChangeNotify(0x00002000, 0x00002005, $shortcut, [IntPtr]::Zero)
    }
}
# SHCNE_ASSOCCHANGED invalidates the Shell's icon cache for paths replaced in place.
[Devlin.ShellIcons]::SHChangeNotify(0x08000000, 0x00002000, $null, [IntPtr]::Zero)
Write-Host "Windows icon refresh requested. Reopen the application folder or press F5."
