import json
import os
import stat
import subprocess
import zipfile
from pathlib import Path
import pytest
from scripts import package_windows as installer


def release_at(path, version):
    path.mkdir(parents=True)
    (path/'DevlinDesk.exe').write_text(version)
    (path/'_internal').mkdir()
    (path/'_internal/asset.txt').write_text('assets-'+version)
    (path/'install-info.json').write_text(json.dumps({'application':'DevlinDesk'}))
    return path


def test_replace_verified_release_keeps_backup_and_user_data(tmp_path):
    old=release_at(tmp_path/'dist/DevlinDesk','old')
    new=release_at(tmp_path/'build/release','new')
    userdata=tmp_path/'LocalAppData/DevlinDesk'; userdata.mkdir(parents=True)
    (userdata/'vault.key').write_bytes(b'never-touch-vault')
    (userdata/'desk.sqlite3').write_bytes(b'never-touch-settings')
    calls=[]
    backup=installer.promote(new,old,lambda folder: calls.append((folder/'DevlinDesk.exe').read_text()))
    assert calls==['new']
    assert (old/'DevlinDesk.exe').read_text()=='new'
    assert (backup/'DevlinDesk.exe').read_text()=='old'
    assert (userdata/'vault.key').read_bytes()==b'never-touch-vault'
    assert (userdata/'desk.sqlite3').read_bytes()==b'never-touch-settings'
    # Existing rollback releases are retained on a later update, never overwritten.
    newer=release_at(tmp_path/'build/newer','newer')
    second=installer.promote(newer,old,lambda folder: None)
    assert second!=backup and (backup/'DevlinDesk.exe').read_text()=='old'
    assert (second/'DevlinDesk.exe').read_text()=='new'


@pytest.mark.parametrize('failure',['readiness','running'])
def test_preflight_failure_never_replaces_old_release(tmp_path,failure):
    old=release_at(tmp_path/'dist/DevlinDesk','old')
    new=release_at(tmp_path/'build/release','new')
    def reject(): raise installer.InstallError('blocked')
    with pytest.raises(installer.InstallError):
        installer.promote(new,old,lambda folder: reject() if failure=='readiness' else None,
                          lambda: reject() if failure=='running' else None)
    assert (old/'DevlinDesk.exe').read_text()=='old'
    assert not list(old.parent.glob('.DevlinDesk-staging-*'))
    assert not list(old.parent.glob('DevlinDesk.previous-*'))


def test_failed_final_rename_restores_the_previous_release(tmp_path,monkeypatch):
    old=release_at(tmp_path/'dist/DevlinDesk','old')
    new=release_at(tmp_path/'build/release','new')
    original=Path.rename
    def fail_only_publish(path,destination):
        if path.name.startswith('.DevlinDesk-staging-'):
            raise PermissionError('simulated Windows destination lock')
        return original(path,destination)
    monkeypatch.setattr(Path,'rename',fail_only_publish)
    with pytest.raises(PermissionError): installer.promote(new,old,lambda folder:None)
    assert (old/'DevlinDesk.exe').read_text()=='old'
    assert not list(old.parent.glob('.DevlinDesk-staging-*'))


def test_unrelated_destination_is_not_overwritten(tmp_path):
    target=tmp_path/'personal'; target.mkdir(); (target/'document.txt').write_text('preserve')
    new=release_at(tmp_path/'build/release','new')
    with pytest.raises(installer.InstallError,match='unrelated'):
        installer.promote(new,target,lambda folder:None)
    assert (target/'document.txt').read_text()=='preserve'


@pytest.mark.parametrize('name',['../escape.txt','/absolute.txt','root/../../escape.txt','C:/escape.txt','root\\escape.txt'])
def test_source_zip_rejects_unsafe_paths(tmp_path,name):
    archive=tmp_path/'source.zip'
    with zipfile.ZipFile(archive,'w') as z: z.writestr(name,'unsafe')
    with pytest.raises(installer.InstallError): installer.extract_archive(archive,tmp_path/'extract')
    assert not (tmp_path/'escape.txt').exists()


def test_source_zip_rejects_symlinks_and_accepts_single_root(tmp_path):
    bad=tmp_path/'link.zip'
    info=zipfile.ZipInfo('root/link'); info.create_system=3; info.external_attr=(stat.S_IFLNK|0o777)<<16
    with zipfile.ZipFile(bad,'w') as z: z.writestr(info,'/outside')
    with pytest.raises(installer.InstallError,match='Symbolic'):
        installer.extract_archive(bad,tmp_path/'bad')
    good=tmp_path/'source.zip'
    with zipfile.ZipFile(good,'w') as z: z.writestr('jobs_dashboard-main/run.py','valid')
    source=installer.extract_archive(good,tmp_path/'valid')
    assert (source/'run.py').read_text()=='valid'


