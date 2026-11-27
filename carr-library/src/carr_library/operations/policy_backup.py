"""Portable cooperative allowances; runtime leases and engine paths stay local."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time

from ._state import local_path, regular
from .models import ResourceLimits, ResourceStateError

FILENAME = 'state/operations/resources.json'
MAX_BYTES = 32768
MAX_SNAPSHOT_BYTES = MAX_BYTES
MAX_FILES = 1
_TRANSIENT = re.compile(r'\.(?:pending|checkpoint)-[A-Za-z0-9_-]{1,100}\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Resource policy snapshot cancelled.')


def _unique(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('Duplicate resource policy field.')
        value[key] = child
    return value


def _constant(_):
    raise ValueError('Resource allowances must be finite JSON values.')


def decode_policy(raw):
    if not isinstance(raw, (str, bytes)):
        raise ValueError('Resource policy must be JSON text.')
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Resource policy exceeds its byte budget.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError('Invalid resource policy JSON.') from exc
    if type(value) is not dict or set(value) != {'schema', 'limits'} or value['schema'] != 'carr-resource-policy/1':
        raise ValueError('Unsupported portable resource policy.')
    return ResourceLimits.from_dict(value['limits'])


def is_state_path(value):
    return value == FILENAME


def state_limit(value):
    if not is_state_path(value):
        raise ValueError('Unsupported resource policy path.')
    return MAX_BYTES


def state_paths(root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    directory = root / 'state' / 'operations'
    _check(cancelled)
    if not os.path.lexists(directory):
        return ()
    local_path(directory, root=root, exists=True)
    regular(directory, directory=True)
    paths = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            _check(cancelled)
            if index >= 256:
                raise ValueError('Resource policy directory exceeds its entry budget.')
            path = local_path(directory / entry.name, root=root, exists=True)
            info = regular(path)
            if entry.name == 'resources.json':
                if not 0 < info.st_size <= MAX_BYTES:
                    raise ValueError('Resource policy exceeds its byte budget.')
                paths.append(path)
            elif not _TRANSIENT.fullmatch(entry.name):
                raise ValueError('Unregistered durable state in the resource policy directory.')
    return tuple(paths)


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Pure data validation: no coordinator, model, provider or native lock."""
    from ..audio.jobs import verified_reader
    for path in state_paths(root, cancelled=cancelled):
        with verified_reader(path) as stream:
            raw = stream.read(MAX_BYTES + 1)
        decode_policy(raw)
        _check(cancelled)


@contextmanager
def snapshot_guard(root, *, cancelled=lambda: False):
    """The caller holds the library writer; reuse the broker's short gate."""
    root = local_path(root, exists=True)
    paths = state_paths(root, cancelled=cancelled)
    if not paths:
        yield
        return
    # Snapshot preparation may establish transient coordinator files. Verification
    # never calls this guard. No lease is adopted, revoked or copied.
    from .broker import ResourceBroker
    broker = ResourceBroker.for_app(root, cancelled=cancelled)
    with broker._gate(time.monotonic(), cancelled):
        if state_paths(root, cancelled=cancelled) != paths:
            raise ResourceStateError('Resource allowances changed before snapshot.')
        yield
