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
def test_attach_existing_git_and_confirmed_origin_replacement_preserve_history(repo, tmp_path):
    work, original = repo
    first, second = tmp_path/'first.git', tmp_path/'second.git'
    for path in (first, second): git(tmp_path, 'init', '--bare', str(path))
    config = tmp_path/'git.config'
    urls = ['git@gitlab.test:team/first.git', 'git@gitlab.test:team/second.git']
    config.write_text(''.join('[url "'+path.as_uri()+'"]\n\tinsteadOf = '+url+'\n' for path,url in zip((first,second),urls)))
    conn = LocalConnection(work, {'GIT_CONFIG_GLOBAL':str(config), 'GIT_ALLOW_PROTOCOL':'file:ssh'})
    before = git(work, 'rev-parse', 'HEAD')
    (work/'index.php').write_text('uncommitted server edits')
    current = gitops.status(conn)
    request = SimpleNamespace(remote=urls[0], branch='main', snapshot=current['snapshot'], expected_remote='',
                              expected_git_profile_id='', replace_remote=False, confirm=True)
    assert gitops.attach(conn, request)['ok']
    assert git(work,'config','--get','remote.origin.url') == urls[0]
    git(work, 'config', 'remote.origin.pushurl', urls[0])
    current = gitops.status(conn)
    request.remote = urls[1]; request.snapshot = current['snapshot']; request.expected_remote = urls[0]
    with pytest.raises(RemoteError, match='Подтвердите замену'): gitops.attach(conn, request)
    assert git(work,'config','--get','remote.origin.url') == urls[0]
    request.replace_remote = True
    assert gitops.attach(conn, request)['ok']
    assert git(work,'config','--get','remote.origin.url') == urls[1]
    assert subprocess.run(['git','-C',str(work),'config','--get','remote.origin.pushurl'],capture_output=True).returncode == 1
    assert git(work,'rev-parse','HEAD') == before
    assert (work/'index.php').read_text() == 'uncommitted server edits'


@POSIX_ONLY
def test_attach_rejects_stale_files_before_changing_origin(repo):
    work, conn = repo
    request = SimpleNamespace(remote='https://github.com/team/project.git', branch='main',
        snapshot=gitops.status(conn)['snapshot'], expected_remote='', expected_git_profile_id='', replace_remote=False, confirm=True)
    (work/'index.php').write_text('changed after viewing')
    with pytest.raises(RemoteError, match='Состояние репозитория изменилось'): gitops.attach(conn, request)
    assert not gitops.status(conn)['remote']


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
def test_snapshot_hashes_links_without_reading_targets_and_commits_first_import(tmp_path):
    work = tmp_path/'project'; work.mkdir()
    git(work,'init','-b','main')
    outside = tmp_path/'outside'; outside.mkdir()
    (outside/'private.txt').write_text('do not fingerprint target content')
    (work/'broken-link').symlink_to('missing-target')
    (work/'directory-link').symlink_to(outside, target_is_directory=True)
    conn = LocalConnection(work)
    before = gitops.status(conn)
    assert {f['path'] for f in before['files']} == {'broken-link','directory-link'}
    (outside/'private.txt').write_text('target changed independently')
    assert gitops.status(conn)['snapshot'] == before['snapshot']
    (work/'broken-link').unlink(); (work/'broken-link').symlink_to('another-missing-target')
    after = gitops.status(conn)
    assert after['snapshot'] != before['snapshot']
    request = SimpleNamespace(snapshot=after['snapshot'],files=['broken-link','directory-link'],message='Links',push=False)
    result = gitops.sync(conn,request,{'git_name':'Desk','git_email':'desk@example.com'})
    assert result['commit'] and not result['pushed']
    assert git(work,'show','HEAD:broken-link') == 'another-missing-target'
    assert git(work,'show','HEAD:directory-link') == str(outside)
    assert 'private.txt' not in git(work,'ls-tree','-r','--name-only','HEAD')


@POSIX_ONLY
def test_snapshot_retains_trailing_newlines_in_link_target(tmp_path):
    git(tmp_path,'init','-b','main')
    link = tmp_path/'link'; link.symlink_to('missing\n')
    conn = LocalConnection(tmp_path)
    before = gitops.status(conn)['snapshot']
    link.unlink(); link.symlink_to('missing')
    assert gitops.status(conn)['snapshot'] != before


