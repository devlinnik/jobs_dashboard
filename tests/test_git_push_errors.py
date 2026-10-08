from types import SimpleNamespace
import json
import pytest
from dashboard import gitops
from dashboard.remote import RemoteError, command_failure
from dashboard.gitdebug import trace_commands
from tests.test_git import repo, git, POSIX_ONLY


@pytest.mark.parametrize(('output','expected'), [
    ('remote: error: GH001: Large files detected.\n! [remote rejected] main -> main (pre-receive hook declined)', '100 МиБ'),
    ('remote: File private.bin is 120 MB; this exceeds GitHub file size limit', 'из истории'),
    ('remote: refusing to allow a Personal Access Token to create or update workflow without workflow scope\n[remote rejected]', 'Workflows'),
    ('remote: Write access to repository not granted.\nfatal: requested URL returned error: 403', 'Contents: Read and write'),
    ('remote: Permission to private/repo denied to user.', 'запись запрещена'),
    ('remote: You are not allowed to push code to this project.\n[remote rejected]', 'write_repository'),
    ('remote: error: GH006: Protected branch update failed\n[remote rejected]', 'защитой ветки'),
    ('remote: error: GH013: Repository rule violations found\n[remote rejected]', 'запрет секретов'),
    ('! [rejected] main -> main (fetch first)', 'Объединить историю'),
    ('error: RPC failed; HTTP 413\nfatal: remote end hung up unexpectedly', 'прокси'),
    ('fatal: unable to create thread: Resource temporarily unavailable', 'потоков или процессов'),
    ('fatal: pack-objects: malloc failed: Cannot allocate memory', 'не хватило памяти'),
    ('! [remote rejected] main -> main (pre-receive hook declined)', 'проверка не распознана'),
])
def test_push_reasons_are_specific_and_do_not_disclose_server_output(output, expected):
    message = str(command_failure(1, (output+'\nhttps://user:private-token@host.test/private/repo\nprivate-file-content').encode(), 'Отправка коммитов в origin'))
    assert expected in message
    assert not any(s in message for s in ('private-token','private-file-content','private/repo','private.bin'))


@pytest.mark.parametrize('profile', [False, True])
@pytest.mark.parametrize('hosting', [False, True])
def test_network_pack_limits_apply_for_hosting_without_persistent_config(profile, hosting):
    remote = 'https://github.com/team/project.git'; calls=[]
    conn = SimpleNamespace(project={'hosting':hosting},
        git_profile={'provider':'github','base_url':'https://github.com'} if profile else None,
        git_token='private-token', text=lambda cmd:'git version 2.52.0' if cmd=='git --version' else remote,
        run=lambda cmd,**kwargs:(calls.append((cmd,kwargs)) or (0,b'',b'')))
    gitops.network(conn,'push origin HEAD',remote)
    command,options = calls[0]
    assert ('pack.threads=1' in command) == hosting
    assert ('pack.windowMemory=32m' in command) == hosting
    assert options['timeout'] == 600
    assert 'private-token' not in command and '--local' not in command


@POSIX_ONLY
@pytest.mark.parametrize('merged_stdout', [False, True])
def test_push_preserves_actual_failure_in_api_error_and_debug_report(repo, monkeypatch, merged_stdout):
    work,conn=repo; remote='https://github.com/test/repo.git'
    git(work,'remote','add','origin',remote)
    monkeypatch.setattr(gitops,'fetch_commits',lambda *a,**k:{'ahead':1,'behind':0,'head':git(work,'rev-parse','HEAD'),'branch':'main'})
    original=conn.run; records=[]; before=git(work,'rev-parse','HEAD')
    def run(command,**options):
        if ' push origin ' in command:
            error=b'remote: error: GH001: Large files detected.\n[remote rejected]\nhttps://user:private-token@host.test/repo'
            return (1,error,b'') if merged_stdout else (1,b'',error)
        return original(command,**options)
    conn.run=run
    with trace_commands(conn,records.append,'push','test-trace'):
        with pytest.raises(RemoteError,match='100 МиБ'): gitops.push(conn)
    assert git(work,'rev-parse','HEAD')==before
    row=next(r for r in records if r['command']=='git push')
    assert row['exit_code']==1 and '100 МиБ' in row['reason']
    assert 'private-token' not in json.dumps(records)
