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


def path_fields(raw, key='path'):
    """JSON-safe, reversible identity; never replace filename bytes used by Git."""
    text = raw.decode('utf-8', errors='surrogateescape')
    identity = ''.join('\\\\' if c == '\\' else '\\x%02x' % (ord(c) - 0xdc00)
                       if 0xdc80 <= ord(c) <= 0xdcff else c for c in text)
    fields = {key: identity}
    if identity != text:
        fields['raw_' + key] = raw.hex()
        # CP1251 is a display hint only. Commands always use the original bytes.
        fields['display_' + key] = raw.decode('cp1251', errors='backslashreplace') if any(0xdc80 <= ord(c) <= 0xdcff for c in text) else text
    return fields


def path_bytes(item, key='path'):
    return bytes.fromhex(item['raw_' + key]) if 'raw_' + key in item else item[key].encode('utf-8')


def quote_path(item, key='path'):
    raw = path_bytes(item, key)
    try:
        return shlex.quote(raw.decode('utf-8'))
    except UnicodeDecodeError:
        # ASCII-only SSH command; POSIX printf reconstructs the exact bytes.
        return '"$(printf ' + shlex.quote(''.join('\\%03o' % b for b in raw)) + ')"'


def raw_output(conn, command):
    code, out, err = conn.run(command)
    if code:
        raise command_failure(code, err + b'\n' + out)
    return out


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
    getattr(conn, 'report_progress', lambda *args: None)('Отправка коммитов' if arguments.startswith('push ') else 'Проверка удалённого репозитория', 0, 0)
    profile = getattr(conn, 'git_profile', None)
    # The server may select a non-default key using core.sshCommand. A fixed
    # GIT_SSH_COMMAND overrides that setting and silently loses the identity.
    # Keep Git's command precedence, then add noninteractive/trust options.
    setup = ('devlin_git_ssh=${GIT_SSH_COMMAND:-}; '
             'if test -z "$devlin_git_ssh"; then '
             'devlin_git_ssh=$(git config --get core.sshCommand) || devlin_git_ssh=ssh; fi; '
             'if test -z "$devlin_git_ssh"; then devlin_git_ssh=ssh; fi; ')
    override = getattr(conn, 'server_git_ssh_command', '')
    if override:
        setup = 'devlin_git_ssh=' + shlex.quote(override) + '; '
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


def access_settings(conn):
    """Expose identity paths and transport, never arbitrary commands or key contents."""
    code, out, _ = conn.run('if test -n "${GIT_SSH_COMMAND:-}"; then printf "%s" "$GIT_SSH_COMMAND"; else git config --get core.sshCommand; fi')
    key = ''
    if code == 0:
        try:
            args = shlex.split(out.decode('utf-8', errors='replace'))
            for index, arg in enumerate(args):
                if arg == '-i' and index + 1 < len(args):
                    key = args[index + 1]
                    break
                if arg.startswith('IdentityFile='):
                    key = arg.split('=', 1)[1]
                    break
        except ValueError:
            pass
    _, raw, _ = conn.run('git config --get remote.origin.url')
    remote = raw.decode('utf-8', errors='replace').strip()
    ssh_url = ''
    try:
        parsed = urlsplit(remote)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme == 'https' and parsed.hostname in ('github.com', 'gitlab.com') and not parsed.username and not parsed.password and re.fullmatch(r'/[a-zA-Z0-9_./-]+', parsed.path):
        ssh_url = 'git@' + parsed.hostname + ':' + parsed.path.lstrip('/')
    return {'server_ssh_key': key, 'ssh_url': ssh_url,
            'command_configured': code == 0 and bool(out.strip()),
            'environment_override': conn.text('test -z "${GIT_SSH_COMMAND:-}" && printf no || printf yes').strip() == 'yes'}


