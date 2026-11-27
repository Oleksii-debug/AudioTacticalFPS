"""Passive backup closure for saved numeric effect presets.

Only declared files in the copied state are read. EffectPreset validation
checks names and numeric settings; it never configures a codec or executes it.
"""
from hashlib import sha256
from pathlib import Path
import re

from .durable_state import local_path

PREFIX = 'state/studio/effect-presets'
MAX_RECORD_BYTES = 64 * 1024
MAX_RECORDS = 256
MAX_COLLECTION_BYTES = 16 * 1024 * 1024
_RECORD = re.compile(re.escape(PREFIX) + r'/([0-9a-f]{32})\.json\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Effect-preset backup validation cancelled.')


def reserved(name):
    """Recognize this reserved namespace, including portable case aliases."""
    folded = name.casefold()
    return folded == PREFIX or folded.startswith(PREFIX + '/')


def limit(name):
    return MAX_RECORD_BYTES if _RECORD.fullmatch(name) else None


def validate_directory(name):
    # The root may legitimately be empty after opening the preset editor.
    # Preset records are flat: arbitrary subdirectories cannot be omitted.
    if reserved(name) and name != PREFIX:
        raise ValueError('Invalid directory in the reserved effect-preset namespace.')


def ignored(name):
    """Reuse atomic-writer temporaries only at this exact producer root."""
    parent, separator, leaf = name.rpartition('/')
    return (separator and parent == PREFIX
            and (leaf == '.effect-presets.lock' or leaf.startswith('.pending-')))


def validate_record(name, value):
    from .audio.effect_presets import EffectPreset
    match = _RECORD.fullmatch(name)
    if match is None:
        raise ValueError('Unknown file in the reserved effect-preset namespace.')
    preset = EffectPreset.from_dict(value)
    if preset.id != match[1]:
        raise ValueError('Effect preset identity differs from its exact filename.')


def collection_budgets(entries, *, cancelled=lambda: False):
    names, total = set(), 0
    for entry in entries:
        _check(cancelled)
        name = entry['filename']
        if not reserved(name):
            continue
        size = entry['size_bytes']
        if (_RECORD.fullmatch(name) is None or type(size) is not int
                or not 1 <= size <= MAX_RECORD_BYTES or name in names):
            raise ValueError('Invalid bounded effect-preset inventory.')
        names.add(name)
        total += size
        if len(names) > MAX_RECORDS or total > MAX_COLLECTION_BYTES:
            raise ValueError('Effect presets exceed their collection budget.')
    _check(cancelled)


def validate_collection(copied_state_root, entries, *, cancelled=lambda: False):
    """Validate actual copied bytes against the manifest, without live authority."""
    from .backup_sidecars import _json, read_regular
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
        raw = read_regular(path, MAX_RECORD_BYTES)
        _check(cancelled)
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Copied effect preset differs from its verified manifest.')
        validate_record(name, _json(raw))
    _check(cancelled)