@POSIX_ONLY
def test_snapshot_does_not_run_clean_filters_but_staging_reports_failure(tmp_path):
    git(tmp_path,'init','-b','main')
    (tmp_path/'.gitattributes').write_text('index.php filter=broken\n')
    (tmp_path/'index.php').write_text('server content')
    git(tmp_path,'config','filter.broken.clean','echo SECRET_TOKEN >&2; exit 128')
    git(tmp_path,'config','filter.broken.required','true')
    conn = LocalConnection(tmp_path)
    state = gitops.status(conn)
    assert state['snapshot']  # Raw preview must not execute the filter.
    with pytest.raises(RemoteError,match='Добавление файлов в индекс Git: index.php') as error:
        gitops.sync(conn,SimpleNamespace(snapshot=state['snapshot'],files=['index.php'],message='Commit',push=False),
                    {'git_name':'Desk','git_email':'desk@example.com'})
    assert 'Git-фильтр' in str(error.value) and 'SECRET_TOKEN' not in str(error.value)
    assert (tmp_path/'index.php').read_text() == 'server content'


@POSIX_ONLY
def test_snapshot_error_identifies_file_and_preserves_staged_changes(repo):
    work, original = repo
    (work/'index.php').write_text('staged edits'); git(work,'add','index.php')
    class Unreadable(LocalConnection):
        def run(self, command, **kwargs):
            if 'DEVLIN_HASH_ERROR' in command:
                return 128, b'file:\nDEVLIN_HASH_ERROR:1\n', b'fatal: permission denied SECRET_TOKEN'
            return super().run(command, **kwargs)
    with pytest.raises(RemoteError,match='Проверка содержимого файла «index.php»') as error:
        gitops.status(Unreadable(work))
    assert 'SECRET_TOKEN' not in str(error.value)
    assert git(work,'diff','--cached','--name-only') == 'index.php'


@POSIX_ONLY
def test_commit_hook_failure_reports_stage_and_keeps_index(repo):
    work, conn = repo
    hook = work/'.git/hooks/pre-commit'; hook.write_text('#!/bin/sh\necho SECRET_TOKEN >&2\nexit 128\n'); hook.chmod(0o755)
    (work/'index.php').write_text('server edits')
    state = gitops.status(conn)
    with pytest.raises(RemoteError,match='Создание коммита из подготовленных файлов') as error:
        gitops.sync(conn,SimpleNamespace(snapshot=state['snapshot'],files=['index.php'],message='Commit',push=False),
                    {'git_name':'Desk','git_email':'desk@example.com'})
    assert 'SECRET_TOKEN' not in str(error.value)
    assert git(work,'diff','--cached','--name-only') == 'index.php'
    assert git(work,'log','-1','--format=%s') == 'initial'


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
def test_initial_import_respects_existing_gitignore_and_preserves_files(tmp_path):
    work = tmp_path/'project'; work.mkdir()
    (work/'.gitignore').write_text('cache/\n*.log\n')
    (work/'cache').mkdir(); (work/'cache/result.txt').write_text('keep on server')
    (work/'server.log').write_text('keep log')
    (work/'index.php').write_text('site')
    config = tmp_path/'git.config'; config.write_text('')
    conn = LocalConnection(work, {'GIT_CONFIG_GLOBAL':str(config), 'GIT_CONFIG_NOSYSTEM':'1'})
    initial = gitops.status(conn)
    assert {f['path'] for f in initial['files']} == {'.gitignore','index.php'}
    assert not (work/'.git').exists()  # A preview must not initialize the project.
    result = gitops.initialize(conn, SimpleNamespace(remote='https://github.com/team/project.git',branch='main',
        snapshot=initial['snapshot'],files=['.gitignore','index.php'],message='Import',push=False),
        {'git_name':'Fallback Author','git_email':'fallback@example.com'})
    assert not result['pushed'] and result['author']['source'] == 'Настройки приложения'
    assert git(work, 'log', '-1', '--format=%an <%ae>') == 'Fallback Author <fallback@example.com>'
    assert (work/'cache/result.txt').read_text() == 'keep on server'
    assert (work/'server.log').read_text() == 'keep log'
    assert gitops.status(conn)['files'] == []


