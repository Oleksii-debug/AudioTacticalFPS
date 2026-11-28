"""Typed backup/restore of passive language evidence, without runtime authority."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path

from ..durable_state import atomic_bytes, encode, local_path, read_json_snapshot, regular
from .evidence import EvidenceSnapshot, MAX_STATE_BYTES, RELATIVE_PATH, _check

PREFIX = 'state/language-qualification'
FILENAME = 'state/' + RELATIVE_PATH


def reserved(name):
    folded = name.casefold()
    return folded == PREFIX or folded.startswith(PREFIX + '/')


def limit(name):
    return MAX_STATE_BYTES if name == FILENAME else None


def ignored(name):
    parent, separator, leaf = name.rpartition('/')
    return (separator and parent == PREFIX
            and (leaf == '.language-evidence.lock' or leaf.startswith('.pending-')))


def validate_directory(name):
    if reserved(name) and name != PREFIX:
        raise ValueError('Invalid directory in the reserved language evidence namespace.')


def validate_record(name, value, *, cancelled=lambda: False):
    if name != FILENAME:
        raise ValueError('Unknown file in the reserved language evidence namespace.')
    return EvidenceSnapshot.from_dict(value, cancelled=cancelled)


def collection_budgets(entries, *, cancelled=lambda: False):
    seen = False
    for entry in entries:
        _check(cancelled)
        name = entry['filename']
        if not reserved(name):
            continue
        if (name != FILENAME or seen or type(entry['size_bytes']) is not int
                or not 1 <= entry['size_bytes'] <= MAX_STATE_BYTES):
            raise ValueError('Invalid bounded language evidence inventory.')
        seen = True
    _check(cancelled)


def validate_collection(copied_state_root, entries, *, cancelled=lambda: False):
    from ..backup_sidecars import _json, read_regular
    root = Path(copied_state_root)
    entries = tuple(entries)
    collection_budgets(
        ({**entry, 'filename': 'state/' + entry['filename']} for entry in entries),
        cancelled=cancelled)
    for entry in entries:
        _check(cancelled)
        name = 'state/' + entry['filename']
        if not reserved(name):
            continue
        path = local_path(root / entry['filename'], root=root, exists=True)
        raw = read_regular(path, MAX_STATE_BYTES)
        _check(cancelled)
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Copied language evidence differs from its verified manifest.')
        validate_record(name, _json(raw), cancelled=cancelled)
    _check(cancelled)


def prepare_restored_state(state_root, *, cancelled=lambda: False):
    """Keep all observations/reviews; no restored positive assessment is active."""
    _check(cancelled)
    root = local_path(state_root, exists=True)
    regular(root, directory=True)
    path = root / RELATIVE_PATH
    if not os.path.lexists(path):
        return False
    value, _ = read_json_snapshot(path, max_bytes=MAX_STATE_BYTES)
    snapshot = EvidenceSnapshot.from_dict(value, cancelled=cancelled)
    if snapshot.dormant_through == len(snapshot.reviews):
        return False
    restored = replace(snapshot, revision=snapshot.revision + 1,
                       dormant_through=len(snapshot.reviews))
    _check(cancelled)
    atomic_bytes(path, encode(restored.to_dict()), replace=True)
    return True


def prepare_restored_inventory(stage, entries, *, cancelled=lambda: False):
    from .. import backup_sidecars
    entries = tuple(entries)
    selected = [row for row in entries if row['filename'] == FILENAME]
    if not selected:
        return entries
    if len(selected) != 1:
        raise ValueError('The restored language evidence entry must be unique.')
    stage = local_path(stage, exists=True)
    path = stage / FILENAME
    raw = backup_sidecars.read_regular(path, MAX_STATE_BYTES)
    original = selected[0]
    if len(raw) != original['size_bytes'] or sha256(raw).hexdigest() != original['sha256']:
        raise ValueError('Staged language evidence differs from the verified backup.')
    validate_record(FILENAME, backup_sidecars._json(raw), cancelled=cancelled)
    if not prepare_restored_state(stage / 'state', cancelled=cancelled):
        return entries
    raw = backup_sidecars.read_regular(path, MAX_STATE_BYTES)
    replacement = dict(filename=FILENAME, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    _check(cancelled)
    return tuple(replacement if row['filename'] == FILENAME else row for row in entries)
