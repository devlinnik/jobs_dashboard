import re
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, field_validator, model_validator


def web_url(value, https=False):
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in (('https',) if https else ('http', 'https')) or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Нужен адрес HTTPS' if https else 'Нужен адрес HTTP/HTTPS без логина и пароля')
    if any(c in value for c in '\r\n'):
        raise ValueError('Некорректный адрес')
    return value


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=400)
    url: str = Field(default='', max_length=2048)
    expected_codes: str = '200'
    expected_text: str = Field(default='', max_length=300)
    interval: int = Field(default=300, ge=30, le=86400)
    enabled: bool = True
    notify: bool = True
    ssh_host: str = Field(default='', max_length=253)
    ssh_profile_id: str = Field(default='', max_length=100)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default='', max_length=80)
    ssh_path: str = Field(default='', max_length=1024)
    ssh_key: str = Field(default='', max_length=1024)
    ssh_password: str | None = Field(default=None, max_length=2048)
    clear_password: bool = False
    disk_mode: str = 'ssh'
    disk_url: str = Field(default='', max_length=2048)
    disk_token: str | None = Field(default=None, max_length=2048)
    disk_threshold: int = Field(default=15, ge=1, le=95)
    git_monitor: bool = True
    auto_sync: bool = False
    auto_push: bool = True
    git_branch: str = Field(default='', max_length=100)

    @field_validator('name', 'ssh_host', 'ssh_user', 'ssh_path', 'ssh_key', mode='before')
    @classmethod
    def strip_value(cls, value):
        value = value.strip()
        if '\x00' in value or '\n' in value or '\r' in value:
            raise ValueError('Недопустимые символы')
        return value

    @field_validator('url')
    @classmethod
    def check_url(cls, value):
        return web_url(value)

    @field_validator('disk_url')
    @classmethod
    def check_disk_url(cls, value):
        return web_url(value, https=True)

    @field_validator('git_branch')
    @classmethod
    def branch_name(cls, value):
        if value and (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_./-]*', value) or '..' in value or value.endswith(('/', '.')) or '.lock' in value):
            raise ValueError('Некорректное имя ветки')
        return value

    @field_validator('expected_codes')
    @classmethod
    def codes(cls, value):
        values = [s.strip() for s in value.split(',')]
        if not values or any(not s.isdigit() or not 100 <= int(s) <= 599 for s in values):
            raise ValueError('HTTP-коды: например 200,204')
        return ','.join(values)

    @model_validator(mode='after')
    def validate_connections(self):
        if self.ssh_host and not self.ssh_user:
            raise ValueError('Для SSH нужен пользователь сервера')
        if self.ssh_path and not self.ssh_path.startswith('/'):
            raise ValueError('Путь к проекту должен быть абсолютным; либо оставьте его пустым и выберите через SSH-проводник')
        if self.ssh_host and not re.fullmatch(r'[a-zA-Z0-9_.:\-]+', self.ssh_host):
            raise ValueError('SSH-хост должен быть именем или IP, без протокола')
        if self.disk_mode not in ('ssh', 'http', 'off'):
            raise ValueError('Неизвестный способ проверки диска')
        if self.auto_sync and not ((self.ssh_host or self.ssh_profile_id) and self.git_monitor):
            raise ValueError('Автосинхронизация требует SSH и мониторинга Git')
        return self


class SSHProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ssh_host: str = Field(min_length=1, max_length=253)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, max_length=80)
    ssh_key: str = Field(default='', max_length=1024)
    ssh_password: str | None = Field(default=None, max_length=2048)
    clear_password: bool = False

    @model_validator(mode='after')
    def validate_connection(self):
        clean = ProjectInput(name=self.name, ssh_host=self.ssh_host, ssh_port=self.ssh_port,
                             ssh_user=self.ssh_user, ssh_key=self.ssh_key)
        self.name, self.ssh_host, self.ssh_user, self.ssh_key = clean.name, clean.ssh_host, clean.ssh_user, clean.ssh_key
        return self


class SettingsInput(BaseModel):
    telegram_token: str | None = Field(default=None, max_length=300)
    clear_telegram_token: bool = False
    telegram_chat: str = Field(default='', max_length=100)
    git_name: str = Field(default='Devlin Desk', min_length=1, max_length=100)
    git_email: str = Field(default='desk@localhost', min_length=3, max_length=200)
    notify_recovery: bool = True
    pin: str | None = None
    current_pin: str | None = None
    disable_pin: bool = False

    @field_validator('pin')
    @classmethod
    def validate_pin(cls, value):
        if value is not None and (not value.isdigit() or not 4 <= len(value) <= 12):
            raise ValueError('PIN должен содержать от 4 до 12 цифр')
        return value

    @field_validator('telegram_chat')
    @classmethod
    def validate_chat(cls, value):
        if value and not re.fullmatch(r'-?\d+|@[a-zA-Z0-9_]+', value):
            raise ValueError('Chat ID: число или @имя канала')
        return value

    @field_validator('telegram_token')
    @classmethod
    def validate_token(cls, value):
        if value and not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', value):
            raise ValueError('Некорректный формат токена Telegram')
        return value

    @field_validator('git_name','git_email')
    @classmethod
    def clean_identity(cls, value):
        if any(c in value for c in '\n\r\x00'):
            raise ValueError('Некорректная Git-подпись')
        return value


class SyncInput(BaseModel):
    snapshot: str
    files: list[str] = Field(min_length=1, max_length=2000)
    message: str = Field(min_length=1, max_length=500)
    push: bool = True


class InitInput(SyncInput):
    remote: str = Field(max_length=2048)
    branch: str = Field(default='main', min_length=1, max_length=100)
