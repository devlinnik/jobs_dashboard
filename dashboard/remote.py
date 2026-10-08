import base64
import hashlib
import hmac
import os
import posixpath
import shlex
import socket
import stat
import time
from contextlib import contextmanager
import paramiko


class RemoteError(Exception):
    pass


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def inspect_host(project):
    if not project.get('ssh_host'):
        raise RemoteError('SSH не настроен')
    try:
        sock = socket.create_connection((project['ssh_host'], project['ssh_port']), timeout=10)
        with paramiko.Transport(sock) as transport:
            transport.start_client(timeout=10)
            key = transport.get_remote_server_key()
            return {'fingerprint': fingerprint(key), 'algorithm': key.get_name(),
                    'host': project['ssh_host'], 'port': project['ssh_port']}
    except (OSError, paramiko.SSHException) as exc:
        raise RemoteError('Не удалось получить ключ SSH-сервера. Проверьте адрес, порт и сеть.') from exc


class PinnedKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        if not hmac.compare_digest(fingerprint(key), self.expected):
            raise RemoteError('Ключ SSH-сервера изменился. Подключение остановлено; проверьте отпечаток.')


def command_failure(code, err, context=''):
    # Git output may include a credential-bearing configured remote; do not return raw stderr.
    text = err.decode('utf-8', errors='replace').lower()
    reason = 'Проверьте Git/SSH-права и состояние проекта на сервере.'
    if 'disk quota exceeded' in text or 'no space left on device' in text:
        reason = 'Исчерпана квота хостинга или место на диске. Проверьте тариф в панели хостинга; освободите место перед повтором. Размер общего диска не показывает квоту аккаунта.'
    elif any(value in text for value in ('unable to create thread', 'cannot create async thread', 'pthread_create', 'resource temporarily unavailable', 'unable to fork', 'cannot fork')):
        reason = 'Хостинг ограничил создание потоков или процессов. В SSH-профиле включите режим хостинга: добавление, коммит, загрузка и отправка будут выполняться с ограничением потоков Git. Проверьте лимиты процессов аккаунта в панели хостинга.'
    elif any(value in text for value in ('cannot allocate memory', 'out of memory', 'unable to mmap', 'malloc failed')):
        reason = 'Git не хватило памяти в пределах лимитов аккаунта хостинга. В SSH-профиле включите режим хостинга, который ограничивает потоки и память упаковки при отправке. Если он уже включён, проверьте лимит памяти аккаунта и лишние процессы.'
    elif 'too many open files' in text:
        reason = 'Достигнут лимит открытых файлов аккаунта хостинга. Проверьте лимиты и работающие процессы в панели хостинга.'
    elif any(value in text for value in ('index file corrupt', 'index file smaller than expected', 'invalid index', 'bad index file', 'invalid object', 'bad object')):
        reason = 'Git обнаружил повреждённый индекс или объект. Не удаляйте .git: сохраните резервную копию и проверьте репозиторий через git fsck на сервере.'
    elif 'beyond a symbolic link' in text:
        reason = 'Выбранный путь проходит через символическую ссылку. Git не добавляет файлы через такой путь; выберите саму ссылку или реальную папку проекта.'
    elif 'insufficient permission for adding an object' in text or 'unable to write new index file' in text or 'unable to index file' in text or 'failed to insert into database' in text or 'unable to create temporary file' in text:
        reason = 'Git не смог записать файл в индекс или .git/objects. Проверьте владельца и права .git/objects и .git/index, дисковую квоту аккаунта и лимит количества файлов (inode). Подготовленные файлы могут остаться в индексе.'
    elif 'detected dubious ownership' in text or 'unsafe repository' in text:
        reason = 'Git не доверяет владельцу папки (safe.directory). Проверьте владельца и SSH-пользователя. Для доверенного проекта добавьте только его абсолютный путь в safe.directory через SSH; не используйте *. '
    elif 'index.lock' in text and ('exists' in text or 'unable to create' in text):
        reason = 'Git не может создать .git/index.lock: другая Git-операция, оставшаяся блокировка или нет прав записи. Проверьте диагностику. Блокировка автоматически не удаляется.'
    elif 'filter' in text and any(value in text for value in ('failed', 'cannot run', 'not found')):
        reason = 'Не работает Git-фильтр файла. Проверьте .gitattributes, настройки filter.* и доступность его программы (например Git LFS) на сервере. При добавлении файлов приложение сохраняет правила Git-фильтров.'
    elif any(s in text for s in ('author identity unknown', 'unable to auto-detect email', 'empty ident name', 'invalid author', 'empty ident email')):
        reason = 'Git не смог определить автора. Заполните имя и email вместе в Git-профиле либо исправьте user.name/user.email на сервере.'
    elif any(s in text for s in ('gpg failed', 'failed to write commit object', 'no secret key', 'cannot run gpg', 'could not sign')):
        reason = 'Не удалось создать или подписать объект коммита. Проверьте квоту и права .git/objects, а также commit.gpgsign и ключ подписи на сервере. Изменения в индексе сохранены.'
    elif 'read-only file system' in text or 'operation not permitted' in text:
        reason = 'Файловая система или файлы Git недоступны для записи. Проверьте права и ограничения хостинга.'
    elif any(value in text for value in ('gh001', 'exceeds github', 'large files detected', 'file exceeds', 'exceeds the maximum allowed', 'file size limit')):
        reason = 'Git-хостинг отклонил слишком большой файл в отправляемых коммитах. Для GitHub обычный Git допускает файлы до 100 МиБ. Добавление в .gitignore после коммита не убирает файл из истории. Нужен перенос файла в Git LFS или исправление ещё не отправленных коммитов; повтор push без этого не поможет.'
    elif 'workflow' in text and any(value in text for value in ('without', 'scope', 'permission', 'refusing to allow')):
        reason = 'Токен не разрешает изменение GitHub Actions (.github/workflows). В Git-профиле обновите токен: для classic PAT нужен scope workflow вместе с repo; для fine-grained PAT — Contents и Workflows с правом записи для выбранного репозитория.'
    elif any(value in text for value in ('gh006', 'gh013', 'protected branch', 'protected branch hook', 'repository rule', 'push declined due to repository')):
        reason = 'Отправка отклонена правилами или защитой ветки Git-хостинга. Возможны обязательный Pull/Merge Request, подпись коммитов или запрет секретов. Проверьте правила ветки и разрешённый способ отправки в GitHub/GitLab. Force push не выполняется.'
    elif any(value in text for value in ('write access to repository not granted', 'not allowed to push', 'permission to', 'http 403', 'requested url returned error: 403')):
        reason = 'Чтение репозитория доступно, но запись запрещена. В Git-профиле проверьте аккаунт и токен: GitHub — доступ к выбранному репозиторию и Contents: Read and write (classic PAT: repo); GitLab — write_repository. Аккаунт также должен иметь право записи, а токен — нужное одобрение организации/SSO.'
    elif 'non-fast-forward' in text or 'fetch first' in text:
        reason = 'На origin появились новые коммиты. Обновите список и используйте «Объединить историю с origin» либо «Загрузить из Git» для чистого проекта. Force push не выполняется.'
    elif any(value in text for value in ('rpc failed', 'remote end hung up', 'unexpected disconnect', 'connection reset', 'http 413', 'requested url returned error: 413', 'early eof')):
        reason = 'Передача Git прервана сетью, прокси или лимитом размера запроса. Проверьте доступность Git-хостинга и лимиты хостинга. Обновите список с origin перед повтором: отправка могла успеть завершиться.'
    elif 'rejected' in text or 'pre-receive hook declined' in text:
        reason = 'Git-хостинг отклонил отправку серверной проверкой. Проверьте правила ветки, ограничения файлов и права записи. Точная проверка не распознана; сохраните «Отчёт Git». Force push не выполняется.'
    elif 'permission denied' in text or 'authentication failed' in text or 'insufficient permission' in text:
        reason = 'Нет доступа. Проверьте SSH-пользователя, владельца и права записи .git, включая objects/refs; для сетевых операций — Git-профиль или SSH-ключ Git-хостинга.'
    elif 'host key verification failed' in text:
        reason = 'На сервере не подтверждён ключ Git-хостинга. Проверьте known_hosts через доверенный канал.'
    elif 'could not resolve' in text:
        reason = 'Не удалось разрешить имя Git-хостинга. Проверьте DNS и сеть сервера.'
    elif 'command not found' in text or 'git: not found' in text:
        reason = 'На сервере не установлен Git или нужная системная команда.'
    elif 'no such file or directory' in text or "can't cd" in text or 'cannot cd' in text:
        reason = ('Файл исчез или недоступен для чтения. Обновите список изменений и проверьте путь файла.'
                  if context.startswith('Проверка содержимого файла') else
                  'Папка проекта не найдена или недоступна. Выберите её через SSH-проводник.')
    elif 'not a git repository' in text:
        reason = ('Существующая .git не распознана: метаданные неполные, повреждены или недоступны. Проверьте диагностику и выбранную папку проекта в приложении. Существующая .git сохраняется.'
                  if 'существующего Git' in context or 'уже есть .git' in context else
                  'В выбранной папке нет Git-репозитория. Подключите его в разделе Git.')
    if code == 128:
        reason += ' Откройте «Git & файлы → Диагностика Git»: приложение покажет причину и доступные исправления.'
    return RemoteError((context + ': ' if context else '') + 'Команда сервера завершилась с ошибкой (код %s). %s' % (code, reason))


