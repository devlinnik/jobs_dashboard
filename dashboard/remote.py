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


class Connection:
    def __init__(self, client, project):
        self.client = client
        self.project = project
        self.path = project['ssh_path']

    def run(self, command, timeout=35, max_output=2_000_000, in_project=True):
        if in_project and not self.path:
            raise RemoteError('Папка проекта не выбрана. Откройте SSH-проводник в настройках проекта.')
        full = ('cd -- ' + shlex.quote(self.path) + ' && ' if in_project else '') + command
        channel = self.client.get_transport().open_session(timeout=10)
        channel.settimeout(1)
        channel.exec_command(full)
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() > deadline:
                    raise RemoteError('SSH-команда превысила время ожидания. Проверьте состояние на сервере перед повтором.')
                if channel.recv_ready():
                    stdout.extend(channel.recv(65536))
                if channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(65536))
                if len(stdout) + len(stderr) > max_output:
                    raise RemoteError('Ответ сервера слишком большой. Сократите число файлов или размер diff.')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return channel.recv_exit_status(), bytes(stdout), bytes(stderr)
                time.sleep(0.015)
        finally:
            channel.close()

    def text(self, command, **kwargs):
        code, out, err = self.run(command, **kwargs)
        if code:
            # Git output may include a credential-bearing configured remote; do not return raw stderr.
            text = err.decode('utf-8', errors='replace').lower()
            reason = 'Проверьте Git/SSH-права и состояние проекта на сервере.'
            if 'non-fast-forward' in text or 'rejected' in text:
                reason = 'Push отклонён: история веток разошлась или ветка защищена. Force push не выполняется.'
            elif 'permission denied' in text or 'authentication failed' in text:
                reason = 'Нет доступа. Проверьте SSH-ключ сервера и права записи в репозиторий.'
            elif 'host key verification failed' in text:
                reason = 'На сервере не подтверждён ключ Git-хостинга. Проверьте known_hosts через доверенный канал.'
            elif 'could not resolve' in text:
                reason = 'Не удалось разрешить имя Git-хостинга. Проверьте DNS и сеть сервера.'
            elif 'command not found' in text or 'git: not found' in text:
                reason = 'На сервере не установлен Git или нужная системная команда.'
            elif 'no such file or directory' in text or "can't cd" in text or 'cannot cd' in text:
                reason = 'Папка проекта не найдена или недоступна. Выберите её через SSH-проводник.'
            elif 'not a git repository' in text:
                reason = 'В выбранной папке нет Git-репозитория. Подключите его в разделе Git.'
            raise RemoteError('Команда сервера завершилась с ошибкой (код %s). %s' % (code, reason))
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
        output = self.text('LC_ALL=C df -Pk -- ' + shlex.quote(self.path))
        fields = output.strip().splitlines()[-1].split()
        if len(fields) < 6:
            raise RemoteError('Не удалось разобрать ответ df')
        total, free = int(fields[1]) * 1024, int(fields[3]) * 1024
        return {'total': total, 'free': free, 'free_percent': round(free / total * 100, 1), 'source': 'SSH'}


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
