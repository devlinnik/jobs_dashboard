import hashlib
import base64
import json
import re
import shlex
import stat
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from .remote import RemoteError, command_failure
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
    # The server may select a non-default key using core.sshCommand. A fixed
    # GIT_SSH_COMMAND overrides that setting and silently loses the identity.
    # Keep Git's command precedence, then add noninteractive/trust options.
    setup = ('devlin_git_ssh=${GIT_SSH_COMMAND:-}; '
             'if test -z "$devlin_git_ssh"; then '
             'devlin_git_ssh=$(git config --get core.sshCommand) || devlin_git_ssh=ssh; fi; '
             'if test -z "$devlin_git_ssh"; then devlin_git_ssh=ssh; fi; ')
    common = ('GIT_TERMINAL_PROMPT=0 '
              'GIT_SSH_COMMAND="$devlin_git_ssh -o BatchMode=yes -o StrictHostKeyChecking=yes" ')

    if not profile:
        return conn.run(setup + common + 'git ' + arguments, timeout=90)
    repository_path(profile, remote)  # Never send a profile's token to another service.
    version = conn.text('git --version').strip()
    match = re.search(r'(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (2, 31):
        raise RemoteError('Git-профили требуют Git 2.31+ на сервере. Обновите Git или используйте серверный SSH-ключ без Git-профиля.')
    if 'origin' in shlex.split(arguments):
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
    script = (setup + 'IFS= read -r DEVLIN_GIT_AUTH || exit 71; unset GIT_SSL_NO_VERIFY GIT_CURL_VERBOSE; ' + common +
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
    code, _, error = network(conn, 'ls-remote origin', remote)
    if code:
        raise command_failure(code, error, 'Проверка доступа к репозиторию')
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
    code, _, error = network(conn, 'ls-remote ' + shlex.quote(request.remote), request.remote)
    if code:
        raise command_failure(code, error, 'Подключение не изменено; проверка доступа к репозиторию')
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
        # Fingerprint raw bytes, not clean-filter output. A symlink stores its target
        # string in Git; never read the target file or traverse a linked directory.
        # The sentinel preserves trailing newlines through shell substitution.
        command = 'devlin_file_index=0; for f in ' + ' '.join(shlex.quote(p) for p in batch) + '; do '
        command += ('devlin_file_index=$((devlin_file_index + 1)); '
            'if test -L "$f"; then '
            'if devlin_link=$(readlink -- "$f" && printf "."); then '
            'printf "link:"; printf "%s" "$devlin_link" | LC_ALL=C git hash-object --stdin; '
            'devlin_hash_code=$?; else devlin_hash_code=$?; fi; '
            'elif test -f "$f"; then printf "file:"; LC_ALL=C git hash-object --no-filters -- "$f"; devlin_hash_code=$?; '
            'elif test -e "$f"; then printf "other\\n"; devlin_hash_code=0; '
            'else printf "deleted\\n"; devlin_hash_code=0; fi; '
            'if test "$devlin_hash_code" -ne 0; then printf "\\nDEVLIN_HASH_ERROR:%s\\n" "$devlin_file_index"; exit "$devlin_hash_code"; fi; done')
        code, out, err = conn.run(command)
        if code:
            match = re.search(rb'\nDEVLIN_HASH_ERROR:(\d+)\n', out)
            index = int(match.group(1)) - 1 if match else -1
            path = batch[index] if 0 <= index < len(batch) else '(пакет файлов)'
            raise command_failure(code, err, 'Проверка содержимого файла «' + path + '»')
        hashes += out.decode('utf-8', errors='replace')
    return hashlib.sha256((conn.path + '\0' + head + raw + hashes).encode()).hexdigest()


def candidates(conn):
    # Preview through a disposable Git directory outside the project. This applies
    # existing .gitignore/global excludes before initialization, without creating .git.
    excludes = ' '.join('--exclude=' + shlex.quote(p + '/') for p in sorted(EXCLUDED_DIRS))
    raw = conn.text('devlin_git_preview=$(mktemp -d "${TMPDIR:-/tmp}/devlin-git-preview.XXXXXX") || exit; '
        'trap \'rm -rf -- "$devlin_git_preview"\' EXIT; '
        'git init --bare -q "$devlin_git_preview" && '
        'git --git-dir="$devlin_git_preview" --work-tree=. ls-files --others --exclude-standard -z ' + excludes)
    paths = sorted(p for p in raw.split('\x00') if p)
    if len(paths) > 2000:
        raise RemoteError('Первичная загрузка ограничена 2000 файлами. Исключите зависимости и архивы перед подключением.')
    return [{'path': p, 'status': '??', 'allowed': safe_file(p)} for p in paths]


def status(conn, include_snapshot=True):
    code, _, _ = conn.run('git rev-parse --is-inside-work-tree')
    if code:
        metadata, _, _ = conn.run('test -e .git || test -L .git')
        if metadata == 0:
            raise RemoteError('В папке уже есть .git, но Git не смог открыть репозиторий. Проверьте права, владельца, safe.directory и состояние .git. Существующая история не изменена.')
        files = candidates(conn) if include_snapshot else []
        raw = json.dumps(files, sort_keys=True)
        return {'connected': False, 'files': files, 'snapshot': snapshot(conn, raw, files) if include_snapshot else '', 'branch': '', 'head': '', 'remote': ''}
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
    return {'connected': True, 'files': files, 'snapshot': snapshot(conn, raw, files, head) if include_snapshot else '',
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
    return conn.text('git --literal-pathspecs diff --no-ext-diff ' + base + ' -- ' + shlex.quote(path), max_output=200_000) or 'Нет текстового diff (например, изменение прав файла).'


def author_identity(conn, settings):
    profile = getattr(conn, 'git_profile', {}) or {}
    if profile.get('git_name') and profile.get('git_email'):
        return {'name': profile['git_name'], 'email': profile['git_email'], 'source': 'Git-профиль'}
    values = []
    for key in ('user.name', 'user.email'):
        code, out, _ = conn.run('git config --get ' + key)
        values.append(out.decode('utf-8', errors='replace').strip() if code == 0 else '')
    if all(values) and not any(c in ''.join(values) for c in '\x00\r\n'):
        return {'name': values[0], 'email': values[1], 'source': 'Настройки Git на сервере'}
    return {'name': settings['git_name'], 'email': settings['git_email'], 'source': 'Настройки приложения'}


def diagnostics(conn, settings):
    """Read-only checks; never return arbitrary Git stderr or credentials."""
    checks = []
    code, out, _ = conn.run('git --version')
    checks.append({'ok': code == 0, 'message': 'Git установлен.' if code == 0 else 'Git не найден на сервере.'})
    if code:
        return {'checks': checks}
    try:
        current = status(conn, include_snapshot=False)
    except RemoteError as exc:
        checks.append({'ok': False, 'message': str(exc)})
        return {'checks': checks}
    if not current['connected']:
        checks.append({'ok': False, 'message': 'В корне проекта ещё нет Git-репозитория.'})
        return {'checks': checks}
    try:
        snapshot(conn, json.dumps(current['files'], sort_keys=True), current['files'], current['head'])
        checks.append({'ok': True, 'message': 'Предварительная проверка содержимого изменённых файлов прошла. Символические ссылки проверяются без чтения их целей.'})
    except RemoteError as exc:
        checks.append({'ok': False, 'message': str(exc)})
    author = author_identity(conn, settings)
    checks.append({'ok': bool(author['name'] and author['email']), 'message': 'Автор: ' + author['name'] + ' <' + author['email'] + '> · ' + author['source']})
    for part in ('', 'objects', 'refs'):
        path = conn.text('git rev-parse --git-path ' + shlex.quote(part or '.')).strip()
        code, _, _ = conn.run('test -w ' + shlex.quote(path))
        checks.append({'ok': code == 0, 'message': ('Есть права записи: ' if code == 0 else 'Нет прав записи: ') + ('.git/' + part)})
    lock = conn.text('git rev-parse --git-path index.lock').strip()
    code, _, _ = conn.run('test -e ' + shlex.quote(lock))
    checks.append({'ok': code != 0, 'message': 'Блокировки индекса нет.' if code else 'Обнаружен index.lock. Дождитесь завершения Git-процессов; проверяйте оставшуюся блокировку вручную.'})
    code, out, _ = conn.run('git config --bool --get commit.gpgsign')
    signing = code == 0 and out.strip() == b'true'
    checks.append({'ok': not signing, 'message': 'Подпись коммитов включена: SSH-сеансу нужен доступный ключ GPG/SSH. Если подпись не нужна, настройте commit.gpgsign=false вручную для этого проекта.' if signing else 'Обязательная подпись коммитов не включена.'})
    checks.append({'ok': None, 'message': 'Проверка прав не гарантирует запись при исчерпанной квоте. При ошибке 128 проверьте квоту в панели хостинга и сообщения Git. Диагностика не создаёт коммит и не изменяет файлы.'})
    return {'checks': checks}


def log_commits(conn, revision):
    raw = conn.text('git log -z -n 50 --format=%H%x00%h%x00%an%x00%ae%x00%aI%x00%s ' +
                    shlex.quote(revision) + ' --', max_output=400_000)
    fields = raw.rstrip('\x00').split('\x00') if raw else []
    if len(fields) % 6:
        raise RemoteError('Не удалось разобрать историю коммитов')
    return [dict(zip(('hash','short_hash','author','email','date','subject'), fields[i:i+6]))
            for i in range(0, len(fields), 6)]


def history(conn, settings, remote_tip=None):
    current = status(conn, include_snapshot=False)
    result = {'commits': [], 'outgoing': [], 'ahead': None, 'behind': None, 'outgoing_known': False,
              'head': '', 'branch': current['branch'], 'remote': current['remote'],
              'author': author_identity(conn, settings), 'checked': remote_tip is not None,
              'message': 'Локальный Git ещё не создан.'}
    if not current['connected']:
        return result
    if current['head']:
        result['head'] = conn.text('git rev-parse HEAD').strip()
        result['commits'] = log_commits(conn, 'HEAD')
    if current['branch'] == '(detached HEAD)':
        result['message'] = 'HEAD не привязан к ветке. Историю можно смотреть; для отправки выберите ветку на сервере.'
        return result
    validate_branch(current['branch'])
    if not current['remote']:
        result['message'] = 'origin не настроен. Коммиты остаются на сервере до подключения репозитория.'
        return result
    if remote_tip is None:
        code, out, _ = conn.run('git rev-parse --verify refs/remotes/origin/' + shlex.quote(current['branch']))
        if code:
            result['message'] = 'Нужно обновить данные с origin, чтобы определить неотправленные коммиты.'
            return result
        remote_tip = out.decode().strip()
    if not result['head']:
        result.update(ahead=0, behind=0, outgoing_known=True, message='Коммитов на сервере пока нет. Существующие файлы можно добавить первым коммитом.')
        return result
    if remote_tip:
        if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', remote_tip):
            raise RemoteError('Некорректная ссылка удалённой ветки')
        counts = conn.text('git rev-list --left-right --count HEAD...' + remote_tip).split()
        ahead, behind = map(int, counts)
        result['outgoing'] = log_commits(conn, remote_tip + '..HEAD') if ahead else []
    else:
        ahead, behind = int(conn.text('git rev-list --count HEAD').strip()), 0
        result['outgoing'] = result['commits']
    result.update(ahead=ahead, behind=behind, outgoing_known=True,
                  message='По локальной копии origin; обновите перед отправкой.' if not result['checked'] else
                  'Удалённая ветка ещё не создана. При push будет отправлена история текущей ветки.' if not remote_tip else
                  'Состояние origin обновлено. Файлы проекта не изменены.')
    return result


def fetch_commits(conn, settings):
    current = status(conn, include_snapshot=False)
    if not current['connected'] or not current['remote'] or current['branch'] == '(detached HEAD)':
        return history(conn, settings)
    branch = validate_branch(current['branch'])
    remote = conn.text('git config --get remote.origin.url').strip()
    code, out, _ = network(conn, 'ls-remote --heads origin ' + shlex.quote('refs/heads/' + branch), remote)
    if code:
        raise RemoteError('Не удалось получить список коммитов с origin. Локальные коммиты сохранены; проверьте Git-профиль, права и сеть.')
    if not out.strip():
        conn.text('git update-ref -d ' + shlex.quote('refs/remotes/origin/' + branch))
        return history(conn, settings, remote_tip='')
    code, _, _ = network(conn, 'fetch --no-tags origin ' + shlex.quote(branch), remote)
    if code:
        raise RemoteError('Не удалось обновить историю origin. Рабочие файлы не изменены; проверьте Git-доступ.')
    tip = conn.text('git rev-parse --verify FETCH_HEAD').strip()
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', tip):
        raise RemoteError('Git вернул некорректную ссылку origin')
    conn.text('git update-ref ' + shlex.quote('refs/remotes/origin/' + branch) + ' ' + tip)
    latest = status(conn, include_snapshot=False)
    if latest['branch'] != branch or latest['remote'] != current['remote']:
        raise RemoteError('Ветка или origin изменились во время обновления истории. Обновите вкладку Git заново.')
    return history(conn, settings, remote_tip=tip)


def commit(conn, current, files, message, settings, push=False):
    if current.get('staged'):
        raise RemoteError('На сервере уже есть staged-изменения. Завершите или отмените их на сервере перед синхронизацией.')
    allowed = {f['path'] for f in current['files'] if f['allowed']}
    if len(set(files)) != len(files) or any(p not in allowed for p in files):
        raise RemoteError('Выбранные файлы больше не входят в допустимые изменения. Обновите список: .gitignore, исключённые пути или изменения на сервере могли изменить набор. Файлы не добавлены в индекс.')
    if current.get('branch') == '(detached HEAD)':
        raise RemoteError('HEAD не привязан к ветке. Выберите ветку на сервере.')
    configured_branch = getattr(conn, 'project', {}).get('git_branch', '')
    if configured_branch and current.get('branch') != configured_branch:
        raise RemoteError('Текущая ветка сервера отличается от выбранной: ' + configured_branch)
    if '\x00' in message:
        raise RemoteError('Некорректное сообщение коммита')
    paths = list(files)
    identity = author_identity(conn, settings)
    for item in current['files']:
        if item['path'] in files and item.get('old_path'):
            paths.append(item['old_path'])
    for start in range(0, len(paths), 100):
        batch = paths[start:start + 100]
        conn.text('LC_ALL=C git --literal-pathspecs add -A -- ' + ' '.join(shlex.quote(p) for p in batch),
                  context='Добавление файлов в индекс Git: ' + ', '.join(batch[:5]) + (' …' if len(batch) > 5 else ''))
    # If a hook fails, do not undo the server index or overwrite the user's work.
    environment = ' '.join(key + '=' + shlex.quote(value) for key,value in (
        ('GIT_AUTHOR_NAME',identity['name']), ('GIT_AUTHOR_EMAIL',identity['email']),
        ('GIT_COMMITTER_NAME',identity['name']), ('GIT_COMMITTER_EMAIL',identity['email'])))
    conn.text('LC_ALL=C ' + environment + ' git -c user.name=' + shlex.quote(identity['name']) + ' -c user.email=' + shlex.quote(identity['email']) +
              ' commit -m ' + shlex.quote(message), timeout=90, context='Создание коммита из подготовленных файлов')
    head = conn.text('git rev-parse --short HEAD').strip()
    result = {'commit': head, 'pushed': False, 'warning': '', 'author': identity}
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
        code, _, error = network(conn, 'ls-remote ' + shlex.quote(request.remote), request.remote)
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


def push(conn, request=None, settings=None):
    current = status(conn, include_snapshot=False)
    enforce_branch(conn, current)
    if not current['connected'] or current['branch'] == '(detached HEAD)':
        raise RemoteError('Нужен репозиторий с выбранной веткой')
    remote = conn.text('git config --get remote.origin.url').strip()
    if not current['head']:
        raise RemoteError('Сначала создайте коммит на сервере')
    def verify_request():
        if not request:
            return
        actual = status(conn, include_snapshot=False)
        head = conn.text('git rev-parse HEAD').strip()
        if ((request.expected_head and request.expected_head != head) or
                (request.expected_branch and request.expected_branch != actual['branch']) or
                (request.expected_remote and request.expected_remote != actual['remote'])):
            raise RemoteError('Коммиты, ветка или origin изменились после просмотра. Обновите список и подтвердите отправку заново.')
    verify_request()
    outgoing = fetch_commits(conn, settings or {'git_name':'Devlin Desk', 'git_email':'desk@localhost'})
    verify_request()
    if outgoing['behind']:
        raise RemoteError('На origin есть новые коммиты, которых нет на сервере. Сначала обновите чистый проект через pull или согласуйте историю вручную. Force push не выполняется.')
    if outgoing['ahead'] == 0:
        return {'message': 'Неотправленных коммитов нет: текущая ветка уже актуальна.'}
    # Pin the reviewed commit and destination: a concurrent server commit must not
    # silently enter this push after the final HEAD check.
    revision = outgoing['head'] + ':refs/heads/' + outgoing['branch']
    code, _, _ = network(conn, 'push origin ' + shlex.quote(revision), remote)
    if code:
        raise RemoteError('Push не выполнен. Проверьте Git-доступ, права выбранной ветки и расхождение истории.')
    return {'message': 'Коммиты отправлены в текущую ветку origin'}
