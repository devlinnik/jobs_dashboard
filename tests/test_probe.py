import hashlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
import httpx
import pytest
from dashboard.monitor import probe_script


def test_probe_requires_bearer_and_returns_real_disk_totals(tmp_path):
    php = os.getenv('DEVLIN_TEST_PHP') or shutil.which('php')
    if not php:
        pytest.skip('Optional PHP 8+ runtime not installed; Python/SSH workflow does not require it')
    token = 'test-only-probe-secret'
    script = probe_script(token)
    assert token not in script
    assert hashlib.sha256(token.encode()).hexdigest() in script
    (tmp_path/'probe.php').write_text(script)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    proc = subprocess.Popen([php,'-S',f'127.0.0.1:{port}','-t',str(tmp_path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    url=f'http://127.0.0.1:{port}/probe.php'
    try:
        with httpx.Client(trust_env=False) as client:
            for _ in range(100):
                try:
                    r=client.get(url); break
                except httpx.ConnectError: time.sleep(.05)
            else: pytest.fail('PHP did not start')
            assert r.status_code == 403
            assert client.get(url,headers={'Authorization':'Bearer wrong'}).status_code == 403
            response=client.get(url,headers={'Authorization':'Bearer '+token})
            assert response.status_code == 200
            data=response.json()
            assert set(data) == {'free','total'}
            assert 0 < data['free'] <= data['total']
            assert response.headers['cache-control'] == 'no-store'
    finally:
        proc.terminate(); proc.wait(timeout=10)
