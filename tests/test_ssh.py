import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
import paramiko
import pytest
from fastapi.testclient import TestClient
from dashboard.app import create_app
from dashboard.remote import fingerprint, PinnedKey, RemoteError


class TestServer(paramiko.ServerInterface):
    __test__ = False
    def __init__(self, env=None):
        self.env = os.environ | (env or {})
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == 'deploy' and password == 'test-password' else paramiko.AUTH_FAILED
    def get_allowed_auths(self, username):
        return 'password'
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def check_channel_exec_request(self, channel, command):
        def execute():
            try:
                proc = subprocess.run(command.decode(),shell=True,capture_output=True,timeout=15,env=self.env)
                if proc.stdout: channel.sendall(proc.stdout)
                if proc.stderr: channel.sendall_stderr(proc.stderr)
                channel.send_exit_status(proc.returncode)
            finally:
                channel.close()
        threading.Thread(target=execute,daemon=True).start()
        return True


@contextmanager
def ssh_server(env=None):
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(); listener.settimeout(.2)
    port = listener.getsockname()[1]
    stopped = threading.Event(); transports = []
    def client(sock):
        transport = paramiko.Transport(sock); transports.append(transport); transport.add_server_key(key)
        try:
            transport.start_server(server=TestServer(env))
            channels = []
            while transport.is_active() and not stopped.is_set():
                channel = transport.accept(.2)
                if channel is not None: channels.append(channel)
        except (EOFError,OSError,paramiko.SSHException):
            pass
        finally: transport.close()
    def accept():
        while not stopped.is_set():
            try: sock,_ = listener.accept()
            except socket.timeout: continue
            except OSError: break
            threading.Thread(target=client,args=(sock,),daemon=True).start()
    thread = threading.Thread(target=accept,daemon=True); thread.start()
    try: yield port,key
    finally:
        stopped.set(); listener.close()
        for transport in transports: transport.close()
        thread.join(1)


def test_host_key_change_is_rejected():
    trusted,changed = paramiko.RSAKey.generate(2048),paramiko.RSAKey.generate(2048)
    policy = PinnedKey(fingerprint(trusted))
    policy.missing_host_key(None,'server',trusted)
    with pytest.raises(RemoteError,match='изменился'):
        policy.missing_host_key(None,'server',changed)


@pytest.mark.skipif(os.name == 'nt',reason='Loopback fixture simulates a Unix SSH server')
def test_real_ssh_trust_authentication_directory_and_disk(tmp_path):
    project_dir = tmp_path/'project'; project_dir.mkdir()
    with ssh_server() as (port,key), TestClient(create_app(tmp_path/'data',start_monitor=False),headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock',json={})
        project = client.post('/api/projects',json={'name':'SSH Test','ssh_host':'127.0.0.1','ssh_port':port,
                'ssh_user':'deploy','ssh_path':str(project_dir),'ssh_password':'test-password','git_monitor':False}).json()
        path = '/api/projects/'+project['id']
        assert client.post(path+'/ssh/test').status_code == 409
        actual = client.get(path+'/ssh/fingerprint').json()
        assert actual['fingerprint'] == fingerprint(key)
        assert client.post(path+'/ssh/trust',json={'fingerprint':actual['fingerprint']}).status_code == 200
        response = client.post(path+'/ssh/test')
        assert response.status_code == 200, response.text
        assert response.json()['disk']['total'] > 0
        state = client.post(path+'/check').json()
        assert state['ssh']['ok'] and state['disk']['free'] > 0
        assert state['status'] == 'ok'


@pytest.mark.skipif(os.name == 'nt',reason='Simulates Linux server Git and SSH')
def test_auto_sync_real_push_and_secret_stop(tmp_path):
    project_dir = tmp_path/'project'; project_dir.mkdir()
    bare = tmp_path/'remote.git'
    subprocess.run(['git','init','--bare',str(bare)],check=True,capture_output=True)
    config = tmp_path/'git-test.conf'
    remote='git@gitlab.test:team/project.git'
    config.write_text('[url "'+bare.as_uri()+'"]\n\tinsteadOf = '+remote+'\n')
    env={'GIT_CONFIG_GLOBAL':str(config),'GIT_ALLOW_PROTOCOL':'file:ssh'}
    def git(*args):
        return subprocess.check_output(['git','-C',str(project_dir),*args],env=os.environ | env,stderr=subprocess.STDOUT).decode().strip()
    git('init','-b','main'); git('config','user.name','Test'); git('config','user.email','test@example.com')
    (project_dir/'index.php').write_text('initial')
    git('add','index.php'); git('commit','-m','initial'); git('remote','add','origin',remote); git('push','origin','main')
    with ssh_server(env) as (port,key), TestClient(create_app(tmp_path/'data',start_monitor=False),headers={'X-App-Request':'1'}) as client:
        client.post('/api/unlock',json={})
        project = client.post('/api/projects',json={'name':'Auto Git','ssh_host':'127.0.0.1','ssh_port':port,
            'ssh_user':'deploy','ssh_path':str(project_dir),'ssh_password':'test-password',
            'git_branch':'main','auto_sync':True,'auto_push':True,'notify':False}).json()
        raw=client.app.state.store.project(project['id']); raw['ssh_fingerprint']=fingerprint(key); client.app.state.store.save_project(raw)
        path='/api/projects/'+project['id']+'/check'
        (project_dir/'index.php').write_text('updated automatically')
        state=client.post(path).json()
        assert state['auto_sync_result']['pushed'], state
        assert subprocess.check_output(['git','-C',str(bare),'show','main:index.php']).decode() == 'updated automatically'
        (project_dir/'.env').write_text('NEVER_UPLOAD_SECRET')
        (project_dir/'index.php').write_text('blocked by secret file')
        state=client.post(path).json()
        assert state['git']['auto_error']
        assert git('show','HEAD:index.php') == 'updated automatically'
        assert '.env' not in git('ls-tree','--name-only','HEAD')
        # A rejected push is a persistent visible warning, even when no files remain dirty.
        (project_dir/'.env').unlink()
        clone=tmp_path/'other'
        subprocess.run(['git','clone','--branch','main',str(bare),str(clone)],check=True,capture_output=True)
        def other(*args):
            return subprocess.run(['git','-C',str(clone),*args],check=True,capture_output=True)
        other('config','user.name','Other'); other('config','user.email','other@example.com')
        (clone/'remote.txt').write_text('remote change')
        other('add','remote.txt'); other('commit','-m','remote commit'); other('push','origin','main')
        state=client.post(path).json()
        assert state['auto_sync_result']['warning'] and state['pending_push']
        state=client.post(path).json()
        assert state['pending_push'] and state['status']=='error'
