import json
import base64
import os
import shutil
import subprocess
import httpx
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.gitprovider import Provider, repository_path
from dashboard import gitops
from dashboard.remote import RemoteError
from tests.git_https_server import git_https_server, TOKEN
from tests.test_ssh import ssh_server


def test_profile_crud_encryption_account_and_project_references(tmp_path, monkeypatch):
    original_request = Provider.request
    def request(self, path, params=None):
        assert self.token == 'test-token'
        if path == '/user': return {'login': 'developer', 'name': 'Developer'}
        assert path == '/user/repos' and params['page'] == 2
        return [{'clone_url': 'https://github.com/team/project.git', 'default_branch': 'main', 'private': True}]
    monkeypatch.setattr(Provider, 'request', request)
    with TestClient(create_app(tmp_path, start_monitor=False), headers={'X-App-Request': '1'}) as client:
        assert client.get('/api/git-profiles').status_code == 401
        client.post('/api/unlock', json={})
        profile = client.post('/api/git-profiles', json={'name': 'GitHub', 'token': 'test-token'}).json()
        prefix = '/api/git-profiles/'+profile['id']
        assert profile['has_token'] and 'test-token' not in json.dumps(profile) and 'token_enc' not in profile
        stored = client.app.state.store.git_profile(profile['id'])
        assert 'test-token' not in json.dumps(stored)
        assert client.app.state.store.decrypt(stored['token_enc']) == 'test-token'
        assert client.post(prefix+'/test').json()['account']['username'] == 'developer'
        assert client.get(prefix+'/repositories?page=2').json()['repositories'][0]['private']
        assert client.get(prefix+'/repositories?page=0').status_code == 422
        assert client.post('/api/projects', json={'name':'Invalid', 'git_profile_id':'missing'}).status_code == 404
        for name in ('One', 'Two'):
            assert client.post('/api/projects', json={'name':name, 'git_profile_id':profile['id']}).status_code == 200
        assert client.get('/api/git-profiles').json()[0]['project_count'] == 2
        assert client.delete(prefix).status_code == 409
        assert all(p['git_profile_name'] == 'GitHub' for p in client.get('/api/projects').json())
        edited = client.put(prefix, json={'name':'Renamed'}).json()
        assert edited['has_token'] and edited['account'] == 'developer'
        edited = client.put(prefix, json={'name':'Lab', 'provider':'gitlab', 'base_url':'https://gitlab.com'}).json()
        assert not edited['has_token'] and not edited['account']  # Never transfer token to another host.
        monkeypatch.setattr(Provider, 'request', original_request)
        assert client.post(prefix+'/test').status_code == 409
        client.post('/api/lock')
        for path in (prefix+'/repositories', '/api/git-profiles'):
            assert client.get(path).status_code == 401
        assert client.post(prefix+'/test').status_code == 401


@pytest.mark.parametrize('values', [
    {'provider':'other'}, {'base_url':'https://other.test'}, {'provider':'gitlab','base_url':'http://gitlab.com'},
    {'provider':'gitlab','base_url':'https://user:pass@gitlab.com'}, {'token':'bad\ntoken'},
    {'provider':'gitlab','base_url':'https://gitlab.com/path'}])
