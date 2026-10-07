import json
import os
import sqlite3
import sys
import threading
import uuid
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
            CREATE TABLE IF NOT EXISTS ssh_profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS git_profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT, level TEXT, message TEXT, created_at TEXT);
        ''')
        self.db.commit()
        (self.directory / 'desk.sqlite3').chmod(0o600)
        self.migrate_ssh_profiles()

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
            if data.get('ssh_profile_id'):
                self.profile(data['ssh_profile_id'])
            if data.get('git_profile_id'):
                self.git_profile(data['git_profile_id'])
            self.db.execute('INSERT OR REPLACE INTO projects VALUES (?, ?)', (data['id'], json.dumps(data)))

    def profiles(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM ssh_profiles ORDER BY rowid DESC')]

    def profile(self, profile_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM ssh_profiles WHERE id=?', (profile_id,)).fetchone()
            if not row:
                raise KeyError(profile_id)
            return json.loads(row[0])

    def save_profile(self, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO ssh_profiles VALUES (?, ?)', (data['id'], json.dumps(data)))

    def delete_profile(self, profile_id):
        with self.lock, self.db:
            self.profile(profile_id)
            if any(p.get('ssh_profile_id') == profile_id for p in self.projects()):
                return False
            self.db.execute('DELETE FROM ssh_profiles WHERE id=?', (profile_id,))
            return True

    def resolve_project(self, project):
        with self.lock:
            project = project.copy()
            if project.get('git_profile_id'):
                project['git_profile_name'] = self.git_profile(project['git_profile_id'])['name']
            if not project.get('ssh_profile_id'):
                return project.copy()
            profile = self.profile(project['ssh_profile_id'])
            fields = ('ssh_host', 'ssh_port', 'ssh_user', 'ssh_key', 'ssh_password_enc', 'ssh_fingerprint')
            return project | {k: profile.get(k, '') for k in fields} | {'ssh_profile_name': profile['name']}

    def git_profiles(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM git_profiles ORDER BY rowid DESC')]

    def git_profile(self, profile_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM git_profiles WHERE id=?', (profile_id,)).fetchone()
            if not row:
                raise KeyError(profile_id)
            return json.loads(row[0])

    def save_git_profile(self, data):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO git_profiles VALUES (?, ?)', (data['id'], json.dumps(data)))

    def delete_git_profile(self, profile_id):
        with self.lock, self.db:
            self.git_profile(profile_id)
            if any(p.get('git_profile_id') == profile_id for p in self.projects()):
                return False
            self.db.execute('DELETE FROM git_profiles WHERE id=?', (profile_id,))
            return True

    def migrate_ssh_profiles(self):
        """Move legacy credentials once; share only identical, equally trusted connections."""
        with self.lock, self.db:
            legacy = [p for p in self.projects() if not p.get('ssh_profile_id') and p.get('ssh_host')]
            if not legacy:
                return
            backup_dir = self.directory / 'migration-backups'
            backup_dir.mkdir(exist_ok=True, mode=0o700)
            backup_path = backup_dir / ('before-ssh-profiles-' + uuid.uuid4().hex + '.sqlite3')
            backup = sqlite3.connect(backup_path)
            try:
                self.db.backup(backup)
            finally:
                backup.close()
            backup_path.chmod(0o600)
            def identity(p):
                return (p.get('ssh_host'), p.get('ssh_port', 22), p.get('ssh_user'), p.get('ssh_key', ''),
                        self.decrypt(p.get('ssh_password_enc', '')), p.get('ssh_fingerprint', ''))
            known = {identity(p): p['id'] for p in self.profiles()}
            for project in legacy:
                signature = identity(project)
                profile_id = known.get(signature)
                if not profile_id:
                    profile_id = uuid.uuid4().hex
                    profile = {k: project.get(k, '') for k in ('ssh_host','ssh_user','ssh_key','ssh_password_enc','ssh_fingerprint')}
                    profile.update(id=profile_id, name=f"{project['name']} · {project['ssh_user']}@{project['ssh_host']}"[:80],
                                   ssh_port=project.get('ssh_port', 22), created_at=now())
                    # Use this transaction directly so migration is all-or-nothing.
                    self.db.execute('INSERT INTO ssh_profiles VALUES (?, ?)', (profile_id, json.dumps(profile)))
                    known[signature] = profile_id
                project['ssh_profile_id'] = profile_id
                for key in ('ssh_host','ssh_user','ssh_key','ssh_password_enc','ssh_fingerprint'):
                    project[key] = ''
                self.db.execute('UPDATE projects SET data=? WHERE id=?', (json.dumps(project), project['id']))

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
