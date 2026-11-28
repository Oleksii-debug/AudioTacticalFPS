"""Pure, default-off connection declaration for an explicitly reviewed ASR runtime."""
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

FILENAME = 'transcription-runtime.json'
MAX_BYTES = 65536


def validate_settings_bytes(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('The transcription connection exceeds its byte allowance.')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate transcription connection field.')
            result[key] = value
        return result
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'version', 'enabled', 'manifest_path'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['enabled']) is not bool):
        raise ValueError('Invalid transcription connection declaration.')
    name = value['manifest_path']
    if (not isinstance(name, str) or len(name) > 8000 or '\x00' in name
            or name.startswith(('//', '\\\\')) or '://' in name):
        raise ValueError('Choose an explicit local runtime manifest path.')
    if name and not (PurePosixPath(name).is_absolute() or PureWindowsPath(name).is_absolute()):
        raise ValueError('The runtime manifest path must be absolute.')
    if value['enabled'] and not name:
        raise ValueError('A manifest is required before enabling transcription.')
    return value


def restored_settings_bytes(raw):
    value = validate_settings_bytes(raw)
    value['enabled'] = False
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')


def load_settings(root):
    from ..durable_state import local_path, regular
    path = local_path(Path(root) / FILENAME)
    if not path.exists():
        return {'version': 1, 'enabled': False, 'manifest_path': ''}
    if regular(path).st_size > MAX_BYTES:
        raise ValueError('The transcription connection exceeds its byte allowance.')
    with path.open('rb') as stream:
        return validate_settings_bytes(stream.read(MAX_BYTES + 1))


def save_settings(root, value):
    from ..durable_state import atomic_bytes, local_path
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    validate_settings_bytes(raw)
    path = local_path(Path(root) / FILENAME)
    atomic_bytes(path, raw, replace=True)


def disable(root):
    value = load_settings(root)
    value['enabled'] = False
    save_settings(root, value)