@POSIX_ONLY
def test_author_profile_precedence_preserves_server_configuration(repo):
    work, conn = repo
    settings = {'git_name':'App Author','git_email':'app@example.com'}
    assert gitops.author_identity(conn, settings)['name'] == 'Test'
    conn.git_profile = {'git_name':'Profile Author', 'git_email':'profile@example.com'}
    (work/'index.php').write_text('changed')
    state = gitops.status(conn)
    result = gitops.sync(conn, SimpleNamespace(snapshot=state['snapshot'],files=['index.php'],message='Commit',push=False), settings)
    assert result['author']['source'] == 'Git-профиль'
    assert git(work,'log','-1','--format=%an <%ae>') == 'Profile Author <profile@example.com>'
    assert git(work,'config','--local','--get','user.name') == 'Test'
    assert git(work,'config','--local','--get','user.email') == 'test@example.com'


@POSIX_ONLY
def test_invalid_existing_git_metadata_is_not_reinitialized(tmp_path):
    work = tmp_path/'project'; work.mkdir()
    (work/'.git').mkdir(); (work/'.git/keep.txt').write_text('existing metadata')
    with pytest.raises(RemoteError, match='уже есть .git'):
        gitops.status(LocalConnection(work))
    assert (work/'.git/keep.txt').read_text() == 'existing metadata'


@POSIX_ONLY
def test_outgoing_commits_are_listed_and_sent_separately(repo, tmp_path, monkeypatch):
    work, _ = repo
    bare = tmp_path/'origin.git'; git(tmp_path,'init','--bare','-b','main',str(bare))
    config = tmp_path/'git.config'
    remote = 'git@gitlab.test:team/project.git'
    config.write_text('[url "'+bare.as_uri()+'"]\n\tinsteadOf = '+remote+'\n')
    conn = LocalConnection(work, {'GIT_CONFIG_GLOBAL':str(config),'GIT_ALLOW_PROTOCOL':'file:ssh'})
    git(work,'remote','add','origin',remote)
    settings = {'git_name':'App Author','git_email':'app@example.com'}
    for value in ('second', 'third'):
        (work/'index.php').write_text(value)
        state = gitops.status(conn)
        result = gitops.sync(conn, SimpleNamespace(snapshot=state['snapshot'],files=['index.php'],message=value,push=False), settings)
        assert not result['pushed']
    assert subprocess.run(['git','-C',str(bare),'rev-parse','--verify','main'],capture_output=True).returncode != 0
    outgoing = gitops.fetch_commits(conn, settings)
    assert outgoing['ahead'] == 3 and outgoing['behind'] == 0
    assert [c['subject'] for c in outgoing['outgoing']] == ['third','second','initial']
    request = SimpleNamespace(expected_head=outgoing['head'],expected_branch='main',expected_remote=remote)
    assert gitops.push(conn, request, settings)['message']
    assert git(bare,'show','main:index.php') == 'third'
    assert gitops.fetch_commits(conn, settings)['ahead'] == 0
    # A commit created after confirmation must never be silently included in push.
    (work/'index.php').write_text('after preview')
    state = gitops.status(conn)
    gitops.sync(conn, SimpleNamespace(snapshot=state['snapshot'],files=['index.php'],message='new',push=False), settings)
    with pytest.raises(RemoteError, match='изменились после просмотра'): gitops.push(conn, request, settings)
    assert git(bare,'show','main:index.php') == 'third'
    reviewed = gitops.fetch_commits(conn, settings)
    request.expected_head = reviewed['head']
    original_network = gitops.network
    def concurrent_commit(connection, arguments, remote_url):
        if arguments.startswith('push '):
            (work/'index.php').write_text('concurrent unreviewed commit')
            git(work,'add','index.php'); git(work,'commit','-m','Concurrent server commit')
        return original_network(connection, arguments, remote_url)
    monkeypatch.setattr(gitops, 'network', concurrent_commit)
    gitops.push(conn, request, settings)
    assert git(bare,'rev-parse','main') == reviewed['head']
    assert git(bare,'show','main:index.php') == 'after preview'
    assert gitops.fetch_commits(conn, settings)['ahead'] == 1


