import os
import subprocess
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.remote import Connection, RemoteError
from tests.test_ssh import ssh_server
from tests.test_git import LocalConnection, git
from dashboard import gitops


@pytest.mark.skipif(os.name == 'nt', reason='Unix SSH fixture')
def test_shared_hosting_quota_overrides_http_and_alerts_without_deleting_files(tmp_path, monkeypatch):
    account = tmp_path/'account'; account.mkdir()
    first = account/'site-one'; first.mkdir()
    second = account/'site-two'; second.mkdir()
    (second/'data.bin').write_bytes(b'x'*20000)
    with ssh_server(home=str(account)) as (port, _), TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={})
        notifications = []
        async def telegram(text):
            notifications.append(text)
            return True
        monkeypatch.setattr(client.app.state.monitor, 'telegram', telegram)
        data = {'name':'Hosting','ssh_host':'127.0.0.1','ssh_port':port,'ssh_user':'deploy',
            'ssh_password':'test-password','hosting':True,'hosting_path':str(account),'hosting_limit_gb':0.01}
        profile = client.post('/api/ssh-profiles', json=data).json()
        prefix = '/api/ssh-profiles/'+profile['id']
        actual = client.get(prefix+'/fingerprint').json()['fingerprint']
        client.post(prefix+'/trust',json={'fingerprint':actual})
        ids = []
        for folder in (first, second):
            project = client.post('/api/projects',json={'name':folder.name,'ssh_profile_id':profile['id'],
                'ssh_path':str(folder),'disk_mode':'http','git_monitor':False,'notify':True}).json()
            ids.append(project['id'])
        states = [client.post('/api/projects/'+pid+'/check').json() for pid in ids]
        used = int(subprocess.check_output(['du','-sk',str(account)]).split()[0])*1024
        for state in states:
            assert not state['issues']
            disk = state['disk']
            assert disk['used'] == used and disk['total'] == 10000000
            assert disk['free'] == disk['total'] - used and disk['estimated']
        assert client.post(prefix+'/test').json()['disk']['used'] == used
        # Legacy API updates that omit hosting options preserve the existing tariff.
        old_style = {'name':'Renamed','ssh_host':'127.0.0.1','ssh_port':port,'ssh_user':'deploy'}
        assert client.put(prefix,json=old_style).json()['hosting']
        data['hosting_limit_gb'] = 0.000001
        assert client.put(prefix,json=data).status_code == 200
        state = client.post('/api/projects/'+ids[0]+'/check').json()
        assert state['disk']['free'] == 0 and state['disk']['over_limit'] > 0
        assert any('по оценке тарифа' in issue for issue in state['issues'])
        assert len(notifications) == 1 and 'по оценке тарифа' in notifications[0]
        client.post('/api/projects/'+ids[0]+'/check')
        assert len(notifications) == 1  # The same quota issue must not send another notification.
        assert (second/'data.bin').stat().st_size == 20000
        client.put('/api/projects/'+ids[0],json={'name':'Off','ssh_profile_id':profile['id'],'ssh_path':str(first),'disk_mode':'off','git_monitor':False,'notify':False})
        assert 'disk' not in client.post('/api/projects/'+ids[0]+'/check').json()


@pytest.mark.parametrize('extra', [
    {'hosting':True}, {'hosting':True,'hosting_path':'/srv','hosting_limit_gb':0},
    {'hosting_path':'/'}, {'hosting_path':'../project'}, {'hosting_path':'/srv/../other'},
    {'hosting_path':'/srv\ncommand'}, {'hosting_limit_gb':-1}, {'hosting_unit':'invalid'}])
def test_hosting_profile_validation(tmp_path, extra):
    with TestClient(create_app(tmp_path, start_monitor=False),headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock',json={})
        response = client.post('/api/ssh-profiles',json={'name':'Test','ssh_host':'server','ssh_user':'user'} | extra)
        assert response.status_code == 422


def test_partial_usage_never_returns_fake_free_space():
    class Conn(Connection):
        def text(self, *args, **kwargs): return '/srv/account'
        def run(self, *args, **kwargs): return 1, b'200 /srv/account', b'Permission denied'
    conn = Conn(None, {'ssh_path':'','hosting_path':'/srv/account','hosting_limit_gb':10})
    with pytest.raises(RemoteError,match='Частичный'): conn.hosting_disk()


@pytest.mark.parametrize(('stderr','hint'), [
    (b'fatal: disk quota exceeded SECRET_TOKEN', 'квота'),
    (b'fatal: unable to create index.lock: File exists SECRET_TOKEN','index.lock'),
    (b'fatal: detected dubious ownership SECRET_TOKEN','safe.directory'),
    (b'Author identity unknown SECRET_TOKEN','автора'),
    (b'gpg failed to sign SECRET_TOKEN','подписать'),
    (b'insufficient permission for adding an object SECRET_TOKEN','objects/refs')])
def test_git_128_diagnostics_hide_raw_output(stderr, hint):
    class Failed(Connection):
        def run(self, *args, **kwargs): return 128, b'', stderr
    with pytest.raises(RemoteError) as caught: Failed(None,{'ssh_path':'/srv'}).text('git commit')
    assert hint in str(caught.value) and 'SECRET_TOKEN' not in str(caught.value)


@pytest.mark.skipif(os.name == 'nt', reason='Unix Git commands')
def test_read_only_git_diagnostics_report_lock_and_signing(tmp_path):
    git(tmp_path,'init','-b','main')
    git(tmp_path,'config','commit.gpgsign','true')
    (tmp_path/'.git/index.lock').write_text('preserve lock')
    checks = gitops.diagnostics(LocalConnection(tmp_path), {'git_name':'Author','git_email':'author@example.com'})['checks']
    assert any(c['ok'] is False and 'index.lock' in c['message'] for c in checks)
    assert any(c['ok'] is False and 'Подпись' in c['message'] for c in checks)
    assert (tmp_path/'.git/index.lock').read_text() == 'preserve lock'
    assert git(tmp_path,'config','commit.gpgsign') == 'true'
    (tmp_path/'index.php').write_text('server changes')
    with pytest.raises(RemoteError, match='index.lock'):
        LocalConnection(tmp_path).text('LC_ALL=C git add -- index.php')
    assert (tmp_path/'index.php').read_text() == 'server changes'
    assert (tmp_path/'.git/index.lock').read_text() == 'preserve lock'