def prepare_server_key(conn, value):
    if any(c in value for c in '\x00\r\n') or not (value.startswith('/') or value.startswith('~/')):
        raise RemoteError('Укажите абсолютный путь к SSH-ключу на сервере или путь ~/.ssh/имя_ключа.')
    if value.startswith('~/'):
        home = conn.text('printf "%s" "$HOME"').strip()
        if not home.startswith('/'):
            raise RemoteError('Не удалось определить домашнюю папку SSH-пользователя.')
        value = home.rstrip('/') + value[1:]
    code, _, _ = conn.run('test -f ' + shlex.quote(value) + ' && test -r ' + shlex.quote(value))
    if code:
        raise RemoteError('SSH-ключ не найден или недоступен для чтения на сервере. Создайте ключ в консоли хостинга; нужен файл без .pub.')
    if value.endswith('.pub'):
        raise RemoteError('Укажите приватный SSH-ключ без .pub. Его содержимое не передаётся на ПК.')
    return 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -i ' + shlex.quote(value) + ' -o IdentitiesOnly=yes'


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
    key = getattr(request, 'server_ssh_key', '').strip()
    command = ''
    if key:
        if profile or request.remote.startswith('https://'):
            raise RemoteError('Серверный SSH-ключ используется с SSH URL и без Git-профиля с HTTPS-токеном.')
        if access_settings(conn)['environment_override']:
            raise RemoteError('На сервере задан GIT_SSH_COMMAND: он перекрывает настройки проекта. Измените эту переменную на сервере или уберите её перед выбором ключа.')
        command = prepare_server_key(conn, key)
        conn.server_git_ssh_command = command
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
    if command:
        conn.text('git config --local core.sshCommand ' + shlex.quote(command))
    return {'ok': True, 'message': 'Репозиторий подключён. Файлы и коммиты не изменены. Для выгрузки выберите файлы и создайте коммит.'}


def snapshot(conn, raw, files, head=''):
    # Include actual file content hashes: stale previews cannot stage a later edit silently.
    paths = [f for f in files if f['allowed']]
    hashes = ''
    for start in range(0, len(paths), 100):
        batch = paths[start:start + 100]
        # Fingerprint raw bytes, not clean-filter output. A symlink stores its target
        # string in Git; never read the target file or traverse a linked directory.
        # The sentinel preserves trailing newlines through shell substitution.
        command = 'devlin_file_index=0; for f in ' + ' '.join(quote_path(p) for p in batch) + '; do '
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
            item = batch[index] if 0 <= index < len(batch) else {}
            path = item.get('display_path', item.get('path', '(пакет файлов)'))
            raise command_failure(code, err, 'Проверка содержимого файла «' + path + '»')
        hashes += out.decode('utf-8', errors='replace')
    return hashlib.sha256((conn.path + '\0' + head + raw + hashes).encode()).hexdigest()


def candidates(conn):
    # Preview through a disposable Git directory outside the project. This applies
    # existing .gitignore/global excludes before initialization, without creating .git.
    excludes = ' '.join('--exclude=' + shlex.quote(p + '/') for p in sorted(EXCLUDED_DIRS))
    raw = raw_output(conn, 'devlin_git_preview=$(mktemp -d "${TMPDIR:-/tmp}/devlin-git-preview.XXXXXX") || exit; '
        'trap \'rm -rf -- "$devlin_git_preview"\' EXIT; '
        'git init --bare -q "$devlin_git_preview" && '
        'git --git-dir="$devlin_git_preview" --work-tree=. ls-files --others --exclude-standard -z ' + excludes)
    paths = sorted(p for p in raw.split(b'\x00') if p)
    if len(paths) > 2000:
        raise RemoteError('Первичная загрузка ограничена 2000 файлами. Исключите зависимости и архивы перед подключением.')
    return [{**path_fields(p), 'status': '??', 'allowed': safe_file(p.decode('utf-8', errors='surrogateescape'))} for p in paths]


