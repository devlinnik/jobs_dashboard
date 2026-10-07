import base64
import hashlib
import hmac
import os
import shlex
import socket
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

    def run(self, command, timeout=35, max_output=2_000_000):
        full = 'cd -- ' + shlex.quote(self.path) + ' && ' + command
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
            raise RemoteError('Команда сервера завершилась с ошибкой (код %s). %s' % (code, reason))
        return out.decode('utf-8', errors='replace')

    def disk(self):
        output = self.text('LC_ALL=C df -Pk -- ' + shlex.quote(self.path))
        fields = output.strip().splitlines()[-1].split()
        if len(fields) < 6:
            raise RemoteError('Не удалось разобрать ответ df')
        total, free = int(fields[1]) * 1024, int(fields[3]) * 1024
        return {'total': total, 'free': free, 'free_percent': round(free / total * 100, 1), 'source': 'SSH'}


@contextmanager
def connect(store, project):
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
        conn.text('test -d ' + shlex.quote(project['ssh_path']) + ' && pwd -P')
        yield conn
    except RemoteError:
        raise
    except paramiko.AuthenticationException as exc:
        raise RemoteError('SSH-аутентификация не прошла. Проверьте пользователя, пароль или ключ.') from exc
    except (OSError, paramiko.SSHException) as exc:
        raise RemoteError('SSH-подключение не удалось. Проверьте сеть, доступ и файл ключа.') from exc
    finally:
        client.close()
