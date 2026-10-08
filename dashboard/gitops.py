import hashlib
import base64
import json
import re
import shlex
import stat
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from .remote import RemoteError, command_failure
from .gitprovider import Provider, repository_path

EXCLUDED_DIRS = {'.git', '.ssh', '.venv', 'venv', 'node_modules', 'vendor', '__pycache__', '.idea', '.devlin-git-backups'}
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


def remote_digest(conn, command):
    """Hash an arbitrary stream on the server without hiding producer errors."""
    script = ('devlin_digest_dir=$(mktemp -d "${TMPDIR:-/tmp}/devlin-git-digest.XXXXXX") || exit; '
              'trap \'rm -rf -- "$devlin_digest_dir"\' EXIT; '
              '( ' + command + '; devlin_digest_code=$?; '
              'printf "%s\\n" "$devlin_digest_code" > "$devlin_digest_dir/status" ) | sha256sum; '
              'devlin_hash_code=$?; test "$devlin_hash_code" -eq 0 || exit "$devlin_hash_code"; '
              'test -s "$devlin_digest_dir/status" || exit 1; '
              'read -r devlin_digest_code < "$devlin_digest_dir/status"; exit "$devlin_digest_code"')
    code, out, err = conn.run(script)
    digest = out.split()[0].decode('ascii', errors='replace') if out.split() else ''
    if not code and not re.fullmatch(r'[0-9a-f]{64}', digest):
        return 1, '', b'Invalid SHA256 response'
    return code, digest if not code else '', err


def status_preview(conn):
    """Consume the entire NUL-delimited status remotely, send a bounded prefix.

    Keep rename pairs together and observe staged entries beyond the preview.
    The producer's exit code is retained even though awk drains its output.
    """
    awk = r'''BEGIN { RS="\0"; ORS=""; nul=sprintf("%c",0) }
function keep(entry) {
    total++;
    if (substr(entry,1,1)!=" " && substr(entry,1,1)!="?") staged=1;
    if (!full && kept<2000 && bytes+length(entry)+1<=800000) {
        preview=preview entry nul; bytes+=length(entry)+1; kept++;
    } else full=1;
}
{
    if (pending!="") { keep(pending nul $0); pending=""; }
    else if (substr($0,1,2) ~ /[RC]/) pending=$0;
    else keep($0);
}
END {
    if (pending!="") exit 44;
    printf "%d %d %d%c",total,kept,staged,0;
    printf "%s",preview;
}'''
    command = ('devlin_status_dir=$(mktemp -d "${TMPDIR:-/tmp}/devlin-git-status.XXXXXX") || exit; '
               'trap \'rm -rf -- "$devlin_status_dir"\' EXIT; '
               '(git status --porcelain=v1 -z --untracked-files=all; devlin_status_code=$?; '
               'printf "%s\\n" "$devlin_status_code" > "$devlin_status_dir/code") | LC_ALL=C awk ' + shlex.quote(awk) + '; '
               'devlin_awk_code=$?; test "$devlin_awk_code" -eq 0 || exit "$devlin_awk_code"; '
               'test -s "$devlin_status_dir/code" || exit 1; '
               'read -r devlin_status_code < "$devlin_status_dir/code"; exit "$devlin_status_code"')
    raw = raw_output(conn, command)
    try:
        header, preview = raw.split(b'\0', 1)
        total, shown, staged = map(int, header.split())
        if not 0 <= shown <= min(total, 2000) or staged not in (0, 1):
            raise ValueError
    except (ValueError, TypeError):
        raise RemoteError('Не удалось прочитать ограниченный список изменений Git')
    return preview, total, shown, bool(staged)


