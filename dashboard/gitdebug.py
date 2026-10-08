"""Safe command telemetry: no raw commands, arguments, stdin or server output."""
import re
import time
import logging
from contextlib import contextmanager
from .remote import command_failure


def command_label(command):
    for pattern, label in (
        (r'\bls-remote\b', 'git ls-remote'),
        (r'\bfetch\b', 'git fetch'),
        (r'\bpush\b', 'git push'),
        (r'\bpull\b', 'git pull'),
        (r'\bhash-object\b', 'git hash-object'),
        (r'\badd -A\b', 'git add'),
        (r'git\s+(?:-[^;]*?\s+)?status\b', 'git status'),
        (r'\bls-files\b', 'git ls-files'),
        (r'git\s+rev-parse\b', 'git rev-parse'),
        (r'git\s+config\b', 'git config'),
        (r'\bfind\s+\.git\b', 'find .git'),
        (r'\bcommit\b', 'git commit'),
        (r'\bhead -c\b', 'Чтение метаданных'),
        (r'\bpwd -P\b', 'Проверка папки'),
        (r'\bid -u\b', 'Проверка пользователя'),
    ):
        if re.search(pattern, command):
            return label
    return 'Проверка или изменение на сервере'


@contextmanager
def trace_commands(conn, sink, operation, trace_id):
    original = conn.run
    sequence = 0
    def traced(command, **options):
        nonlocal sequence
        sequence += 1
        start = time.monotonic()
        record = {'trace_id': trace_id, 'operation': operation, 'sequence': sequence,
                  'command': command_label(command), 'limit_bytes': options.get('max_output', 2_000_000),
                  'timeout_seconds': options.get('timeout', 35), 'exit_code': None,
                  'stdout_bytes': 0, 'stderr_bytes': 0, 'result': 'exception'}
        conn.last_command_metrics = {}
        try:
            code, out, err = original(command, **options)
            record.update(exit_code=code, stdout_bytes=len(out), stderr_bytes=len(err), result='completed')
            if code:
                record['reason'] = str(command_failure(code, err + b'\n' + out))
            return code, out, err
        except Exception as exc:
            record.update(getattr(conn, 'last_command_metrics', {}))
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record['duration_ms'] = round((time.monotonic() - start)*1000)
            try:
                sink(record)
            except Exception:
                # Telemetry failure must never turn a completed mutation into a retry.
                logging.getLogger(__name__).warning('Git telemetry could not be saved')
    conn.run = traced
    previous_active = getattr(conn, 'git_trace_active', False)
    conn.git_trace_active = True
    try:
        yield
    finally:
        conn.run = original
        conn.git_trace_active = previous_active
