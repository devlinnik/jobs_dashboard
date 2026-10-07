"""Windows desktop entry point; --browser is also useful for headless development."""
import argparse
import json
import tempfile
import logging
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
import uvicorn
from dashboard.app import create_app
from dashboard.store import data_directory
from dashboard.instance import InstanceLock


def desktop_self_test(output=None):
    """Check the packaged backend/assets in an isolated data directory without opening a GUI."""
    result = {'ok': False}
    try:
        from dashboard.desktop import ASSETS
        if not (ASSETS / 'devlin-desk.ico').read_bytes().startswith(b'\x00\x00\x01\x00'):
            raise RuntimeError('Missing or invalid Windows icon')
        if not (ASSETS / 'devlin-desk.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('Missing or invalid icon source')
        if sys.platform == 'win32':
            # Validate bundled tray dependencies without creating a notification icon.
            import pystray
            from PIL import Image
            with Image.open(ASSETS / 'devlin-desk.ico') as icon:
                icon.load()
            if getattr(sys, 'frozen', False):
                from dashboard.windows_icon import verify_executable_icon
                verify_executable_icon(sys.executable, ASSETS / 'devlin-desk.ico')
        with tempfile.TemporaryDirectory(prefix='devlin-self-test-') as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            app = create_app(directory, start_monitor=False)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_config=None,
                access_log=False, log_level='warning', loop='asyncio', http='h11', ws='none', lifespan='on'))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = f'http://127.0.0.1:{port}'
            try:
                for _ in range(100):
                    if not thread.is_alive():
                        raise RuntimeError('Self-test server exited before readiness')
                    try:
                        with local.open(base + '/api/session', timeout=.5) as response:
                            session = json.loads(response.read())
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Self-test server did not start')
                if session.get('pin_enabled') is not False or session.get('authenticated') is not False:
                    raise RuntimeError('Self-test did not use isolated application data')
                for path, expected in [('/', b'Devlin Desk'), ('/static/app.js', b'loadProjects'), ('/static/styles.css', b'--mint')]:
                    with local.open(base + path, timeout=5) as response:
                        body = response.read()
                        if response.status != 200 or expected not in body:
                            raise RuntimeError('Missing or invalid packaged asset: ' + path)
                checks = ['local-api', 'isolated-data', 'html', 'javascript', 'css', 'application-icons']
                if sys.platform == 'win32':
                    checks.append('tray-dependencies')
                    if getattr(sys, 'frozen', False):
                        checks.append('embedded-exe-icon')
                result = {'ok': True, 'checks': checks}
            finally:
                server.should_exit = True
                thread.join()
    except Exception as error:
        result['error'] = str(error)
    if output:
        with Path(output).open('x', encoding='utf-8') as report:
            json.dump(result, report)
    if sys.stdout:
        print(json.dumps(result), flush=True)
    return result['ok']


def main():
    parser = argparse.ArgumentParser(description='Devlin Desk — управление проектами')
    parser.add_argument('--browser', action='store_true', help='Открыть в браузере вместо окна приложения')
    parser.add_argument('--no-open', action='store_true', help='Не открывать окно, для серверных проверок')
    parser.add_argument('--port', type=int, default=0, help='Локальный порт (по умолчанию свободный)')
    parser.add_argument('--data-dir', type=Path, help='Каталог локальных данных')
    parser.add_argument('--self-test', action='store_true', help='Проверить сборку без GUI и пользовательских данных')
    parser.add_argument('--self-test-output', type=Path, help='Новый JSON-файл результата проверки')
    args = parser.parse_args()
    if sys.platform == 'win32':
        # Stable taskbar identity, independent of the Python build interpreter.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Devlin.DevlinDesk')
    if args.self_test:
        if not desktop_self_test(args.self_test_output):
            raise RuntimeError('Проверка сборки не прошла')
        return
    if args.self_test_output:
        parser.error('--self-test-output требует --self-test')
    if args.port and not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535')
    port = args.port
    if not port:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
    instance = InstanceLock(args.data_dir or data_directory())
    app = create_app(args.data_dir)
    # Windowed .exe has no stdout/stderr; file logs never include API payloads or tokens.
    log_file = app.state.store.directory / 'app.log'
    logging.basicConfig(filename=log_file, level=logging.WARNING,
                        format='%(asctime)s %(levelname)s %(message)s')
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                            log_config=None, access_log=False, log_level='warning', loop='asyncio', http='h11', ws='none', lifespan='on'))
    url = f'http://127.0.0.1:{port}'
    if args.no_open:
        print('Devlin Desk:', url, flush=True)
        try:
            server.run()
        finally:
            instance.close()
        return
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        if not thread.is_alive():
            raise RuntimeError('Локальный сервер не запустился. См. app.log.')
        try:
            with local.open(url + '/api/session', timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError('Локальный сервер не отвечает')
    try:
        if args.browser:
            webbrowser.open(url)
            print('Devlin Desk:', url, '\nДля завершения нажмите Ctrl+C.', flush=True)
            while thread.is_alive():
                thread.join(1)
        else:
            try:
                import webview
            except ImportError:
                raise RuntimeError('Установите requirements-desktop.txt или запустите python run.py --browser')
            window = webview.create_window('Devlin Desk', url, width=1440, height=920, min_size=(1000, 700))
            if sys.platform == 'win32':
                from dashboard.desktop import DesktopTray, ASSETS
                tray = DesktopTray(window, app.state.store)
                window.events.closing += tray.closing
                try:
                    # Set up the tray after the native window exists.
                    webview.start(tray.start, icon=str(ASSETS / 'devlin-desk.ico'))
                finally:
                    tray.close()
            else:
                webview.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join()
        instance.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if sys.stderr:
            logging.exception('Application startup failed')
        if sys.platform == 'win32' and '--self-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Devlin Desk', 0x10)
        elif sys.stderr:
            print(str(error), file=sys.stderr)
        sys.exit(1)
