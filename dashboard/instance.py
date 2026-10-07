"""Prevent two desktop instances from running monitors on the same database."""
import os


class InstanceLock:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / 'instance.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b'1'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('Devlin Desk уже запущен с этим каталогом данных. Откройте окно через значок в системном трее; для завершения выберите «Выход» в его меню.') from exc

    def close(self):
        if not self.file.closed:
            self.file.close()