class Connection:
    def __init__(self, client, project):
        self.client = client
        self.project = project
        self.path = project['ssh_path']

    def run(self, command, timeout=35, max_output=2_000_000, in_project=True, input_data=None):
        self.last_command_metrics = {}
        debug_hint = ' Откройте «Git & файлы → Отчёт Git» и скопируйте отчёт.' if getattr(self, 'git_trace_active', False) else ''
        if in_project and not self.path:
            raise RemoteError('Папка проекта не выбрана. Откройте SSH-проводник в настройках проекта.')
        full = ('cd -- ' + shlex.quote(self.path) + ' && ' if in_project else '') + command
        channel = self.client.get_transport().open_session(timeout=10)
        channel.settimeout(1)
        channel.exec_command(full)
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            if input_data is not None:
                channel.sendall(input_data)
            channel.shutdown_write()
            while True:
                if time.monotonic() > deadline:
                    self.last_command_metrics = {'result': 'timeout', 'stdout_bytes':len(stdout), 'stderr_bytes':len(stderr)}
                    raise RemoteError('SSH-команда превысила время ожидания. Проверьте состояние на сервере перед повтором.' + debug_hint)
                if channel.recv_ready():
                    stdout.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(65536))
                if len(stdout) + len(stderr) > max_output:
                    self.last_command_metrics = {'result': 'output_limit', 'stdout_bytes':len(stdout), 'stderr_bytes':len(stderr)}
                    raise RemoteError(('Ответ сервера слишком большой: получено %s байт при лимите %s.' % (len(stdout)+len(stderr), max_output)) + debug_hint)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return channel.recv_exit_status(), bytes(stdout), bytes(stderr)
                time.sleep(0.015)
        finally:
            channel.close()

    def text(self, command, *, context='', **kwargs):
        code, out, err = self.run(command, **kwargs)
        if code:
            # Some hosting wrappers merge stderr into stdout. Classify both,
            # still return only fixed safe messages rather than raw server output.
            raise command_failure(code, err + b'\n' + out, context)
        return out.decode('utf-8', errors='replace')

    def check_directory(self):
        if not self.path:
            raise RemoteError('Папка проекта не выбрана. Выберите её через SSH-проводник в настройках проекта.')
        path = shlex.quote(self.path)
        try:
            code, out, _ = self.run(f'test -d {path} || exit 41; test -x {path} || exit 42; cd -- {path} && pwd -P', in_project=False)
        except paramiko.SSHException as exc:
            raise RemoteError('SSH подключён, но проверить папку через shell не удалось. Проверьте, разрешено ли выполнение команд этому пользователю.') from exc
        if code == 41:
            raise RemoteError('Папка проекта не существует или недоступна для этого SSH-пользователя. Выберите другую папку через SSH-проводник.')
        if code:
            raise RemoteError('SSH подключён, но перейти в папку проекта не удалось. Проверьте права доступа или выберите другую папку.')
        return out.decode('utf-8', errors='replace').strip()

    def directories(self, path=''):
        if path and (not path.startswith('/') or len(path) > 1024 or any(c in path for c in '\x00\r\n')):
            raise RemoteError('Укажите абсолютный путь к папке сервера.')
        try:
            with self.client.open_sftp() as sftp:
                sftp.get_channel().settimeout(15)
                current = sftp.normalize(path or '.')
                if not current.startswith('/') or len(current) > 1024 or any(c in current for c in '\x00\r\n'):
                    raise RemoteError('SFTP-сервер вернул неподдерживаемый путь.')
                if not stat.S_ISDIR(sftp.stat(current).st_mode):
                    raise RemoteError('Выбранный путь не является папкой.')
                folders, truncated = [], False
                deadline = time.monotonic() + 20
                for index, entry in enumerate(sftp.listdir_iter(current, read_aheads=1)):
                    if time.monotonic() > deadline:
                        raise RemoteError('Чтение каталога превысило время ожидания. Откройте более узкую папку по её пути.')
                    if index >= 5000 or len(folders) >= 1000:
                        truncated = True
                        break
                    name = entry.filename
                    child = posixpath.join(current, name)
                    if name in ('.', '..') or '/' in name or len(child) > 1024 or any(c in name for c in '\x00\r\n'):
                        continue
                    if stat.S_ISDIR(entry.st_mode or 0) or stat.S_ISLNK(entry.st_mode or 0):
                        folders.append({'name': name, 'path': child, 'symlink': stat.S_ISLNK(entry.st_mode or 0)})
                folders.sort(key=lambda item: item['name'].casefold())
                return {'path': current, 'parent': posixpath.dirname(current) if current != '/' else None,
                        'directories': folders, 'truncated': truncated}
        except PermissionError as exc:
            raise RemoteError('Нет прав на просмотр этой папки. Выберите другую или проверьте права SSH-пользователя.') from exc
        except FileNotFoundError as exc:
            raise RemoteError('Папка не найдена. Перейдите в домашнюю папку или укажите другой путь.') from exc
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise RemoteError('Не удалось прочитать каталог по SFTP. Проверьте, включён ли SFTP на сервере и есть ли права просмотра.') from exc

    def disk(self):
        if self.project.get('hosting'):
            return self.hosting_disk()
        output = self.text('LC_ALL=C df -Pk -- ' + shlex.quote(self.path))
        fields = output.strip().splitlines()[-1].split()
        if len(fields) < 6:
            raise RemoteError('Не удалось разобрать ответ df')
        total, free = int(fields[1]) * 1024, int(fields[3]) * 1024
        return {'total': total, 'free': free, 'free_percent': round(free / total * 100, 1), 'source': 'SSH',
                'kind': 'filesystem', 'note': 'Свободное место общей файловой системы; квота аккаунта хостинга не учитывается.'}

    def hosting_disk(self):
        root = self.project.get('hosting_path', '')
        limit = self.project.get('hosting_limit_gb', 0)
        unit = self.project.get('hosting_unit', 'GB')
        if not root or not root.startswith('/') or root.rstrip('/') == '' or limit <= 0:
            raise RemoteError('В SSH-профиле хостинга укажите лимит тарифа и корневую папку аккаунта.')
        real_root = self.text('cd -- ' + shlex.quote(root) + ' && pwd -P', in_project=False).strip()
        if real_root == '/':
            raise RemoteError('Для хостинга выберите папку аккаунта, а не корень сервера /.')
        if self.path:
            real_project = self.check_directory()
            if real_project != real_root and not real_project.startswith(real_root.rstrip('/') + '/'):
                raise RemoteError('Проект находится вне папки аккаунта хостинга. Проверьте корневой путь в SSH-профиле.')
        code, out, _ = self.run('LC_ALL=C du -sk -- ' + shlex.quote(real_root), in_project=False, timeout=90, max_output=4096)
        if code:
            raise RemoteError('Не удалось полностью измерить аккаунт хостинга: проверьте права чтения всех папок и наличие du. Частичный результат не используется.')
        try:
            used = int(out.decode().split()[0]) * 1024
            if used < 0:
                raise ValueError()
        except (ValueError, IndexError) as exc:
            raise RemoteError('Команда du вернула неверный размер аккаунта.') from exc
        total = int(limit * (1024**3 if unit == 'GiB' else 1000**3))
        if total <= 0:
            raise RemoteError('Лимит тарифа должен быть больше одного байта.')
        free = max(0, total - used)
        return {'total': total, 'used': used, 'free': free, 'free_percent': round(free / total * 100, 1),
                'kind': 'hosting_estimate', 'estimated': True, 'source': 'SSH · оценка тарифа',
                'path': real_root, 'limit': limit, 'unit': unit, 'over_limit': max(0, used - total),
                'note': 'Лимит тарифа минус размер всей папки аккаунта (du). Почта, базы данных и другие данные вне этой папки не учтены. Точная квота — в панели хостинга.'}


