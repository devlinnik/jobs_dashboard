from types import SimpleNamespace
import pytest
from dashboard import gitops
from dashboard.remote import RemoteError
from tests.test_git import git, repo, LocalConnection, POSIX_ONLY

pytestmark = POSIX_ONLY


def setup_origin(work, tmp_path, unrelated=False):
    bare = tmp_path/'origin.git'; remote_work = tmp_path/'origin-work'
    git(tmp_path,'init','--bare','-b','main',str(bare))
    if unrelated:
        remote_work.mkdir(); git(remote_work,'init','-b','main')
    else:
        git(tmp_path,'clone',str(work),str(remote_work))
    git(remote_work,'config','user.name','Origin Author'); git(remote_work,'config','user.email','origin@example.com')
    (remote_work/'README.md').write_text('origin-only file')
    git(remote_work,'add','.'); git(remote_work,'commit','-m','New origin commit')
    git(remote_work,'remote','add' if unrelated else 'set-url','origin',str(bare))
    git(remote_work,'push','origin','main')
    url = 'https://github.com/test/reconcile.git'
    git(work,'remote','add','origin',url)
    config = tmp_path/'gitconfig'
    config.write_text('[url "'+bare.as_uri()+'"]\n insteadOf = '+url+'\n')
    return bare, remote_work, LocalConnection(work, {'GIT_CONFIG_GLOBAL':str(config),'GIT_ALLOW_PROTOCOL':'file'})


@pytest.mark.parametrize('unrelated', [False, True])
def test_reconcile_preserves_worktree_and_index_and_pushes_without_force(repo, tmp_path, unrelated):
    work, _ = repo
    bare, remote_work, conn = setup_origin(work,tmp_path,unrelated)
    original = git(work,'rev-parse','HEAD'); tip = git(remote_work,'rev-parse','HEAD')
    contents = (work/'index.php').read_bytes(); index = (work/'.git/index').read_bytes()
    plan = gitops.reconcile_plan(conn, {'git_name':'Test','git_email':'test@example.com'})
    assert plan['behind'] > 0 and plan['remote_commits']
    request = SimpleNamespace(**{k:v for k,v in plan.items() if k.startswith('expected_')},confirm=True)
    result = gitops.reconcile_server(conn, request, {'git_name':'Test','git_email':'test@example.com'})
    assert result['ok'] and len(git(work,'log','-1','--format=%P').split()) == 2
    assert git(work,'log','-1','--format=%P').split() == [original,tip]
    assert git(work,'rev-parse','HEAD^{tree}') == git(work,'rev-parse',original+'^{tree}')
    assert (work/'index.php').read_bytes() == contents and (work/'.git/index').read_bytes() == index
    assert not (work/'README.md').exists() and not git(work,'status','--porcelain')
    assert git(work,'merge-base','--is-ancestor',tip,'HEAD') == ''
    gitops.push(conn, settings={'git_name':'Test','git_email':'test@example.com'})
    assert git(bare,'rev-parse','main') == git(work,'rev-parse','HEAD')
    assert git(bare,'show','main:index.php') == 'v1'
    assert (remote_work/'README.md').read_text() == 'origin-only file'


def test_reconcile_requires_clean_project_confirmation_and_unchanged_origin(repo, tmp_path):
    work, _ = repo
    _, remote_work, conn = setup_origin(work,tmp_path,True)
    (work/'new.txt').write_text('uncommitted live file')
    with pytest.raises(RemoteError, match='Создать коммит из всех изменений'):
        gitops.reconcile_plan(conn, {})
    assert (work/'new.txt').read_text() == 'uncommitted live file'
    (work/'new.txt').unlink()
    plan = gitops.reconcile_plan(conn, {})
    request = SimpleNamespace(**{k:v for k,v in plan.items() if k.startswith('expected_')},confirm=False)
    with pytest.raises(RemoteError, match='Подтвердите'):
        gitops.reconcile_server(conn, request, {})
    before = git(work,'rev-parse','HEAD')
    (remote_work/'README.md').write_text('another remote edit'); git(remote_work,'commit','-am','Remote advanced'); git(remote_work,'push','origin','main')
    request.confirm = True
    with pytest.raises(RemoteError, match='изменилась после просмотра'):
        gitops.reconcile_server(conn, request, {})
    assert git(work,'rev-parse','HEAD') == before


def test_compare_and_swap_does_not_overwrite_concurrent_local_commit(repo, tmp_path):
    work, _ = repo
    _, _, conn = setup_origin(work,tmp_path,True)
    plan = gitops.reconcile_plan(conn,{})
    request = SimpleNamespace(**{k:v for k,v in plan.items() if k.startswith('expected_')},confirm=True)
    original_run = conn.run; intervening = []
    def concurrent(command, **options):
        if command.startswith('git update-ref refs/heads/'):
            git(work,'commit','--allow-empty','-m','External commit')
            intervening.append(git(work,'rev-parse','HEAD'))
        return original_run(command, **options)
    conn.run = concurrent
    with pytest.raises(RemoteError): gitops.reconcile_server(conn, request, {})
    assert intervening and git(work,'rev-parse','HEAD') == intervening[0]
