import hashlib
import base64
import json
import re
import shlex
import stat
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from .remote import RemoteError
from .gitprovider import Provider, repository_path

EXCLUDED_DIRS = {'.git', '.ssh', '.venv', 'venv', 'node_modules', 'vendor', '__pycache__', '.idea'}
SECRET_NAMES = {'.env', 'id_rsa', 'id_ed25519', 'credentials.json', 'vault.key', 'desk.sqlite3', 'auth.json'}


def safe_file(path):
    p = PurePosixPath(path)
    if not path or p.is_absolute() or '..' in p.parts or any(c in path for c in '\x00\r\n'):
        return False
    if any(part in EXCLUDED_DIRS for part in p.parts):
        return False
    name = p.name.lower()
    if name in SECRET_NAMES or (name.startswith('.env.') and name not in ('.env.example', '.env.sample', '.env.dist')):
        return False
    return not name.endswith(('.pem', '.key', '.p12', '.pfx', '.sqlite3', '.sqlite', '.sql', '.bak'))


def validate_remote(remote):
    if any(c in remote for c in '\x00\r\n') or remote.startswith('-'):
        raise RemoteError('Некорректный адрес репозитория')
    if re.fullmatch(r'[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+:[a-zA-Z0-9_./-]+', remote):
        return remote
    parsed = urlsplit(remote)
    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
        return remote
    if parsed.scheme == 'ssh' and parsed.hostname and not parsed.password:
        return remote
    raise RemoteError('Используйте SSH или HTTPS URL без токенов и паролей. Git авторизуется на сервере.')


