"""Project-root .gitignore editor and read-only SFTP picker."""
import hashlib
from functools import wraps
import posixpath
import secrets
import shlex
import stat
import time
import paramiko
from .remote import RemoteError

LIMIT = 256_000


def sftp_errors(fn):
    @wraps(fn)
    def execute(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise RemoteError('Не удалось прочитать или сохранить файлы по SFTP. Проверьте путь проекта, права SSH-пользователя и доступность сервера. Правила в редакторе можно скопировать перед повтором.') from exc
    return execute


def read_file(sftp, root):
    path = posixpath.join(root, '.gitignore')
    try:
        info = sftp.lstat(path)
    except FileNotFoundError:
        return {'exists': False, 'content': '', 'snapshot': hashlib.sha256((root + '\0missing').encode()).hexdigest()}
    if not stat.S_ISREG(info.st_mode):
        raise RemoteError('.gitignore должен быть обычным файлом. Ссылки и папки не редактируются.')
    if info.st_size > LIMIT:
        raise RemoteError('.gitignore превышает 256 КБ. Отредактируйте его на сервере.')
    with sftp.open(path, 'rb') as f:
        data = f.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise RemoteError('.gitignore превышает 256 КБ.')
    try:
        content = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RemoteError('Для редактора .gitignore должен быть в UTF-8. Существующий файл не изменён.') from exc
    return {'exists': True, 'content': content, 'snapshot': hashlib.sha256(root.encode() + b'\0exists\0' + data).hexdigest()}


@sftp_errors
def read(conn):
    with conn.client.open_sftp() as sftp:
        sftp.get_channel().settimeout(15)
        return read_file(sftp, sftp.normalize(conn.path))


@sftp_errors
def save(conn, request):
    if not request.confirm:
        raise RemoteError('Подтвердите сохранение .gitignore')
    data = request.content.encode('utf-8')
    if len(data) > LIMIT or b'\x00' in data:
        raise RemoteError('.gitignore должен быть текстом до 256 КБ без нулевых символов.')
    with conn.client.open_sftp() as sftp:
        sftp.get_channel().settimeout(15)
        root = sftp.normalize(conn.path)
        current = read_file(sftp, root)
        if current['snapshot'] != request.snapshot:
            raise RemoteError('.gitignore изменился на сервере. Откройте редактор заново; ваши правила не записаны.')
        target = posixpath.join(root, '.gitignore')
        temp = posixpath.join(root, '.devlin-gitignore-' + secrets.token_hex(12))
        try:
            with sftp.open(temp, 'wx') as f:
                f.write(data)
            mode = stat.S_IMODE(sftp.lstat(target).st_mode) if current['exists'] else 0o644
            sftp.chmod(temp, mode)
            if read_file(sftp, root)['snapshot'] != request.snapshot:
                raise RemoteError('.gitignore изменился во время сохранения. Откройте редактор заново.')
            if current['exists']:
                # Rename in the same directory atomically publishes the complete file.
                conn.text('mv -f -- ' + shlex.quote(temp) + ' ' + shlex.quote(target))
            else:
                # A hard link publishes without replacing a file created concurrently.
                conn.text('ln -- ' + shlex.quote(temp) + ' ' + shlex.quote(target))
            return {'ok': True, 'message': '.gitignore сохранён. Обновите список изменений; уже отслеживаемые Git файлы остаются отслеживаемыми.'}
        finally:
            try:
                sftp.remove(temp)
            except OSError:
                pass


def literal_rule(path, directory=False):
    # Anchor to the project root and escape wildcards/spaces in actual file names.
    value = ''.join('\\' + c if c in '\\*?[] ' else c for c in path)
    return '/' + value + ('/' if directory else '')


@sftp_errors
def browse(conn, path=''):
    if path and (path.startswith('/') or any(p in ('', '.', '..', '.git') for p in path.split('/')) or
                 len(path) > 2048 or any(c in path for c in '\x00\r\n')):
        raise RemoteError('Выберите папку внутри проекта. Переход в .git или за пределы проекта запрещён.')
    with conn.client.open_sftp() as sftp:
        sftp.get_channel().settimeout(15)
        root = sftp.normalize(conn.path)
        current = root
        for part in path.split('/') if path else []:
            current = posixpath.join(current, part)
            if not stat.S_ISDIR(sftp.lstat(current).st_mode):
                raise RemoteError('Проводник открывает только обычные папки внутри проекта, без перехода по ссылкам.')
        entries, truncated = [], False
        deadline = time.monotonic() + 20
        for index, item in enumerate(sftp.listdir_iter(current, read_aheads=1)):
            if time.monotonic() > deadline:
                raise RemoteError('Чтение каталога превысило время ожидания.')
            if index >= 2000 or len(entries) >= 1000:
                truncated = True
                break
            name = item.filename
            if name in ('.', '..', '.git', '.gitignore') or name.startswith('.devlin-gitignore-') or '/' in name or any(c in name for c in '\x00\r\n'):
                continue
            relative = posixpath.join(path, name) if path else name
            directory = stat.S_ISDIR(item.st_mode or 0)
            entries.append({'name': name, 'path': relative, 'directory': directory,
                            'symlink': stat.S_ISLNK(item.st_mode or 0), 'rule': literal_rule(relative, directory)})
        entries.sort(key=lambda item: (not item['directory'], item['name'].casefold()))
        return {'path': path, 'parent': posixpath.dirname(path) if path else None,
                'entries': entries, 'truncated': truncated}
