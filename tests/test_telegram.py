import json
import httpx
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app

TOKEN = '123456:TEST_SECRET_TOKEN'


def configure(client, target):
    client.post('/api/unlock', json={})
    response = client.put('/api/settings', json={'telegram_token':TOKEN, 'telegram_chat':target})
    assert response.status_code == 200
    assert TOKEN not in response.text


@pytest.mark.parametrize(('target', 'kind'), [('nickname','private'),('@public_channel','channel'),('-12345','group')])
def test_bot_status_resolves_target_and_sends_to_correct_chat(tmp_path, target, kind):
    requests = []
    def handler(request):
        method = request.url.path.rsplit('/',1)[-1]
        body = json.loads(request.content); requests.append((method, body))
        if method == 'getMe':
            return httpx.Response(200,json={'ok':True,'result':{'id':123456,'is_bot':True,'username':'desk_bot'}})
        if method == 'getChat':
            if kind == 'private': return httpx.Response(400,json={'ok':False,'description':'Bad Request: chat not found'})
            return httpx.Response(200,json={'ok':True,'result':{'id':-12345,'type':kind,'username':'public_channel','title':'Public'}})
        if method == 'getUpdates':
            assert 'offset' not in body and 'allowed_updates' not in body and body['timeout'] == 0
            # A matching username in a group/forwarded chat must never resolve a private recipient.
            return httpx.Response(200,json={'ok':True,'result':[
                {'message':{'chat':{'type':'group','id':-999},'from':{'id':456,'username':'nickname'}}},
                {'message':{'chat':{'type':'private','id':456},'from':{'id':456,'username':'Nickname','first_name':'Recipient'},'text':'/start'}},
                {'message':{'chat':{'type':'private','id':999},'from':{'id':456,'username':'nickname'}}},
            ]})
        assert method == 'sendMessage'
        assert str(body['chat_id']) == ('456' if kind == 'private' else '-12345')
        return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
    with TestClient(create_app(tmp_path,start_monitor=False),headers={'X-App-Request':'1'}) as client:
        assert client.post('/api/telegram/status').status_code == 401
        configure(client,target)
        assert client.get('/api/settings').json()['telegram_status'] == {}
        monitor = client.app.state.monitor
        client.portal.call(monitor.http.aclose)
        monitor.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = client.post('/api/telegram/status')
        status = result.json()
        assert status['state'] == 'ready' and status['bot_username'] == 'desk_bot'
        assert TOKEN not in result.text
        assert not any(method == 'sendMessage' for method,_ in requests)
        saved = client.get('/api/settings').json()
        assert saved['telegram_status'] == status
        assert saved['telegram_chat'] == ('@nickname' if kind == 'private' else target)
        assert client.post('/api/telegram/test').status_code == 200
        assert client.put('/api/settings',json={'telegram_chat':'@other'}).json()['telegram_status'] == {}
        client.post('/api/lock')
        assert client.post('/api/telegram/status').status_code == 401


@pytest.mark.parametrize('failure',['invalid_token','unknown_private','webhook','redirect'])
def test_failures_are_actionable_without_exposing_tokens_or_consuming_updates(tmp_path, failure):
    calls = []
    def handler(request):
        method = request.url.path.rsplit('/',1)[-1]; calls.append(method)
        if failure == 'invalid_token': return httpx.Response(401,json={'ok':False,'error_code':401,'description':TOKEN})
        if failure == 'redirect': return httpx.Response(302,headers={'Location':'https://other.test/'+TOKEN})
        if method == 'getMe': return httpx.Response(200,json={'ok':True,'result':{'id':123,'is_bot':True,'username':'desk_bot'}})
        if method == 'getChat': return httpx.Response(400,json={'ok':False})
        assert method == 'getUpdates' and 'offset' not in json.loads(request.content)
        return httpx.Response(409,json={'ok':False,'error_code':409,'description':TOKEN}) if failure == 'webhook' else httpx.Response(200,json={'ok':True,'result':[]})
    with TestClient(create_app(tmp_path,start_monitor=False),headers={'X-App-Request':'1'}) as client:
        configure(client,'@nickname')
        monitor=client.app.state.monitor; client.portal.call(monitor.http.aclose)
        monitor.http=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True)
        response = client.post('/api/telegram/status')
        assert TOKEN not in response.text
        status=response.json()
        assert status['state'] in ('error','recipient_error')
        if failure == 'unknown_private': assert '/start' in status['message']
        if failure == 'webhook': assert 'webhook' in status['message']
        if failure == 'redirect': assert calls == ['getMe']
        assert TOKEN not in json.dumps(client.get('/api/settings').json())


def test_verification_preserves_concurrent_settings_and_does_not_cache_other_token(tmp_path):
    with TestClient(create_app(tmp_path,start_monitor=False),headers={'X-App-Request':'1'}) as client:
        configure(client,'')
        store=client.app.state.store
        switch_token=[False]
        def handler(request):
            settings=store.settings(); settings['pin_hash']='updated-pin-hash'
            if switch_token[0]: settings['telegram_token']=store.encrypt('987654:OTHER_TOKEN')
            store.save_settings(settings)
            return httpx.Response(200,json={'ok':True,'result':{'id':123,'is_bot':True,'username':'desk_bot'}})
        monitor=client.app.state.monitor; client.portal.call(monitor.http.aclose)
        monitor.http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert client.post('/api/telegram/status').json()['state'] == 'token_ok'
        assert store.settings()['pin_hash'] == 'updated-pin-hash'
        switch_token[0]=True
        assert client.post('/api/telegram/status').json()['state'] == 'pending'
        assert store.decrypt(store.settings()['telegram_token']) == '987654:OTHER_TOKEN'


@pytest.mark.parametrize('target',['@bad name','https://t.me/user','user\nname'])
def test_invalid_recipient_names_rejected(tmp_path,target):
    with TestClient(create_app(tmp_path,start_monitor=False),headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock',json={})
        assert client.put('/api/settings',json={'telegram_chat':target}).status_code == 422