def safe_file(path):
    p = PurePosixPath(path)
    if not path or p.is_absolute() or '..' in p.parts or any(c in path for c in '\x00\r\n'):
        return False
    if any(part in EXCLUDED_DIRS for part in p.parts):
        return False
    if any(part.startswith('.devlin-git-new.') for part in p.parts):
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
    network_timeout = 600 if arguments.startswith(('push ', 'fetch ')) else 90
    # Hosting limits also apply to pack creation during fetch/push, not only add.
    local_git = ('git -c core.preloadIndex=false -c index.threads=1 -c pack.threads=1 '
                 '-c pack.windowMemory=32m -c pack.deltaCacheSize=16m '
                 if getattr(conn, 'project', {}).get('hosting', False) else 'git ')
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
        return conn.run(setup + common + local_git + arguments, timeout=network_timeout)
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
    script += local_git + '-c protocol.file.allow=never -c protocol.ext.allow=never ' + arguments
    return conn.run(script, timeout=network_timeout, input_data=(authorization + '\n').encode())


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
            if b'not a git repository' in (err + b'\n' + out).lower():
                raise RemoteError('В папке уже есть .git, но Git её не распознал. Ниже показаны проверка структуры и доступные действия восстановления.')
            raise command_failure(code, err + b'\n' + out, 'В папке уже есть .git, но репозиторий недоступен. Запустите диагностику и исправление в приложении')
        files = candidates(conn) if include_snapshot else []
        raw = json.dumps(files, sort_keys=True)
        return {'connected': False, 'files': files, 'snapshot': snapshot(conn, raw, files) if include_snapshot else '', 'branch': '', 'head': '', 'remote': ''}
    root = conn.text('git rev-parse --show-toplevel').strip()
    real = conn.text('pwd -P').strip()
    if root != real:
        raise RemoteError('Указанная директория находится внутри другого репозитория. Укажите корень Git-проекта.')
    raw_bytes, total_files, shown_files, staged = status_preview(conn)
    raw = raw_bytes.hex()
    if include_snapshot and total_files > shown_files:
        code, digest, error = remote_digest(conn, 'git status --porcelain=v1 -z --untracked-files=all')
        if code:
            raise command_failure(code, error, 'Проверка полного списка изменений Git')
        raw += digest
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
    if len(files) != shown_files:
        raise RemoteError('Git вернул неполный список изменений. Обновите вкладку Git.')
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
    index_snapshot = ''
    if staged and include_snapshot:
        index_code, index_snapshot, index_error = remote_digest(conn, 'git ls-files --stage -z')
        if index_code:
            raise command_failure(index_code, index_error, 'Проверка индекса Git')
    return {'connected': True, 'files': files, 'snapshot': snapshot(conn, raw, files, head) if include_snapshot else '',
            'branch': branch, 'head': head[:12], 'remote': remote,
            'staged': staged, 'index_snapshot': index_snapshot,
            'total_files': total_files, 'truncated': total_files > shown_files}


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
    structure = {}
    if code and meta_code == 0 and 'not a git repository' in error:
        for name, kind in (('HEAD','f'), ('objects','d'), ('refs','d'), ('config','f')):
            path = shlex.quote('.git/' + name)
            probe, _, _ = conn.run('if ! test -e ' + path + ' && ! test -L ' + path + '; then exit 41; fi; '
                'test -' + kind + ' ' + path + ' || exit 42; test -r ' + path + ' || exit 43')
            structure[name] = probe
            label = {0:'доступен',41:'отсутствует',42:'неверный тип или повреждённая ссылка',43:'нет прав чтения'}.get(probe,'недоступен')
            checks.append({'ok': probe == 0, 'message': '.git/' + name + ': ' + label + '.'})
        head_code, head, _ = conn.run('head -c 8192 -- .git/HEAD')
        structure['head_bytes'] = head.hex() if head_code == 0 else ''
        if structure['HEAD'] == 0:
            head_valid = bool(re.fullmatch(rb'(?:ref: refs/[^\x00\r\n ]+|[0-9a-f]{40}|[0-9a-f]{64})\n?', head))
            structure['head_valid'] = head_valid
            if not head_valid:
                checks.append({'ok': False, 'message': '.git/HEAD имеет неверный формат: Git не может определить текущую ветку.'})
        config_code, _, _ = conn.run('git config --file .git/config --list >/dev/null')
        structure['config_code'] = config_code
        if structure['config'] == 0 and config_code:
            checks.append({'ok': False, 'message': '.git/config содержит некорректные настройки. Его содержимое сохранится в резервной копии.'})
        pointer_code, pointer, _ = conn.run('test -f .git && head -c 8192 -- .git')
        structure['pointer'] = pointer.hex() if pointer_code == 0 else ''
        if pointer_code == 0:
            checks.append({'ok': False, 'message': '.git — файл ссылки на другой Git-каталог. Указанный каталог не открывается; возможен перенос проекта с другого ПК или сервера.'})
    elif code and meta_code == 0:
        checks.append({'ok': False, 'message': str(command_failure(code, err + b'\n' + out, 'Открытие существующего Git'))})
    # A recursive chmod is offered only for a traversable, wholly user-owned,
    # non-symlink metadata tree. Worktree files and permissions of others stay intact.
    own_code, _, _ = conn.run('(test -d .git || test -f .git) && ! test -L .git && '
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
    backup_base = ''
    if structure and own_code == 0 and mode_code == 0 and not modes:
        manifest_code, manifest, _ = remote_digest(conn, "find .git -xdev -printf '%P:%y:%s:%T@:%m:%u:%g\\0'")
        structure['manifest'] = manifest
        home_code, home_out, _ = conn.run('cd -- "$HOME" && pwd -P')
        home = home_out.decode('utf-8', errors='replace').strip()
        if home_code == 0 and home.startswith('/') and home != '/' and not any(c in home for c in '\0\r\n'):
            backup_base = home + '/.devlin-git-backups/' + hashlib.sha256(root.encode()).hexdigest()[:24]
            writable, _, _ = conn.run('test -w ' + shlex.quote(home) + ' && ! test -L ' + shlex.quote(home + '/.devlin-git-backups') + ' && ! test -L ' + shlex.quote(backup_base))
            if writable == 0 and manifest_code == 0:
                branch = getattr(conn, 'project', {}).get('git_branch') or 'main'
                try:
                    validate_branch(branch)
                except RemoteError:
                    branch = ''
                broken_structure = (any(structure.get(p, 0) for p in ('HEAD','objects','refs')) or
                                    structure.get('head_valid') is False or
                                    (structure.get('config') == 0 and config_code != 0))
                if branch and broken_structure:
                    structure['branch'] = branch
                    refs_code, refs, _ = conn.run(
                        'find .git/refs/heads -type f -print -quit || exit; '
                        'test ! -f .git/refs/heads/' + shlex.quote(branch) + ' || printf "chosen\\n"; '
                        'if test -f .git/packed-refs; then awk -v chosen=' + shlex.quote('refs/heads/' + branch) +
                        ' \'$2 == chosen { found=1 } $2 ~ /^refs\\// { any=1 } '
                        'END { if (any) print "refs"; if (found) print "chosen" }\' .git/packed-refs; fi')
                    # Restore HEAD only when the chosen branch exists, or no
                    # branches exist yet. Do not guess among existing histories.
                    has_branch = refs_code == 0 and b'chosen' in refs.splitlines()
                    other_branches = refs_code != 0 or bool(refs.strip())
                    if (structure['HEAD'] == 41 or structure.get('head_valid') is False) and structure['objects'] == 0 and structure['refs'] == 0 and config_code == 0 and (has_branch or not other_branches):
                        actions.append({'id':'restore-head','label':'Восстановить HEAD',
                            'description':'Сохранить копию метаданных в ' + backup_base + ' и восстановить ссылку HEAD на ветку ' + branch + '. Объекты Git, индекс, коммиты и файлы сайта сохраняются.'})
                    root_write, _, _ = conn.run('test -w .')
                    structure['root_write'] = root_write
                    if root_write == 0:
                        actions.append({'id':'archive-reinitialize','label':'Сохранить .git и настроить заново',
                            'description':'Перенести всю неисправную .git в резервную копию ' + backup_base + ' и создать новый Git в ветке ' + branch + '. Файлы сайта останутся на месте. Новый Git начнёт пустую историю; исходная .git останется в резервной копии, её история не будет перенесена в новый Git. После этого подключите репозиторий через приложение.'})
                    else:
                        checks.append({'ok': False, 'message':'SSH-пользователь не может изменять корень проекта. Для переустановки метаданных выберите профиль владельца этой папки.'})
    if structure and not actions:
        checks.append({'ok': None, 'message': 'Автоматическое восстановление недоступно для текущих прав или структуры. В настройках проекта можно выбрать правильную папку и SSH-профиль её владельца.'})
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
        profile.get('id', ''), profile.get('base_url', ''), structure, actions], sort_keys=True).encode()).hexdigest()
    return {'checks': checks, 'actions': actions, 'repair_snapshot': snapshot, 'backup_base':backup_base, 'structure':structure}


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
    backup = ''
    if request.action in ('restore-head','archive-reinitialize'):
        backup = current['backup_base'] + '/' + uuid.uuid4().hex
        branch = current['structure']['branch']
        base = shlex.quote(current['backup_base']); dest = shlex.quote(backup)
        conn.text('umask 077; mkdir -p -- ' + base + ' && mkdir -- ' + dest, context='Создание папки резервной копии Git')
        if request.action == 'restore-head':
            conn.text('cp -a -- .git ' + shlex.quote(backup + '/original.git'), timeout=90, context='Резервная копия Git перед восстановлением HEAD')
            latest = recovery(conn)
            if latest['repair_snapshot'] != request.snapshot:
                raise RemoteError('Метаданные изменились. Резервная копия сохранена; повторите диагностику.')
            conn.text('devlin_head=$(mktemp .git/HEAD.devlin.XXXXXX) || exit; '
                'trap \'rm -f -- "$devlin_head"\' EXIT; '
                '(test ! -f .git/HEAD || chmod --reference=.git/HEAD "$devlin_head") && '
                'printf "%s\\n" ' + shlex.quote('ref: refs/heads/' + branch) + ' > "$devlin_head" && mv -- "$devlin_head" .git/HEAD', context='Восстановление HEAD')
        else:
            # Prepare fresh metadata before archiving the original. The final
            # install is a same-directory rename; no checkout or reset occurs.
            conn.text('umask 077; devlin_new_git=$(mktemp -d .devlin-git-new.XXXXXX) || exit; '
                'trap \'rm -rf -- "$devlin_new_git"\' EXIT; '
                'git init --bare --template= -b ' + shlex.quote(branch) + ' "$devlin_new_git" && '
                'git --git-dir="$devlin_new_git" config core.bare false && '
                'mv -- .git ' + shlex.quote(backup + '/original.git') + ' && '
                '(mv -- "$devlin_new_git" .git || { test -e .git || mv -- ' + shlex.quote(backup + '/original.git') + ' .git; exit 1; })', timeout=90, context='Сохранение неисправной .git и настройка нового Git')
    elif request.action == 'trust-directory':
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
    message = ('Создан новый Git. Файлы сайта сохранены. Подключите репозиторий через приложение. Старая .git сохранена в ' + backup
               if request.action == 'archive-reinitialize' else 'Настройки Git исправлены. Файлы и коммиты сохранены. Обновите состояние репозитория.' + (' Резервная копия: ' + backup if backup else ''))
    return {'ok': True, 'message': message, 'backup':backup}


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
        if not any(c['ok'] is False for c in checks):
            checks.append({'ok': False, 'message': str(exc)})
        return {**recovered, 'checks': checks}
    if not current['connected']:
        checks.append({'ok': False, 'message': 'В корне проекта ещё нет Git-репозитория.'})
        return {**recovered, 'checks': checks}
    access = access_settings(conn)
    if current.get('truncated'):
        checks.append({'ok':None, 'message':'В проекте %s изменений. Показаны первые %s; список ограничен для работы с большим проектом. Исключите ненужные папки через редактор .gitignore или создавайте коммиты из показанных файлов по частям.' % (current['total_files'], len(current['files']))})
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


