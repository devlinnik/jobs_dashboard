"""Check the shipped executable, not just the C# source or a mock launcher."""
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'DevlinLauncher.exe'


def test_shipped_launcher_is_windows_gui_with_current_installer_resources():
    binary = EXE.read_bytes()
    normalized = binary.replace(b'\r\n', b'\n')
    assert binary[:2] == b'MZ'
    pe = struct.unpack_from('<I', binary, 0x3c)[0]
    assert binary[pe:pe + 4] == b'PE\0\0'
    optional = pe + 24
    assert struct.unpack_from('<H', binary, optional + 68)[0] == 2  # GUI subsystem
    for name in ('install-windows.ps1', 'package_windows.py', 'check-running.ps1', 'refresh-icons.ps1'):
        assert ('DevlinInstaller.' + name).encode() in binary
        assert (ROOT / 'scripts' / name).read_bytes().replace(b'\r\n', b'\n') in normalized, f'Stale embedded resource: {name}; rebuild launcher'
    assert (ROOT / 'dashboard/assets/devlin-desk.ico').read_bytes() in binary
    assert not (ROOT / 'Install-Devlin.cmd').exists()
    assert not (ROOT / 'Update-Devlin.cmd').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Native .NET Framework launcher needs Windows')
def test_native_launcher_process_and_icon(tmp_path):
    from dashboard.windows_icon import verify_executable_icon
    report = tmp_path / 'launcher-report.json'
    result = subprocess.run([str(EXE), '--self-test-output', str(report)], timeout=60)
    data = json.loads(report.read_text(encoding='utf-8-sig'))
    assert result.returncode == 0, data
    assert data['ok'] and len(data['checks']) == 5, data
    verify_executable_icon(EXE, ROOT / 'dashboard/assets/devlin-desk.ico', group_identifier=32512)
