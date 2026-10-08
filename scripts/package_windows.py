"""Build, verify and replace a Windows release without touching application data.

This coordinator uses only the standard library so it can bootstrap its build venv.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
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


def retry_windows_lock(action, description):
    """Retry only Windows access/sharing errors, never a failing program exit."""
    for attempt in range(8):
        try:
            return action()
        except OSError as error:
            if getattr(error, 'winerror', None) not in (5, 32, 33):
                raise
            if attempt == 7:
                if error.winerror in (32, 33):
                    guidance = ('A process is holding this file or folder open. Exit Devlin Desk through the tray menu, '
                                'close other launchers/editors and terminal windows opened inside the application folder '
                                '(or change their current directory to the repository root), then retry. '
                                'No processes were terminated automatically.')
                else:
                    guidance = ('Check Windows Security Protection history and application-control policies '
                                'for this exact file, and verify folder permissions. Do not disable protection.')
                raise InstallError(
                    f'{description}: Windows denied access after 8 attempts. {error}. ' + guidance
                ) from error
            print(f'{description}: file temporarily unavailable; retrying in 2 seconds '
                  f'({attempt + 1}/8).', flush=True)
            time.sleep(2)


def cleanup_directory(path):
    """A locked temporary EXE must not hide the original installation error."""
    try:
        retry_windows_lock(lambda: shutil.rmtree(path), 'Removing temporary build files')
    except (OSError, InstallError) as error:
        print(f'WARNING: Temporary files retained at {path}: {error}', file=sys.stderr)


@contextmanager
def build_workspace(build_root):
    work = Path(tempfile.mkdtemp(prefix='release-', dir=build_root))
    try:
        yield work
    except Exception:
        log = build_root / 'install-error.log'
        try:
            # A parent handoff failure must not overwrite the child's real error.
            with log.open('a', encoding='utf-8') as output:
                output.write('\n--- ' + time.strftime('%Y-%m-%d %H:%M:%S') + ' ---\n')
                output.write(traceback.format_exc())
            print(f'Installation diagnostic log: {log}', file=sys.stderr)
        except OSError:
            traceback.print_exc()
        raise
    finally:
        cleanup_directory(work)


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
            # ZipInfo normalizes backslashes on Windows and truncates at NUL.
            # Validate the original central-directory name before that loss.
            original = item.orig_filename
            path = PurePosixPath(original)
            if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in original or '\0' in original or ':' in path.parts[0] or original != item.filename:
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
    for name in ('DevlinLauncher.exe', 'run.py', 'requirements-desktop.txt', 'dashboard/static/index.html',
                 'dashboard/assets/devlin-desk.ico', 'dashboard/assets/devlin-desk.png',
                 'scripts/refresh-icons.ps1',
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


def build(source, root, work, metadata=None):
    python = ensure_build_python(root)
    print('Installing/updating isolated build dependencies...', flush=True)
    run([python, '-m', 'pip', 'install', '-r', source / 'requirements-desktop.txt'], cwd=source)
    run([python, '-m', 'pip', 'check'], cwd=source)
    print('Building DevlinDesk.exe...', flush=True)
    build_info = work / 'build-info.json'
    build_info.write_text(json.dumps({**(metadata or {}),
        'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}), encoding='utf-8')
    run([python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
         '--contents-directory', 'runtime', '--icon', source / 'dashboard/assets/devlin-desk.ico',
         '--name', 'DevlinDesk', '--distpath', work / 'dist', '--workpath', work / 'pyinstaller',
         '--specpath', work, '--add-data', str(source / 'dashboard/static') + os.pathsep + 'dashboard/static',
         '--add-data', str(source / 'dashboard/assets') + os.pathsep + 'dashboard/assets',
         '--add-data', str(source / 'dashboard/version.json') + os.pathsep + 'dashboard',
         '--add-data', str(build_info) + os.pathsep + 'dashboard',
         '--hidden-import', 'pystray._win32',
         '--collect-all', 'webview', '--hidden-import', 'uvicorn.logging',
         '--hidden-import', 'uvicorn.loops.asyncio', '--hidden-import', 'uvicorn.protocols.http.h11_impl',
         '--hidden-import', 'uvicorn.lifespan.on', source / 'run.py'], cwd=source)
    release = work / 'dist/DevlinDesk'
    if not (release / 'DevlinDesk.exe').is_file() or not (release / 'runtime').is_dir():
        raise InstallError('Build did not produce a complete executable folder.')
    return release


def write_launchers(release, source, root, metadata):
    maintenance = release / 'maintenance'
    maintenance.mkdir()
    for name in ('install-windows.ps1', 'package_windows.py', 'check-running.ps1', 'launch-update.ps1', 'refresh-icons.ps1'):
        shutil.copy2(source / 'scripts' / name, maintenance / name)
    shutil.copy2(source / 'DevlinLauncher.exe', release / 'DevlinLauncher.exe')
    (release / 'README.txt').write_text('''Devlin Desk

Start: DevlinDesk.exe (or the desktop shortcut).
Closing the window hides it to the Windows notification area.
Click the tray icon to open; right-click for project statuses and Exit.
Exit stops monitoring. Close via Exit before updating.
Install/update: DevlinLauncher.exe (live installation log).

runtime/     Python runtime, libraries, interface and application icon.
maintenance/ Update tools and installation manifest. Do not edit these files.
User data: %LOCALAPPDATA%\\DevlinDesk (not in this application folder).
Backups: ../backups/ (previous releases, retained after updates).
Move the entire DevlinDesk folder; runtime and maintenance are required.
''', encoding='ascii', newline='\r\n')
    manifest = {'application': 'DevlinDesk', 'format': 2, 'source_directory': str(root.resolve()),
                'repository': REPOSITORY, 'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **metadata}
    (maintenance / 'install-info.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def verify_release(release, work):
    report = work / ('self-test-' + uuid.uuid4().hex + '.json')
    executable = release / 'DevlinDesk.exe'
    print(f'Checking executable startup: {executable}', flush=True)
    try:
        retry_windows_lock(lambda: run([executable, '--self-test', '--self-test-output', report], timeout=90),
                           f'Starting {executable}')
    except InstallError as error:
        if report.is_file():
            try:
                details = json.loads(report.read_text(encoding='utf-8')).get('error')
            except (OSError, ValueError, AttributeError):
                details = None
            if details:
                raise InstallError(f'{error}. Startup diagnostic: {details}') from error
        raise
    try:
        result = json.loads(report.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise InstallError('The executable did not produce a readiness report.') from exc
    if result.get('ok') is not True:
        raise InstallError('The built executable failed its startup/asset check: ' + str(result.get('error', 'no details')))


def verify_launcher(release, work):
    report = work / ('launcher-test-' + uuid.uuid4().hex + '.json')
    executable = release / 'DevlinLauncher.exe'
    print(f'Checking standalone launcher: {executable}', flush=True)
    retry_windows_lock(lambda: run([executable, '--self-test-output', report], timeout=60),
                       f'Starting {executable}')
    try:
        result = json.loads(report.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise InstallError('The launcher did not produce a readiness report.') from exc
    if result.get('ok') is not True or len(result.get('checks', [])) < 5:
        raise InstallError('The launcher failed its process/resource check: ' + str(result.get('error', 'no details')))


def check_running(target, checker):
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', checker, '-TargetDirectory', target])


def updated_packager(source):
    """A future release can change its build recipe as well as application code."""
    recipe = source / 'scripts/package_windows.py'
    return recipe if recipe.read_bytes() != Path(__file__).read_bytes() else None


def known_installation(target):
    if not target.exists():
        return
    if target.is_symlink() or not target.is_dir():
        raise InstallError('The installation destination is not a normal directory.')
    if not any(target.iterdir()):
        return
    for marker in (target / 'maintenance/install-info.json', target / 'install-info.json'):
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
        print(f'Copying built application to: {prepared}', flush=True)
        shutil.copytree(release, prepared, copy_function=lambda src, dst:
                        retry_windows_lock(lambda: shutil.copy2(src, dst), f'Copying {src} to {dst}'))
        verify(prepared)
        before_replace()
        # Recheck after a potentially long copy/test and before touching the old folder.
        known_installation(target)
        if target.exists():
            backups = target.parent / 'backups'
            if backups.is_symlink():
                raise InstallError('The backups directory must not be a symbolic link.')
            backups.mkdir(exist_ok=True)
            backup = backups / (target.name + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
            retry_windows_lock(lambda: target.rename(backup), f'Preserving previous application at {backup}')
        try:
            retry_windows_lock(lambda: prepared.rename(target), f'Installing application at {target}')
        except (OSError, InstallError):
            if backup and backup.exists() and not target.exists():
                retry_windows_lock(lambda: backup.rename(target), f'Restoring previous application at {target}')
            raise
    finally:
        if prepared.exists():
            cleanup_directory(prepared)
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--target-folder', type=Path, required=True)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--build-source', type=Path,
                        help='Already prepared source directory (used when handing off to an updated packager)')
    parser.add_argument('--build-metadata', type=Path)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    if args.refresh and args.build_source:
        raise InstallError('Prepared sources cannot also be refreshed.')
    if args.build_metadata and not args.build_source:
        raise InstallError('Prepared source metadata requires a prepared source directory.')
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
    # Keep downloaded sources alive through a handoff, but release both locks before
    # the updated coordinator acquires them. It performs all preflight checks again.
    with build_workspace(build_root) as work:
        handoff = None
        with FileLock(build_root / "install.lock"), FileLock(target.parent / (".DevlinDesk-install-" + target.name + ".lock")):
            source, metadata = args.build_source or root, {'method': 'local'}
            if args.build_metadata:
                metadata = json.loads(args.build_metadata.read_text(encoding='utf-8'))
                if not isinstance(metadata, dict):
                    raise InstallError('Invalid prepared source metadata.')
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
            recipe = updated_packager(source) if args.refresh else None
            if recipe:
                metadata_path = work / 'source-metadata.json'
                metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
                handoff = [sys.executable, recipe, '--source-root', root, '--target-folder', target,
                           '--build-source', source, '--build-metadata', metadata_path, '--result', args.result]
            else:
                release = build(source, root, work, metadata)
                write_launchers(release, source, root, metadata)
                verify_launcher(release, work)
                backup = promote(release, target, lambda folder: verify_release(folder, work),
                                 lambda: check_running(target, checker))
                result = {'ok': True, 'executable': str(target / 'DevlinDesk.exe'), 'backup': str(backup) if backup else None}
                args.result.write_text(json.dumps(result, indent=2), encoding='utf-8')
                print('Application installed:', result['executable'], flush=True)
                if backup:
                    print('Previous version preserved:', backup, flush=True)
        if handoff:
            print('Build recipe updated: using the new packaging coordinator...', flush=True)
            run(handoff)


if __name__ == '__main__':
    try:
        main()
    except (InstallError, OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print('Installation failed:', error, file=sys.stderr)
        sys.exit(1)