@contextmanager
def connect(store, project, require_project=True):
    if not project.get('ssh_host'):
        raise RemoteError('Сначала настройте SSH проекта')
    if not project.get('ssh_fingerprint'):
        raise RemoteError('Сначала подтвердите отпечаток SSH-сервера')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedKey(project['ssh_fingerprint']))
    try:
        key_path = os.path.expanduser(project.get('ssh_key', '')) or None
        password = store.decrypt(project.get('ssh_password_enc', '')) or None
        client.connect(project['ssh_host'], port=project['ssh_port'], username=project['ssh_user'],
                       password=password, key_filename=key_path, passphrase=password,
                       timeout=10, banner_timeout=10, auth_timeout=15,
                       allow_agent=True, look_for_keys=True)
        client.get_transport().set_keepalive(20)
        conn = Connection(client, project)
        if project.get('git_profile_id'):
            conn.git_profile = store.git_profile(project['git_profile_id'])
            conn.git_token = store.decrypt(conn.git_profile.get('token_enc', ''))
        # Require a real directory, never create or move a server project implicitly.
        if require_project:
            conn.check_directory()
        yield conn
    except RemoteError:
        raise
    except paramiko.AuthenticationException as exc:
        raise RemoteError('SSH-аутентификация не прошла. Проверьте пользователя, пароль или ключ.') from exc
    except (OSError, paramiko.SSHException) as exc:
        raise RemoteError('SSH-подключение не удалось. Проверьте сеть, доступ и файл ключа.') from exc
    finally:
        client.close()
