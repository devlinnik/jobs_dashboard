"""Release identity shipped alongside the executable and its frontend assets."""
import json
import sys
from pathlib import Path


def app_info():
    directory = Path(__file__).parent
    info = json.loads((directory / 'version.json').read_text(encoding='utf-8'))
    build_file = directory / 'build-info.json'
    if build_file.exists():
        build = json.loads(build_file.read_text(encoding='utf-8'))
        info.update({key: build[key] for key in ('commit', 'built_at', 'dirty') if key in build})
    info['packaged'] = bool(getattr(sys, 'frozen', False))
    info['executable'] = sys.executable
    return info
