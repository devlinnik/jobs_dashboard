import asyncio
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, Depends, HTTPException, Request, Response, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from .store import Store, now
from .security import Sessions, hash_pin, verify_pin
from .models import ProjectInput, SettingsInput, SyncInput, InitInput, SSHProfileInput, web_url
from .remote import connect, inspect_host, RemoteError
from .monitor import Monitor, probe_script
from . import gitops

STATIC = Path(__file__).parent / 'static'


def public_project(project, store):
    project = store.resolve_project(project)
    result = {k: v for k, v in project.items() if not k.endswith('_enc')}
    result['has_password'] = bool(project.get('ssh_password_enc'))
    result['has_disk_token'] = bool(project.get('disk_token_enc'))
    result['state'] = store.state(project['id'])
    return result


def public_profile(profile, store):
    return {k: v for k, v in profile.items() if not k.endswith('_enc')} | {
        'has_password': bool(profile.get('ssh_password_enc')),
        'project_count': sum(p.get('ssh_profile_id') == profile['id'] for p in store.projects())}


def public_settings(settings):
    return {k: v for k, v in settings.items() if k not in ('telegram_token', 'pin_hash')} | {
        'telegram_configured': bool(settings['telegram_token']), 'pin_enabled': bool(settings['pin_hash'])}


class UnlockInput(BaseModel):
    pin: str = Field(default='', max_length=100)


class TrustInput(BaseModel):
    fingerprint: str = Field(min_length=40, max_length=100)


class ConfirmInput(BaseModel):
    confirm: bool = False


class ProbeDeployInput(ConfirmInput):
    disk_url: str = Field(default='', max_length=2048)

    @field_validator('disk_url')
    @classmethod
    def validate_url(cls, value):
        return web_url(value, https=True)


