"""Copied-data validation for inactive local schedule declarations.

No path recorded in a policy is resolved here. Saved timer/arming authority does
not exist. Domain jobs and snapshot payloads retain their existing validators.
"""
from hashlib import sha256
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .local_schedule_model import MAX_BYTES, MAX_RULES, validate_record as _record

PREFIX = 'state/local-schedules'
_FILE = re.compile(r'state/local-schedules/([0-9a-f]{32})\.json\Z')


def reserved(name):
    key = unicodedata.normalize('NFC', name).casefold()
    return key == PREFIX or key.startswith(PREFIX + '/')


def validate_directory(name):
    if reserved(name) and name != PREFIX:
        raise ValueError('Local schedules use one exact flat metadata directory.')


def ignored(name):
    return (PurePosixPath(name).parent.as_posix() == PREFIX
            and (PurePosixPath(name).name == '.catalog.lock'
                 or re.fullmatch(r'\.pending-[a-zA-Z0-9_]{8}', PurePosixPath(name).name) is not None))


def limit(name):
    if not reserved(name):
        return None
    if _FILE.fullmatch(name) is None:
        raise ValueError('Unknown or aliased local schedule metadata path.')
    return MAX_BYTES


def validate_record(name, value, *, db=None):
    match = _FILE.fullmatch(name)
    if match is None:
        raise ValueError('Invalid local schedule filename.')
    _record(value, identity_hint=match[1])
    if db is not None and value['kind'] == 'watch':
        # Archived scopes remain historical metadata; execution reauthorizes.
        if db.execute('SELECT 1 FROM workspace WHERE id=?', (value['workspace'],)).fetchone() is None:
            raise ValueError('A watched-folder policy has no matching copied library scope.')
    return value


def collection_budgets(entries, *, cancelled=lambda: False):
    count, total, seen = 0, 0, set()
    for entry in entries:
        if cancelled():
            raise InterruptedError('Local schedule backup cancelled.')
        name = entry['filename']
        if not reserved(name):
            continue
        maximum = limit(name)
        size = entry['size_bytes']
        if name in seen or type(size) is not int or not 1 <= size <= maximum:
            raise ValueError('Invalid local schedule size or duplicate metadata entry.')
        seen.add(name)
        count += 1
        total += size
        if count > MAX_RULES or total > MAX_RULES * MAX_BYTES:
            raise ValueError('Local schedules exceed their bounded snapshot allowance.')


def validate_collection(copied_state, entries, *, db=None, cancelled=lambda: False):
    from .backup_sidecars import _json, read_regular
    from .durable_state import local_path
    copied_state = Path(copied_state)
    entries = tuple(entries)
    collection_budgets(({**entry, 'filename': 'state/' + entry['filename']}
                        for entry in entries), cancelled=cancelled)
    for entry in entries:
        name = 'state/' + entry['filename']
        if not reserved(name):
            continue
        if cancelled():
            raise InterruptedError('Local schedule backup cancelled.')
        path = local_path(copied_state / entry['filename'], root=copied_state, exists=True)
        raw = read_regular(path, MAX_BYTES)
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Copied local schedule bytes differ from the backup manifest.')
        validate_record(name, _json(raw), db=db)