def status(conn, include_snapshot=True):
    code, out, err = conn.run('git rev-parse --is-inside-work-tree')
    if code:
        metadata, _, _ = conn.run('test -e .git || test -L .git')
        if metadata == 0:
            raise command_failure(code, err + b'\n' + out, 'В папке уже есть .git, но репозиторий недоступен. Запустите диагностику и исправление в приложении')
        files = candidates(conn) if include_snapshot else []
        raw = json.dumps(files, sort_keys=True)
        return {'connected': False, 'files': files, 'snapshot': snapshot(conn, raw, files) if include_snapshot else '', 'branch': '', 'head': '', 'remote': ''}
    root = conn.text('git rev-parse --show-toplevel').strip()
    real = conn.text('pwd -P').strip()
    if root != real:
        raise RemoteError('Указанная директория находится внутри другого репозитория. Укажите корень Git-проекта.')
    raw_bytes = raw_output(conn, 'git status --porcelain=v1 -z --untracked-files=all')
    raw = raw_bytes.hex()
    parts, files = raw_bytes.split(b'\x00'), []
    i = 0
    while i < len(parts) and parts[i]:
        entry = parts[i]
        flag, path = entry[:2].decode('ascii'), entry[3:]
        item = {'status': flag, **path_fields(path), 'allowed': safe_file(path.decode('utf-8', errors='surrogateescape'))}
        if 'R' in flag or 'C' in flag:
            i += 1
            item.update(path_fields(parts[i], 'old_path'))
            item['allowed'] = item['allowed'] and safe_file(parts[i].decode('utf-8', errors='surrogateescape'))
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
    staged = any(f['status'][0] not in (' ', '?') for f in files)
    index_snapshot = hashlib.sha256(raw_output(conn, 'git ls-files --stage -z')).hexdigest() if staged and include_snapshot else ''
    return {'connected': True, 'files': files, 'snapshot': snapshot(conn, raw, files, head) if include_snapshot else '',
            'branch': branch, 'head': head[:12], 'remote': remote,
            'staged': staged, 'index_snapshot': index_snapshot}


def diff(conn, path):
    if not safe_file(path):
        raise RemoteError('Просмотр потенциально секретного файла отключён')
    current = status(conn)
    if path not in [f['path'] for f in current['files']]:
        raise RemoteError('Файл отсутствует в текущем списке изменений')
    item = next(f for f in current['files'] if f['path'] == path)
    if not item['allowed']:
        raise RemoteError('Просмотр потенциально секретного файла отключён')
    if not current['connected'] or item['status'] == '??':
        if 'raw_path' in item:
            # SFTP servers/libraries can insist on UTF-8 paths. Read the bounded
            # preview through an ASCII shell command for byte-encoded names.
            quoted = quote_path(item)
            code, content, err = conn.run('devlin_preview_path=' + quoted + '; '
                'if test -L "$devlin_preview_path"; then exit 43; fi; '
                'head -c 100001 -- "$devlin_preview_path"', max_output=110_000)
            if code == 43:
                raise RemoteError('Просмотр содержимого символьной ссылки запрещён')
            if code:
                raise command_failure(code, err, 'Просмотр файла «' + item.get('display_path', path) + '»')
        else:
            with conn.client.open_sftp() as sftp:
                remote_path = conn.path.rstrip('/') + '/' + path
                if stat.S_ISLNK(sftp.lstat(remote_path).st_mode):
                    raise RemoteError('Просмотр содержимого символьной ссылки запрещён')
                with sftp.open(remote_path, 'rb') as f:
                    content = f.read(100_001)
        if len(content) > 100_000:
            return 'Файл превышает 100 КБ. Откройте его на сервере.'
        if b'\x00' in content:
            return 'Двоичный файл: текстовый просмотр недоступен.'
        return content.decode('utf-8', errors='replace')
    base = 'HEAD' if current['head'] else '--cached'
    return conn.text('git --literal-pathspecs diff --no-ext-diff ' + base + ' -- ' + quote_path(item), max_output=200_000) or 'Нет текстового diff (например, изменение прав файла).'


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


