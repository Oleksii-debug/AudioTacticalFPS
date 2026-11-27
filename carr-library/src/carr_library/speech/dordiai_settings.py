"""Pure saved declarations for an explicitly enabled private local voice engine.

Reading or restoring these bytes never inspects or runs the declared runtime.
"""
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

FILENAME = 'dordiai-voices.json'
MAX_BYTES = 65536
DEFAULT_WORKER_SHA256 = '8fb4303098489bc8ad255696895a05f3daf731e60c4dbb8b271c16fdc31d121a'
_FIELDS = {'version', 'enabled', 'runtime_directory', 'python_executable', 'expected_worker_sha256'}


def default_settings():
    return dict(version=1, enabled=False, runtime_directory=None,
                python_executable=None, expected_worker_sha256=DEFAULT_WORKER_SHA256)


def _declared_path(value):
    if value is None:
        return
    if (type(value) is not str or not 0 < len(value) <= 32768
            or value.startswith(('//', '\\\\')) or '://' in value
            or any(ord(char) < 32 for char in value)
            or not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())
            or any(part in ('.', '..') for part in value.replace('\\', '/').split('/'))):
        raise ValueError('Choose an absolute local runtime path, not a network, device or relative path.')


def validate_settings(value):
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value['version']) is not int or value['version'] != 1
            or type(value['enabled']) is not bool):
        raise ValueError('Unsupported dordiai-voices settings declaration.')
    _declared_path(value['runtime_directory'])
    _declared_path(value['python_executable'])
    if (type(value['expected_worker_sha256']) is not str
            or re.fullmatch(r'[0-9a-f]{64}', value['expected_worker_sha256']) is None):
        raise ValueError('A reviewed lowercase SHA256 worker identity is required.')
    if value['enabled'] and value['runtime_directory'] is None:
        raise ValueError('Choose an installed kx47Runtime directory before enabling dordiai-voices.')
    return dict(value)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Repeated dordiai-voices settings field.')
        value[key] = item
    return value


def validate_settings_bytes(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Dordiai-voices settings exceed their byte budget.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid dordiai-voices settings data.') from exc
    return validate_settings(value)


def settings_bytes(value):
    value = validate_settings(value)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    if len(raw) > MAX_BYTES:
        raise ValueError('Dordiai-voices settings exceed their byte budget.')
    return raw


def restored_settings_bytes(raw):
    value = validate_settings_bytes(raw)
    value['enabled'] = False
    return settings_bytes(value)


def load_settings(root):
    from ..ingest.writer_guard import _private_root, _regular_if_present
    base = _private_root(root)
    path = base / FILENAME
    _regular_if_present(path)
    if not path.exists():
        return default_settings()
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ValueError('Dordiai-voices settings exceed their byte budget.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Dordiai-voices settings changed while reading.')
    return validate_settings_bytes(raw)


def save_settings(root, value):
    from ..ingest.writer_guard import exclusive_writer, _regular_if_present
    raw = settings_bytes(value)
    with exclusive_writer(root) as base:
        path = base / FILENAME
        _regular_if_present(path)
        fd, temporary = tempfile.mkstemp(prefix='.dordiai-settings-', suffix='.tmp', dir=base)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return validate_settings_bytes(raw)
