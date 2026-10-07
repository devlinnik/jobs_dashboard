"""Real browser + loopback HTTP/SSH/Git smoke. Run on Linux: python -m tests.ui_smoke."""
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from tests.test_ssh import ssh_server
from tests.git_https_server import git_https_server, TOKEN

ROOT = Path(__file__).resolve().parents[1]


class Website(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503 if self.path == '/down' else 200)
        self.end_headers(); self.wfile.write(b'Portal Ready')
    def log_message(self, *args): pass


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); return sock.getsockname()[1]


def main():
    with tempfile.TemporaryDirectory(prefix='devlin-ui-') as directory, ExitStack() as stack:
        temp = Path(directory)
        website = ThreadingHTTPServer(('127.0.0.1',0),Website)
        thread = threading.Thread(target=website.serve_forever,daemon=True); thread.start()
        stack.callback(website.server_close); stack.callback(website.shutdown)
        port = free_port()
        git_host = stack.enter_context(git_https_server(temp/'git-https'))
        proc = subprocess.Popen([sys.executable,str(ROOT/'run.py'),'--no-open','--port',str(port),'--data-dir',str(temp/'data')],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
            env=os.environ | {'SSL_CERT_FILE':str(git_host['ca']), 'NO_PROXY':'127.0.0.1,localhost'})
        def stop():
            proc.terminate()
            try: proc.wait(timeout=20)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        stack.callback(stop)
        url = f'http://127.0.0.1:{port}'
        for _ in range(100):
            try:
                urllib.request.urlopen(url+'/api/session',timeout=.2).close(); break
            except OSError: time.sleep(.1)
        else: raise AssertionError('UI server did not start')
        server_files = temp/'server'; server_files.mkdir()
        (server_files/'index.php').write_text('<?php echo "Initial";')
        bare = git_host['bare']
        ssh_port, key = stack.enter_context(ssh_server({'GIT_SSL_CAINFO':str(git_host['ca']), 'NO_PROXY':'127.0.0.1,localhost'}, home=str(temp)))
        with sync_playwright() as playwright:
            executable = os.environ.get('CHROMIUM_PATH')
            if not executable and Path('/usr/bin/chromium').exists(): executable = '/usr/bin/chromium'
            browser = playwright.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1440,'height':1000})
            errors = []; page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#app-shell')).to_be_visible()
            expect(page.locator('#app-version')).to_have_text('v1.4.0')
            page.locator('[data-nav=ssh-profiles]').click()
            expect(page.locator('#ssh-profiles-page')).to_be_visible()
            page.locator('#new-ssh-profile').click()
            profile_form = page.locator('#ssh-profile-form')
            profile_form.locator('[name=name]').fill('Общий сервер')
            profile_form.locator('[name=ssh_host]').fill('127.0.0.1')
            profile_form.locator('[name=ssh_port]').fill(str(ssh_port))
            profile_form.locator('[name=ssh_user]').fill('deploy')
            profile_form.locator('[name=ssh_password]').fill('test-password')
            profile_form.locator('button[type=submit]').click()
            expect(page.locator('#ssh-profile-trust')).to_be_enabled()
            page.locator('#ssh-profile-trust').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.locator('#confirm-accept').click()
            expect(page.locator('#ssh-profile-info')).to_contain_text('Ключ подтверждён')
            page.locator('#ssh-profile-test').click()
            expect(page.get_by_text('SSH-профиль подключён. Теперь выберите папку для проекта.', exact=True)).to_be_visible()
            page.locator('[data-close=ssh-profile-dialog]').first.click()
            expect(page.locator('.ssh-profile-row')).to_have_count(1)
            page.locator('[data-nav=git-profiles]').click()
            page.locator('#new-git-profile').click()
            git_form = page.locator('#git-profile-form')
            git_form.locator('[name=name]').fill('Рабочий GitLab')
            git_form.locator('[name=provider]').select_option('gitlab')
            git_form.locator('[name=base_url]').fill(git_host['base'])
            git_form.locator('[name=token]').fill(TOKEN)
            git_form.locator('button[type=submit]').click()
            expect(page.locator('#git-profile-test')).to_be_enabled()
            page.locator('#git-profile-test').click()
            expect(page.locator('#git-profile-info')).to_have_text('Авторизация проверена: developer')
            page.locator('[data-close=git-profile-dialog]').first.click()
            screenshot = ROOT/'test-results'; screenshot.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot/'git-profiles.png'))
            page.locator('[data-nav=projects]').click()
            page.locator('#hero-add').click()
            form=page.locator('#project-form')
            form.locator('[name=name]').fill('Корпоративный портал')
            form.locator('[name=url]').fill(f'http://127.0.0.1:{website.server_port}/healthy')
            form.locator('[name=description]').fill('Интеграционный тест: HTTP, SSH и Git')
            form.locator('[name=notify]').uncheck()
            form.locator('[name=ssh_profile_id]').select_option(label='Общий сервер')
            expect(page.locator('#individual-ssh-fields')).not_to_be_visible()
            page.locator('#ssh-browse').click()
            expect(page.locator('#remote-path')).to_have_value(str(temp), timeout=20000)
            page.locator(f'#remote-folders [data-remote-path="{server_files}"]').click(timeout=20000)
            expect(page.locator('#remote-path')).to_have_value(str(server_files), timeout=20000)
            page.locator('#remote-select').click()
            expect(form.locator('[name=ssh_path]')).to_have_value(str(server_files))
            assert page.evaluate("fetch('/api/projects').then(r=>r.json()).then(p=>p.length)") == 0
            form.locator('[name=git_branch]').fill('main')
            form.locator('[name=git_profile_id]').select_option(label='Рабочий GitLab · gitlab')
            form.locator('button[type=submit]').click()
            expect(page.locator('#project-form-dialog')).not_to_be_visible()
            expect(page.locator('.project-card')).to_have_count(1)
            page.locator('[data-open]').click()
            expect(page.locator('#detail-dialog')).to_be_visible()
            page.locator('[data-detail-action=browse]').click()
            expect(page.locator('#remote-browser-dialog')).to_be_visible()
            expect(page.locator('#remote-path')).to_have_value(str(temp), timeout=20000)
            page.locator('#remote-path').fill(str(temp/'missing'))
            page.locator('#remote-path-form button').click()
            expect(page.locator('#remote-error')).to_contain_text('не найдена', timeout=20000)
            expect(page.locator('#remote-select')).to_be_disabled()
            page.locator('#remote-home').click()
            page.locator(f'#remote-folders [data-remote-path="{server_files}"]').click(timeout=20000)
            expect(page.locator('#remote-path')).to_have_value(str(server_files), timeout=20000)
            screenshot = ROOT/'test-results'; screenshot.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot/'ssh-browser.png'))
            page.locator('#remote-select').click()
            expect(form.locator('[name=ssh_path]')).to_have_value(str(server_files))
            assert page.evaluate("fetch('/api/projects',{headers:{'X-App-Request':'1'}}).then(r=>r.json()).then(p=>p[0].ssh_path)") == str(server_files)
            form.locator('button[type=submit]').click()
            expect(page.locator('#project-form-dialog')).not_to_be_visible()
            page.locator('[data-detail-action=ssh-test]').click()
            expect(page.get_by_text('SSH подключён. Директория проекта доступна.',exact=True)).to_be_visible()
            page.locator('[data-detail-action=deploy]').click()
            expect(page.locator('#probe-setup-dialog')).to_be_visible()
            expect(page.locator('#probe-target')).to_contain_text(str(server_files))
            page.locator('#probe-url').fill('http://probe.test/.devlin-disk.php')
            page.locator('#probe-setup-form button[type=submit]').click()
            expect(page.locator('#probe-setup-error')).to_contain_text('HTTPS URL')
            page.locator('[data-close=probe-setup-dialog]').first.click()
            page.locator('[data-detail-action=check]').click()
            expect(page.locator('.big-value').first).to_contain_text('HTTP 200')
            page.locator('[data-tab=git]').click()
            page.locator('[data-git-action=check]').click()
            expect(page.get_by_text('Репозиторий не подключён. Выберите его через кнопку «Подключить репозиторий».',exact=True)).to_be_visible()
            page.locator('[data-git-action=attach]').click()
            page.locator('#git-repository-picker').click()
            expect(page.locator('[data-repository-index]')).to_have_count(1)
            page.locator('[data-repository-index]').click()
            expect(page.locator('#git-connect-remote')).to_have_value(git_host['remote'])
            page.locator('#git-connect-form button[type=submit]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.locator('#confirm-accept').click()
            expect(page.locator('#git-connect-dialog')).not_to_be_visible(timeout=20000)
            expect(page.locator('#detail-content')).to_contain_text(git_host['remote'])
            page.locator('[data-git-action=check]').click()
            expect(page.get_by_text('Репозиторий подключён, доступ на чтение с сервера проверен. Право записи проверяется при push.',exact=True)).to_be_visible(timeout=20000)
            page.locator('[data-git-action=commit]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.locator('#confirm-accept').click()
            try:
                expect(page.get_by_text('Рабочая директория чистая. Изменений для коммита нет.')).to_be_visible(timeout=20000)
            except AssertionError:
                print('Git import diagnostic:', page.locator('#detail-content').inner_text(), page.locator('#toast').inner_text())
                raise
            assert subprocess.check_output(['git','-C',str(bare),'show','main:index.php']).decode() == '<?php echo "Initial";'
            (server_files/'index.php').write_text('<?php echo "Updated on server";')
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('.git-file')).to_have_count(1)
            page.locator('[data-file-diff]').click()
            expect(page.locator('#file-diff')).to_contain_text('Updated on server')
            page.locator('[data-git-action=commit]').click(); page.locator('#confirm-accept').click()
            expect(page.get_by_text('Рабочая директория чистая. Изменений для коммита нет.')).to_be_visible(timeout=20000)
            assert 'Updated on server' in subprocess.check_output(['git','-C',str(bare),'show','main:index.php']).decode()
            page.locator('[data-tab=events]').click()
            expect(page.locator('#detail-content .event-row')).not_to_have_count(0)
            page.locator('[data-close=detail-dialog]').click()
            # Create two additional actual monitored local sites to exercise cards and filters.
            page.evaluate('''async base => {
              for (const [name,path,enabled] of [['API / сервис','down',true],['Сайт / разработка','healthy',false]]) {
                const p=await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json','X-App-Request':'1'},body:JSON.stringify({name,url:base+'/'+path,enabled,notify:false})}).then(r=>r.json());
                await fetch('/api/projects/'+p.id+'/check',{method:'POST',headers:{'X-App-Request':'1'}});
              }
            }''', f'http://127.0.0.1:{website.server_port}')
            page.reload(); expect(page.locator('.project-card')).to_have_count(3)
            page.locator('#status-filter').select_option('error'); expect(page.locator('.project-card')).to_have_count(1)
            page.locator('#status-filter').select_option('all')
            screenshot=ROOT/'test-results'; screenshot.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot/'dashboard.png'),full_page=True)
            page.locator('[data-nav=settings]').click()
            expect(page.locator('#settings-form')).to_be_visible()
            page.locator('#settings-form [name=pin]').fill('1234')
            page.locator('#settings-form button[type=submit]').click()
            expect(page.locator('#pin-status')).to_have_text('Включена')
            page.locator('#lock-button').click(); expect(page.locator('#lock-screen')).to_be_visible()
            page.locator('#unlock-pin').fill('9999'); page.locator('#unlock-form button').click()
            expect(page.locator('#unlock-error')).to_have_text('Неверный PIN')
            page.locator('#unlock-pin').fill('1234'); page.locator('#unlock-form button').click()
            expect(page.locator('#app-shell')).to_be_visible()
            page.locator('[data-nav=projects]').click()
            # Ensure escaped text cannot become executable markup.
            page.locator('#hero-add').click()
            page.locator('#project-form [name=name]').fill('<img src=x onerror=alert(1)>')
            page.locator('#project-form [name=notify]').uncheck()
            page.locator('#project-form button[type=submit]').click()
            expect(page.locator('.project-card')).to_have_count(4)
            assert page.locator('.project-card img').count() == 0
            page.get_by_role('heading',name='<img src=x onerror=alert(1)>').locator('..').locator('..').locator('..').locator('[data-open]').click()
            page.locator('[data-detail-action=delete]').click(); page.locator('#confirm-accept').click()
            expect(page.locator('#detail-dialog')).not_to_be_visible()
            expect(page.locator('.project-card')).to_have_count(3)
            # Check layout at the desktop's documented minimum and a smaller responsive viewport.
            for width in [1000,640]:
                page.set_viewport_size({'width':width,'height':800})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Horizontal overflow at {width}px'
            assert errors == [], errors
            browser.close()
            print('UI smoke passed: SSH/Git profiles, provider authorization, repository picker/attach/access check, verified HTTPS Git push, SFTP browsing, probe HTTPS validation, CRUD, HTTP, diff/commit, journal, filters, PIN, XSS escaping, responsive layout. Screenshots: test-results/dashboard.png, ssh-browser.png, git-profiles.png')


if __name__ == '__main__': main()