def recovery(conn):
    """Offer only bounded repairs justified by the current remote observations."""
    checks, actions = [], []
    root = conn.text('pwd -P').strip()
    uid = conn.text('id -u').strip()
    if not root.startswith('/') or any(c in root for c in '\0\r\n') or not uid.isdigit():
        raise RemoteError('Не удалось безопасно определить папку проекта и SSH-пользователя')
    # Git status may create/remove index.lock while refreshing its cache. Do not
    # bind repairs to directory mtime changed by our own read operations.
    meta_code, meta, _ = conn.run("stat -c '%d:%i:%u:%g:%a' -- .git")
    code, out, err = conn.run('git rev-parse --is-inside-work-tree')
    error = (err + b'\n' + out).decode('utf-8', errors='replace').lower()
    quoted = shlex.quote(root)
    if code and ('dubious ownership' in error or 'unsafe repository' in error):
        checks.append({'ok': False, 'message': 'Git блокирует проект из-за другого владельца папки. Можно разрешить только эту папку для текущего SSH-пользователя.'})
        test_code, test_root, _ = conn.run('git -c safe.directory=' + quoted + ' rev-parse --show-toplevel')
        if test_code == 0 and test_root.decode('utf-8', errors='replace').strip() == root:
            actions.append({'id': 'trust-directory', 'label': 'Доверять папке проекта',
                'description': 'Добавить ' + root + ' в safe.directory текущего SSH-пользователя. Разрешение касается только этого проекта. Файлы, владельцы и коммиты сохраняются.'})
    elif code and meta_code == 0:
        checks.append({'ok': False, 'message': str(command_failure(code, err + b'\n' + out, 'Открытие существующего Git'))})
        checks.append({'ok': None, 'message': 'Приложение сохраняет существующую .git. Она не будет удалена или заменена новым репозиторием.'})
    # A recursive chmod is offered only for a traversable, wholly user-owned,
    # non-symlink metadata tree. Worktree files and permissions of others stay intact.
    own_code, _, _ = conn.run('test -d .git && ! test -L .git && '
        'test -z "$(find .git -xdev \\( -type l -o ! -uid ' + uid + ' \\) -print -quit)" && '
        'find .git -xdev -type d -print >/dev/null')
    mode_code, modes, _ = conn.run('find .git -xdev \\( -type f ! -perm -u+r -o '
        '-type d \\( ! -perm -u+r -o ! -perm -u+w -o ! -perm -u+x \\) \\) -print -quit')
    if meta_code == 0 and own_code == 0 and mode_code == 0 and modes:
        checks.append({'ok': False, 'message': 'Владелец файлов .git — текущий SSH-пользователь, но у владельца не хватает прав чтения, записи или прохода по папкам.'})
        actions.append({'id': 'owner-permissions', 'label': 'Восстановить права владельца .git',
            'description': 'Добавить владельцу чтение файлов .git и чтение, запись и проход по её папкам. Права остальных пользователей, содержимое файлов и история сохраняются; Git-объекты остаются защищены от изменения.'})
    if meta_code == 0 and own_code != 0:
        checks.append({'ok': None, 'message': 'Нельзя безопасно изменить права .git: внутри есть чужие файлы, ссылки или недоступные папки. Выберите SSH-профиль владельца проекта в настройках; приложение не может получить права другого пользователя.'})
    remote = ''
    if code == 0:
        _, raw_remote, _ = conn.run('git config --get remote.origin.url')
        remote = raw_remote.decode('utf-8', errors='replace').strip()
        profile = getattr(conn, 'git_profile', None)
        if profile and remote and not remote.startswith('https://'):
            checks.append({'ok': False, 'message': 'Выбран Git-профиль с HTTPS-токеном, но origin использует SSH. Можно проверить токен и переключить адрес этого же репозитория на HTTPS.'})
            try:
                target = profile_https_url(profile, remote)
                actions.append({'id': 'profile-https', 'label': 'Подключить через Git-профиль',
                    'description': 'Проверить доступ к ' + target + ' с токеном выбранного профиля и заменить адреса fetch/push origin только после успешной проверки. Коммиты, файлы, ветка и автор сохраняются.'})
            except RemoteError:
                checks.append({'ok': False, 'message': 'SSH-адрес относится к другому сервису или не поддерживается. Выберите подходящий Git-профиль или репозиторий в настройках подключения.'})
    profile = getattr(conn, 'git_profile', {}) or {}
    project = getattr(conn, 'project', {})
    snapshot = hashlib.sha256(json.dumps([root, uid, meta_code, meta.hex(), code, remote,
        {key:project.get(key, '') for key in ('id','ssh_host','ssh_port','ssh_user','ssh_fingerprint','git_profile_id')},
        profile.get('id', ''), profile.get('base_url', ''), actions], sort_keys=True).encode()).hexdigest()
    return {'checks': checks, 'actions': actions, 'repair_snapshot': snapshot}


