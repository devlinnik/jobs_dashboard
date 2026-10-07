import asyncio
import threading
from types import SimpleNamespace

import pytest

from dashboard.desktop import DesktopTray, tray_status
from dashboard.models import ProjectInput
from dashboard.monitor import Monitor
from dashboard.store import Store


class FakeWindow:
    def __init__(self):
        self.visible = True
        self.destroyed = False
        self.restored = False

    def hide(self):
        self.visible = False

    def show(self):
        self.visible = True

    def restore(self):
        self.restored = True

    def destroy(self):
        self.destroyed = True


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeItem:
    def __init__(self, text, action, **kwargs):
        self.text, self.action, self.options = text, action, kwargs


class FakeIcon:
    def __init__(self, *args, **kwargs):
        self.finished = threading.Event()
        self.menu = kwargs.get('menu')
        self.visible = False
        self.notifications = []
        self.refreshes = 0

    def run(self, setup):
        setup(self)
        self.finished.wait(5)

    def stop(self):
        self.finished.set()

    def update_menu(self):
        self.refreshes += 1

    def notify(self, text, title):
        self.notifications.append((text, title))


FAKE_TRAY = SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeItem)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    yield store
    store.close()


def controller(store):
    return DesktopTray(FakeWindow(), store, tray_module=FAKE_TRAY, image=object())


def test_window_close_hides_monitor_continues_and_tray_reopens(store):
    project = ProjectInput(name='Example', url='https://example.test', notify=False).model_dump() | {'id': 'one'}
    store.save_project(project)
    tray = controller(store)
    tray.start()
    try:
        assert tray.closing() is False
        assert not tray.window.visible and not tray.window.destroyed
        assert not tray.exiting.is_set()
        # A real monitor check still persists state with the window hidden.
        async def check():
            monitor = Monitor(store, {})
            async def website(project):
                return {'ok': True, 'code': 200, 'error': ''}
            monitor.website_check = website
            try:
                return await monitor.check('one')
            finally:
                await monitor.close()
        assert asyncio.run(check())['status'] == 'ok'
        assert store.state('one')['web']['code'] == 200
        assert 'проверки проходят' in tray_status(store)[1][0][0]
        tray.show()
        assert tray.window.visible and tray.window.restored
        assert tray.closing() is False
        assert len(tray.icon.notifications) == 1
        tray.exit()
        assert tray.exiting.is_set() and tray.window.destroyed
        assert tray.closing() is True
    finally:
        tray.close()
    assert not tray.thread.is_alive() and not tray.refresh_thread.is_alive()


def test_no_tray_or_stopped_tray_never_strands_hidden_window(store):
    tray = controller(store)
    assert tray.closing() is True
    tray.ready.set()
    tray.stopped.set()
    assert tray.closing() is True and tray.window.visible


def test_tray_failure_returns_hidden_window(store):
    tray = controller(store)
    tray.window.hide()
    def failed(setup):
        raise RuntimeError('notification area unavailable')
    tray.icon.run = failed
    with pytest.raises(RuntimeError, match='значок'):
        tray.start()
    assert tray.window.visible
    assert tray.closing() is True


def test_statuses_include_paused_pending_errors_and_git_changes(store):
    for pid, enabled, state in [
        ('ok', True, {'status': 'ok', 'git': {'files': [{'path': 'index.html'}]}}),
        ('broken', True, {'status': 'error'}),
        ('paused', False, {'status': 'error'}),
        ('pending', True, {}),
    ]:
        store.save_project({'id': pid, 'name': pid, 'enabled': enabled})
        store.save_state(pid, state)
    title, rows = tray_status(store)
    assert title == 'Проектов: 4 · проблем: 1'
    labels = dict((pid, label) for label, pid in rows)
    assert 'изменений: 1' in labels['ok']
    assert 'есть проблемы' in labels['broken']
    assert 'выключен' in labels['paused']
    assert 'ожидает' in labels['pending']


def test_pin_hides_project_names_and_states_in_menu_and_tooltip(store):
    store.save_project({'id': 'private', 'name': 'Private client', 'enabled': True})
    store.save_state('private', {'status': 'error'})
    store.save_settings({'pin_hash': 'configured'})
    title, rows = tray_status(store)
    assert 'PIN' in title
    assert all(pid is None and 'Private' not in text for text, pid in rows)
    menu = list(controller(store).menu_items())
    assert menu[0].options['default']
    assert menu[-1].text == 'Выход — остановить мониторинг'


def test_empty_projects_produce_disabled_placeholder(store):
    title, rows = tray_status(store)
    assert title == 'Нет проектов' and rows[0][1] is None