def commit(conn, current, files, message, settings, push=False, *, all_changes=False):
    if current.get('staged'):
        raise RemoteError('На сервере уже есть подготовленные изменения. В разделе «Git & файлы» нажмите «Убрать подготовку к коммиту», затем выберите файлы заново.')
    allowed = {f['path'] for f in current['files'] if f['allowed']}
    if not all_changes and (len(set(files)) != len(files) or any(p not in allowed for p in files)):
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
    if all_changes:
        report('Добавление всех изменений проекта в индекс Git', 0, 0)
        conn.text('LC_ALL=C GIT_TERMINAL_PROMPT=0 ' + local_git + ' add -A -- .', timeout=600,
                  context='Добавление всех изменений проекта в индекс Git')
    else:
        report('Добавление файлов в индекс', 0, len(paths))
    for number, (item, key) in enumerate(paths, 1):
        # One literal path per command: no large argument lists, and a failure
        # names exactly the file that failed rather than the first batch entries.
        conn.text('LC_ALL=C ' + local_git + ' --literal-pathspecs add -A -- ' + quote_path(item, key),
                  context='Добавление файла в индекс Git «' + item.get('display_' + key, item[key]) + '»')
        report('Добавление файлов в индекс', number, len(paths))
    report('Создание одного коммита из всех изменений' if all_changes else 'Создание коммита', len(paths), len(paths))
    # If a hook fails, do not undo the server index or overwrite the user's work.
    environment = ' '.join(key + '=' + shlex.quote(value) for key,value in (
        ('GIT_AUTHOR_NAME',identity['name']), ('GIT_AUTHOR_EMAIL',identity['email']),
        ('GIT_COMMITTER_NAME',identity['name']), ('GIT_COMMITTER_EMAIL',identity['email'])))
    conn.text('LC_ALL=C ' + environment + ' ' + local_git + ' -c user.name=' + shlex.quote(identity['name']) + ' -c user.email=' + shlex.quote(identity['email']) +
              (' commit --quiet -m ' if all_changes else ' commit -m ') + shlex.quote(message),
              timeout=600 if all_changes else 90, context='Создание коммита из подготовленных файлов')
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


