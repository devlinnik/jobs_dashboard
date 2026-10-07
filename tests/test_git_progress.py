"""A progress request stays responsive during the real commit operation."""
import os
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient
import pytest
from dashboard.app import create_app
from dashboard import gitops
from tests.test_git import LocalConnection, git


@pytest.mark.skipif(os.name == 'nt', reason='Remote shell targets Unix')
def test_progress_api_is_live_scoped_and_authenticated(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('dashboard.app')
    root = tmp_path / 'repo'; root.mkdir(); git(root, 'init', '-b', 'main')
    paths = [f'file-{n}.txt' for n in range(80)]
    for path in paths: (root / path).write_text(path)
    conn = LocalConnection(root)
    current = gitops.status(conn)
    @contextmanager
    def connect(*args, **kwargs):
        original = conn.run
        def slowed(command, **options):
            if ' add -A -- ' in command: time.sleep(.015)
            return original(command, **options)
        conn.run = slowed
        try: yield conn
        finally: conn.run = original
    monkeypatch.setattr(module, 'connect', connect)
    with TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={'pin':''})
        project = client.post('/api/projects', json={'name':'Repo','ssh_path':str(root)}).json()
        other = client.post('/api/projects', json={'name':'Other'}).json()
        client.put('/api/settings', json={'git_name':'Test','git_email':'test@example.com'})
        op = str(uuid.uuid4()); responses = []
        thread = threading.Thread(target=lambda: responses.append(client.post(f"/api/projects/{project['id']}/git/sync", json={
            'snapshot':current['snapshot'],'files':paths,'message':'80 files'}, headers={'X-Devlin-Operation':op})))
        thread.start(); seen = []
        while thread.is_alive():
            result = client.get(f"/api/projects/{project['id']}/git/progress", params={'operation_id':op})
            if result.status_code == 200: seen.append(result.json())
            time.sleep(.03)
        thread.join(timeout=10)
        assert responses[0].status_code == 200, responses[0].text
        assert any(0 < p['completed'] < 80 and not p['done'] for p in seen)
        final = client.get(f"/api/projects/{project['id']}/git/progress", params={'operation_id':op}).json()
        assert final['done'] and final['completed'] == final['total'] == 80
        assert client.get(f"/api/projects/{other['id']}/git/progress", params={'operation_id':op}).status_code == 404
        client.post('/api/lock')
        assert client.get(f"/api/projects/{project['id']}/git/progress", params={'operation_id':op}).status_code == 401
