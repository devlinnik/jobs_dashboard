import os
from types import SimpleNamespace
import pytest
from dashboard import gitops
from dashboard.remote import RemoteError
from tests.test_git import LocalConnection, git, repo, POSIX_ONLY

pytestmark = POSIX_ONLY


def test_real_status_larger_than_ssh_limit_has_bounded_preview(repo):
    work, conn = repo
    for i in range(14000):
        (work/('file-%05d-' % i + 'x'*175 + '.txt')).touch()
    # Reproduce the exact producer from the user's report, not a large diff.
    import subprocess
    complete = subprocess.check_output(['git','status','--porcelain=v1','-z','--untracked-files=all'], cwd=work)
    assert len(complete) > 2_000_000
    preview, total, shown, staged = gitops.status_preview(conn)
    assert len(preview) <= 800000 and total == 14000 and shown == 2000 and not staged
    current = gitops.status(conn, include_snapshot=False)
    assert current['connected'] and current['truncated'] and current['total_files'] == 14000
    assert len(current['files']) == 2000


def test_long_paths_are_limited_by_bytes_as_well_as_count(repo):
    work, conn = repo
    folder = work
    for _ in range(8): folder = folder/('x'*250)
    folder.mkdir(parents=True)
    for i in range(500): (folder/('file-%03d.bak' % i)).touch()
    preview, total, shown, _ = gitops.status_preview(conn)
    assert total == 500 and 0 < shown < total and len(preview) <= 800000
    current = gitops.status(conn, include_snapshot=False)
    assert current['truncated'] and len(current['files']) == shown


def test_truncated_preview_still_detects_unseen_changes_and_commits_only_selection(repo):
    work, conn = repo
    (work/'a.txt').write_text('selected')
    for i in range(2100): (work/('b-%04d.bak' % i)).touch()
    current = gitops.status(conn)
    assert current['truncated'] and current['total_files'] == 2101
    (work/'zz-hidden.bak').write_text('changed outside preview')
    with pytest.raises(RemoteError, match='изменились'):
        gitops.sync(conn, SimpleNamespace(snapshot=current['snapshot'],files=['a.txt'],message='batch',push=False), {})
    latest = gitops.status(conn)
    result = gitops.sync(conn, SimpleNamespace(snapshot=latest['snapshot'],files=['a.txt'],message='batch',push=False),
                         {'git_name':'Test','git_email':'test@example.com'})
    assert result['commit']
    assert git(work,'show','--pretty=','--name-only','HEAD') == 'a.txt'
    assert gitops.status(conn, include_snapshot=False)['total_files'] == 2101
    assert (work/'zz-hidden.bak').read_text() == 'changed outside preview'


def test_staged_entry_beyond_preview_blocks_accidental_commit(repo):
    work, conn = repo
    for i in range(2100): (work/('a-%04d.bak' % i)).touch()
    git(work,'add','.'); git(work,'commit','-m','tracked baseline')
    for i in range(2100): (work/('a-%04d.bak' % i)).write_text('modified')
    (work/'zz-staged.txt').write_text('already staged')
    git(work,'add','zz-staged.txt')
    current = gitops.status(conn)
    assert all(f['path'] != 'zz-staged.txt' for f in current['files'])
    assert current['staged'] and current['index_snapshot']
    with pytest.raises(RemoteError, match='подготовленные'):
        gitops.commit(conn, current, [], 'must not commit', {})
    assert git(work,'diff','--cached','--name-only') == 'zz-staged.txt'


@pytest.mark.parametrize('count', [1999, 2000])
def test_rename_pair_is_never_split_at_preview_boundary(repo, count):
    work, conn = repo
    for i in range(count): (work/('a-%04d.bak' % i)).touch()
    git(work,'add','.'); git(work,'commit','-m','tracked baseline')
    for i in range(count): (work/('a-%04d.bak' % i)).write_text('modified')
    git(work, 'mv', 'index.php', 'zz-renamed.php')
    current = gitops.status(conn, include_snapshot=False)
    renames = [f for f in current['files'] if 'R' in f['status']]
    assert bool(renames) == (count == 1999)
    if renames: assert renames[0]['old_path'] == 'index.php'
    assert current['total_files'] == count+1 and current['staged']


def test_status_pipeline_does_not_hide_git_failure(tmp_path):
    executable = tmp_path/'git'
    executable.write_text('#!/bin/sh\nprintf "?? a.txt\\000"\nexit 47\n')
    executable.chmod(0o700)
    conn = LocalConnection(tmp_path, {'PATH':str(tmp_path)+os.pathsep+os.environ['PATH'], 'TMPDIR':str(tmp_path)})
    with pytest.raises(RemoteError, match='код 47'):
        gitops.status_preview(conn)
    assert not list(tmp_path.glob('devlin-git-status.*'))
