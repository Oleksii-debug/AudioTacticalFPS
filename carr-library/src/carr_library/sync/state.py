"""Passive transfer history; it contains no credentials or executable authority."""
import json
import re

NAME = 'portable-sync-state.json'
MAX_BYTES = 1024 * 1024
_TOKEN = re.compile(r'[0-9a-f]{64}\Z')
_FAMILY = re.compile(r'[0-9a-f]{32}\Z')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate portable history field.')
        value[key] = item
    return value


def validate_state(value):
    if (type(value) is not dict or set(value) != {'kind', 'version', 'family_id', 'vaults'}
            or value['kind'] != 'carr-portable-state'
            or type(value['version']) is not int or value['version'] != 1
            or type(value['family_id']) is not str or not _FAMILY.fullmatch(value['family_id'])
            or type(value['vaults']) is not dict or len(value['vaults']) > 4096):
        raise ValueError('Invalid passive portable-snapshot history.')
    for key, record in value['vaults'].items():
        if (type(key) is not str or not _TOKEN.fullmatch(key)
                or type(record) is not dict or set(record) != {'head', 'label'}
                or type(record['head']) is not str or not _TOKEN.fullmatch(record['head'])):
            raise ValueError('Invalid portable-snapshot history entry.')
        label = record['label']
        if (type(label) is not str or not label.strip() or len(label) > 200
                or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF for c in label)):
            raise ValueError('Invalid portable-snapshot history label.')
    return value


def validate_portable_state(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Portable history exceeds its bounded record size.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON number.')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid portable history JSON.') from exc
    return validate_state(value)
