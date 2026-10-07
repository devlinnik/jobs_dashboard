"""Build, verify and replace a Windows release without touching application data.

This coordinator uses only the standard library so it can bootstrap its build venv.
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REPOSITORY = 'devlinnik/jobs_dashboard'
ARCHIVE_URL = f'https://codeload.github.com/{REPOSITORY}/zip/refs/heads/main'
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_EXPANDED = 600 * 1024 * 1024


class InstallError(RuntimeError):
    pass


class FileLock:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0, 2)
                if not self.file.tell():
                    self.file.write(b'1'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise InstallError('Another installer/update is running. Wait for it to finish.') from exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


def run(args, **kwargs):
    result = subprocess.run([str(a) for a in args], **kwargs)
    if result.returncode:
        raise InstallError(f'Command failed (exit {result.returncode}): {Path(str(args[0])).name}')
    return result


def git_output(root, *args):
    return run(['git', '-C', root, *args], capture_output=True, text=True).stdout.strip()


def trusted_origin(remote):
    if remote in (f'git@github.com:{REPOSITORY}', f'git@github.com:{REPOSITORY}.git'):
        return True
    parsed = urlsplit(remote)
    return (parsed.scheme == 'https' and parsed.hostname == 'github.com' and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path.rstrip('/').removesuffix('.git') == '/' + REPOSITORY)


def refresh_git(root):
    if not shutil.which('git'):
        print('Git is not installed: building the current local sources.', flush=True)
        return
    # A Git checkout with local work is never reset or replaced by an archive.
    if git_output(root, 'status', '--porcelain', '--untracked-files=all'):
        print('Local changes found: keeping them and building the current sources.', flush=True)
        return
    if git_output(root, 'branch', '--show-current') != 'main':
        print('Checkout is not on main: keeping the current branch and building it.', flush=True)
        return
    if not trusted_origin(git_output(root, 'config', '--get', 'remote.origin.url')):
        raise InstallError('origin is not devlinnik/jobs_dashboard on GitHub. Use local build mode or correct the remote.')
    print('Updating clean main from GitHub (fast-forward only)...', flush=True)
    run(['git', '-C', root, 'fetch', 'origin', 'refs/heads/main'], timeout=180)
    run(['git', '-C', root, 'merge', '--ff-only', 'FETCH_HEAD'], timeout=120)


def extract_archive(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 50000 or sum(x.file_size for x in members) > MAX_EXPANDED:
            raise InstallError('Source archive is too large.')
        roots = set()
        for item in members:
            path = PurePosixPath(item.filename)
            if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in path.parts[0]:
                raise InstallError('Unsafe source archive path.')
            if stat.S_ISLNK(item.external_attr >> 16):
                raise InstallError('Symbolic links in source archives are not allowed.')
            roots.add(path.parts[0])
            if not (destination / item.filename).resolve().is_relative_to(destination.resolve()):
                raise InstallError('Source archive escapes the staging directory.')
        if len(roots) != 1:
            raise InstallError('Expected one root directory in the source archive.')
        bundle.extractall(destination)
    return destination / next(iter(roots))


def download_sources(work):
    archive = work / 'source.zip'
    print('Downloading current main from the official GitHub repository...', flush=True)
    digest = hashlib.sha256()
    with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response, archive.open('xb') as output:
        if urlsplit(response.url).scheme != 'https':
            raise InstallError('Source download was redirected to an insecure URL.')
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE:
                raise InstallError('Source archive exceeds 100 MB.')
            digest.update(chunk)
            output.write(chunk)
    source = extract_archive(archive, work / 'source')
    return source, {'method': 'github-archive', 'archive_sha256': digest.hexdigest()}


def validate_source(source):
    for name in ('run.py', 'requirements-desktop.txt', 'dashboard/static/index.html',
                 'scripts/install-windows.ps1', 'scripts/package_windows.py', 'scripts/check-running.ps1', 'scripts/launch-update.ps1'):
        if not (source / name).is_file():
            raise InstallError(f'Missing source file: {name}')


def ensure_build_python(root):
    venv = root / '.build-venv'
    python = venv / 'Scripts/python.exe'
    if python.exists():
        result = subprocess.run([str(python), '-c', 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'], capture_output=True)
        if result.returncode:
            raise InstallError('Existing .build-venv is not Python 3.12. Rename it and retry; it was not removed.')
    else:
        if venv.exists() and any(venv.iterdir()):
            raise InstallError('Existing .build-venv is incomplete or belongs to another platform. Rename it and retry.')
        run([sys.executable, '-m', 'venv', venv])
    return python


def build(source, root, work):
    python = ensure_build_python(root)
    print('Installing/updating isolated build dependencies...', flush=True)
    run([python, '-m', 'pip', 'install', '-r', source / 'requirements-desktop.txt'], cwd=source)
    run([python, '-m', 'pip', 'check'], cwd=source)
    print('Building DevlinDesk.exe...', flush=True)
    run([python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
         '--name', 'DevlinDesk', '--distpath', work / 'dist', '--workpath', work / 'pyinstaller',
         '--specpath', work, '--add-data', str(source / 'dashboard/static') + os.pathsep + 'dashboard/static',
         '--collect-all', 'webview', '--hidden-import', 'uvicorn.logging',
         '--hidden-import', 'uvicorn.loops.asyncio', '--hidden-import', 'uvicorn.protocols.http.h11_impl',
         '--hidden-import', 'uvicorn.lifespan.on', source / 'run.py'], cwd=source)
    release = work / 'dist/DevlinDesk'
    if not (release / 'DevlinDesk.exe').is_file() or not (release / '_internal').is_dir():
        raise InstallError('Build did not produce a complete executable folder.')
    return release


def write_launchers(release, source, root, metadata):
    maintenance = release / '_maintenance'
    maintenance.mkdir()
    for name in ('install-windows.ps1', 'package_windows.py', 'check-running.ps1', 'launch-update.ps1'):
        shutil.copy2(source / 'scripts' / name, maintenance / name)
    (release / 'Update-Devlin.cmd').write_text('''@echo off
start "Devlin Desk Update" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0_maintenance\\launch-update.ps1" -TargetDirectory "%~dp0." %*
exit /b 0
''', encoding='ascii', newline='\r\n')
    (release / 'Start-Devlin.cmd').write_text('''@echo off
start "" /D "%~dp0." "%~dp0DevlinDesk.exe"
''', encoding='ascii', newline='\r\n')
    manifest = {'application': 'DevlinDesk', 'format': 1, 'source_directory': str(root.resolve()),
                'repository': REPOSITORY, 'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **metadata}
    (release / 'install-info.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def verify_release(release, work):
    report = work / ('self-test-' + uuid.uuid4().hex + '.json')
    run([release / 'DevlinDesk.exe', '--self-test', '--self-test-output', report], timeout=90)
    try:
        result = json.loads(report.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise InstallError('The executable did not produce a readiness report.') from exc
    if result.get('ok') is not True:
        raise InstallError('The built executable failed its startup/asset check.')


def check_running(target, checker):
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', checker, '-TargetDirectory', target])


def known_installation(target):
    if not target.exists():
        return
    if target.is_symlink() or not target.is_dir():
        raise InstallError('The installation destination is not a normal directory.')
    if not any(target.iterdir()):
        return
    marker = target / 'install-info.json'
    if marker.exists():
        try:
            if json.loads(marker.read_text(encoding='utf-8')).get('application') == 'DevlinDesk':
                return
        except (ValueError, OSError):
            pass
    # Allow upgrading the original unmarked PyInstaller release from this project.
    if (target / 'DevlinDesk.exe').is_file() and (target / '_internal').is_dir():
        return
    raise InstallError('Destination contains unrelated files. Choose an empty folder; nothing was replaced.')


def promote(release, target, verify, before_replace=lambda: None):
    """Only rename a verified copy on the destination volume; keep every old release."""
    known_installation(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise InstallError('The destination parent must not be a symbolic link.')
    prepared = target.parent / ('.DevlinDesk-staging-' + uuid.uuid4().hex)
    backup = None
    try:
        shutil.copytree(release, prepared)
        verify(prepared)
        before_replace()
        # Recheck after a potentially long copy/test and before touching the old folder.
        known_installation(target)
        if target.exists():
            backup = target.parent / (target.name + '.previous-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
            target.rename(backup)
        try:
            prepared.rename(target)
        except OSError:
            if backup and backup.exists() and not target.exists():
                backup.rename(target)
            raise
    finally:
        if prepared.exists():
            shutil.rmtree(prepared)
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--target-folder', type=Path, required=True)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        raise InstallError('Native .exe packaging must run on Windows.')
    root, target = args.source_root.resolve(), args.target_folder.resolve()
    # Never permit an installation to replace source code or its parent directory.
    if root == target or root.is_relative_to(target):
        raise InstallError('Installation cannot replace the source root or its parent.')
    known_installation(target)
    root.mkdir(parents=True, exist_ok=True)
    build_root = root / '.build'
    build_root.mkdir(exist_ok=True)
    # A stale report cannot claim that a failed build succeeded.
    if args.result.exists():
        args.result.unlink()
    with FileLock(build_root / "install.lock"), FileLock(target.parent / (".DevlinDesk-install-" + target.name + ".lock")):
        with tempfile.TemporaryDirectory(prefix='release-', dir=build_root) as temp:
            work = Path(temp)
            source, metadata = root, {'method': 'local'}
            if args.refresh:
                if (root / '.git').exists():
                    refresh_git(root)
                else:
                    source, metadata = download_sources(work)
            validate_source(source)
            if (source / '.git').exists() and shutil.which('git'):
                metadata['commit'] = git_output(source, 'rev-parse', 'HEAD')
                metadata['dirty'] = bool(git_output(source, 'status', '--porcelain', '--untracked-files=all'))
            checker = source / 'scripts/check-running.ps1'
            check_running(target, checker)
            release = build(source, root, work)
            write_launchers(release, source, root, metadata)
            backup = promote(release, target, lambda folder: verify_release(folder, work),
                             lambda: check_running(target, checker))
            result = {'ok': True, 'executable': str(target / 'DevlinDesk.exe'), 'backup': str(backup) if backup else None}
            args.result.write_text(json.dumps(result, indent=2), encoding='utf-8')
            print('Application installed:', result['executable'], flush=True)
            if backup:
                print('Previous version preserved:', backup, flush=True)


if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print('Installation failed:', error, file=sys.stderr)
        sys.exit(1)
