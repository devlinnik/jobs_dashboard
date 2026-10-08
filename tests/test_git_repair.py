"""Real Git repairs keep user files and history and reject stale/unsafe actions."""
import os
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from dashboard import gitops
from dashboard.remote import RemoteError
from tests.test_git import LocalConnection, git, repo, POSIX_ONLY

pytestmark = POSIX_ONLY


def request(report, action, **changes):
    return SimpleNamespace(**({'action':action, 'snapshot':report['repair_snapshot'], 'confirm':True} | changes))


def test_trust_exact_directory_preserves_history_and_worktree(repo, tmp_path):
    work, _ = repo
    config = tmp_path / 'user-gitconfig'
    conn = LocalConnection(work, {'GIT_TEST_ASSUME_DIFFERENT_OWNER':'1', 'GIT_CONFIG_GLOBAL':str(config)})
    before = git(work, 'rev-parse', 'HEAD')
    (work / 'index.php').write_text('unsaved server edits')
    with pytest.raises(RemoteError, match='safe.directory'): gitops.status(conn)
    report = gitops.diagnostics(conn, {})
    assert 'trust-directory' in {a['id'] for a in report['actions']}
    assert gitops.repair(conn, request(report, 'trust-directory'))['ok']
    assert gitops.status(conn)['head'] == before[:12]
    assert (work / 'index.php').read_text() == 'unsaved server edits'
    assert git(work, 'config', '--file', str(config), '--get-all', 'safe.directory') == str(work)
    assert '*' not in config.read_text()
    assert not gitops.recovery(conn)['actions']


def test_repair_requires_confirmation_available_action_and_current_snapshot(repo):
    _, conn = repo
    report = gitops.recovery(conn)
    with pytest.raises(RemoteError, match='Подтвердите'):
        gitops.repair(conn, request(report, 'trust-directory', confirm=False))
    with pytest.raises(RemoteError, match='изменилось'):
        gitops.repair(conn, request(report, 'trust-directory', snapshot='0'*64))
    with pytest.raises(RemoteError, match='недоступно'):
        gitops.repair(conn, request(report, 'trust-directory'))


def test_permission_repair_preserves_object_write_protection(repo):
    work, conn = repo
    objects = [p for p in (work/'.git/objects').rglob('*') if p.is_file()]
    modes = {p:p.stat().st_mode for p in objects}
    assert not gitops.recovery(conn)['actions']
    (work/'.git/refs').chmod(0o500)
    report = gitops.recovery(conn)
    assert 'owner-permissions' in {a['id'] for a in report['actions']}
    before = git(work, 'rev-parse', 'HEAD')
    assert gitops.repair(conn, request(report, 'owner-permissions'))['ok']
    assert (work/'.git/refs').stat().st_mode & 0o700 == 0o700
    assert {p:p.stat().st_mode for p in objects} == modes
    assert git(work, 'rev-parse', 'HEAD') == before


def test_metadata_symlink_prevents_recursive_permission_repair(repo, tmp_path):
    work, conn = repo
    target = tmp_path/'untouched'; target.write_text('private'); target.chmod(0o400)
    (work/'.git/external-link').symlink_to(target)
    (work/'.git/refs').chmod(0o500)
    report = gitops.recovery(conn)
    assert 'owner-permissions' not in {a['id'] for a in report['actions']}
    assert target.stat().st_mode & 0o777 == 0o400


def test_invalid_metadata_is_diagnosed_without_reinitialization(tmp_path):
    work = tmp_path/'broken'; work.mkdir(); (work/'.git').mkdir()
    (work/'.git/important').write_text('preserve')
    report = gitops.diagnostics(LocalConnection(work), {})
    assert not report['actions']
    assert any('не распознана' in c['message'] for c in report['checks'])
    assert (work/'.git/important').read_text() == 'preserve'
    assert not (work/'.git/HEAD').exists()


@pytest.mark.parametrize('remote,expected', [
    ('git@github.com:team/site.git','https://github.com/team/site.git'),
    ('ssh://git@gitlab.example:2222/group/site.git','https://gitlab.example/group/site.git')])
def test_profile_transport_conversion_stays_on_same_service(remote, expected):
    host = 'github.com' if 'github.com' in remote else 'gitlab.example'
    assert gitops.profile_https_url({'base_url':'https://'+host,'provider':'github' if host=='github.com' else 'gitlab'},remote) == expected
    with pytest.raises(RemoteError):
        gitops.profile_https_url({'base_url':'https://different.example','provider':'gitlab'},remote)


def test_profile_repair_checks_access_before_replacing_origin(repo, monkeypatch):
    work, conn = repo
    conn.git_profile = {'id':'profile', 'base_url':'https://github.com','provider':'github'}
    conn.git_token = 'do-not-expose-token'
    ssh = 'git@github.com:team/site.git'; https = 'https://github.com/team/site.git'
    git(work,'remote','add','origin',ssh)
    before = git(work,'rev-parse','HEAD')
    monkeypatch.setattr(gitops.Provider, 'repository', lambda *args: {})
    monkeypatch.setattr(gitops, 'network', lambda *args: (128,b'',b'Authentication failed'))
    report = gitops.recovery(conn)
    assert 'profile-https' in {a['id'] for a in report['actions']}
    with pytest.raises(RemoteError, match='origin сохранён'):
        gitops.repair(conn, request(report,'profile-https'))
    assert git(work,'remote','get-url','origin') == ssh
    def checked_access(*args):
        # Git's cache refresh can touch directory mtime without changing the
        # repository identity or proposed repair; this must not make it stale.
        metadata = work/'.git'
        os.utime(metadata, (metadata.stat().st_atime, metadata.stat().st_mtime + 10))
        return 0,b'',b''
    monkeypatch.setattr(gitops, 'network', checked_access)
    assert gitops.repair(conn, request(report,'profile-https'))['ok']
    assert git(work,'remote','get-url','origin') == https
    assert git(work,'rev-parse','HEAD') == before
    assert not gitops.recovery(conn)['actions']


def test_repair_api_checks_confirmation_pin_and_project_binding(repo, tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('dashboard.app')
    _, conn = repo
    conn.env.update(GIT_TEST_ASSUME_DIFFERENT_OWNER='1', GIT_CONFIG_GLOBAL=str(tmp_path/'global-config'))
    @contextmanager
    def connect(store, project, **kwargs):
        conn.project = project
        yield conn
    monkeypatch.setattr(module, 'connect', connect)
    with TestClient(module.create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={'pin':''})
        project = client.post('/api/projects', json={'name':'Repo','ssh_path':conn.path}).json()
        other = client.post('/api/projects', json={'name':'Other','ssh_path':conn.path}).json()
        endpoint = f"/api/projects/{project['id']}/git"
        report = client.get(endpoint+'/diagnostics').json()
        body = {'action':'trust-directory', 'snapshot':report['repair_snapshot'], 'confirm':False}
        assert client.post(endpoint+'/repair', json=body).status_code == 409
        body['confirm'] = True
        assert client.post(f"/api/projects/{other['id']}/git/repair", json=body).status_code == 409
        client.post('/api/lock')
        assert client.post(endpoint+'/repair', json=body).status_code == 401
        client.post('/api/unlock', json={'pin':''})
        assert client.post(endpoint+'/repair', json=body).status_code == 200
        assert client.get(endpoint).status_code == 200
