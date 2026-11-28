"""Restore passive voice declarations without reauthorizing external assets."""
from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path

from ..durable_state import atomic_json, local_path, read_json, regular
from ..runtime_settings import _normalize_settings

_ENGINE = 'state/audio/engines.json'


def prepare_restored_state(state_root, *, cancelled=lambda: False):
    """Keep copied consent/revocations, but require a new explicit local review.

    Only the already-copied settings file is read. No external runtime, model,
    reference, manifest or ownership path is opened during restore.
    """
    if cancelled():
        raise InterruptedError('Voice declaration restoration cancelled.')
    root = local_path(state_root, exists=True)
    regular(root, directory=True)
    path = root / 'audio' / 'engines.json'
    if not os.path.lexists(path):
        return False
    path = local_path(path, exists=True)
    regular(path)
    value = _normalize_settings(read_json(path))
    row = value['dordiai']
    if row is None or 'management' not in row:
        return False
    value = deepcopy(value)
    value['dordiai']['enabled'] = False
    value['dordiai']['management']['restored'] = True
    if cancelled():
        raise InterruptedError('Voice declaration restoration cancelled.')
    atomic_json(path, value, replace=True)
    return True


def prepare_restored_inventory(stage, entries, *, cancelled=lambda: False):
    """Refresh only the verified staged settings entry before its publication."""
    from .. import backup_sidecars
    rows = tuple(entries)
    selected = [row for row in rows if row['filename'] == _ENGINE]
    if not selected:
        return rows
    if len(selected) != 1:
        raise ValueError('The restored engine settings entry must be unique.')
    stage = local_path(stage, exists=True)
    path = stage / _ENGINE
    raw = backup_sidecars.read_regular(path, backup_sidecars.sidecar_limit(_ENGINE))
    original = selected[0]
    if (len(raw) != original['size_bytes']
            or sha256(raw).hexdigest() != original['sha256']):
        raise ValueError('The staged engine settings differ from their verified backup.')
    if not prepare_restored_state(stage / 'state', cancelled=cancelled):
        return rows
    raw = backup_sidecars.read_regular(path, backup_sidecars.sidecar_limit(_ENGINE))
    replacement = dict(filename=_ENGINE, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    if cancelled():
        raise InterruptedError('Voice declaration restoration cancelled.')
    return tuple(replacement if row['filename'] == _ENGINE else row for row in rows)
