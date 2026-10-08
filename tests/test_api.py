import json
import httpx
from fastapi.testclient import TestClient
from dashboard.app import create_app

HEADERS = {'X-App-Request': '1'}


def client_for(tmp_path):
    return TestClient(create_app(tmp_path, start_monitor=False), headers=HEADERS)


def unlock(client, pin=''):
    return client.post('/api/unlock', json={'pin': pin})


def test_release_identity_and_uncached_frontend(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get('/api/app-info').status_code == 401
        assert client.get('/api/session').json()['version'] == '1.10.15'
        unlock(client)
        info = client.get('/api/app-info').json()
        assert info['version'] == '1.10.15' and info['executable']
        for path in ('/', '/static/app.js', '/static/styles.css'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'


def test_crud_encryption_and_no_secret_leak(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get('/api/projects').status_code == 401
        assert unlock(client).status_code == 200
        project = client.post('/api/projects', json={'name': 'Portal', 'url': 'https://example.com',
            'ssh_host': 'server.example.com', 'ssh_user': 'deploy', 'ssh_path': '/var/www/project',
            'ssh_password': 'test-secret-password', 'disk_token': 'probe-secret'}).json()
        assert project['has_password'] is True
        assert 'test-secret-password' not in json.dumps(project)
        assert 'probe-secret' not in json.dumps(project)
        stored = client.app.state.store.project(project['id'])
        assert client.app.state.store.decrypt(stored['ssh_password_enc']) == 'test-secret-password'
        assert 'test-secret-password' not in json.dumps(stored)
        assert client.put('/api/projects/'+project['id'], json={'name':'Renamed', 'url':'https://example.com'}).status_code == 200
        project = client.get('/api/projects').json()[0]
        assert project['has_password'] and project['name'] == 'Renamed'
        assert client.delete('/api/projects/'+project['id']).status_code == 200
        assert client.get('/api/projects').json() == []
    with client_for(tmp_path) as client:
        unlock(client)
        assert client.get('/api/projects').json() == []


def test_pin_change_requires_old_pin_and_revokes_sessions(tmp_path):
    with client_for(tmp_path) as client:
        unlock(client)
        assert client.put('/api/settings', json={'pin': '1234'}).status_code == 200
        assert client.get('/api/settings').json()['pin_enabled']
        assert 'pin_hash' not in client.get('/api/settings').json()
        assert client.put('/api/settings', json={'pin':'5678'}).status_code == 403
        assert client.put('/api/settings', json={'pin':'5678','current_pin':'1234'}).status_code == 200
        client.post('/api/lock')
        assert client.get('/api/projects').status_code == 401
        assert unlock(client,'1234').status_code == 401
        assert unlock(client,'5678').status_code == 200
    with client_for(tmp_path) as client:
        assert client.get('/api/session').json()['pin_enabled']
        assert unlock(client,'5678').status_code == 200
        assert client.put('/api/settings',json={'disable_pin':True,'current_pin':'5678'}).status_code == 200


def test_pin_lockout_and_cross_site_boundary(tmp_path):
    with client_for(tmp_path) as client:
        unlock(client)
        client.put('/api/settings',json={'pin':'1234'})
        client.post('/api/lock')
        for _ in range(5):
            assert unlock(client,'9999').status_code == 401
        assert unlock(client,'1234').status_code == 429
        assert client.post('/api/unlock',json={'pin':'1234'},headers={'Origin':'https://evil.example'}).status_code == 403
        assert client.get('/api/session',headers={'Host':'evil.example'}).status_code == 403
        assert client.post('/api/unlock',json={},headers={'X-App-Request':'0'}).status_code == 403


def test_validation_and_fingerprint_invalidation(tmp_path):
    with client_for(tmp_path) as client:
        unlock(client)
        for payload in ({'name':'bad','url':'file:///etc/passwd'}, {'name':'bad','ssh_host':'host','ssh_path':'relative'},
                        {'name':'bad','disk_url':'http://example.com/probe'}, {'name':'bad','expected_codes':'abc'},
                        {'name':'bad','auto_sync':True}):
            assert client.post('/api/projects',json=payload).status_code == 422
        project = client.post('/api/projects',json={'name':'ok','ssh_host':'host','ssh_user':'deploy','ssh_path':'/srv/app'}).json()
        store = client.app.state.store
        raw = store.project(project['id']); raw['ssh_fingerprint'] = 'SHA256:verified'; store.save_project(raw)
        client.put('/api/projects/'+project['id'],json={'name':'ok','ssh_host':'other','ssh_user':'deploy','ssh_path':'/srv/app'})
        assert client.get('/api/projects').json()[0]['ssh_fingerprint'] == ''


def test_monitor_http_transitions_and_telegram_deduplication(tmp_path):
    current_code = [503]
    notifications = []
    def handler(request):
        if request.url.host == 'api.telegram.org':
            notifications.append(json.loads(request.content))
            return httpx.Response(200,json={'ok':True})
        return httpx.Response(current_code[0],text='Portal Ready')
    with client_for(tmp_path) as client:
        unlock(client)
        client.put('/api/settings',json={'telegram_token':'123456:TEST_TOKEN','telegram_chat':'12345'})
        token_response = client.get('/api/settings').json()
        assert token_response['telegram_configured'] and 'telegram_token' not in token_response
        monitor = client.app.state.monitor
        # Replace only the network layer: real monitoring logic, transition handling and storage run.
        original = monitor.http
        client.portal.call(original.aclose)
        monitor.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        project = client.post('/api/projects',json={'name':'Portal','url':'https://portal.test','expected_text':'Ready'}).json()
        path = '/api/projects/'+project['id']+'/check'
        first = client.post(path).json()
        assert first['status'] == 'error' and first['web']['code'] == 503
        client.post(path)
        assert len(notifications) == 1
        current_code[0] = 200
        assert client.post(path).json()['status'] == 'ok'
        assert len(notifications) == 2
        assert 'снова проходят' in notifications[-1]['text']
        assert len(client.get('/api/events').json()) >= 4


def test_disk_http_rejects_redirect_and_bad_totals(tmp_path):
    replies = [httpx.Response(302,headers={'Location':'https://other.test'}), httpx.Response(200,json={'free':100,'total':10}), httpx.Response(200,json={'free':25,'total':100})]
    requests = []
    def handler(request):
        requests.append(request)
        return replies.pop(0)
    with client_for(tmp_path) as client:
        unlock(client)
        monitor = client.app.state.monitor
        client.portal.call(monitor.http.aclose)
        monitor.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        p = client.post('/api/projects',json={'name':'Disk','disk_mode':'http','disk_url':'https://probe.test','disk_token':'secret'}).json()
        path = '/api/projects/'+p['id']+'/check'
        assert client.post(path).json()['disk'].get('error')
        assert len(requests) == 1
        assert client.post(path).json()['disk'].get('error')
        state = client.post(path).json()
        assert state['disk']['free_percent'] == 25
        assert requests[-1].headers['Authorization'] == 'Bearer secret'


def test_empty_name_and_instance_lock(tmp_path):
    from dashboard.instance import InstanceLock
    import pytest
    with client_for(tmp_path/'data') as client:
        unlock(client)
        assert client.post('/api/projects',json={'name':'   '}).status_code == 422
    lock = InstanceLock(tmp_path/'instance')
    try:
        with pytest.raises(RuntimeError,match='уже запущен'):
            InstanceLock(tmp_path/'instance')
    finally:
        lock.close()
    other = InstanceLock(tmp_path/'instance'); other.close()


def test_shutdown_waits_for_server_write_and_persists_result(tmp_path):
    import asyncio
    import threading
    import time
    from dashboard.models import ProjectInput
    from dashboard.monitor import Monitor
    from dashboard.store import Store
    async def scenario():
        store=Store(tmp_path)
        project=ProjectInput(name='Shutdown',ssh_host='test',ssh_user='deploy',ssh_path='/srv/project',notify=False).model_dump()
        project['id']='shutdown'
        store.save_project(project)
        completed=threading.Event()
        monitor=Monitor(store,{})
        def worker(_):
            time.sleep(.1)
            completed.set()
            return {'ssh':{'ok':True}, 'auto_sync_result':{'commit':'abc123','pushed':False,'warning':'Push not confirmed'}}
        monitor.server_check=worker
        task=asyncio.create_task(monitor.check('shutdown'))
        await asyncio.sleep(.02)
        task.cancel()
        state=await task
        assert completed.is_set() and state['pending_push'] == 'Push not confirmed'
        assert store.state('shutdown')['auto_sync_result']['commit'] == 'abc123'
        await monitor.close(); store.close()
    asyncio.run(scenario())
