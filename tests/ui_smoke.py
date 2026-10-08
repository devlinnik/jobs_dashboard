"""Real browser + loopback HTTP/SSH/Git smoke. Run on Linux: python -m tests.ui_smoke."""
import os
import json
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


def assert_toast_above_dialog(page, dialog, message):
    toast = page.locator('#toast-region .toast').filter(has_text=message).last
    expect(toast).to_be_visible()
    expect(toast).to_have_css('opacity', '1')
    expect(page.locator(dialog+' > #toast-region')).to_be_attached()
    toast.scroll_into_view_if_needed()
    assert toast.evaluate('''node => {
        const rect = node.getBoundingClientRect();
        const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return node === target || node.contains(target);
    }'''), 'Notification is hidden behind the modal or its backdrop'


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
        (server_files/'hosting-link').symlink_to('missing-target')
        (server_files/'muha/audio').mkdir(parents=True)
        legacy_name = 'muha/audio/Песня - Привет.mp3'.encode('cp1251')
        with open(os.fsencode(server_files) + b'/' + legacy_name, 'wb') as output:
            output.write(b'\x00MP3 sample')
        (server_files/'cache').mkdir(); (server_files/'cache/result.txt').write_text('keep ignored')
        (server_files/'server.log').write_text('keep log')
        bare = git_host['bare']
        ssh_env = {'GIT_SSL_CAINFO':str(git_host['ca']), 'NO_PROXY':'127.0.0.1,localhost',
                   'GIT_CONFIG_GLOBAL':str(temp/'ssh-gitconfig'), 'HOME':str(temp)}
        ssh_port, key = stack.enter_context(ssh_server(ssh_env, home=str(temp), writable=True))
        with sync_playwright() as playwright:
            executable = os.environ.get('CHROMIUM_PATH')
            if not executable and Path('/usr/bin/chromium').exists(): executable = '/usr/bin/chromium'
            browser = playwright.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox'])
            page = browser.new_page(viewport={'width':1440,'height':1000})
            errors = []; page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#app-shell')).to_be_visible()
            expect(page.locator('#app-version')).to_have_text('v1.10.12')
            assert page.evaluate("getComputedStyle(document.documentElement).colorScheme") == 'light'
            assert page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)") >= 16
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
            assert_toast_above_dialog(page, '#ssh-profile-dialog', 'SSH-профиль подключён')
            page.locator('#toast-region .toast').filter(has_text='SSH-профиль подключён').get_by_role('button',name='Закрыть уведомление').click()
            expect(page.locator('#toast-region .toast').filter(has_text='SSH-профиль подключён')).to_have_count(0)
            page.locator('[data-close=ssh-profile-dialog]').first.click()
            expect(page.locator('.ssh-profile-row')).to_have_count(1)
            page.locator('[data-nav=git-profiles]').click()
            page.locator('#new-git-profile').click()
            git_form = page.locator('#git-profile-form')
            git_form.locator('[name=name]').fill('Рабочий GitLab')
            git_form.locator('[name=provider]').select_option('gitlab')
            git_form.locator('[name=base_url]').fill(git_host['base'])
            git_form.locator('[name=token]').fill(TOKEN)
            git_form.locator('[name=git_name]').fill('Browser Author')
            git_form.locator('[name=git_email]').fill('browser@example.com')
            git_form.locator('button[type=submit]').click()
            expect(page.locator('#git-profile-test')).to_be_enabled()
            page.locator('#git-profile-test').click()
            expect(page.locator('#git-profile-info')).to_have_text('Авторизация проверена: developer')
            assert_toast_above_dialog(page, '#git-profile-dialog', 'Авторизация проверена: developer')
            page.locator('[data-close=git-profile-dialog]').first.click()
            screenshot = ROOT/'test-results'; screenshot.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot/'git-profiles.png'), animations='disabled')
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
            page.screenshot(path=str(screenshot/'ssh-browser.png'), animations='disabled')
            page.locator('#remote-select').click()
            expect(form.locator('[name=ssh_path]')).to_have_value(str(server_files))
            assert page.evaluate("fetch('/api/projects',{headers:{'X-App-Request':'1'}}).then(r=>r.json()).then(p=>p[0].ssh_path)") == str(server_files)
            form.locator('button[type=submit]').click()
            expect(page.locator('#project-form-dialog')).not_to_be_visible()
            page.locator('[data-detail-action=ssh-test]').click()
            expect(page.get_by_text('SSH подключён. Директория проекта доступна.',exact=True)).to_be_visible()
            assert_toast_above_dialog(page, '#detail-dialog', 'SSH подключён. Директория проекта доступна.')
            page.locator('[data-detail-action=fingerprint]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            assert_toast_above_dialog(page, '#confirm-dialog', 'SSH подключён. Директория проекта доступна.')
            page.locator('#confirm-cancel').click()
            assert_toast_above_dialog(page, '#detail-dialog', 'SSH подключён. Директория проекта доступна.')
            page.locator('[data-detail-action=deploy]').click()
            expect(page.locator('#probe-setup-dialog')).to_be_visible()
            expect(page.locator('#probe-target')).to_contain_text(str(server_files))
            page.locator('#probe-url').fill('http://probe.test/.devlin-disk.php')
            page.locator('#probe-setup-form button[type=submit]').click()
            expect(page.locator('#probe-setup-error')).to_contain_text('HTTPS URL')
            page.locator('[data-close=probe-setup-dialog]').first.click()
            page.locator('[data-detail-action=check]').click()
            expect(page.locator('.big-value').first).to_contain_text('HTTP 200')
            page.locator('[data-close=detail-dialog]').click()
            page.locator('[data-nav=ssh-profiles]').click()
            page.locator('[data-profile-action=edit]').click()
            profile_form.locator('[name=hosting]').check()
            profile_form.locator('[name=hosting_limit_gb]').fill('10')
            page.locator('#hosting-browse').click()
            expect(page.locator('#remote-path')).to_have_value(str(temp), timeout=20000)
            page.locator('#remote-select').click()
            expect(profile_form.locator('[name=hosting_path]')).to_have_value(str(temp))
            profile_form.locator('button[type=submit]').click()
            expect(page.get_by_text('SSH-профиль сохранён', exact=True).last).to_be_visible(timeout=20000)
            page.locator('[data-close=ssh-profile-dialog]').first.click()
            page.locator('[data-nav=projects]').click()
            page.locator('[data-open]').click()
            page.locator('[data-detail-action=check]').click()
            expect(page.locator('#detail-content')).to_contain_text('ОСТАТОК ТАРИФА ХОСТИНГА · ОЦЕНКА', timeout=20000)
            expect(page.locator('#detail-content')).to_contain_text('лимит: 10 ГБ', timeout=20000)
            expect(page.locator('#detail-content')).to_contain_text('Точная квота — в панели хостинга.')
            page.screenshot(path=str(screenshot/'hosting-quota.png, telegram-settings.png'), animations='disabled')
            page.locator('[data-tab=git]').click()
            expect(page.locator('#gitignore-status')).to_contain_text('Файл отсутствует', timeout=20000)
            page.locator('[data-git-action=ignore]').click()
            expect(page.locator('#gitignore-content')).to_be_enabled(timeout=20000)
            page.locator('#gitignore-content').fill('# Server project rules\n')
            page.locator('#gitignore-pick').click()
            expect(page.locator('[data-ignore-path="cache"]')).to_be_visible(timeout=20000)
            page.locator('[data-ignore-open="cache"]').click()
            expect(page.locator('[data-ignore-path="cache/result.txt"]')).to_be_visible(timeout=20000)
            page.locator('#gitignore-up').click()
            expect(page.locator('[data-ignore-path="cache"]')).to_be_visible(timeout=20000)
            page.locator('[data-ignore-path="cache"]').check()
            page.locator('[data-ignore-path="server.log"]').check()
            page.locator('#gitignore-add').click()
            expect(page.locator('#gitignore-content')).to_have_value('# Server project rules\n/cache/\n/server.log\n')
            screenshot = ROOT/'test-results'; screenshot.mkdir(exist_ok=True)
            page.screenshot(path=str(screenshot/'gitignore-editor.png, hosting-quota.png, telegram-settings.png'), animations='disabled')
            page.locator('#gitignore-save').click(); page.locator('#confirm-accept').click()
            expect(page.locator('#gitignore-dialog')).not_to_be_visible(timeout=20000)
            expect(page.locator('#gitignore-status')).to_contain_text('Файл существует', timeout=20000)
            assert (server_files/'cache/result.txt').read_text() == 'keep ignored'
            expect(page.locator('.git-file')).to_have_count(4)
            expect(page.locator('.file-name').filter(has_text='Песня - Привет.mp3')).to_contain_text('имя в байтах')
            assert '\ufffd' not in page.locator('.file-list').inner_text()
            page.locator('.file-row').filter(has_text='Песня - Привет.mp3').locator('.view-diff').click()
            expect(page.locator('#file-diff')).to_contain_text('Двоичный файл: текстовый просмотр недоступен.')
            page.locator('[data-git-action=ignore]').click()
            expect(page.locator('#gitignore-content')).to_be_enabled(timeout=20000)
            assert '# Server project rules' in page.locator('#gitignore-content').input_value()
            page.locator('#gitignore-content').fill(page.locator('#gitignore-content').input_value() + '*.tmp\n')
            page.locator('#gitignore-save').click(); page.locator('#confirm-accept').click()
            expect(page.locator('#gitignore-dialog')).not_to_be_visible(timeout=20000)
            page.locator('[data-git-action=diagnostics]').click()
            expect(page.locator('#git-diagnostics-dialog')).to_be_visible(timeout=20000)
            expect(page.locator('#git-diagnostics-content')).to_contain_text('В корне проекта ещё нет Git-репозитория.')
            page.locator('[data-close=git-diagnostics-dialog]').first.click()
            (server_files/'.git').mkdir()
            (server_files/'.git/important-original').write_text('keep original metadata')
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('#git-recovery-panel')).to_contain_text('.git/HEAD: отсутствует', timeout=20000)
            assert page.locator('#git-recovery-panel').evaluate('(panel) => panel.getBoundingClientRect().top - panel.previousElementSibling.getBoundingClientRect().bottom') >= 16
            page.locator('[data-git-action="debug"]').click()
            expect(page.locator('#git-debug-dialog')).to_be_visible()
            report = json.loads(page.locator('#git-debug-text').input_value())
            assert report['records'] and any(row['operation'] == 'status' for row in report['records'])
            assert all('stdout_bytes' in row and 'duration_ms' in row for row in report['records'])
            page.context.grant_permissions(['clipboard-read','clipboard-write'])
            page.locator('#git-debug-copy').click()
            expect(page.locator('.toast').last).to_contain_text('Отчёт Git скопирован')
            assert json.loads(page.evaluate('navigator.clipboard.readText()')) == report
            page.locator('[data-close="git-debug-dialog"]').first.click()
            page.locator('[data-git-repair="archive-reinitialize"]').click()
            expect(page.locator('#confirm-description')).to_contain_text('пустую историю')
            page.locator('#confirm-accept').click()
            expect(page.locator('[data-git-action=commit]')).to_be_enabled(timeout=20000)
            backups = list((temp/'.devlin-git-backups').rglob('important-original'))
            assert len(backups) == 1 and backups[0].read_text() == 'keep original metadata'
            assert (server_files/'index.php').read_text() == '<?php echo "Initial";'
            page.locator('[data-git-action=check]').click()
            expect(page.get_by_text('Репозиторий не подключён. Выберите его через кнопку «Подключить репозиторий».',exact=True)).to_be_visible()
            page.locator('[data-git-action=attach]').click()
            expect(page.locator('#git-connect-info')).to_contain_text('Версия приложения: v1.10.12')
            expect(page.locator('#git-server-key-field')).not_to_be_visible()
            expect(page.locator('#git-connect-auth-hint')).to_contain_text('HTTPS и токен')
            page.locator('#git-repository-picker').click()
            expect(page.locator('[data-repository-index]')).to_have_count(1)
            page.locator('[data-repository-index]').click()
            expect(page.locator('#git-connect-remote')).to_have_value(git_host['remote'])
            page.locator('#git-connect-form button[type=submit]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            page.locator('#confirm-accept').click()
            expect(page.locator('#git-connect-dialog')).not_to_be_visible(timeout=20000)
            expect(page.locator('#detail-content')).to_contain_text(git_host['remote'])
            # Force real Git's ownership guard in subsequent SSH sessions. Repair
            # must be offered inline and write only the exact project trust entry.
            ssh_env['GIT_TEST_ASSUME_DIFFERENT_OWNER'] = '1'
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('[data-git-repair="trust-directory"]')).to_be_visible(timeout=20000)
            page.locator('[data-git-repair="trust-directory"]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            expect(page.locator('#confirm-description')).to_contain_text(str(server_files))
            page.locator('#confirm-accept').click()
            expect(page.locator('[data-git-action=commit]')).to_be_enabled(timeout=20000)
            assert subprocess.check_output(['git','config','--file',str(temp/'ssh-gitconfig'),'--get-all','safe.directory']).decode().strip() == str(server_files)
            assert page.locator('.file-name').first.evaluate('node => parseFloat(getComputedStyle(node).fontSize)') >= 14
            page.locator('[data-git-action=check]').click()
            expect(page.get_by_text('Репозиторий подключён, доступ на чтение с сервера проверен. Право записи проверяется при push.',exact=True)).to_be_visible(timeout=20000)
            subprocess.run(['git','-C',str(server_files),'add','--','.gitignore'],check=True)
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('[data-git-action=commit]')).to_be_disabled(timeout=20000)
            expect(page.locator('[data-git-action=commit]')).to_have_css('cursor', 'not-allowed')
            page.locator('[data-git-action=unstage]').click()
            expect(page.locator('#operation-region')).to_be_visible()
            expect(page.locator('#operation-region')).to_contain_text('Отмена подготовки файлов')
            expect(page.locator('#operation-region')).to_have_attribute('popover', 'manual')
            assert page.locator('#operation-region').evaluate("node => node.matches(':popover-open')")
            assert page.locator('#operation-region').evaluate('''node => {
                const previous = node.style.pointerEvents; node.style.pointerEvents = 'auto';
                const r = node.getBoundingClientRect(); const target = document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                node.style.pointerEvents = previous;
                return node === target || node.contains(target);
            }'''), 'Operation indicator is hidden by the modal'
            expect(page.locator('#confirm-extra')).to_contain_text('.gitignore')
            page.locator('#confirm-cancel').click()
            expect(page.locator('#operation-region')).not_to_be_visible()
            assert subprocess.check_output(['git','-C',str(server_files),'ls-files','--cached']).decode().strip() == '.gitignore'
            page.locator('[data-git-action=unstage]').click(); page.locator('#confirm-accept').click()
            expect(page.locator('[data-git-action=commit]')).to_be_enabled(timeout=20000)
            assert (server_files/'.gitignore').exists()
            page.locator('[data-git-action=commit]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            expect(page.locator('#confirm-extra')).to_contain_text('Песня - Привет.mp3')
            page.locator('#confirm-accept').click()
            try:
                expect(page.get_by_text('Рабочая директория чистая. Изменений для коммита нет.')).to_be_visible(timeout=20000)
            except AssertionError:
                print('Git import diagnostic:', page.locator('#detail-content').inner_text(), page.locator('#toast').inner_text())
                raise
            expect(page.locator('#outgoing-commits .commit-row')).to_have_count(1, timeout=20000)
            expect(page.locator('#outgoing-commits')).to_contain_text('Browser Author')
            assert_toast_above_dialog(page, '#detail-dialog', 'создан на сервере')
            subprocess.run(['git','-C',str(server_files),'remote','set-url','origin','git@127.0.0.1:team/project.git'],check=True)
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('[data-git-repair="profile-https"]')).to_be_visible(timeout=20000)
            page.locator('[data-git-repair="profile-https"]').click()
            expect(page.locator('#confirm-description')).to_contain_text(git_host['remote'])
            page.locator('#confirm-accept').click()
            try:
                expect(page.locator('#git-history-panel [data-git-action=push]')).to_be_enabled(timeout=20000)
            except AssertionError:
                print('Git repair diagnostic:', page.locator('#detail-content').inner_text(), page.locator('#toast-region').inner_text())
                raise
            assert subprocess.check_output(['git','-C',str(server_files),'remote','get-url','origin']).decode().strip() == git_host['remote']
            assert subprocess.run(['git','-C',str(bare),'rev-parse','--verify','main'],capture_output=True).returncode != 0
            assert_toast_above_dialog(page, '#detail-dialog', 'Настройки Git исправлены')
            page.screenshot(path=str(screenshot/'git-outgoing.png, gitignore-editor.png, hosting-quota.png, telegram-settings.png'), animations='disabled')
            page.locator('#git-history-panel [data-git-action=push]').click()
            expect(page.locator('#confirm-dialog')).to_be_visible()
            expect(page.locator('#confirm-extra')).to_contain_text('Browser Author')
            page.locator('#confirm-accept').click()
            expect(page.locator('#outgoing-commits')).to_have_count(0, timeout=20000)
            assert subprocess.check_output(['git','-C',str(bare),'show','main:index.php']).decode() == '<?php echo "Initial";'
            tracked_names = subprocess.check_output(['git','-C',str(bare),'ls-tree','-rz','--name-only','main']).split(b'\0')
            assert legacy_name in tracked_names
            assert subprocess.check_output(['git','-C',str(bare),'show','main:hosting-link']).decode() == 'missing-target'
            page.screenshot(path=str(screenshot/'git-notification.png'), animations='disabled')
            (server_files/'index.php').write_text('<?php echo "Updated on server";')
            page.locator('[data-git-action=refresh]').click()
            expect(page.locator('.git-file')).to_have_count(1)
            assert page.locator('#detail-dialog').evaluate('node => node.scrollHeight <= node.clientHeight + 1'), 'Project dialog has an extra outer scrollbar'
            page.locator('[data-file-diff]').click()
            expect(page.locator('#file-diff')).to_contain_text('Updated on server')
            page.locator('[data-git-action=commit]').click(); page.locator('#confirm-accept').click()
            expect(page.get_by_text('Рабочая директория чистая. Изменений для коммита нет.')).to_be_visible(timeout=20000)
            expect(page.locator('#outgoing-commits .commit-row')).to_have_count(1, timeout=20000)
            assert subprocess.check_output(['git','-C',str(bare),'show','main:index.php']).decode() == '<?php echo "Initial";'
            page.locator('#git-history-panel [data-git-action=push]').click(); page.locator('#confirm-accept').click()
            expect(page.locator('#outgoing-commits')).to_have_count(0, timeout=20000)
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
            page.screenshot(path=str(screenshot/'dashboard.png'),full_page=True, animations='disabled')
            page.locator('[data-nav=settings]').click()
            expect(page.locator('#settings-form')).to_be_visible()
            assert page.locator('.topbar .user-avatar').count() == 0
            gear = page.locator('[data-nav=settings] svg')
            box = gear.bounding_box(); assert abs(box['width'] - box['height']) < 1
            status_requests = []
            def bot_status(route):
                status_requests.append(route.request.method)
                route.fulfill(json={'state':'ready','bot_username':'desk_test_bot','chat_id':12345,
                    'recipient_label':'Browser Recipient','message':'Бот доступен, получатель найден.',
                    'checked_at':'2026-10-07T12:00:00Z'})
            page.route('**/api/telegram/status', bot_status)
            page.locator('#settings-form [name=telegram_token]').fill('123456:UI_TEST_TOKEN')
            page.locator('#settings-form [name=telegram_chat]').fill('browser_recipient')
            page.locator('#settings-form button[type=submit]').click()
            expect(page.locator('#bot-status')).to_have_text('Бот доступен', timeout=20000)
            expect(page.locator('#telegram-status-info')).to_contain_text('@desk_test_bot')
            expect(page.locator('#settings-form [name=telegram_chat]')).to_have_value('@browser_recipient')
            assert status_requests == ['POST']
            assert page.locator('#settings-form [name=telegram_token]').input_value() == ''
            page.screenshot(path=str(screenshot/'telegram-settings.png'), animations='disabled')

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
            print('UI smoke passed: Telegram nickname/automatic bot status (mocked), square gear/no decorative avatar, first commit with dangling symlink, light theme, larger fonts, clickable notifications above nested modals, project scrolling, SSH/Git profiles, provider authorization, repository picker/attach/access check, hosting tariff/shared root/SSH picker, Git diagnostics, visual .gitignore creation/editing/picker, profile author, local commits/outgoing history/separate HTTPS Git push, SFTP, CRUD, diff/commit, journal, filters, PIN, XSS, responsive layout. Screenshots: test-results/dashboard.png, ssh-browser.png, git-profiles.png, git-notification.png, git-outgoing.png, gitignore-editor.png, hosting-quota.png, telegram-settings.png')


if __name__ == '__main__': main()
