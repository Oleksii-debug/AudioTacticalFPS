"""Bounded backup of typed assistant checkpoints; never follow archived paths."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from ..ingest.bundle import _plain
from ..ingest.restore import RestoreError
from ..ingest.writer_guard import _regular_if_present
from ..reader.reading_state import _file_identity
from .backup_state import MAX_BYTES as ITEM_BYTES, MAX_REVIEW_BYTES, validate_state_bytes, restore_review_bytes

DIRECTORY = 'state/assistant'
MAX_FILES = 100_000
MAX_BYTES = 512 * 1024 * 1024
_NAME = re.compile(r'state/assistant/([0-9a-f]{32})(?:\.(control|restore-review))?\.json\Z')
_VOLATILE = re.compile(r'(?:[0-9a-f]{32}\.lock|\.[0-9a-f]{32}(?:\.control)?\.json\.[0-9a-f]{32}\.tmp)\Z')


def sidecar_limit(name):
    match = _NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        raise RestoreError('Unsupported assistant backup sidecar.')
    return MAX_REVIEW_BYTES if match[2] == 'restore-review' else 1024 if match[2] == 'control' else ITEM_BYTES


def is_sidecar(name):
    return isinstance(name, str) and _NAME.fullmatch(name) is not None


def read_regular(path, maximum):
    # Reuse the library's identity-checked bounded local-file reader.
    from ..library_backup import _optional_sidecar
    raw = _optional_sidecar(Path(path), maximum)
    if raw is None:
        raise FileNotFoundError(path)
    return raw


def _capture(root, *, live):
    root = Path(root)
    folder = root / DIRECTORY
    if not os.path.lexists(folder):
        return {}, ()
    parents = (root / 'state', folder)
    identities = tuple(_file_identity(_plain(p, directory=True)) for p in parents)
    files = {}
    visited = 0
    for path in folder.iterdir():
        visited += 1
        if visited > MAX_FILES * 3 + 16:
            raise RestoreError('Assistant checkpoint inventory exceeds its file budget.')
        _regular_if_present(path)
        info = path.lstat()
        if live and _VOLATILE.fullmatch(path.name):
            continue
        name = DIRECTORY + '/' + path.name
        sidecar_limit(name)
        if len(files) >= MAX_FILES:
            raise RestoreError('Assistant checkpoint inventory exceeds its file budget.')
        files[name] = _file_identity(info)
    return files, identities


def validate_pairs(sidecars):
    selected = {name: raw for name, raw in sidecars.items() if is_sidecar(name)}
    if len(selected) > MAX_FILES or sum(map(len, selected.values())) > MAX_BYTES:
        raise RestoreError('Assistant checkpoint snapshot exceeds its resource budget.')
    for name, raw in selected.items():
        match = _NAME.fullmatch(name)
        if not match[2]:
            continue
        checkpoint = selected.get(DIRECTORY + '/' + match[1] + '.json')
        if checkpoint is None:
            raise RestoreError('Assistant state has no matching job checkpoint.')
        if match[2] == 'restore-review':
            # Typed validation rejects duplicate fields and unknown marker keys.
            validate_state_bytes(name, raw)
            if json.loads(raw)['checkpoint_sha256'] != sha256(checkpoint).hexdigest():
                raise RestoreError('Assistant restore review marker does not match its checkpoint.')


def snapshot_files(root, *, live=False):
    before = _capture(root, live=live)
    files, total = {}, 0
    for name in sorted(before[0]):
        raw = read_regular(Path(root) / name, sidecar_limit(name))
        total += len(raw)
        if total > MAX_BYTES:
            raise RestoreError('Assistant checkpoint snapshot exceeds its byte budget.')
        validate_state_bytes(name, raw)
        files[name] = raw
    if _capture(root, live=live) != before:
        raise RestoreError('Assistant state changed during backup; pause its producer and retry.')
    validate_pairs(files)
    return files


def add_restore_reviews(sidecars):
    """Retain archived bytes and require new permissions for every restored job."""
    result = dict(sidecars)
    validate_pairs(result)
    for name, raw in tuple(sorted(result.items())):
        match = _NAME.fullmatch(name)
        if match is None or match[2]:
            continue
        validate_state_bytes(name, raw)
        marker_name = DIRECTORY + '/' + match[1] + '.restore-review.json'
        if marker_name not in result:
            result[marker_name] = restore_review_bytes(name, raw)
    validate_pairs(result)
    return result
