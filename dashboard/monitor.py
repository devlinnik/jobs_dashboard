import asyncio
import hashlib
import json
import math
import secrets
import time
from types import SimpleNamespace
import httpx
from . import gitops
from .remote import connect, RemoteError
from .store import now


def probe_script(token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    return '''<?php
// Devlin Desk: only disk totals; never return paths or execute input.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
$auth = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
$token = str_starts_with($auth, 'Bearer ') ? substr($auth, 7) : '';
if (!hash_equals('%s', hash('sha256', $token))) {
    http_response_code(403); echo json_encode(['error' => 'Forbidden']); exit;
}
$free = disk_free_space(__DIR__); $total = disk_total_space(__DIR__);
if ($free === false || $total === false) {
    http_response_code(503); echo json_encode(['error' => 'Unavailable']); exit;
}
echo json_encode(['free' => $free, 'total' => $total]);
''' % digest


class Monitor:
    def __init__(self, store, locks):
        self.store, self.locks = store, locks
        self.due = {}
        self.pending = set()
        self.semaphore = asyncio.Semaphore(3)
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=True)
        self.running = set()

    async def telegram(self, text):
        settings = self.store.settings()
        token = self.store.decrypt(settings['telegram_token'])
        if not token or not settings['telegram_chat']:
            return False
        try:
            response = await self.http.post('https://api.telegram.org/bot' + token + '/sendMessage',
                    json={'chat_id': settings['telegram_chat'], 'text': text[:4000], 'disable_web_page_preview': True})
            if response.status_code != 200 or response.json().get('ok') is not True:
                self.store.event(None, 'error', 'Telegram: сообщение не отправлено. Проверьте токен, Chat ID и права бота.')
                return False
            return True
        except (httpx.HTTPError, ValueError):
            self.store.event(None, 'error', 'Telegram: соединение не удалось. Проверьте сеть.')
            return False

    async def loop(self):
        try:
            while True:
                for project in self.store.projects():
                    pid = project['id']
                    if project['enabled'] and time.monotonic() >= self.due.get(pid, 0) and pid not in self.pending:
                        self.pending.add(pid)
                        task = asyncio.create_task(self.scheduled(pid))
                        self.running.add(task)
                        task.add_done_callback(self.running.discard)
                await asyncio.sleep(2)
        finally:
            for task in list(self.running):
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)

    async def scheduled(self, pid):
        try:
            async with self.semaphore:
                await self.check(pid)
        except KeyError:
            pass
        except Exception:
            self.store.event(pid, 'error', 'Фоновая проверка не завершилась. Повторите проверку вручную.')
        finally:
            self.pending.discard(pid)

    def server_check(self, project):
        result = {}
        try:
            with connect(self.store, project, require_project=False) as conn:
                result['ssh'] = {'ok': True}
                try:
                    result['directory'] = {'ok': True, 'path': conn.check_directory()}
                except RemoteError as exc:
                    result['directory'] = {'ok': False, 'error': str(exc)}
                    return result
                if project['disk_mode'] == 'ssh':
                    try:
                        result['disk'] = conn.disk()
                    except (RemoteError, ValueError):
                        result['disk'] = {'error': 'Не удалось получить свободное место по SSH'}
                if project['git_monitor']:
                    try:
                        git = gitops.status(conn)
                        result['git'] = git
                        if project['auto_sync'] and git['connected'] and git['files']:
                            if git.get('staged') or any(not f['allowed'] for f in git['files']):
                                git['auto_error'] = 'Автосинхронизация остановлена: staged или потенциально секретные файлы.'
                            else:
                                request = SimpleNamespace(snapshot=git['snapshot'], files=[f['path'] for f in git['files']],
                                    message='Devlin Desk: server changes ' + now(), push=project['auto_push'])
                                sync = gitops.sync(conn, request, self.store.settings())
                                result['auto_sync_result'] = sync
                                result['git'] = gitops.status(conn)
                    except RemoteError as exc:
                        result['git'] = {'error': str(exc)}
        except RemoteError as exc:
            result['ssh'] = {'ok': False, 'error': str(exc)}
        return result

    async def website_check(self, project):
        started = time.monotonic()
        try:
            # Stream and cap response; an unexpectedly huge page cannot consume all memory.
            async with self.http.stream('GET', project['url']) as response:
                body = bytearray()
                if project['expected_text']:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            break
                expected = {int(x) for x in project['expected_codes'].split(',')}
                matches = not project['expected_text'] or project['expected_text'] in body.decode(response.encoding or 'utf-8', errors='replace')
                ok = response.status_code in expected and matches
                return {'ok': ok, 'code': response.status_code, 'latency': round((time.monotonic() - started) * 1000),
                        'error': '' if ok else ('Не найден контрольный текст' if not matches else 'Неожиданный HTTP-код')}
        except httpx.HTTPError:
            return {'ok': False, 'code': None, 'latency': None, 'error': 'Сайт недоступен: сеть, DNS, тайм-аут или TLS'}

    async def disk_http(self, project):
        token = self.store.decrypt(project.get('disk_token_enc', ''))
        if not project['disk_url'] and not token:
            return {'error': 'PHP-проба не настроена: установите её из карточки проекта, указав HTTPS URL. Или выберите проверку диска через SSH.'}
        if not project['disk_url']:
            return {'error': 'Ключ PHP-пробы сохранён, но HTTPS URL файла не указан. Заполните его в настройках проекта.'}
        if not token:
            return {'error': 'HTTPS URL указан, но ключ PHP-пробы отсутствует. Установите пробу из карточки проекта или укажите ключ существующей пробы.'}
        try:
            # Never forward the bearer token to a redirected destination.
            r = await self.http.get(project['disk_url'], headers={'Authorization': 'Bearer ' + token}, follow_redirects=False)
            r.raise_for_status()
            data = r.json()
            free, total = float(data['free']), float(data['total'])
            if not (math.isfinite(free) and math.isfinite(total)) or not 0 <= free <= total or total <= 0:
                raise ValueError()
            return {'free': free, 'total': total, 'free_percent': round(free / total * 100, 1), 'source': 'HTTPS'}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {'error': 'HTTP-проба недоступна или вернула неверные данные'}

    async def check(self, pid):
        lock = self.locks.setdefault(pid, asyncio.Lock())
        async with lock:
            project = self.store.resolve_project(self.store.project(pid))
            previous = self.store.state(pid)
            state = {'checked_at': now()}
            if previous.get('pending_push'):
                state['pending_push'] = previous['pending_push']
            if project['url']:
                state['web'] = await self.website_check(project)
            if project['ssh_host']:
                job = asyncio.create_task(asyncio.to_thread(self.server_check, project))
                try:
                    server_state = await asyncio.shield(job)
                except asyncio.CancelledError:
                    # A thread may already be writing a commit. Finish it and persist its
                    # result before releasing the project lock or closing the database.
                    server_state = await job
                state.update(server_state)
            if project['disk_mode'] == 'http':
                state['disk'] = await self.disk_http(project)
            issues = []
            if state.get('web', {}).get('ok') is False:
                issues.append('Сайт: ' + state['web']['error'])
            if state.get('ssh', {}).get('ok') is False:
                issues.append('SSH: ' + state['ssh']['error'])
            if state.get('directory', {}).get('ok') is False:
                issues.append('Папка проекта: ' + state['directory']['error'])
            disk = state.get('disk', {})
            if disk.get('error'):
                issues.append('Диск: ' + disk['error'])
            elif disk and disk['free_percent'] < project['disk_threshold']:
                issues.append('Мало места: свободно ' + str(disk['free_percent']) + '%')
            if state.get('git', {}).get('error'):
                issues.append('Git: ' + state['git']['error'])
            if state.get('git', {}).get('auto_error'):
                issues.append(state['git']['auto_error'])
            auto = state.get('auto_sync_result')
            if auto:
                self.store.event(pid, 'warning' if auto.get('warning') else 'success',
                    'Автосинхронизация: коммит ' + auto['commit'] + (' отправлен' if auto['pushed'] else ' создан локально') + '. ' + auto.get('warning', ''))
                if auto.get('warning'):
                    state['pending_push'] = auto['warning']
                elif auto['pushed']:
                    state.pop('pending_push', None)
                if project['notify']:
                    await self.telegram('📤 ' + project['name'] + ': автоматический коммит ' + auto['commit'] + (' отправлен' if auto['pushed'] else ' создан на сервере'))
            if state.get('pending_push'):
                issues.append(state['pending_push'])
            state['issues'] = issues
            state['status'] = 'error' if issues else ('ok' if any(k in state for k in ('web', 'ssh', 'disk')) else 'unconfigured')
            previous_issues = previous.get('issues', [])
            if issues != previous_issues:
                if issues:
                    message = project['name'] + '\n' + '\n'.join(issues)
                    self.store.event(pid, 'error', message)
                    if project['notify']:
                        await self.telegram('⚠️ ' + message)
                elif previous_issues:
                    self.store.event(pid, 'success', 'Проверки снова проходят')
                    if project['notify'] and self.store.settings()['notify_recovery']:
                        await self.telegram('✅ ' + project['name'] + ': проверки снова проходят')
            git = state.get('git', {})
            if git.get('files') and git.get('snapshot') != previous.get('git', {}).get('snapshot'):
                count = len(git['files'])
                self.store.event(pid, 'warning', 'На сервере обнаружено изменений: ' + str(count))
                if project['notify']:
                    await self.telegram('📝 ' + project['name'] + ': файлов с изменениями — ' + str(count))
            state['next_check_at'] = time.time() + project['interval']
            self.due[pid] = time.monotonic() + project['interval']
            self.store.save_state(pid, state)
            return state

    async def close(self):
        await self.http.aclose()