def create_app(directory=None, start_monitor=True):
    store, sessions, locks = Store(directory), Sessions(), {}
    monitor = Monitor(store, locks)
    profiles_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor.loop()) if start_monitor else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await monitor.close()
        store.close()

    app = FastAPI(title='Devlin Desk', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.monitor, app.state.sessions = store, monitor, sessions

    @app.middleware('http')
    async def local_boundary(request, call_next):
        # Prevent DNS rebinding and cross-site commands targeting the loopback server.
        host = urlsplit('http://' + request.headers.get('host', '')).hostname
        if host not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            return JSONResponse({'detail': 'Доступ разрешён только с локального компьютера'}, status_code=403)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Запрос с другого сайта запрещён'}, status_code=403)
            if request.headers.get('x-app-request') != '1':
                return JSONResponse({'detail': 'Нет заголовка приложения'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RemoteError)
    async def remote_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({'detail': 'Проект не найден'}, status_code=404)

    def authenticated(request: Request):
        if not sessions.valid(request.cookies.get('desk_session')):
            raise HTTPException(401, 'Приложение заблокировано')

    auth = [Depends(authenticated)]

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'index.html')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/api/session')
    def session(request: Request):
        return {'authenticated': sessions.valid(request.cookies.get('desk_session')),
                'pin_enabled': bool(store.settings()['pin_hash']), 'version': '1.0.0'}

    @app.post('/api/unlock')
    def unlock(body: UnlockInput, response: Response):
        pin_hash = store.settings()['pin_hash']
        if time.monotonic() < sessions.blocked_until:
            raise HTTPException(429, 'Слишком много попыток. Подождите минуту.')
        if pin_hash and not verify_pin(body.pin, pin_hash):
            sessions.failures += 1
            if sessions.failures >= 5:
                sessions.blocked_until = time.monotonic() + 60
                sessions.failures = 0
            raise HTTPException(401, 'Неверный PIN')
        sessions.failures = 0
        response.set_cookie('desk_session', sessions.create(), httponly=True, samesite='strict', max_age=43200)
        return {'ok': True}

    @app.post('/api/lock', dependencies=auth)
    def lock(response: Response):
        sessions.clear()
        response.delete_cookie('desk_session')
        return {'ok': True}

    @app.get('/api/projects', dependencies=auth)
    def projects():
        return [public_project(p, store) for p in store.projects()]

    @app.post('/api/projects', dependencies=auth)
    def add_project(body: ProjectInput):
        data = body.model_dump(exclude={'ssh_password', 'disk_token', 'clear_password'})
        data.update(id=uuid.uuid4().hex, created_at=now(), ssh_fingerprint='',
                    ssh_password_enc=store.encrypt(body.ssh_password or ''), disk_token_enc=store.encrypt(body.disk_token or ''))
        if data.get('ssh_profile_id'):
            for key in ('ssh_host','ssh_user','ssh_key','ssh_password_enc','ssh_fingerprint'):
                data[key] = ''
        store.save_project(data)
        monitor.due[data['id']] = 0
        store.event(data['id'], 'info', 'Проект добавлен')
        return public_project(data, store)

    @app.put('/api/projects/{pid}', dependencies=auth)
    async def edit_project(pid: str, body: ProjectInput):
        async with locks.setdefault(pid, asyncio.Lock()):
            old = store.project(pid)
            data = old | body.model_dump(exclude={'ssh_password', 'disk_token', 'clear_password'})
            if data.get('ssh_profile_id'):
                for key in ('ssh_host','ssh_user','ssh_key','ssh_password_enc','ssh_fingerprint'):
                    data[key] = ''
            elif any(old.get(k) != data.get(k) for k in ('ssh_host','ssh_port')):
                data['ssh_fingerprint'] = ''
            if body.ssh_password is not None and not data.get('ssh_profile_id'):
                data['ssh_password_enc'] = store.encrypt(body.ssh_password)
            if body.clear_password:
                data['ssh_password_enc'] = ''
            if body.disk_token is not None:
                data['disk_token_enc'] = store.encrypt(body.disk_token)
            store.save_project(data)
            monitor.due[pid] = 0
            store.event(pid, 'info', 'Настройки проекта обновлены')
            return public_project(data, store)

    @app.delete('/api/projects/{pid}', dependencies=auth)
    async def delete_project(pid: str):
        async with locks.setdefault(pid, asyncio.Lock()):
            store.project(pid)
            store.delete_project(pid)
            monitor.due.pop(pid, None)
        return {'ok': True}

    @app.post('/api/projects/{pid}/check', dependencies=auth)
    async def check(pid: str):
        return await monitor.check(pid)

    @app.get('/api/events', dependencies=auth)
    def events(project_id: str | None = None):
        return store.events(project_id)

    @app.get('/api/settings', dependencies=auth)
    def settings():
        return public_settings(store.settings())

    @app.put('/api/settings', dependencies=auth)
    def set_settings(body: SettingsInput, response: Response):
        old = store.settings()
        if (body.pin is not None or body.disable_pin) and old['pin_hash'] and not verify_pin(body.current_pin or '', old['pin_hash']):
            raise HTTPException(403, 'Для изменения PIN введите действующий PIN')
        data = old | body.model_dump(exclude={'telegram_token','clear_telegram_token','pin','current_pin','disable_pin'})
        if body.telegram_token:
            data['telegram_token'] = store.encrypt(body.telegram_token)
        if body.clear_telegram_token:
            data['telegram_token'] = ''
        if body.pin is not None:
            data['pin_hash'] = hash_pin(body.pin)
        if body.disable_pin:
            data['pin_hash'] = ''
        store.save_settings(data)
        if body.pin is not None or body.disable_pin:
            sessions.clear()
            response.set_cookie('desk_session', sessions.create(), httponly=True, samesite='strict', max_age=43200)
        store.event(None, 'info', 'Настройки приложения сохранены')
        return public_settings(data)

    @app.post('/api/telegram/test', dependencies=auth)
    async def telegram_test():
        if not await monitor.telegram('✅ Devlin Desk: тестовое уведомление. Бот подключён.'):
            raise HTTPException(400, 'Сообщение не отправлено. Сохраните токен и Chat ID, откройте диалог с ботом и проверьте сеть.')
        store.event(None, 'success', 'Тестовое уведомление Telegram отправлено')
        return {'message': 'Сообщение отправлено в Telegram'}

    @app.get('/api/projects/{pid}/ssh/fingerprint', dependencies=auth)
    async def host_fingerprint(pid: str):
        return await asyncio.to_thread(inspect_host, store.resolve_project(store.project(pid)))

    @app.post('/api/projects/{pid}/ssh/trust', dependencies=auth)
    async def trust_host(pid: str, body: TrustInput):
        async with locks.setdefault(pid, asyncio.Lock()), profiles_lock:
            raw = store.project(pid)
            project = store.resolve_project(raw)
            actual = await asyncio.to_thread(inspect_host, project)
            if not secrets.compare_digest(body.fingerprint, actual['fingerprint']):
                raise RemoteError('Отпечаток изменился. Получите и проверьте его заново.')
            if raw.get('ssh_profile_id'):
                profile = store.profile(raw['ssh_profile_id'])
                profile['ssh_fingerprint'] = body.fingerprint
                store.save_profile(profile)
            else:
                raw['ssh_fingerprint'] = body.fingerprint
                store.save_project(raw)
            store.event(pid, 'info', 'Подтверждён отпечаток SSH-сервера')
        return {'ok': True}

    async def operation(pid, fn, *args, require_project=True):
        async with locks.setdefault(pid, asyncio.Lock()):
            project = store.resolve_project(store.project(pid))
            def execute():
                with connect(store, project, require_project=require_project) as conn:
                    return fn(conn, *args)
            return await asyncio.to_thread(execute)

    @app.post('/api/projects/{pid}/ssh/test', dependencies=auth)
    async def test_ssh(pid: str):
        def test(conn):
            result = {'ok': True, 'message': 'SSH подключён. Можно выбрать папку проекта через проводник.'}
            try:
                result['directory'] = {'ok': True, 'path': conn.check_directory()}
                result['message'] = 'SSH подключён. Директория проекта доступна.'
            except RemoteError as exc:
                result['directory'] = {'ok': False, 'error': str(exc)}
            if result['directory']['ok'] and conn.project['disk_mode'] == 'ssh':
                try:
                    result['disk'] = conn.disk()
                except RemoteError as exc:
                    result['disk'] = {'error': str(exc)}
            return result
        return await operation(pid, test, require_project=False)

    @app.get('/api/projects/{pid}/ssh/directories', dependencies=auth)
    async def ssh_directories(pid: str, path: str = Query(default='', max_length=1024)):
        return await operation(pid, lambda conn: conn.directories(path), require_project=False)

    @app.get('/api/ssh-profiles', dependencies=auth)
    def ssh_profiles():
        return [public_profile(p, store) for p in store.profiles()]

    @app.post('/api/ssh-profiles', dependencies=auth)
    async def add_profile(body: SSHProfileInput):
        async with profiles_lock:
            data = body.model_dump(exclude={'ssh_password','clear_password'})
            data.update(id=uuid.uuid4().hex, created_at=now(), ssh_fingerprint='',
                        ssh_password_enc=store.encrypt(body.ssh_password or ''))
            store.save_profile(data)
            return public_profile(data, store)

    @app.put('/api/ssh-profiles/{profile_id}', dependencies=auth)
    async def edit_profile(profile_id: str, body: SSHProfileInput):
        async with profiles_lock:
            old = store.profile(profile_id)
            data = old | body.model_dump(exclude={'ssh_password','clear_password'})
            if any(old[k] != data[k] for k in ('ssh_host','ssh_port')):
                data['ssh_fingerprint'] = ''
            if body.ssh_password is not None:
                data['ssh_password_enc'] = store.encrypt(body.ssh_password)
            if body.clear_password:
                data['ssh_password_enc'] = ''
            store.save_profile(data)
            for p in store.projects():
                if p.get('ssh_profile_id') == profile_id:
                    monitor.due[p['id']] = 0
            return public_profile(data, store)

    @app.delete('/api/ssh-profiles/{profile_id}', dependencies=auth)
    async def delete_profile(profile_id: str):
        async with profiles_lock:
            if not store.delete_profile(profile_id):
                raise RemoteError('Профиль используется проектами. Сначала выберите для них другой профиль.')
        return {'ok': True}

    @app.get('/api/ssh-profiles/{profile_id}/fingerprint', dependencies=auth)
    async def profile_fingerprint(profile_id: str):
        return await asyncio.to_thread(inspect_host, store.profile(profile_id))

    @app.post('/api/ssh-profiles/{profile_id}/trust', dependencies=auth)
    async def profile_trust(profile_id: str, body: TrustInput):
        async with profiles_lock:
            profile = store.profile(profile_id)
            actual = await asyncio.to_thread(inspect_host, profile)
            if not secrets.compare_digest(body.fingerprint, actual['fingerprint']):
                raise RemoteError('Отпечаток изменился. Получите и проверьте его заново.')
            profile['ssh_fingerprint'] = body.fingerprint
            store.save_profile(profile)
        return {'ok': True}

    @app.post('/api/ssh-profiles/{profile_id}/test', dependencies=auth)
    async def profile_test(profile_id: str):
        profile = store.profile(profile_id) | {'ssh_path': ''}
        def test():
            with connect(store, profile, require_project=False):
                return {'ok': True, 'message': 'SSH-профиль подключён. Теперь выберите папку для проекта.'}
        return await asyncio.to_thread(test)

    @app.get('/api/projects/{pid}/git', dependencies=auth)
    async def git_status(pid: str):
        return await operation(pid, gitops.status)

    @app.get('/api/projects/{pid}/git/diff', dependencies=auth)
    async def git_diff(pid: str, path: str):
        return {'diff': await operation(pid, gitops.diff, path)}

    @app.post('/api/projects/{pid}/git/sync', dependencies=auth)
    async def git_sync(pid: str, body: SyncInput):
        result = await operation(pid, gitops.sync, body, store.settings())
        store.event(pid, 'warning' if result['warning'] else 'success', 'Коммит ' + result['commit'] + (' отправлен в origin' if result['pushed'] else ' создан на сервере') + '. ' + result['warning'])
        previous = store.state(pid)
        if result['warning']:
            previous['pending_push'] = result['warning']
        elif result['pushed']:
            previous.pop('pending_push', None)
        store.save_state(pid, previous)
        monitor.due[pid] = 0
        return result

    @app.post('/api/projects/{pid}/git/init', dependencies=auth)
    async def git_init(pid: str, body: InitInput):
        result = await operation(pid, gitops.initialize, body, store.settings())
        store.event(pid, 'warning' if result['warning'] else 'success', 'Репозиторий подключён. Коммит ' + result['commit'] + '. ' + result['warning'])
        previous = store.state(pid)
        if result['warning']:
            previous['pending_push'] = result['warning']
        elif result['pushed']:
            previous.pop('pending_push', None)
        store.save_state(pid, previous)
        monitor.due[pid] = 0
        return result

    @app.post('/api/projects/{pid}/git/{action}', dependencies=auth)
    async def git_action(pid: str, action: str, body: ConfirmInput):
        if action not in ('pull', 'push') or not body.confirm:
            raise HTTPException(400, 'Нужно подтвердить операцию pull/push')
        result = await operation(pid, gitops.pull if action == 'pull' else gitops.push)
        store.event(pid, 'success', result['message'])
        if action == 'push':
            previous = store.state(pid)
            previous.pop('pending_push', None)
            store.save_state(pid, previous)
        monitor.due[pid] = 0
        return result

    @app.post('/api/projects/{pid}/disk/deploy', dependencies=auth)
    async def deploy_probe(pid: str, body: ProbeDeployInput):
        if not body.confirm:
            raise HTTPException(400, 'Подтвердите установку PHP-файла')
        async with locks.setdefault(pid, asyncio.Lock()):
            raw = store.project(pid)
            project = store.resolve_project(raw)
            token = secrets.token_urlsafe(32)
            def deploy():
                with connect(store, project) as conn, conn.client.open_sftp() as sftp:
                    path = conn.path.rstrip('/') + '/.devlin-disk.php'
                    try:
                        with sftp.open(path, 'wx') as f:
                            f.write(probe_script(token))
                        sftp.chmod(path, 0o644)
                    except OSError as exc:
                        raise RemoteError('Файл .devlin-disk.php уже существует или недоступен для записи. Существующий файл не перезаписывается.') from exc
            await asyncio.to_thread(deploy)
            raw['disk_token_enc'] = store.encrypt(token)
            if body.disk_url:
                raw['disk_url'] = body.disk_url
                raw['disk_mode'] = 'http'
            store.save_project(raw)
            monitor.due[pid] = 0
            store.event(pid, 'info', 'В корень проекта установлен .devlin-disk.php. Настройте HTTPS URL для пробы.')
            return {'token': token, 'filename': '.devlin-disk.php', 'message': 'Установлено. Скопируйте ключ сейчас: повторно он не показывается.'}

    return app