def test_invalid_git_profile_input(tmp_path, values):
    with TestClient(create_app(tmp_path, start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={})
        response = client.post('/api/git-profiles', json={'name':'Profile', **values})
        assert response.status_code == 422
        assert all('input' not in error for error in response.json()['detail'])


def test_token_scope_rejects_other_hosts_and_unsafe_urls():
    profile = {'provider':'github', 'base_url':'https://github.com'}
    assert repository_path(profile, 'https://github.com/team/project.git') == 'team/project'
    for url in ('https://evil.test/team/project.git', 'https://github.com.evil.test/team/project.git',
                'https://user:secret@github.com/team/project', 'https://github.com/team/../other',
                'git@github.com:team/project.git', 'https://github.com/team/project?token=test'):
        with pytest.raises(RemoteError): repository_path(profile, url)


@pytest.mark.parametrize('code', [200, 401, 403, 404, 429, 302])
def test_github_api_authentication_and_no_redirects(monkeypatch, code):
    real_client = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == 'api.github.com'
        assert request.headers['Authorization'] == 'Bearer secret-test-token'
        return httpx.Response(code, json={'login':'developer'}, headers={'Location':'https://other.test/user'})
    monkeypatch.setattr('dashboard.gitprovider.httpx.Client', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    provider = Provider({'provider':'github','base_url':'https://github.com'}, 'secret-test-token')
    if code == 200:
        assert provider.account()['username'] == 'developer'
    else:
        with pytest.raises(RemoteError) as exc: provider.account()
        assert 'secret-test-token' not in str(exc.value)
    assert len(calls) == 1


def test_git_token_uses_stdin_scoped_configuration_not_command_arguments():
    calls = []
    remote = 'https://github.com/team/project.git'
    def text(command):
        return 'git version 2.52.0' if command == 'git --version' else remote
    def run(command, **kwargs):
        calls.append((command,kwargs)); return 0,b'',b''
    conn = SimpleNamespace(git_profile={'provider':'github','base_url':'https://github.com'}, git_token='private-test-token', text=text, run=run)
    gitops.network(conn, 'push origin HEAD', remote)
    command, options = calls[0]
    assert 'private-test-token' not in command and options['input_data'].decode().strip() not in command
    assert options['input_data'].endswith(b'\n')
    assert base64.b64decode(options['input_data']) == b'x-access-token:private-test-token'
    assert 'http.' + remote + '.extraHeader' in command
    assert 'http.followRedirects' in command and 'http.sslVerify' in command and 'credential.helper' in command
    with pytest.raises(RemoteError): gitops.network(conn, 'push origin HEAD', 'https://other.test/team/project.git')
    assert len(calls) == 1


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('openssl'), reason='Linux SSH, Git HTTP backend and test TLS certificate')
def test_real_profile_attach_commit_push_pull_and_read_access(tmp_path, monkeypatch):
    project_dir = tmp_path/'server'; project_dir.mkdir()
    (project_dir/'index.php').write_text('initial')
    with git_https_server(tmp_path/'https') as host:
        monkeypatch.setenv('SSL_CERT_FILE', str(host['ca']))
        monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
        with ssh_server({'GIT_SSL_CAINFO':str(host['ca']), 'NO_PROXY':'127.0.0.1,localhost'}) as (port, key), TestClient(create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
            client.post('/api/unlock', json={})
            profile = client.post('/api/git-profiles', json={'name':'Lab','provider':'gitlab','base_url':host['base'],'token':TOKEN}).json()
            assert client.post('/api/git-profiles/'+profile['id']+'/test').json()['account']['username'] == 'developer'
            repositories = client.get('/api/git-profiles/'+profile['id']+'/repositories').json()
            assert repositories['repositories'][0]['remote'] == host['remote']
            p = client.post('/api/projects', json={'name':'Site','ssh_host':'127.0.0.1','ssh_port':port,'ssh_user':'deploy',
                'ssh_password':'test-password','ssh_path':str(project_dir),'git_profile_id':profile['id'],'git_branch':'main','notify':False}).json()
            prefix = '/api/projects/'+p['id']
            fingerprint = client.get(prefix+'/ssh/fingerprint').json()['fingerprint']
            client.post(prefix+'/ssh/trust', json={'fingerprint':fingerprint})
            assert client.post(prefix+'/git/check').json()['ok'] is False
            state = client.get(prefix+'/git').json()
            payload = {'remote':host['remote'],'snapshot':state['snapshot'],'expected_git_profile_id':profile['id'],'confirm':True}
            assert client.post(prefix+'/git/attach', json=payload | {'confirm':False}).status_code == 409
            assert not (project_dir/'.git').exists()
            attached = client.post(prefix+'/git/attach', json=payload)
            assert attached.status_code == 200, attached.text
            assert (project_dir/'index.php').read_text() == 'initial'
            assert client.post(prefix+'/git/check').json()['ok']
            state = client.get(prefix+'/git').json()
            committed = client.post(prefix+'/git/sync', json={'snapshot':state['snapshot'],'files':['index.php'],'message':'Import','push':True})
            assert committed.status_code == 200 and committed.json()['pushed'], committed.text
            assert subprocess.check_output(['git','-C',str(host['bare']),'show','main:index.php']).decode() == 'initial'
            # A new remote commit is pulled through the same profile without storing credentials.
            clone = tmp_path/'author'
            subprocess.run(['git','clone',str(host['bare']),str(clone)],check=True,capture_output=True)
            (clone/'index.php').write_text('remote edit')
            for args in (['add','index.php'],['-c','user.name=Test','-c','user.email=test@example.com','commit','-m','Remote edit'],['push']):
                subprocess.run(['git','-C',str(clone),*args],check=True,capture_output=True)
            assert client.post(prefix+'/git/pull',json={'confirm':True}).status_code == 200
            assert (project_dir/'index.php').read_text() == 'remote edit'
            assert TOKEN not in (project_dir/'.git/config').read_text()
            assert all(authenticated for _,_,authenticated in host['requests'])
            # Token failure keeps existing origin and files intact.
            client.put('/api/git-profiles/'+profile['id'], json={'name':'Lab','provider':'gitlab','base_url':host['base'],'token':'wrong-token'})
            failed = client.post(prefix+'/git/check')
            assert failed.status_code == 409 and 'wrong-token' not in failed.text