def commit_all(conn, request, settings):
    """Explicit whole-project commit; gitignore applies, selector filtering does not."""
    if not request.confirm:
        raise RemoteError('Подтвердите создание коммита из всех изменений проекта.')
    getattr(conn, 'report_progress', lambda *args: None)('Проверка всего проекта перед коммитом', 0, 0)
    current = status(conn)
    if not current['connected']:
        raise RemoteError('Сначала подключите локальный Git через приложение.')
    if request.snapshot != current['snapshot']:
        raise RemoteError('Изменения проекта поменялись после просмотра. Обновите список и подтвердите коммит заново.')
    if not current['files']:
        raise RemoteError('Изменений для коммита нет.')
    return commit(conn, current, [], request.message, settings, all_changes=True)


def reconcile_plan(conn, settings):
    """Review a merge of histories whose resulting tree is the committed server tree."""
    getattr(conn, 'report_progress', lambda *args: None)('Проверка истории origin и сервера', 0, 0)
    current = status(conn, include_snapshot=False)
    if not current['connected'] or not current['head'] or not current['remote']:
        raise RemoteError('Подключите origin и сначала создайте коммит файлов сервера.')
    enforce_branch(conn, current)
    if current['files']:
        raise RemoteError('Сначала сохраните изменения сервера кнопкой «Создать коммит из всех изменений» или коммитом выбранных файлов. Объединение истории требует чистого проекта; файлы сервера не изменены.')
    history_data = fetch_commits(conn, settings)
    if not history_data['behind']:
        raise RemoteError('Новых коммитов origin для объединения нет. Обновите список и отправьте свои коммиты.')
    branch = validate_branch(history_data['branch'])
    tip = conn.text('git rev-parse --verify ' + shlex.quote('refs/remotes/origin/' + branch)).strip()
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', tip):
        raise RemoteError('Не удалось определить коммит origin.')
    summary = conn.text('LC_ALL=C git diff --no-ext-diff --no-textconv --shortstat ' + tip + ' ' + history_data['head'], timeout=600).strip()
    return {'expected_head':history_data['head'], 'expected_remote_tip':tip,
            'expected_branch':branch, 'expected_remote':history_data['remote'],
            'behind':history_data['behind'], 'ahead':history_data['ahead'], 'summary':summary,
            'remote_commits':log_commits(conn, history_data['head'] + '..' + tip)}


