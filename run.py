"""Windows desktop entry point; --browser is also useful for headless development."""
import argparse
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


def main():
    parser = argparse.ArgumentParser(description='Devlin Desk — управление проектами')
    parser.add_argument('--browser', action='store_true', help='Открыть в браузере вместо окна приложения')
    parser.add_argument('--no-open', action='store_true', help='Не открывать окно, для серверных проверок')
    parser.add_argument('--port', type=int, default=0, help='Локальный порт (по умолчанию свободный)')
    parser.add_argument('--data-dir', type=Path, help='Каталог локальных данных')
    args = parser.parse_args()
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
    for _ in range(100):
        if not thread.is_alive():
            raise RuntimeError('Локальный сервер не запустился. См. app.log.')
        try:
            with urllib.request.urlopen(url + '/api/session', timeout=0.5):
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
            webview.create_window('Devlin Desk', url, width=1440, height=920, min_size=(1000, 700))
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
        logging.exception('Application startup failed')
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'Devlin Desk', 0x10)
        elif sys.stderr:
            print(str(error), file=sys.stderr)
        sys.exit(1)
