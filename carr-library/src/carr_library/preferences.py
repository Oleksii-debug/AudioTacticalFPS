"""Small local desktop preference: the last explicitly opened data folder."""
import json
import os
from pathlib import Path
from uuid import uuid4


def preference_file():
    from platformdirs import user_config_path
    return user_config_path('Universal Research Library', appauthor=False, roaming=False) / 'settings.json'


def selected_directory(settings=None):
    path = Path(settings) if settings is not None else preference_file()
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000:
        raise ValueError('The desktop settings file is invalid.')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or set(value) != {'version', 'active_data_dir'} or value['version'] != 1:
        raise ValueError('The desktop settings format is unsupported.')
    directory = value['active_data_dir']
    if not isinstance(directory, str) or not directory or len(directory) > 32767 or '\x00' in directory:
        raise ValueError('The saved library location is invalid.')
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('The saved library location must be absolute.')
    return directory


def remember_directory(directory, settings=None):
    directory = Path(directory).resolve(strict=True)
    if not (directory / 'library.sqlite').is_file() or (directory / '.restore-incomplete').exists():
        raise ValueError('Open a complete library before remembering its location.')
    path = Path(settings) if settings is not None else preference_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('The desktop settings must not be a link.')
    temporary = path.with_name('settings-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump({'version': 1, 'active_data_dir': str(directory)}, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
