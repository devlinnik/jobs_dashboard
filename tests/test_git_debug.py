import json
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.gitdebug import trace_commands
from dashboard.remote import Connection, RemoteError
from dashboard.store import Store


class Channel:
    def __init__(self, output, error=b'', code=0):
        self.output, self.error, self.code = output, error, code
        self.closed = False
    def settimeout(self, value): pass
    def exec_command(self, command): pass
    def sendall(self, data): pass
    def shutdown_write(self): pass
    def recv_ready(self): return bool(self.output)
    def recv_stderr_ready(self): return bool(self.error)
    def recv(self, count):
        result, self.output = self.output[:count], self.output[count:]
        return result
    def recv_stderr(self, count):
        result, self.error = self.error[:count], self.error[count:]
        return result
    def exit_status_ready(self): return True
    def recv_exit_status(self): return self.code
    def close(self): self.closed = True


def connection(channel):
    transport = SimpleNamespace(open_session=lambda **kwargs: channel)
    client = SimpleNamespace(get_transport=lambda: transport)
    return Connection(client, {'ssh_path':'/srv/project'})


def test_overflow_records_counts_and_stage_without_payload():
    channel = Channel(b'private-file-content'*100)
    conn = connection(channel); records = []
    with trace_commands(conn, records.append, 'status', 'trace'):
        with pytest.raises(RemoteError, match='лимите 128'):
            conn.run('git status --porcelain=v1 -z --untracked-files=all', max_output=128)
    assert channel.closed
    row = records[0]
    assert row['command'] == 'git status' and row['result'] == 'output_limit'
    assert row['stdout_bytes'] > row['limit_bytes'] == 128
    assert row['exit_code'] is None and row['duration_ms'] >= 0
    assert row['error_type'] == 'RemoteError'
    assert 'private-file-content' not in json.dumps(records)


def test_failed_command_classifies_error_without_credentials_or_arguments():
    channel = Channel(b'private stdout', b'fatal: Authentication failed for https://user:secret@github.com/team/repo', 128)
    conn = connection(channel); records = []
    with trace_commands(conn, records.append, 'push', 'trace'):
        code, _, _ = conn.run('git push https://user:secret@github.com/team/repo secret-branch', input_data=b'another-secret')
    assert code == 128 and records[0]['exit_code'] == 128
    assert 'Нет доступа' in records[0]['reason']
    assert not any(value in json.dumps(records) for value in ('secret', 'private stdout', 'user:', 'team/repo'))


def test_timeout_records_unknown_exit_code_and_closes_channel(monkeypatch):
    import itertools
    ticks = itertools.count(0, 100)
    monkeypatch.setattr('dashboard.remote.time.monotonic', lambda:next(ticks))
    channel = Channel(b'not-read-yet')
    conn = connection(channel); records = []
    with trace_commands(conn, records.append, 'push', 'trace'):
        with pytest.raises(RemoteError, match='время ожидания'):
            conn.run('git push origin main', timeout=1)
    assert channel.closed and records[0]['result'] == 'timeout'
    assert records[0]['exit_code'] is None and records[0]['stdout_bytes'] == 0


def test_telemetry_failure_does_not_repeat_completed_git_mutation():
    conn = connection(Channel(b'committed'))
    def unavailable(record): raise OSError('full disk')
    original = conn.run
    with trace_commands(conn, unavailable, 'sync', 'trace'):
        assert conn.run('git commit -m private-message')[0] == 0
    assert conn.run == original


def test_debug_report_is_authenticated_project_scoped_and_survives_failure(tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('dashboard.app')
    @contextmanager
    def connect(*args, **kwargs):
        yield connection(Channel(b'never-save-this-token'*110000))
    monkeypatch.setattr(module, 'connect', connect)
    with TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={'pin':''})
        project = client.post('/api/projects', json={'name':'Project'}).json()['id']
        other = client.post('/api/projects', json={'name':'Other'}).json()['id']
        response = client.get(f'/api/projects/{project}/git')
        assert response.status_code == 409
        report = client.get(f'/api/projects/{project}/git/debug').json()
        assert report['version'] == '1.10.11'
        assert report['records'][0]['result'] == 'output_limit'
        assert report['records'][0]['command'] == 'git rev-parse'
        assert 'never-save-this-token' not in json.dumps(report)
        assert client.get(f'/api/projects/{other}/git/debug').json()['records'] == []
        assert client.get('/api/projects/missing/git/debug').status_code == 404
        events = client.get('/api/events', params={'project_id':project}).json()
        assert any(report['records'][0]['trace_id'] in event['message'] for event in events)
        client.post('/api/lock')
        assert client.get(f'/api/projects/{project}/git/debug').status_code == 401
    store = Store(tmp_path/'data')
    assert store.git_debug_records(project)[0]['result'] == 'output_limit'
    store.close()


def test_debug_records_are_bounded(tmp_path):
    store = Store(tmp_path/'data')
    for i in range(2010): store.git_debug_record('project', {'sequence':i})
    rows = store.git_debug_records('project')
    assert len(rows) == 300 and rows[-1]['sequence'] == 2009
    assert store.db.execute('SELECT count(*) FROM git_debug').fetchone()[0] == 2000
    store.close()
