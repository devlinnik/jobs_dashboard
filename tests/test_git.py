import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from dashboard import gitops
from dashboard.remote import Connection, RemoteError

POSIX_ONLY = pytest.mark.skipif(os.name == 'nt', reason='Remote Git commands target a Linux/Unix SSH server')


class LocalConnection(Connection):
    """Execute real Git in isolated local repositories instead of an SSH shell."""
    def __init__(self, path, env=None):
        self.path = str(path)
        self.env = os.environ | (env or {})

    def run(self, command, timeout=35, max_output=2_000_000):
        result = subprocess.run(command, shell=True, cwd=self.path, capture_output=True, timeout=timeout, env=self.env)
        assert len(result.stdout) + len(result.stderr) <= max_output
        return result.returncode, result.stdout, result.stderr


def git(path, *args, env=None):
    return subprocess.check_output(['git', '-C', str(path), *args],env=os.environ | (env or {}),stderr=subprocess.STDOUT).decode().strip()


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / 'project'; work.mkdir()
    git(work,'init','-b','main')
    git(work,'config','user.name','Test')
    git(work,'config','user.email','test@example.com')
    (work/'index.php').write_text('v1')
    git(work,'add','index.php'); git(work,'commit','-m','initial')
    return work, LocalConnection(work)


@POSIX_ONLY
def test_real_git_selective_commit_preserves_unselected_files(repo):
    work, conn = repo
    (work/'index.php').write_text('v2')
    (work/'style.css').write_text('body{}')
    (work/'.env').write_text('SECRET=dont-upload')
    current = gitops.status(conn)
    assert current['connected'] and current['branch'] == 'main'
    assert not next(f for f in current['files'] if f['path'] == '.env')['allowed']
    request = SimpleNamespace(snapshot=current['snapshot'],files=['index.php'],message='server edit',push=False)
    result = gitops.sync(conn,request,{'git_name':'Desk','git_email':'desk@example.com'})
    assert result['commit'] and not result['pushed']
    assert git(work,'show','HEAD:index.php') == 'v2'
    assert set(f['path'] for f in gitops.status(conn)['files']) == {'.env','style.css'}


@POSIX_ONLY
def test_snapshot_detects_content_changes_and_staged_is_preserved(repo):
    work, conn = repo
    (work/'index.php').write_text('v2')
    old = gitops.status(conn)
    (work/'index.php').write_text('v3')
    with pytest.raises(RemoteError,match='после просмотра'):
        gitops.sync(conn,SimpleNamespace(snapshot=old['snapshot'],files=['index.php'],message='update',push=False),{'git_name':'Desk','git_email':'desk@example.com'})
    git(work,'add','index.php')
    fresh = gitops.status(conn)
    with pytest.raises(RemoteError,match='staged'):
        gitops.sync(conn,SimpleNamespace(snapshot=fresh['snapshot'],files=['index.php'],message='update',push=False),{'git_name':'Desk','git_email':'desk@example.com'})
    assert git(work,'diff','--cached','--name-only') == 'index.php'


@POSIX_ONLY
def test_initialize_and_push_to_real_bare_repository(tmp_path):
    work = tmp_path/'server'; work.mkdir()
    (work/'index.php').write_text('<?php echo "hello";')
    (work/'.env').write_text('secret')
    (work/'node_modules').mkdir(); (work/'node_modules'/'dependency.js').write_text('excluded')
    bare = tmp_path/'gitlab.git'
    subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
    config = tmp_path/'git-test.conf'
    fake_url = 'git@gitlab.test:team/project.git'
    config.write_text('[url "'+bare.as_uri()+'"]\n\tinsteadOf = '+fake_url+'\n')
    env = {'GIT_CONFIG_GLOBAL':str(config),'GIT_ALLOW_PROTOCOL':'file:ssh'}
    conn = LocalConnection(work,env)
    initial = gitops.status(conn)
    assert not initial['connected']
    assert 'node_modules/dependency.js' not in [f['path'] for f in initial['files']]
    result = gitops.initialize(conn,SimpleNamespace(remote=fake_url,branch='main',snapshot=initial['snapshot'],
        files=['index.php'],message='Initial import',push=True),{'git_name':'Desk','git_email':'desk@example.com'})
    assert result['pushed']
    assert git(bare,'show','main:index.php') == '<?php echo "hello";'
    assert git(bare,'ls-tree','--name-only','main') == 'index.php'


@POSIX_ONLY
def test_pull_requires_clean_tree_and_parent_repo_is_rejected(repo):
    work,conn = repo
    (work/'index.php').write_text('server edit')
    with pytest.raises(RemoteError,match='без локальных изменений'):
        gitops.pull(conn)
    sub = work/'child'; sub.mkdir()
    with pytest.raises(RemoteError,match='другого репозитория'):
        gitops.status(LocalConnection(sub))
    assert (work/'index.php').read_text() == 'server edit'


@pytest.mark.parametrize('path',['../outside','/etc/passwd','.env','.env.production','id_rsa','cert.pem','db.sqlite','node_modules/x.js','.ssh/config','a\nb'])
def test_secret_and_unsafe_files_rejected(path):
    assert not gitops.safe_file(path)


@pytest.mark.parametrize('remote',['https://user:token@gitlab.test/a/b.git','file:///tmp/repo','-exec','git@host:path\ncommand'])
def test_credential_urls_and_unsafe_remotes_rejected(remote):
    with pytest.raises(RemoteError):
        gitops.validate_remote(remote)
