"""Provider API calls with bounded pages, TLS verification and no token redirects."""
import re
from urllib.parse import quote, urlsplit
import httpx
from .remote import RemoteError


def repository_path(profile, remote):
    parsed, base = urlsplit(remote), urlsplit(profile['base_url'])
    if (parsed.scheme != 'https' or parsed.netloc.lower() != base.netloc.lower() or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise RemoteError('Для Git-профиля нужен HTTPS URL репозитория на том же сервисе. Подключите его через выбор репозитория.')
    path = parsed.path.strip('/')
    if path.endswith('.git'):
        path = path[:-4]
    parts = path.split('/')
    if (len(parts) < 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts) or
            (profile['provider'] == 'github' and len(parts) != 2)):
        raise RemoteError('Некорректный путь репозитория')
    return path


class Provider:
    def __init__(self, profile, token):
        self.profile, self.token = profile, token

    def request(self, path, params=None):
        if not self.token:
            raise RemoteError('В Git-профиле нет токена. Откройте профиль и настройте авторизацию.')
        github = self.profile['provider'] == 'github'
        base = 'https://api.github.com' if github else self.profile['base_url'] + '/api/v4'
        headers = {'Accept': 'application/json', 'User-Agent': 'DevlinDesk'}
        headers.update({'Authorization': 'Bearer ' + self.token} if github else {'PRIVATE-TOKEN': self.token})
        try:
            with httpx.Client(timeout=20, follow_redirects=False) as client:
                response = client.get(base + path, headers=headers, params=params)
            if response.status_code in (401, 403):
                raise RemoteError('Сервис отклонил авторизацию или доступ. Проверьте срок токена, права, SSO и ограничения API.')
            if response.status_code == 404:
                raise RemoteError('Репозиторий не найден или недоступен выбранному Git-профилю.')
            if response.status_code == 429:
                raise RemoteError('Лимит запросов Git-сервиса исчерпан. Повторите позже.')
            if response.status_code != 200:
                raise RemoteError('Git-сервис не ответил корректно. Проверьте адрес и доступность; перенаправления отключены.')
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RemoteError('Не удалось связаться с Git-сервисом. Проверьте сеть, TLS и адрес профиля.') from exc

    def account(self):
        data = self.request('/user')
        if not isinstance(data, dict):
            raise RemoteError('Git-сервис вернул некорректные данные аккаунта.')
        username = data.get('login') if self.profile['provider'] == 'github' else data.get('username')
        if not isinstance(username, str) or not username:
            raise RemoteError('Git-сервис вернул некорректные данные аккаунта.')
        return {'username': username, 'name': data.get('name') or username}

    def summary(self, item):
        if not isinstance(item, dict):
            raise RemoteError('Git-сервис вернул некорректные данные репозитория.')
        github = self.profile['provider'] == 'github'
        remote = item.get('clone_url') if github else item.get('http_url_to_repo')
        if not isinstance(remote, str):
            raise RemoteError('Git-сервис не вернул HTTPS адрес репозитория.')
        path = repository_path(self.profile, remote or '')
        return {'name': path, 'remote': remote, 'default_branch': item.get('default_branch') or 'main',
                'private': bool(item.get('private')) if github else item.get('visibility') != 'public',
                'archived': bool(item.get('archived'))}

    def repositories(self, page=1):
        github = self.profile['provider'] == 'github'
        params = {'per_page': 50, 'page': page}
        params.update({'sort': 'updated', 'affiliation': 'owner,collaborator,organization_member'} if github else
                      {'membership': 'true', 'simple': 'true', 'order_by': 'last_activity_at'})
        data = self.request('/user/repos' if github else '/projects', params)
        if not isinstance(data, list):
            raise RemoteError('Git-сервис вернул некорректный список репозиториев.')
        return {'repositories': [self.summary(item) for item in data], 'page': page, 'has_next': len(data) == 50}

    def repository(self, remote):
        path = repository_path(self.profile, remote)
        endpoint = '/repos/' + path if self.profile['provider'] == 'github' else '/projects/' + quote(path, safe='')
        result = self.summary(self.request(endpoint))
        if repository_path(self.profile, result['remote']) != path:
            raise RemoteError('Репозиторий был перемещён. Выберите его актуальный адрес в списке.')
        if result['archived']:
            raise RemoteError('Репозиторий архивирован; выберите активный репозиторий.')
        return result
