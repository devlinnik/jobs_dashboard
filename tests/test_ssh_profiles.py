import json
import os
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.models import ProjectInput
from dashboard.store import Store
from tests.test_ssh import ssh_server


@pytest.mark.skipif(os.name == 'nt', reason='Loopback fixture simulates Linux SSH/SFTP')
def test_shared_profile_browsing_without_valid_project_path(tmp_path):
    home = tmp_path/'home'; home.mkdir()
    project_dir = home/'Сайт с пробелом'; project_dir.mkdir()
    (home/'file.txt').write_text('do not read file contents')
    (home/'link').symlink_to(project_dir, target_is_directory=True)
    with ssh_server(home=str(home)) as (port, key), TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request': '1'}) as client:
        client.post('/api/unlock', json={})
        profile = client.post('/api/ssh-profiles', json={'name': 'Shared', 'ssh_host': '127.0.0.1', 'ssh_port': port,
            'ssh_user': 'deploy', 'ssh_password': 'test-password'}).json()
        assert 'test-password' not in json.dumps(profile) and profile['has_password']
        assert 'ssh_password_enc' not in profile
        p = client.post('/api/projects', json={'name': 'One', 'ssh_profile_id': profile['id'], 'ssh_path': str(home/'missing'),
            'git_monitor': False, 'notify': False}).json()
        prefix = '/api/projects/' + p['id']
        assert client.get(prefix+'/ssh/directories').status_code == 409
        key = client.get('/api/ssh-profiles/'+profile['id']+'/fingerprint').json()
        assert client.post('/api/ssh-profiles/'+profile['id']+'/trust', json={'fingerprint': key['fingerprint']}).status_code == 200
        assert client.post('/api/ssh-profiles/'+profile['id']+'/test').json()['ok']
        test = client.post(prefix+'/ssh/test').json()
        assert test['ok'] and test['directory']['ok'] is False
        state = client.post(prefix+'/check').json()
        assert state['ssh']['ok'] and not state['directory']['ok']
        assert any('Папка проекта:' in issue for issue in state['issues'])
        listing = client.get(prefix+'/ssh/directories').json()
        assert listing['path'] == str(home)
        assert {entry['name'] for entry in listing['directories']} == {'Сайт с пробелом', 'link'}
        linked = client.get(prefix+'/ssh/directories', params={'path': str(home/'link')}).json()
        assert linked['path'] == str(project_dir)
        assert client.get(prefix+'/ssh/directories', params={'path': 'relative'}).status_code == 409
        assert client.get(prefix+'/ssh/directories', params={'path': str(home/'missing')}).status_code == 409
        assert client.put(prefix, json={'name': 'One', 'ssh_profile_id': profile['id'], 'ssh_path': listing['directories'][0]['path'],
            'git_monitor': False, 'notify': False}).status_code == 200
        state = client.post(prefix+'/check').json()
        assert state['ssh']['ok'] and state['directory']['ok'] and state['disk']['total'] > 0
        second = client.post('/api/projects', json={'name': 'Two', 'ssh_profile_id': profile['id']}).json()
        assert second['ssh_fingerprint'] == key['fingerprint'] and second['has_password']
        assert client.get('/api/ssh-profiles').json()[0]['project_count'] == 2
        assert client.delete('/api/ssh-profiles/'+profile['id']).status_code == 409
        assert client.put('/api/ssh-profiles/'+profile['id'], json={'name': 'Renamed', 'ssh_host': '127.0.0.1', 'ssh_port': port+1,
            'ssh_user': 'deploy'}).status_code == 200
        assert all(not p['ssh_fingerprint'] for p in client.get('/api/projects').json())
        assert client.get('/api/ssh-profiles').json()[0]['has_password']
        client.post('/api/lock')
        assert client.get(prefix+'/ssh/directories').status_code == 401
        assert client.get('/api/ssh-profiles').status_code == 401