def reconcile_server(conn, request, settings):
    if not request.confirm:
        raise RemoteError('Подтвердите объединение истории с сохранением версии сервера.')
    plan = reconcile_plan(conn, settings)
    if any(getattr(request, key) != plan[key] for key in ('expected_head','expected_remote_tip','expected_branch','expected_remote')):
        raise RemoteError('История сервера или origin изменилась после просмотра. Обновите план объединения и подтвердите заново.')
    identity = author_identity(conn, settings)
    environment = ' '.join(key + '=' + shlex.quote(value) for key,value in (
        ('GIT_AUTHOR_NAME',identity['name']), ('GIT_AUTHOR_EMAIL',identity['email']),
        ('GIT_COMMITTER_NAME',identity['name']), ('GIT_COMMITTER_EMAIL',identity['email'])))
    report = getattr(conn, 'report_progress', lambda *args: None)
    report('Объединение истории без изменения файлов сервера', 0, 0)
    tree = conn.text('git rev-parse ' + plan['expected_head'] + '^{tree}').strip()
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', tree):
        raise RemoteError('Не удалось проверить дерево коммита сервера.')
    message = 'Devlin Desk: merge origin/' + plan['expected_branch'] + ', keep server version'
    merged = conn.text(environment + ' git -c user.name=' + shlex.quote(identity['name']) + ' -c user.email=' + shlex.quote(identity['email']) +
        ' commit-tree ' + tree + ' -p ' + plan['expected_head'] + ' -p ' + plan['expected_remote_tip'] + ' -m ' + shlex.quote(message), timeout=600,
        context='Создание коммита объединения истории').strip()
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', merged):
        raise RemoteError('Не удалось проверить коммит объединения истории.')
    # Compare-and-swap only the branch ref. Index and website files stay intact.
    if conn.text('git symbolic-ref --short -q HEAD').strip() != plan['expected_branch']:
        raise RemoteError('Ветка сервера изменилась; объединение не применено.')
    conn.text('git update-ref ' + shlex.quote('refs/heads/' + plan['expected_branch']) + ' ' + merged + ' ' + plan['expected_head'],
              context='Применение объединённой истории')
    return {'ok':True, 'commit':merged[:12], 'message':'История объединена с сохранением версии сервера. Файлы проекта не изменены. Теперь отправьте коммиты в origin отдельной кнопкой.'}



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
        conn.text('git reset --mixed --quiet HEAD', timeout=600, context='Отмена подготовки файлов к коммиту')
    else:
        # Unborn branches have no HEAD to reset to. --cached affects only the
        # index; force also allows staged content differing from the worktree.
        conn.text('git rm --cached -r -f --quiet --ignore-unmatch -- .', timeout=600, context='Отмена подготовки первого коммита')
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
        raise RemoteError('На origin есть новые коммиты. Сохраните изменения сервера коммитом, затем используйте «Объединить историю с origin» в списке коммитов. Для загрузки версии origin в чистый проект доступно «Загрузить из Git».')
    if outgoing['ahead'] == 0:
        return {'message': 'Неотправленных коммитов нет: текущая ветка уже актуальна.'}
    # Pin the reviewed commit and destination: a concurrent server commit must not
    # silently enter this push after the final HEAD check.
    revision = outgoing['head'] + ':refs/heads/' + outgoing['branch']
    code, out, err = network(conn, 'push origin ' + shlex.quote(revision), remote)
    if code:
        raise command_failure(code, err + b'\n' + out, 'Отправка коммитов в origin')
    return {'message': 'Коммиты отправлены в текущую ветку origin'}
