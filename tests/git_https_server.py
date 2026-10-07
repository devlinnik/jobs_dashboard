"""Authenticated local GitLab API + real Git smart HTTP over verified TLS."""
import base64
import json
import os
import ssl
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

TOKEN = 'local-test-token'


@contextmanager
def git_https_server(directory):
    directory.mkdir(parents=True, exist_ok=True)
    certificate, key = directory/'ca.pem', directory/'tls.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-keyout', str(key), '-out', str(certificate), '-subj', '/CN=127.0.0.1',
        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
    bare = directory/'repos/team/project.git'; bare.parent.mkdir(parents=True)
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(bare), 'config', 'http.receivepack', 'true'], check=True)
    backend = subprocess.check_output(['git', '--exec-path']).decode().strip() + '/git-http-backend'
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path.startswith('/api/v4/'):
                if self.headers.get('PRIVATE-TOKEN') != TOKEN:
                    return self.respond(401, {'message': 'Unauthorized'})
                item = {'path_with_namespace': 'team/project', 'http_url_to_repo': base+'/team/project.git',
                    'default_branch': 'main', 'visibility': 'private', 'archived': False}
                if path == '/api/v4/user': return self.respond(200, {'username': 'developer', 'name': 'Local Developer'})
                if path == '/api/v4/projects': return self.respond(200, [item])
                if path == '/api/v4/projects/team/project': return self.respond(200, item)
                return self.respond(404, {})
            self.git_request()

        def do_POST(self):
            self.git_request()

        def git_request(self):
            expected = 'Basic ' + base64.b64encode(('oauth2:'+TOKEN).encode()).decode()
            authenticated = self.headers.get('Authorization') == expected
            requests.append((self.command, urlsplit(self.path).path, authenticated))
            if not authenticated:
                self.send_response(401); self.send_header('WWW-Authenticate', 'Basic realm="test"'); self.end_headers(); return
            parsed = urlsplit(self.path)
            if not parsed.path.startswith('/team/project.git/'):
                return self.respond(404, {})
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            env = os.environ | {'GIT_PROJECT_ROOT': str(directory/'repos'), 'GIT_HTTP_EXPORT_ALL': '1',
                'REQUEST_METHOD': self.command, 'PATH_INFO': parsed.path, 'QUERY_STRING': parsed.query,
                'CONTENT_TYPE': self.headers.get('Content-Type', ''), 'CONTENT_LENGTH': str(len(body)),
                'REMOTE_USER': 'developer', 'REMOTE_ADDR': '127.0.0.1', 'SERVER_PROTOCOL': 'HTTP/1.1'}
            result = subprocess.run([backend], input=body, capture_output=True, env=env, timeout=30, check=True)
            headers, data = result.stdout.split(b'\r\n\r\n', 1)
            lines = [line.decode().split(':', 1) for line in headers.split(b'\r\n')]
            code = next((int(value.strip().split()[0]) for name,value in lines if name.lower() == 'status'), 200)
            self.send_response(code)
            for name, value in lines:
                if name.lower() != 'status': self.send_header(name, value.strip())
            self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    base = f'https://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield {'base': base, 'remote': base+'/team/project.git', 'bare': bare, 'ca': certificate, 'requests': requests}
    finally:
        server.shutdown(); server.server_close(); thread.join()