def profile_https_url(profile, remote):
    validate_remote(remote)
    base = urlsplit(profile['base_url'])
    match = re.fullmatch(r'git@([a-zA-Z0-9_.-]+):([a-zA-Z0-9_./-]+)', remote)
    parsed = urlsplit(remote) if not match else None
    host = match[1] if match else parsed.hostname if parsed.scheme == 'ssh' and parsed.username == 'git' else ''
    path = match[2] if match else parsed.path.lstrip('/')
    if not host or host.lower() != base.hostname.lower():
        raise RemoteError('SSH-репозиторий и Git-профиль относятся к разным сервисам')
    target = 'https://' + base.netloc + '/' + path
    repository_path(profile, target)
    return target


def repair(conn, request):
    if not request.confirm:
        raise RemoteError('Подтвердите исправление Git в приложении')
    current = recovery(conn)
    if request.snapshot != current['repair_snapshot']:
        raise RemoteError('Состояние Git изменилось. Повторите диагностику перед исправлением.')
    if request.action not in {a['id'] for a in current['actions']}:
        raise RemoteError('Для текущей проблемы это исправление недоступно. Повторите диагностику.')
    if request.action == 'trust-directory':
        root = conn.text('pwd -P').strip()
        conn.text('git config --global --add safe.directory ' + shlex.quote(root), context='Разрешение папки Git')
    elif request.action == 'owner-permissions':
        # Never traverse symlinks or mount points; only entries validated above.
        conn.text('find .git -xdev -type d -exec chmod u+rwx -- {} + && '
            'find .git -xdev -type f -exec chmod u+r -- {} +', context='Восстановление прав .git')
    elif request.action == 'profile-https':
        current_repo = status(conn)
        target = profile_https_url(conn.git_profile, current_repo['remote'])
        Provider(conn.git_profile, conn.git_token).repository(target)
        code, out, err = network(conn, 'ls-remote ' + shlex.quote(target), target)
        if code:
            raise command_failure(code, err + b'\n' + out, 'Адрес origin сохранён; доступ с токеном не прошёл')
        latest = recovery(conn)
        if latest['repair_snapshot'] != request.snapshot:
            raise RemoteError('Состояние Git изменилось во время проверки. Origin сохранён; повторите диагностику.')
        conn.text('git remote set-url origin ' + shlex.quote(target))
        conn.run('git config --unset-all remote.origin.pushurl')
    # Verify the operation, without changing the index, worktree or history.
    conn.text('git rev-parse --is-inside-work-tree', context='Проверка Git после исправления')
    return {'ok': True, 'message': 'Настройки Git исправлены. Файлы и коммиты сохранены. Обновите состояние репозитория.'}


