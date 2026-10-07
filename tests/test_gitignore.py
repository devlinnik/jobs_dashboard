import os
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.gitignore import literal_rule
from tests.test_ssh import ssh_server


@pytest.fixture
def editor(tmp_path):
    if os.name == 'nt':
        pytest.skip('Loopback SSH fixture runs Unix server commands')
    root = tmp_path/'project'; root.mkdir()
    with ssh_server(home=str(tmp_path), writable=True) as (port, _), TestClient(
        create_app(tmp_path/'data', start_monitor=False), headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock', json={})
        project = client.post('/api/projects', json={'name':'Ignore test','ssh_host':'127.0.0.1',
            'ssh_port':port,'ssh_user':'deploy','ssh_password':'test-password','ssh_path':str(root)}).json()
        prefix = '/api/projects/'+project['id']
        key = client.get(prefix+'/ssh/fingerprint').json()['fingerprint']
        assert client.post(prefix+'/ssh/trust', json={'fingerprint':key}).status_code == 200
        yield root, client, prefix+'/git/ignore'


def test_create_edit_stale_guard_and_real_ignore_rules(editor):
    root, client, api = editor
    (root/'cache').mkdir(); (root/'cache/result.txt').write_text('server data')
    (root/'literal[1]*?.txt').write_text('literal filename')
    (root/'literal111x.txt').write_text('different file')
    (root/'tracked.log').write_text('tracked data')
    subprocess.run(['git','init','-b','main',str(root)], check=True, capture_output=True)
    subprocess.run(['git','-C',str(root),'add','tracked.log'], check=True)
    original = client.get(api).json()
    assert original['exists'] is False and original['content'] == '' and len(original['snapshot']) == 64
    listing = client.get(api+'/files').json()
    rules = {item['name']:item['rule'] for item in listing['entries']}
    assert '.git' not in rules
    assert rules['cache'] == '/cache/'
    assert client.get(api+'/files', params={'path':'cache'}).json()['entries'][0]['path'] == 'cache/result.txt'
    content = '# Preserve comments\n' + rules['cache'] + '\n' + rules['literal[1]*?.txt'] + '\n*.log\n'
    body = {'content':content,'snapshot':original['snapshot'],'confirm':False}
    assert client.put(api, json=body).status_code == 409
    assert not (root/'.gitignore').exists()
    body['confirm'] = True
    assert client.put(api, json=body).status_code == 200
    assert (root/'.gitignore').read_text() == content
    assert (root/'cache/result.txt').read_text() == 'server data'
    ignored = subprocess.run(['git','-C',str(root),'check-ignore','cache/result.txt','literal[1]*?.txt','literal111x.txt','tracked.log'], capture_output=True, text=True)
    assert ignored.stdout.splitlines() == ['cache/result.txt','literal[1]*?.txt']
    state = client.get(api).json()
    os.chmod(root/'.gitignore', 0o640)
    body.update(content=content+'!keep.log\n',snapshot=state['snapshot'])
    assert client.put(api, json=body).status_code == 200
    assert (root/'.gitignore').stat().st_mode & 0o777 == 0o640
    assert client.put(api, json=body).status_code == 409
    assert (root/'.gitignore').read_text() == content+'!keep.log\n'
    assert not list(root.glob('.devlin-gitignore-*'))
    client.post('/api/lock')
    for endpoint in (api, api+'/files'):
        assert client.get(endpoint).status_code == 401
    assert client.put(api, json=body).status_code == 401


def test_browser_and_editor_do_not_follow_symlinks_or_escape_root(editor):
    root, client, api = editor
    outside = root.parent/'outside'; outside.mkdir()
    (outside/'important.txt').write_text('preserve')
    (root/'link').symlink_to(outside, target_is_directory=True)
    for path in ('../outside','/etc','.git','link','link/child','folder/../other','a\nb'):
        assert client.get(api+'/files', params={'path':path}).status_code == 409
    entries = client.get(api+'/files').json()['entries']
    link = next(item for item in entries if item['name'] == 'link')
    assert link['symlink'] and not link['directory']
    (root/'.gitignore').symlink_to(outside/'important.txt')
    assert client.get(api).status_code == 409
    assert client.put(api, json={'content':'overwrite','snapshot':'missing','confirm':True}).status_code == 409
    assert (outside/'important.txt').read_text() == 'preserve'


def test_unsupported_gitignore_is_preserved(editor):
    root, client, api = editor
    file = root/'.gitignore'
    for data in (b'\xff', b'x'*256001):
        file.write_bytes(data)
        assert client.get(api).status_code == 409
        assert file.read_bytes() == data
    file.unlink()
    assert client.put(api,json={'content':'\x00','snapshot':'missing','confirm':True}).status_code == 409
    assert not file.exists()


@pytest.mark.parametrize('name', ['#file','!important','a b','a\\b','[name]*?.log'])
def test_picker_patterns_match_literal_names(tmp_path, name):
    if os.name == 'nt' and any(c in name for c in '*?\\'):
        pytest.skip('Unix server filenames')
    subprocess.run(['git','init',str(tmp_path)],check=True,capture_output=True)
    (tmp_path/name).write_text('test')
    (tmp_path/'.gitignore').write_text(literal_rule(name)+'\n')
    check = subprocess.run(['git','-C',str(tmp_path),'check-ignore','--',name],capture_output=True)
    assert check.returncode == 0
