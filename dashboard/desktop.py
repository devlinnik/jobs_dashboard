"""Desktop lifecycle: the window can hide while the monitor keeps running."""
import logging
import threading
from pathlib import Path

ASSETS = Path(__file__).parent / 'assets'


def tray_status(store):
    """Use persisted check results only; never run network checks from the tray."""
    if store.settings().get('pin_hash'):
        return 'Статусы защищены PIN', [('Откройте приложение и введите PIN', None)]
    rows = []
    errors = 0
    for project in store.projects():
        state = store.state(project['id'])
        status = state.get('status')
        if not project.get('enabled', True):
            label = 'мониторинг выключен'
        elif status == 'error':
            label = 'есть проблемы'
            errors += 1
        elif status == 'ok':
            label = 'проверки проходят'
        elif status == 'unconfigured':
            label = 'нет настроенных проверок'
        else:
            label = 'ожидает проверки'
        changes = len(state.get('git', {}).get('files', []))
        if changes:
            label += f', изменений: {changes}'
        rows.append((f"{project['name'][:48]} — {label}", project['id']))
    title = f'Проектов: {len(rows)} · проблем: {errors}' if rows else 'Нет проектов'
    return title, rows or [('Добавьте первый проект в приложении', None)]


class DesktopTray:
    def __init__(self, window, store, tray_module=None, image=None):
        if tray_module is None:
            import pystray as tray_module
        if image is None:
            from PIL import Image
            image = Image.open(ASSETS / 'devlin-desk.ico').convert('RGBA')
        self.window, self.store, self.tray = window, store, tray_module
        self.exiting = threading.Event()
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.notified = False
        self.icon = tray_module.Icon('DevlinDesk', image, 'Devlin Desk',
                                    menu=tray_module.Menu(self.menu_items))
        self.thread = None
        self.refresh_thread = None

    def menu_items(self):
        Item = self.tray.MenuItem
        yield Item('Открыть Devlin Desk', self.show, default=True)
        yield self.tray.Menu.SEPARATOR
        title, rows = tray_status(self.store)
        yield Item(title, None, enabled=False)
        yield Item('Статусы проектов', self.tray.Menu(*[
            Item(label, self.show if pid else None, enabled=bool(pid)) for label, pid in rows]))
        yield self.tray.Menu.SEPARATOR
        yield Item('Выход — остановить мониторинг', self.exit)

    def show(self, *args):
        if not self.exiting.is_set():
            self.window.show()
            self.window.restore()

    def closing(self):
        if self.exiting.is_set():
            return True
        # Never hide without a functioning tray: that would strand the user.
        if self.ready.is_set() and self.error is None and not self.stopped.is_set():
            self.window.hide()
            if not self.notified:
                self.notified = True
                try:
                    self.icon.notify('Проверки и Telegram продолжают работать. '
                                     'Для завершения выберите «Выход» в меню значка.', 'Devlin Desk работает в фоне')
                except Exception:
                    logging.warning('Tray notification unavailable')
            return False
        return True

    def exit(self, *args):
        self.exiting.set()
        try:
            self.window.destroy()
        except Exception:
            self.exiting.clear()
            logging.exception('Could not close desktop window')

    def _run(self):
        try:
            self.icon.run(self._setup)
        except Exception as error:
            self.error = error
            self.ready.set()
            logging.exception('Tray failed')
        finally:
            self.stopped.set()
            # If the tray disappears unexpectedly, recover the hidden window.
            if not self.exiting.is_set():
                try:
                    self.window.show()
                except Exception:
                    pass

    def _setup(self, icon):
        icon.visible = True
        self.ready.set()

    def _refresh(self):
        while not self.stopped.wait(10):
            try:
                title, _ = tray_status(self.store)
                self.icon.title = ('Devlin Desk · ' + title)[:127]
                self.icon.update_menu()
            except Exception:
                logging.exception('Could not refresh tray status')

    def start(self):
        self.thread = threading.Thread(target=self._run, name='devlin-tray', daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            self.close()
            self.window.show()
            raise RuntimeError('Не удалось создать значок в трее. См. app.log.')
        self.refresh_thread = threading.Thread(target=self._refresh, name='devlin-tray-status', daemon=True)
        self.refresh_thread.start()

    def close(self):
        self.exiting.set()
        self.stopped.set()
        self.icon.stop()
        for thread in (self.thread, self.refresh_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