@POSIX_ONLY
def test_nonempty_origin_history_blocks_push_and_preserves_server_files(repo, tmp_path):
    work, _ = repo
    bare = tmp_path/'origin.git'; git(tmp_path,'init','--bare','-b','main',str(bare))
    remote = 'git@gitlab.test:team/project.git'
    config = tmp_path/'git.config'
    config.write_text('[url "'+bare.as_uri()+'"]\n\tinsteadOf = '+remote+'\n')
    conn = LocalConnection(work, {'GIT_CONFIG_GLOBAL':str(config),'GIT_ALLOW_PROTOCOL':'file:ssh'})
    git(work,'remote','add','origin',remote)
    # The remote has independent history; attachment must never replace either tree.
    other = tmp_path/'other'; other.mkdir()
    git(other,'init','-b','main')
    git(other,'config','user.name','Remote Author'); git(other,'config','user.email','remote@example.com')
    (other/'README.md').write_text('existing remote project')
    git(other,'add','.'); git(other,'commit','-m','Existing remote history')
    git(other,'push',str(bare),'main')
    (work/'index.php').write_text('uncommitted server files')
    head = git(work,'rev-parse','HEAD')
    settings = {'git_name':'App Author','git_email':'app@example.com'}
    state = gitops.fetch_commits(conn, settings)
    assert state['ahead'] == 1 and state['behind'] == 1
    assert state['outgoing'][0]['subject'] == 'initial'
    with pytest.raises(RemoteError, match='origin'):
        gitops.push(conn, SimpleNamespace(expected_head=head,expected_branch='main',expected_remote=remote), settings)
    assert git(work,'rev-parse','HEAD') == head
    assert (work/'index.php').read_text() == 'uncommitted server files'
    assert not (work/'README.md').exists()
    assert git(bare,'show','main:README.md') == 'existing remote project'


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


@POSIX_ONLY
@pytest.mark.parametrize('setting', ['core.sshCommand', 'GIT_SSH_COMMAND', 'default'])
def test_network_uses_server_identity_and_noninteractive_ssh_options(repo, tmp_path, setting):
    import json
    import shlex
    import sys
    work, _ = repo
    log = tmp_path / 'ssh-arguments.json'
    script = tmp_path / 'fake ssh.py'
    script.write_text('import json, os, sys\nopen(os.environ["DEVLIN_SSH_LOG"], "w").write(json.dumps(sys.argv[1:]))\n', encoding='utf-8')
    command = shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' -i ' + shlex.quote('/server/key with spaces')
    env = {'DEVLIN_SSH_LOG': str(log), 'GIT_SSH_VARIANT': 'ssh', 'GIT_SSH_COMMAND': '', 'GIT_CONFIG_GLOBAL': os.devnull}
    if setting == 'core.sshCommand':
        git(work, 'config', '--local', 'core.sshCommand', command)
    elif setting == 'GIT_SSH_COMMAND':
        git(work, 'config', '--local', 'core.sshCommand', 'false')  # environment has priority
        env['GIT_SSH_COMMAND'] = command
    else:
        ssh = tmp_path / 'ssh'
        ssh.write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
        ssh.chmod(0o700)
        env['PATH'] = str(tmp_path) + os.pathsep + os.environ['PATH']
    remote = 'git@github.com:owner/project.git'
    code, _, _ = gitops.network(LocalConnection(work, env), 'ls-remote ' + shlex.quote(remote), remote)
    # The fake transport deliberately does not speak the Git protocol, but real
    # Git must invoke it with the selected identity and literal argument boundaries.
    assert code != 0
    arguments = json.loads(log.read_text())
    assert arguments[:2] == ['-i', '/server/key with spaces']
    assert 'BatchMode=yes' in arguments and 'StrictHostKeyChecking=yes' in arguments
    assert 'git@github.com' in arguments