def test_updater_bundle_is_self_contained_and_records_source_root(tmp_path):
    source=Path(__file__).resolve().parents[1]
    release=release_at(tmp_path/'release','new')
    installer.write_launchers(release,source,source,{'method':'local','commit':'abc'})
    for name in ('install-windows.ps1','package_windows.py','check-running.ps1','launch-update.ps1'):
        assert (release/'_maintenance'/name).is_file()
    launcher=(release/'Update-Devlin.cmd').read_text()
    assert 'start "Devlin Desk Update" powershell.exe' in launcher
    assert 'launch-update.ps1' in launcher
    assert 'pause' not in launcher  # original batch exits before its directory is replaced
    metadata=json.loads((release/'install-info.json').read_text())
    assert metadata['source_directory']==str(source) and metadata['commit']=='abc'


def test_installer_lock_blocks_a_second_update(tmp_path):
    path=tmp_path/'install.lock'
    with installer.FileLock(path):
        with pytest.raises(installer.InstallError,match='Another installer'):
            with installer.FileLock(path): pass
    with installer.FileLock(path): pass


def test_real_git_refresh_is_fast_forward_and_preserves_dirty_work(tmp_path,monkeypatch):
    bare=tmp_path/'remote.git'
    subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
    work=tmp_path/'work'; work.mkdir()
    config=tmp_path/'test-git.conf'
    remote='https://github.com/devlinnik/jobs_dashboard.git'
    config.write_text('[url "'+bare.as_uri()+'"]\n\tinsteadOf = '+remote+'\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL',str(config))
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL','file')
    def git(root,*args):
        return subprocess.check_output(['git','-C',str(root),*args],stderr=subprocess.STDOUT).decode().strip()
    git(work,'init','-b','main'); git(work,'config','user.name','Test'); git(work,'config','user.email','test@example.com')
    (work/'app.txt').write_text('v1'); git(work,'add','.'); git(work,'commit','-m','initial'); git(work,'remote','add','origin',remote); git(work,'push','origin','main')
    other=tmp_path/'other'
    subprocess.run(['git','clone','--branch','main',str(bare),str(other)],check=True,capture_output=True)
    git(other,'config','user.name','Test'); git(other,'config','user.email','test@example.com')
    (other/'app.txt').write_text('v2'); git(other,'add','.'); git(other,'commit','-m','update'); git(other,'push','origin','main')
    installer.refresh_git(work)
    assert (work/'app.txt').read_text()=='v2'
    (work/'app.txt').write_text('local edits')
    (other/'app.txt').write_text('v3'); git(other,'add','.'); git(other,'commit','-m','another'); git(other,'push','origin','main')
    installer.refresh_git(work)
    assert (work/'app.txt').read_text()=='local edits'
    assert git(work,'show','HEAD:app.txt')=='v2'


def test_git_refresh_refuses_divergent_history(tmp_path,monkeypatch):
    root=tmp_path/'repo'; root.mkdir()
    subprocess.run(['git','init','-b','main',str(root)],check=True,capture_output=True)
    # Test the failure contract without any actual network operation.
    def fake_git(root,*args):
        return {('status','--porcelain','--untracked-files=all'):'',('branch','--show-current'):'main',('config','--get','remote.origin.url'):'https://github.com/devlinnik/jobs_dashboard.git'}[args]
    monkeypatch.setattr(installer,'git_output',fake_git)
    commands=[]
    def fake_run(args,**kwargs):
        commands.append(args)
        if 'merge' in args: raise installer.InstallError('not a fast-forward')
    monkeypatch.setattr(installer,'run',fake_run)
    with pytest.raises(installer.InstallError,match='fast-forward'): installer.refresh_git(root)
    assert all('reset' not in args for args in commands)


def test_packaged_verifier_rejects_missing_or_false_report(tmp_path,monkeypatch):
    release=release_at(tmp_path/'release','new')
    monkeypatch.setattr(installer,'run',lambda args,**kwargs:None)
    with pytest.raises(installer.InstallError,match='readiness report'):
        installer.verify_release(release,tmp_path)
    def fake_run(args,**kwargs):
        Path(args[-1]).write_text(json.dumps({'ok':False}))
    monkeypatch.setattr(installer,'run',fake_run)
    with pytest.raises(installer.InstallError,match='startup/asset'):
        installer.verify_release(release,tmp_path)


def test_native_self_test_does_not_use_or_change_user_data(tmp_path,monkeypatch):
    from run import desktop_self_test
    userdata=tmp_path/'userdata'; userdata.mkdir()
    (userdata/'vault.key').write_bytes(b'private')
    (userdata/'desk.sqlite3').write_bytes(b'private-settings')
    monkeypatch.setenv('DEVLIN_DATA_DIR',str(userdata))
    report=tmp_path/'result.json'
    assert desktop_self_test(report)
    assert json.loads(report.read_text())['ok']
    assert (userdata/'vault.key').read_bytes()==b'private'
    assert (userdata/'desk.sqlite3').read_bytes()==b'private-settings'