def test_legacy_migration_is_backed_up_shared_and_idempotent(tmp_path):
    store = Store(tmp_path)
    store.save_settings({'pin_hash':'existing-pin-hash', 'telegram_token':store.encrypt('existing-bot-token'), 'telegram_chat':'123'})
    for pid, password in [('one', 'same'), ('two', 'same'), ('other', 'different')]:
        p = ProjectInput(name=pid, ssh_host='server', ssh_user='deploy', ssh_path='/srv/'+pid).model_dump(exclude={'ssh_password', 'disk_token'})
        p.update(id=pid, ssh_fingerprint='SHA256:verified', ssh_password_enc=store.encrypt(password), disk_token_enc=store.encrypt('probe-secret'))
        store.save_project(p)
    store.close()
    store = Store(tmp_path)
    profiles = store.profiles()
    assert store.settings()['pin_hash'] == 'existing-pin-hash'
    assert store.decrypt(store.settings()['telegram_token']) == 'existing-bot-token'
    assert len(profiles) == 2
    projects = {p['id']: p for p in store.projects()}
    assert projects['one']['ssh_profile_id'] == projects['two']['ssh_profile_id']
    assert projects['one']['ssh_profile_id'] != projects['other']['ssh_profile_id']
    assert all(not p['ssh_password_enc'] for p in projects.values())
    assert store.decrypt(store.resolve_project(projects['one'])['ssh_password_enc']) == 'same'
    assert store.decrypt(projects['one']['disk_token_enc']) == 'probe-secret'
    backups = list((tmp_path/'migration-backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        old = json.loads(db.execute("SELECT data FROM projects WHERE id='one'").fetchone()[0])
        assert old['ssh_host'] == 'server' and not old.get('ssh_profile_id')
        assert store.decrypt(old['ssh_password_enc']) == 'same'
    store.close()
    store = Store(tmp_path)
    assert len(store.profiles()) == 2 and list((tmp_path/'migration-backups').glob('*.sqlite3')) == backups
    store.close()


def test_php_setup_saves_url_mode_and_encrypted_key_together(tmp_path, monkeypatch):
    import dashboard.app as module
    class SFTP:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def open(self, path, mode):
            assert mode == 'wx'
            return (tmp_path/'probe.php').open('x')
        def chmod(self, path, mode): pass
    @contextmanager
    def connected(store, project, **kwargs):
        yield SimpleNamespace(path='/srv/site', client=SimpleNamespace(open_sftp=lambda: SFTP()))
    monkeypatch.setattr(module, 'connect', connected)
    with TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request': '1'}) as client:
        client.post('/api/unlock', json={})
        p = client.post('/api/projects', json={'name': 'Probe', 'ssh_host':'host', 'ssh_user':'deploy', 'ssh_path':'/srv/site'}).json()
        url = '/api/projects/'+p['id']+'/disk/deploy'
        assert client.post(url, json={'confirm':True, 'disk_url':'http://site.test/probe'}).status_code == 422
        response = client.post(url, json={'confirm':True, 'disk_url':'https://site.test/.devlin-disk.php'})
        assert response.status_code == 200
        token = response.json()['token']
        project = client.get('/api/projects').json()[0]
        assert project['disk_mode'] == 'http' and project['has_disk_token']
        assert project['disk_url'] == 'https://site.test/.devlin-disk.php'
        assert token not in json.dumps(project) and token not in (tmp_path/'probe.php').read_text()
        assert client.post(url, json={'confirm':True, 'disk_url':project['disk_url']}).status_code == 409
        raw = client.app.state.store.project(p['id'])
        assert client.app.state.store.decrypt(raw['disk_token_enc']) == token


def test_http_probe_reports_specific_missing_configuration(tmp_path):
    with TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request': '1'}) as client:
        client.post('/api/unlock', json={})
        for config, expected in [({}, 'не настроена'), ({'disk_url':'https://probe.test'}, 'ключ PHP-пробы отсутствует'),
                                 ({'disk_token':'secret'}, 'HTTPS URL файла не указан')]:
            p = client.post('/api/projects', json={'name':'Probe', 'disk_mode':'http', 'notify':False, **config}).json()
            state = client.post('/api/projects/'+p['id']+'/check').json()
            assert expected in state['disk']['error']