@POSIX_ONLY
@pytest.mark.parametrize('operation', ['check', 'attach'])
@pytest.mark.parametrize(('stderr', 'message'), [
    ('Permission denied (publickey).', 'Нет доступа'),
    ('Host key verification failed.', 'не подтверждён ключ'),
    ('Could not resolve hostname github.com.', 'DNS'),
])
def test_repository_access_reports_safe_specific_errors(repo, monkeypatch, operation, stderr, message):
    work, conn = repo
    remote = 'git@github.com:owner/project.git'
    git(work, 'remote', 'add', 'origin', remote)
    current = gitops.status(conn)
    monkeypatch.setattr(gitops, 'network', lambda *args: (128, b'', (stderr + '\nhttps://secret-token@example.test/private').encode()))
    request = SimpleNamespace(remote=remote, branch='main', snapshot=current['snapshot'], expected_remote=remote,
                              expected_git_profile_id='', replace_remote=False, confirm=True)
    with pytest.raises(RemoteError) as raised:
        gitops.check_remote(conn) if operation == 'check' else gitops.attach(conn, request)
    assert message in str(raised.value)
    assert 'secret-token' not in str(raised.value)
    assert git(work, 'remote', 'get-url', 'origin') == remote


@POSIX_ONLY
@pytest.mark.parametrize('accessible', [True, False])
def test_attach_selected_server_key_only_persists_after_success(repo, tmp_path, monkeypatch, accessible):
    import shlex
    work, conn = repo
    key = tmp_path / "server key's name"
    key.write_text('fixture: no private key contents are read')
    old = 'https://github.com/owner/project.git'
    new = 'git@github.com:owner/project.git'
    git(work, 'remote', 'add', 'origin', old)
    before = git(work, 'rev-parse', 'HEAD')
    current = gitops.status(conn)
    def transport(connection, arguments, remote):
        command = shlex.split(connection.server_git_ssh_command)
        assert command[command.index('-i') + 1] == str(key)
        assert 'IdentitiesOnly=yes' in command
        return (0 if accessible else 128), b'', b'Permission denied (publickey).'
    monkeypatch.setattr(gitops, 'network', transport)
    request = SimpleNamespace(remote=new, branch='main', snapshot=current['snapshot'], expected_remote=old,
                              expected_git_profile_id='', replace_remote=True, confirm=True, server_ssh_key=str(key))
    if accessible:
        assert gitops.attach(conn, request)['ok']
        assert git(work, 'remote', 'get-url', 'origin') == new
        assert gitops.access_settings(conn)['server_ssh_key'] == str(key)
    else:
        with pytest.raises(RemoteError, match='Нет доступа'):
            gitops.attach(conn, request)
        assert git(work, 'remote', 'get-url', 'origin') == old
        assert not gitops.access_settings(conn)['command_configured']
    assert git(work, 'rev-parse', 'HEAD') == before
    assert (work / 'index.php').read_text() == 'v1'


@POSIX_ONLY
def test_server_key_paths_missing_public_and_shell_characters(repo, tmp_path):
    work, conn = repo
    with pytest.raises(RemoteError, match='абсолютный путь'):
        gitops.prepare_server_key(conn, 'relative/path')
    with pytest.raises(RemoteError, match='не найден'):
        gitops.prepare_server_key(conn, str(tmp_path / 'missing'))
    public = tmp_path / 'key.pub'; public.write_text('ssh-ed25519 fixture')
    with pytest.raises(RemoteError, match='без .pub'):
        gitops.prepare_server_key(conn, str(public))
    key = tmp_path / '$(touch marker); key'; key.write_text('fixture')
    assert gitops.prepare_server_key(conn, str(key))
    assert not (work / 'marker').exists()
    with pytest.raises(RemoteError, match='абсолютный путь'):
        gitops.prepare_server_key(conn, str(key) + '\ncommand')


@POSIX_ONLY
def test_access_settings_and_diagnostics_explain_https_without_exposing_commands(repo):
    work, conn = repo
    git(work, 'remote', 'add', 'origin', 'https://github.com/owner/project.git')
    git(work, 'config', '--local', 'core.sshCommand', 'ssh -i /server/git-key -o ProxyCommand=secret-command')
    access = gitops.access_settings(conn)
    assert access['ssh_url'] == 'git@github.com:owner/project.git'
    assert access['server_ssh_key'] == '/server/git-key'
    report = gitops.diagnostics(conn, {})
    messages = str(report)
    assert 'HTTPS' in messages and 'SSH-ключ здесь не применяется' in messages
    assert '/server/git-key' in messages and 'secret-command' not in messages
