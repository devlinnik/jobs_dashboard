import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat()


def data_directory():
    if os.getenv('DEVLIN_DATA_DIR'):
        return Path(os.environ['DEVLIN_DATA_DIR'])
    if sys.platform == 'win32':
        return Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'DevlinDesk'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/DevlinDesk'
    return Path(os.getenv('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'devlin-desk'


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or data_directory())
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self.directory / 'vault.key'
        if not key.exists():
            try:
                with key.open('xb') as f:
                    f.write(Fernet.generate_key())
                key.chmod(0o600)
            except FileExistsError:
                pass
        self.vault = Fernet(key.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / 'desk.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT, level TEXT, message TEXT, created_at TEXT);
        ''')
        self.db.commit()
        (self.directory / 'desk.sqlite3').chmod(0o600)

    def encrypt(self, value):
        return self.vault.encrypt(value.encode()).decode() if value else ''

    def decrypt(self, value):
        return self.vault.decrypt(value.encode()).decode() if value else ''

    def projects(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM projects ORDER BY rowid DESC')]

    def project(self, project_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM projects WHERE id=?', (project_id,)).fetchone()
            if not row:
                raise KeyError(project_id)
            return json.loads(row[0])

    def save_project(self, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO projects VALUES (?, ?)', (data['id'], json.dumps(data)))

    def delete_project(self, project_id):
        with self.lock, self.db:
            self.db.execute('DELETE FROM projects WHERE id=?', (project_id,))
            self.db.execute('DELETE FROM state WHERE id=?', (project_id,))

    def state(self, project_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM state WHERE id=?', (project_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_state(self, project_id, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (project_id, json.dumps(data)))

    def settings(self):
        defaults = {'telegram_token': '', 'telegram_chat': '', 'pin_hash': '',
                    'git_name': 'Devlin Desk', 'git_email': 'desk@localhost', 'notify_recovery': True}
        with self.lock:
            row = self.db.execute('SELECT data FROM settings WHERE id=1').fetchone()
            return defaults | (json.loads(row[0]) if row else {})

    def save_settings(self, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)', (json.dumps(data),))

    def event(self, project_id, level, message):
        with self.lock, self.db:
            self.db.execute('INSERT INTO events(project_id,level,message,created_at) VALUES (?,?,?,?)',
                            (project_id, level, message[:3000], now()))
            self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)')

    def events(self, project_id=None):
        query = 'SELECT id,project_id,level,message,created_at FROM events'
        args = ()
        if project_id:
            query += ' WHERE project_id=?'
            args = (project_id,)
        with self.lock:
            rows = self.db.execute(query + ' ORDER BY id DESC LIMIT 150', args).fetchall()
        return [dict(zip(['id','project_id','level','message','created_at'], r)) for r in rows]

    def close(self):
        self.db.close()