def diagnostics(conn, settings):
    """Read-only checks; never return arbitrary Git stderr or credentials."""
    checks = []
    code, out, _ = conn.run('git --version')
    checks.append({'ok': code == 0, 'message': 'Git установлен.' if code == 0 else 'Git не найден на сервере.'})
    if code:
        return {'checks': checks}
    recovered = recovery(conn)
    checks.extend(recovered['checks'])
    try:
        current = status(conn, include_snapshot=False)
    except RemoteError as exc:
        checks.append({'ok': False, 'message': str(exc)})
        return {**recovered, 'checks': checks}
    if not current['connected']:
        checks.append({'ok': False, 'message': 'В корне проекта ещё нет Git-репозитория.'})
        return {**recovered, 'checks': checks}
    access = access_settings(conn)
    if current['remote'].startswith('https://') and not getattr(conn, 'git_profile', None):
        checks.append({'ok': False, 'message': 'origin использует HTTPS: серверный SSH-ключ здесь не применяется. GitHub не принимает пароль аккаунта для Git. Выберите SSH URL через «Настроить подключение репозитория» или назначьте Git-профиль с токеном.'})
    elif current['remote']:
        checks.append({'ok': None, 'message': 'Транспорт Git: ' + ('HTTPS с Git-профилем.' if current['remote'].startswith('https://') else 'SSH с сервера.')})
    checks.append({'ok': None, 'message': ('Серверный SSH-ключ: ' + access['server_ssh_key'] if access['server_ssh_key'] else 'Серверный SSH-ключ явно не указан: используются настройки ~/.ssh/config, SSH-агент и стандартные ключи.') + (' Переменная GIT_SSH_COMMAND перекрывает core.sshCommand.' if access['environment_override'] else '')})
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
    return {**recovered, 'checks': checks}


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
        raise RemoteError('На сервере уже есть подготовленные изменения. В разделе «Git & файлы» нажмите «Убрать подготовку к коммиту», затем выберите файлы заново.')
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
    lookup = {f['path']: f for f in current['files']}
    paths = [(lookup[path], 'path') for path in files]
    identity = author_identity(conn, settings)
    for item in current['files']:
        if item['path'] in files and item.get('old_path'):
            paths.append((item, 'old_path'))
    hosting = getattr(conn, 'project', {}).get('hosting', False)
    local_git = 'git -c core.preloadIndex=false -c index.threads=1 -c pack.threads=1' if hosting else 'git'
    report = getattr(conn, 'report_progress', lambda *args: None)
    report('Добавление файлов в индекс', 0, len(paths))
    for number, (item, key) in enumerate(paths, 1):
        # One literal path per command: no large argument lists, and a failure
        # names exactly the file that failed rather than the first batch entries.
        conn.text('LC_ALL=C ' + local_git + ' --literal-pathspecs add -A -- ' + quote_path(item, key),
                  context='Добавление файла в индекс Git «' + item.get('display_' + key, item[key]) + '»')
        report('Добавление файлов в индекс', number, len(paths))
    report('Создание коммита', len(paths), len(paths))
    # If a hook fails, do not undo the server index or overwrite the user's work.
    environment = ' '.join(key + '=' + shlex.quote(value) for key,value in (
        ('GIT_AUTHOR_NAME',identity['name']), ('GIT_AUTHOR_EMAIL',identity['email']),
        ('GIT_COMMITTER_NAME',identity['name']), ('GIT_COMMITTER_EMAIL',identity['email'])))
    conn.text('LC_ALL=C ' + environment + ' ' + local_git + ' -c user.name=' + shlex.quote(identity['name']) + ' -c user.email=' + shlex.quote(identity['email']) +
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



def unstage(conn, request):
    """Clear only the index after confirming both worktree and staged object hashes."""
    if not request.confirm:
        raise RemoteError('Подтвердите отмену подготовки файлов к коммиту.')
    current = status(conn)
    if not current['connected'] or not current.get('staged'):
        raise RemoteError('Подготовленных изменений нет. Обновите список файлов.')
    if request.snapshot != current['snapshot'] or request.index_snapshot != current['index_snapshot']:
        raise RemoteError('Файлы или индекс изменились после просмотра. Обновите список и подтвердите заново.')
    if conn.text('git ls-files --unmerged -z'):
        raise RemoteError('В индексе есть конфликты слияния. Сначала разрешите их на сервере; подготовка не изменена.')
    if current['head']:
        conn.text('git reset --mixed HEAD', context='Отмена подготовки файлов к коммиту')
    else:
        # Unborn branches have no HEAD to reset to. --cached affects only the
        # index; force also allows staged content differing from the worktree.
        conn.text('git rm --cached -r -f --ignore-unmatch -- .', context='Отмена подготовки первого коммита')
    return {'ok': True, 'message': 'Подготовка к коммиту отменена. Файлы на сервере сохранены; выберите их и создайте коммит.'}


def sync(conn, request, settings):
    getattr(conn, 'report_progress', lambda *args: None)('Проверка состояния файлов', 0, 0)
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