def validate_branch(branch):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_./-]*', branch) or '..' in branch or branch.endswith(('/', '.')) or '@{' in branch or '.lock' in branch:
        raise RemoteError('Некорректное имя ветки')
    return branch


def network(conn, arguments, remote):
    """Supply credentials through SSH stdin and process environment, never URL/argv/disk."""
    validate_remote(remote)
    profile = getattr(conn, 'git_profile', None)
    common = "GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' "
    if not profile:
        return conn.run(common + 'git ' + arguments, timeout=90)
    repository_path(profile, remote)  # Never send a profile's token to another service.
    version = conn.text('git --version').strip()
    match = re.search(r'(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 31):
        raise RemoteError('Git-профили требуют Git 2.31+ на сервере. Обновите Git или используйте серверный SSH-ключ без Git-профиля.')
    if arguments.split()[:2] in (['ls-remote', 'origin'], ['push', 'origin']) or arguments.startswith('pull --ff-only origin '):
        for option in ('--all', '--push --all'):
            urls = conn.text('git remote get-url ' + option + ' origin').strip().splitlines()
            if urls != [remote]:
                raise RemoteError('Адрес fetch/push отличается от origin (pushurl или insteadOf). Настройте один HTTPS адрес выбранного репозитория.')
    token = getattr(conn, 'git_token', '')
    if not token:
        raise RemoteError('В выбранном Git-профиле нет токена.')
    username = 'x-access-token' if profile['provider'] == 'github' else 'oauth2'
    authorization = base64.b64encode((username + ':' + token).encode()).decode()
    # Git >= 2.31 supports runtime configuration in the environment. Clear helpers,
    # disable redirects, require TLS, and scope the header to this exact repository URL.
    config = [('credential.helper', ''), ('http.extraHeader', ''),
              ('http.followRedirects', 'false'), ('http.sslVerify', 'true'),
              ('http.' + remote + '.followRedirects', 'false'), ('http.' + remote + '.sslVerify', 'true'),
              ('http.' + remote + '.extraHeader', None)]
    script = ('IFS= read -r DEVLIN_GIT_AUTH || exit 71; unset GIT_SSL_NO_VERIFY GIT_CURL_VERBOSE; ' + common +
              'GIT_TRACE=0 GIT_TRACE_CURL=0 GIT_TRACE_PACKET=0 GIT_TRACE2=0 GIT_TRACE2_EVENT=0 GIT_TRACE2_PERF=0 ' +
              f'GIT_CONFIG_COUNT={len(config)} ')
    for index, (key, value) in enumerate(config):
        script += f'GIT_CONFIG_KEY_{index}=' + shlex.quote(key) + ' '
        script += f'GIT_CONFIG_VALUE_{index}=' + ('"Authorization: Basic $DEVLIN_GIT_AUTH"' if value is None else shlex.quote(value)) + ' '
    script += 'git -c protocol.file.allow=never -c protocol.ext.allow=never ' + arguments
    return conn.run(script, timeout=90, input_data=(authorization + '\n').encode())


def check_remote(conn):
    current = status(conn)
    if not current['connected'] or not current['remote']:
        return {'ok': False, 'message': 'Репозиторий не подключён. Выберите его через кнопку «Подключить репозиторий».',
                'local_git': current['connected'], 'remote': ''}
    remote = conn.text('git config --get remote.origin.url').strip()
    code, _, _ = network(conn, 'ls-remote origin', remote)
    if code:
        raise RemoteError('Репозиторий настроен, но Git-доступ с сервера не прошёл. Проверьте токен/SSH-ключ, сеть сервера и права репозитория.')
    return {'ok': True, 'local_git': True, 'remote': remote,
            'message': 'Репозиторий подключён, доступ на чтение с сервера проверен. Право записи проверяется при push.'}


def attach(conn, request):
    if not request.confirm:
        raise RemoteError('Подтвердите подключение репозитория')
    if getattr(conn, 'project', {}).get('git_profile_id', '') != request.expected_git_profile_id:
        raise RemoteError('Git-профиль проекта изменился. Обновите вкладку Git и повторите подключение.')
    validate_remote(request.remote)
    validate_branch(request.branch)
    current = status(conn)
    if current['snapshot'] != request.snapshot or current['remote'] != request.expected_remote:
        raise RemoteError('Состояние репозитория изменилось. Обновите вкладку Git и повторите подключение.')
    configured = getattr(conn, 'project', {}).get('git_branch', '')
    if not current['connected'] and configured and configured != request.branch:
        raise RemoteError('Начальная ветка должна совпадать с выбранной веткой проекта')
    profile = getattr(conn, 'git_profile', None)
    if profile:
        Provider(profile, conn.git_token).repository(request.remote)
    code, _, _ = network(conn, 'ls-remote ' + shlex.quote(request.remote), request.remote)
    if code:
        raise RemoteError('Репозиторий недоступен с сервера. Подключение не изменено; проверьте авторизацию и сеть.')
    if current['remote'] and current['remote'] != request.remote and not request.replace_remote:
        raise RemoteError('У проекта уже есть origin. Подтвердите замену адреса; файлы и история останутся на сервере.')
    latest = status(conn)
    if latest['snapshot'] != current['snapshot'] or latest['remote'] != current['remote']:
        raise RemoteError('Файлы или origin изменились во время проверки доступа. Обновите вкладку Git и повторите подключение.')
    if not current['connected']:
        conn.text('git init -b ' + shlex.quote(request.branch))
    if not current['remote']:
        conn.text('git remote add origin ' + shlex.quote(request.remote))
    elif current['remote'] != request.remote:
        conn.text('git remote set-url origin ' + shlex.quote(request.remote))
        conn.run('git config --unset-all remote.origin.pushurl')
    return {'ok': True, 'message': 'Репозиторий подключён. Файлы и коммиты не изменены. Для выгрузки выберите файлы и создайте коммит.'}


def snapshot(conn, raw, files, head=''):
    # Include actual file content hashes: stale previews cannot stage a later edit silently.
    paths = [f['path'] for f in files if safe_file(f['path'])]
    hashes = ''
    for start in range(0, len(paths), 100):
        batch = paths[start:start + 100]
        command = 'for f in ' + ' '.join(shlex.quote(p) for p in batch) + '; do if test -f "$f" || test -L "$f"; then git hash-object -- "$f" || exit; else printf "deleted\\n"; fi; done'
        hashes += conn.text(command)
    return hashlib.sha256((conn.path + '\0' + head + raw + hashes).encode()).hexdigest()


def candidates(conn):
    prune = ' -o '.join('-name ' + shlex.quote(p) for p in sorted(EXCLUDED_DIRS))
    raw = conn.text('find . \\( -type d \\( ' + prune + ' \\) -prune \\) -o \\( -type f -print0 \\)')
    paths = sorted(p[2:] for p in raw.split('\x00') if p)
    if len(paths) > 2000:
        raise RemoteError('Первичная загрузка ограничена 2000 файлами. Исключите зависимости и архивы перед подключением.')
    return [{'path': p, 'status': '??', 'allowed': safe_file(p)} for p in paths]


def status(conn):
    code, _, _ = conn.run('git rev-parse --is-inside-work-tree')
    if code:
        files = candidates(conn)
        raw = json.dumps(files, sort_keys=True)
        return {'connected': False, 'files': files, 'snapshot': snapshot(conn, raw, files), 'branch': '', 'head': '', 'remote': ''}
    root = conn.text('git rev-parse --show-toplevel').strip()
    real = conn.text('pwd -P').strip()
    if root != real:
        raise RemoteError('Указанная директория находится внутри другого репозитория. Укажите корень Git-проекта.')
    raw = conn.text('git status --porcelain=v1 -z --untracked-files=all')
    parts, files = raw.split('\x00'), []
    i = 0
    while i < len(parts) and parts[i]:
        entry = parts[i]
        flag, path = entry[:2], entry[3:]
        item = {'status': flag, 'path': path, 'allowed': safe_file(path)}
        if 'R' in flag or 'C' in flag:
            i += 1
            item['old_path'] = parts[i]
            item['allowed'] = item['allowed'] and safe_file(parts[i])
        files.append(item)
        i += 1
    if len(files) > 2000:
        raise RemoteError('Обнаружено больше 2000 изменений. Сократите набор изменений перед синхронизацией.')
    code, out, _ = conn.run('git rev-parse --verify HEAD')
    head = out.decode().strip() if code == 0 else ''
    branch_code, branch_out, _ = conn.run('git symbolic-ref --short -q HEAD')
    branch = branch_out.decode().strip() if branch_code == 0 else '(detached HEAD)'
    _, remote_out, _ = conn.run('git config --get remote.origin.url')
    remote = remote_out.decode().strip()
    if remote:
        try:
            validate_remote(remote)
        except RemoteError:
            remote = '(адрес скрыт: небезопасный URL; настройте SSH remote на сервере)'
    return {'connected': True, 'files': files, 'snapshot': snapshot(conn, raw, files, head),
            'branch': branch, 'head': head[:12], 'remote': remote,
            'staged': any(f['status'][0] not in (' ', '?') for f in files)}


def diff(conn, path):
    if not safe_file(path):
        raise RemoteError('Просмотр потенциально секретного файла отключён')
    current = status(conn)
    if path not in [f['path'] for f in current['files']]:
        raise RemoteError('Файл отсутствует в текущем списке изменений')
    if not current['connected'] or next(f for f in current['files'] if f['path'] == path)['status'] == '??':
        with conn.client.open_sftp() as sftp:
            if stat.S_ISLNK(sftp.lstat(conn.path.rstrip('/') + '/' + path).st_mode):
                raise RemoteError('Просмотр содержимого символьной ссылки запрещён')
            with sftp.open(conn.path.rstrip('/') + '/' + path, 'rb') as f:
                content = f.read(100_001)
        if len(content) > 100_000:
            return 'Файл превышает 100 КБ. Откройте его на сервере.'
        if b'\x00' in content:
            return 'Двоичный файл: текстовый просмотр недоступен.'
        return content.decode('utf-8', errors='replace')
    base = 'HEAD' if current['head'] else '--cached'
    return conn.text('git diff --no-ext-diff ' + base + ' -- ' + shlex.quote(path), max_output=200_000) or 'Нет текстового diff (например, изменение прав файла).'


def commit(conn, current, files, message, settings, push=True):
    if current.get('staged'):
        raise RemoteError('На сервере уже есть staged-изменения. Завершите или отмените их на сервере перед синхронизацией.')
    allowed = {f['path'] for f in current['files'] if f['allowed']}
    if len(set(files)) != len(files) or any(p not in allowed for p in files):
        raise RemoteError('Набор файлов изменился или содержит запрещённые пути')
    if current.get('branch') == '(detached HEAD)':
        raise RemoteError('HEAD не привязан к ветке. Выберите ветку на сервере.')
    configured_branch = getattr(conn, 'project', {}).get('git_branch', '')
    if configured_branch and current.get('branch') != configured_branch:
        raise RemoteError('Текущая ветка сервера отличается от выбранной: ' + configured_branch)
    if '\x00' in message:
        raise RemoteError('Некорректное сообщение коммита')
    paths = list(files)
    for item in current['files']:
        if item['path'] in files and item.get('old_path'):
            paths.append(item['old_path'])
    for start in range(0, len(paths), 100):
        conn.text('git add -A -- ' + ' '.join(shlex.quote(p) for p in paths[start:start + 100]))
    # If a hook fails, do not undo the server index or overwrite the user's work.
    conn.text('git -c user.name=' + shlex.quote(settings['git_name']) + ' -c user.email=' + shlex.quote(settings['git_email']) +
              ' commit -m ' + shlex.quote(message), timeout=90)
    head = conn.text('git rev-parse --short HEAD').strip()
    result = {'commit': head, 'pushed': False, 'warning': ''}
    if push:
        remote = conn.text('git config --get remote.origin.url').strip()
        validate_remote(remote)
        push_error = ''
        try:
            code, _, _ = network(conn, 'push origin HEAD', remote)
        except RemoteError as exc:
            code = -1
            push_error = ' ' + str(exc)
        if code:
            result['warning'] = 'Коммит ' + head + ' создан, но push не прошёл. Проверьте Git-профиль или SSH-ключ сервера, права ветки и remote.' + push_error + ' Используйте отдельную кнопку «Отправить коммиты» после исправления.'
        else:
            result['pushed'] = True
    return result


def sync(conn, request, settings):
    current = status(conn)
    if not current['connected']:
        raise RemoteError('Сначала подключите репозиторий')
    if request.snapshot != current['snapshot']:
        raise RemoteError('Файлы изменились после просмотра. Обновите список и подтвердите новый набор.')
    if request.push:
        validate_remote(conn.text('git config --get remote.origin.url').strip())
    return commit(conn, current, request.files, request.message, settings, request.push)


def initialize(conn, request, settings):
    validate_remote(request.remote)
    validate_branch(request.branch)
    configured_branch = getattr(conn, 'project', {}).get('git_branch', '')
    if configured_branch and request.branch != configured_branch:
        raise RemoteError('Начальная ветка должна совпадать с выбранной веткой проекта')
    current = status(conn)
    if current['connected']:
        raise RemoteError('Репозиторий уже подключён. Изменять remote автоматически нельзя.')
    if current['snapshot'] != request.snapshot:
        raise RemoteError('Файлы изменились после просмотра. Обновите список.')
    allowed = {f['path'] for f in current['files'] if f['allowed']}
    if any(p not in allowed for p in request.files):
        raise RemoteError('Есть запрещённые пути')
    if getattr(conn, 'git_profile', None):
        Provider(conn.git_profile, conn.git_token).repository(request.remote)
        code, _, _ = network(conn, 'ls-remote ' + shlex.quote(request.remote), request.remote)
        if code:
            raise RemoteError('Репозиторий недоступен с сервера. Проверьте Git-профиль перед выгрузкой.')
    conn.text('git init -b ' + shlex.quote(request.branch))
    conn.text('git remote add origin ' + shlex.quote(request.remote))
    fresh = status(conn)
    return commit(conn, fresh, request.files, request.message, settings, request.push)


def enforce_branch(conn, current):
    branch = getattr(conn, 'project', {}).get('git_branch', '')
    if branch and current['branch'] != branch:
        raise RemoteError('Текущая ветка сервера отличается от выбранной: ' + branch)


def pull(conn):
    current = status(conn)
    enforce_branch(conn, current)
    if not current['connected'] or current['files']:
        raise RemoteError('Обновление возможно только в подключённом репозитории без локальных изменений')
    if current['branch'] == '(detached HEAD)':
        raise RemoteError('Выберите ветку на сервере')
    remote = conn.text('git config --get remote.origin.url').strip()
    code, _, _ = network(conn, 'pull --ff-only origin ' + shlex.quote(current['branch']), remote)
    if code:
        raise RemoteError('Pull не выполнен. Проверьте Git-доступ и расхождение истории; перед повтором обновите состояние проекта.')
    return {'message': 'Ветка обновлена без перезаписи локальных изменений'}


def push(conn):
    current = status(conn)
    enforce_branch(conn, current)
    if not current['connected'] or current['branch'] == '(detached HEAD)':
        raise RemoteError('Нужен репозиторий с выбранной веткой')
    remote = conn.text('git config --get remote.origin.url').strip()
    code, _, _ = network(conn, 'push origin HEAD', remote)
    if code:
        raise RemoteError('Push не выполнен. Проверьте Git-доступ, права выбранной ветки и расхождение истории.')
    return {'message': 'Коммиты отправлены в текущую ветку origin'}
