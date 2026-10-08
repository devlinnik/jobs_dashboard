import uuid
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from dashboard import gitops
from dashboard.app import create_app
from dashboard.remote import RemoteError
from tests.test_git import LocalConnection, git, repo, POSIX_ONLY

pytestmark = POSIX_ONLY


def request(state, **changes):
    return SimpleNamespace(**({'snapshot':state['snapshot'],'message':'Whole project','confirm':True} | changes))


def test_75540_changes_make_one_commit_with_one_add_and_bounded_output(repo):
    work, conn = repo
    for i in range(75540): (work/('file-%05d.bak' % i)).touch()
    conn.project = {'hosting':True}
    conn.git_profile = {'git_name':'Bulk Author','git_email':'bulk@example.com'}
    state = gitops.status(conn)
    assert state['truncated'] and state['total_files'] == 75540
    commands = []; original = conn.run; progress = []
    def record(command, **options):
        commands.append(command)
        return original(command, **options)
    conn.run = record; conn.report_progress = lambda *args:progress.append(args)
    before = git(work,'rev-list','--count','HEAD')
    result = gitops.commit_all(conn, request(state), {'git_name':'Bulk Author','git_email':'bulk@example.com'})
    assert result['commit'] and not result['pushed']
    assert int(git(work,'rev-list','--count','HEAD')) == int(before)+1
    assert len(git(work,'ls-files').splitlines()) == 75541
    assert not git(work,'status','--porcelain')
    adds = [c for c in commands if ' add -A -- ' in c]
    assert len(adds) == 1 and adds[0].endswith(' add -A -- .') and 'index.threads=1' in adds[0]
    assert any(' commit --quiet -m ' in c for c in commands)
    assert any('всех изменений' in p[0] for p in progress)
    assert git(work,'log','-1','--format=%an <%ae>') == 'Bulk Author <bulk@example.com>'


def test_bulk_respects_gitignore_and_includes_deletions_and_tracked_ignored_files(repo):
    work, conn = repo
    (work/'tracked.tmp').write_text('baseline')
    git(work,'add','tracked.tmp'); git(work,'commit','-m','tracked before ignore')
    (work/'.gitignore').write_text('*.tmp\n/ignored/\n')
    (work/'tracked.tmp').write_text('modified tracked file')
    (work/'ignored').mkdir(); (work/'ignored/private.txt').write_text('ignored')
    (work/'skip.tmp').write_text('ignored')
    (work/'vendor').mkdir(); (work/'vendor/library.php').write_text('explicit bulk includes disabled selector paths')
    (work/'index.php').unlink()
    current = gitops.status(conn)
    gitops.commit_all(conn, request(current), {'git_name':'Test','git_email':'test@example.com'})
    assert git(work,'ls-files').splitlines() == ['.gitignore','tracked.tmp','vendor/library.php']
    assert git(work,'show','HEAD:tracked.tmp') == 'modified tracked file'
    assert (work/'skip.tmp').read_text() == 'ignored'
    assert not git(work,'status','--porcelain')


def test_bulk_refuses_unconfirmed_stale_or_prepared_index(repo):
    work, conn = repo
    (work/'new.txt').write_text('new')
    current = gitops.status(conn)
    with pytest.raises(RemoteError, match='Подтвердите'):
        gitops.commit_all(conn, request(current,confirm=False), {})
    (work/'changed.txt').touch()
    with pytest.raises(RemoteError, match='поменялись'):
        gitops.commit_all(conn, request(current), {})
    git(work,'add','new.txt')
    fresh = gitops.status(conn)
    with pytest.raises(RemoteError, match='подготовленные'):
        gitops.commit_all(conn, request(fresh), {})
    assert git(work,'diff','--cached','--name-only') == 'new.txt'


def test_bulk_creates_first_commit_in_unborn_branch(tmp_path):
    work = tmp_path/'first-import'; work.mkdir(); git(work,'init','-b','main')
    for i in range(3000): (work/('file-%04d.bak' % i)).touch()
    conn = LocalConnection(work)
    state = gitops.status(conn)
    assert not state['head'] and state['truncated']
    gitops.commit_all(conn, request(state), {'git_name':'Test','git_email':'test@example.com'})
    assert git(work,'rev-list','--count','HEAD') == '1'
    assert len(git(work,'ls-files').splitlines()) == 3000
    assert not git(work,'status','--porcelain')


def test_failed_hook_keeps_prepared_files_for_recovery(repo):
    work, conn = repo
    hook = work/'.git/hooks/pre-commit'; hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o700)
    (work/'new.txt').write_text('new')
    before = git(work,'rev-parse','HEAD')
    with pytest.raises(RemoteError, match='код 1'):
        gitops.commit_all(conn, request(gitops.status(conn)), {'git_name':'Test','git_email':'test@example.com'})
    assert git(work,'rev-parse','HEAD') == before
    assert git(work,'diff','--cached','--name-only') == 'new.txt'
    assert (work/'new.txt').read_text() == 'new'


def test_bulk_api_is_authenticated_has_progress_and_creates_journal_entry(repo, tmp_path, monkeypatch):
    import importlib
    module = importlib.import_module('dashboard.app')
    work, conn = repo; (work/'new.txt').write_text('new')
    @contextmanager
    def connect(*args, **kwargs): yield conn
    monkeypatch.setattr(module,'connect',connect)
    state = gitops.status(conn)
    with TestClient(create_app(tmp_path/'data',start_monitor=False),headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock',json={'pin':''})
        pid = client.post('/api/projects',json={'name':'Repo'}).json()['id']
        client.put('/api/settings',json={'git_name':'Test','git_email':'test@example.com'})
        endpoint = f'/api/projects/{pid}/git/commit-all'
        body = {'snapshot':state['snapshot'],'message':'Bulk API','confirm':False}
        assert client.post(endpoint,json=body).status_code == 409
        assert not git(work,'diff','--cached','--name-only')
        op = str(uuid.uuid4()); body['confirm'] = True
        response = client.post(endpoint,json=body,headers={'X-Devlin-Operation':op})
        assert response.status_code == 200, response.text
        assert client.get(f'/api/projects/{pid}/git/progress',params={'operation_id':op}).json()['done']
        assert any('из всех изменений' in e['message'] for e in client.get('/api/events',params={'project_id':pid}).json())
        client.post('/api/lock')
        assert client.post(endpoint,json=body).status_code == 401
