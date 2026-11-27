"""Read released native-menu preferences through the current typed stores.

Original JSON stays byte-for-byte in state/ui and is a registered backup
sidecar. Menu paths never grant command authority: activation additionally
requires the current registered command and its unchanged original shortcut.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from .backup_files import optional_present
import re

from .backup_files import local_path, regular

PROGRESS = 'state/ui/progress.json'
SHORTCUTS = 'state/ui/shortcuts.json'
MAX_PROGRESS_BYTES = 4096
MAX_SHORTCUT_BYTES = 128 * 1024
_RESERVED_ORIGINALS = {'Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Z', 'Ctrl+Y',
    'Ctrl+Shift+A', 'Ctrl+Shift+C', 'Ctrl+Shift+V', 'Ctrl+Shift+X', 'Ctrl+Shift+Z',
    'Ctrl+F', 'Ctrl+H', 'Ctrl+W', 'F6', 'F10', 'Shift+F10', 'Alt+F4', 'Ctrl+Alt+N'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate canonical preference field.')
        result[key] = value
    return result


def _decode(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise ValueError('Canonical preferences are empty or exceed their size limit.')
    def invalid_constant(_):
        raise ValueError('Non-finite canonical preference value.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=invalid_constant)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Canonical preferences must be bounded UTF-8 JSON.') from exc


def decode_progress(raw):
    value = _decode(raw, MAX_PROGRESS_BYTES)
    fields = {'mode', 'interval_seconds', 'volume', 'quiet', 'background', 'mute_during_audio'}
    if (type(value) is not dict or set(value) != {'schema', 'preferences'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['preferences']) is not dict or set(value['preferences']) != fields):
        raise ValueError('Unsupported canonical progress preference schema.')
    prefs = value['preferences']
    if prefs['mode'] not in ('off', 'beep', 'speech', 'both'):
        raise ValueError('Invalid canonical progress mode.')
    for key, minimum, maximum in (('interval_seconds', 1, 120), ('volume', 0, 1)):
        number = prefs[key]
        if type(number) not in (int, float) or not minimum <= number <= maximum or not math.isfinite(number):
            raise ValueError('Invalid canonical progress ' + key + '.')
    if any(type(prefs[key]) is not bool for key in ('quiet', 'background', 'mute_during_audio')):
        raise ValueError('Canonical progress switches must be boolean.')
    return dict(prefs)


def _shortcut(value):
    # The released format permits F1–F24 and some reserved *original* defaults.
    # Current CommandRegistry validation still decides whether to activate it.
    if type(value) is not str or len(value) > 80 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid canonical shortcut.')
    if not value.strip():
        return ''
    parts = [part.strip() for part in value.strip().split('+')]
    modifiers = []
    for part in parts[:-1]:
        item = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}.get(part.casefold())
        if item is None or item in modifiers:
            raise ValueError('Invalid canonical shortcut modifiers.')
        modifiers.append(item)
    key = parts[-1].upper()
    if not re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])', key):
        raise ValueError('Unsupported canonical shortcut key.')
    if len(key) == 1 and 'Ctrl' not in modifiers:
        raise ValueError('Canonical letter shortcuts require Ctrl.')
    return '+'.join([*(item for item in ('Ctrl', 'Alt', 'Shift') if item in modifiers), key])


def decode_shortcuts(raw):
    value = _decode(raw, MAX_SHORTCUT_BYTES)
    if (type(value) is not dict or set(value) != {'schema', 'overrides', 'originals'}
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['overrides']) is not dict or type(value['originals']) is not dict
            or set(value['overrides']) != set(value['originals']) or len(value['overrides']) > 512):
        raise ValueError('Unsupported canonical shortcut schema.')
    assigned = set()
    for name in value['overrides']:
        if (type(name) is not str or not 0 < len(name) <= 4096 or name != name.casefold()
                or any(not part or part != part.strip() for part in name.split('/'))
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('Invalid canonical menu action path.')
        original = _shortcut(value['originals'][name])
        override = _shortcut(value['overrides'][name])
        if override in _RESERVED_ORIGINALS and override != original:
            raise ValueError('Canonical shortcut overrides cannot claim a reserved key.')
        if override and override in assigned:
            raise ValueError('Canonical shortcut overrides conflict.')
        if override:
            assigned.add(override)
    return value


VALIDATORS = {PROGRESS: (MAX_PROGRESS_BYTES, decode_progress),
              SHORTCUTS: (MAX_SHORTCUT_BYTES, decode_shortcuts)}


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class PreferenceSnapshot:
    name: str
    raw: bytes
    identity: tuple
    root_identity: tuple


def read_snapshot(root, name):
    maximum, validate = VALIDATORS[name]
    root = local_path(root, exists=True)
    info = regular(root, directory=True)
    root_identity = info.st_dev, info.st_ino
    path = local_path(root / name, root=root)
    if not optional_present(path):
        return None
    before = _identity(regular(path))
    if not 0 < before[4] <= maximum:
        raise ValueError('Canonical preferences exceed their size limit.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError('Canonical preferences changed while opening.')
        raw = stream.read(maximum + 1)
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError('Canonical preferences changed while reading.')
    if len(raw) != before[4] or _identity(regular(path)) != before:
        raise ValueError('Canonical preferences changed while reading.')
    validate(raw)
    return PreferenceSnapshot(name, raw, before, root_identity)


@contextmanager
def snapshot_guard(root, *, expected=None):
    """Reuse the released .progress/.shortcuts native locks under the root writer."""
    from filelock import FileLock, Timeout
    root = local_path(root, exists=True)
    directory = local_path(root / 'state' / 'ui', root=root)
    with ExitStack() as stack:
        if optional_present(directory):
            regular(directory, directory=True)
            for name in ('progress', 'shortcuts'):
                path = directory / ('.' + name + '.lock')
                if optional_present(path):
                    regular(path)
                try:
                    stack.enter_context(FileLock(path, timeout=0, mode=0o600))
                except Timeout as exc:
                    raise RuntimeError('Canonical UI preferences are being saved. Retry when idle.') from exc
        if expected is not None and read_snapshot(root, expected.name) != expected:
            raise ValueError('Canonical UI preferences changed after migration was reviewed.')
        yield


def migrate_progress(root):
    """Create current preferences once; voice selection is never imported."""
    from .progress import FILENAME, ProgressPreferences, save_preferences
    root = local_path(root, exists=True)
    if optional_present(root / FILENAME):
        return ''
    source = read_snapshot(root, PROGRESS)
    if source is None:
        return ''
    old = decode_progress(source.raw)
    speech = old['mode'] in ('speech', 'both')
    current = ProgressPreferences(mode='beep_speech' if old['mode'] == 'both' else old['mode'],
        interval_seconds=old['interval_seconds'], volume=old['volume'], quiet=old['quiet'],
        background=old['background'], suppress_during_media=old['mute_during_audio'], muted=speech)
    save_preferences(root, current, require_absent=True,
                     source_guard=snapshot_guard(root, expected=source))
    return ('Previous progress preferences imported; the original file was kept. '
            + ('Speech stays muted. Choose a current local voice and unmute it in Progress feedback.'
               if speech else ''))


def menu_part(label):
    text = label.split('\t', 1)[0].replace('&&', '\x00').replace('&', '').replace('\x00', '&').strip()
    return text.removesuffix('...').removesuffix('…').strip().casefold()


def current_menu_map(manager):
    """Use current registered native items, never captions as fresh authority."""
    if manager.frame is None:
        raise ValueError('Open the native keyboard settings before importing menu shortcuts.')
    by_id = {binding.item.GetId(): (manager.registry.get(identity), binding.caption)
             for identity, binding in manager.bindings.items()}
    result = {}
    def visit(menu, parents):
        for item in menu.GetMenuItems():
            if item.IsSeparator():
                continue
            registered = by_id.get(item.GetId())
            label = registered[1] if registered else item.GetItemLabel()
            part = menu_part(label)
            if not part or '/' in part:
                raise ValueError('The current native menu has an ambiguous command path.')
            parts = (*parents, part)
            child = item.GetSubMenu()
            if child is not None:
                visit(child, parts)
            elif registered is not None:
                name = '/'.join(parts)
                if name in result:
                    raise ValueError('The current native menu has duplicate command paths.')
                spec = registered[0]
                result[name] = (spec.command_id, spec.default_shortcut)
    bar = manager.frame.GetMenuBar()
    for index in range(bar.GetMenuCount()):
        part = menu_part(bar.GetMenuLabel(index))
        if not part or '/' in part:
            raise ValueError('The current native menu has an ambiguous root path.')
        visit(bar.GetMenu(index), (part,))
    return result


def migrate_shortcuts(manager):
    root = manager.store.path.parent
    if optional_present(manager.store.path):
        return False
    source = read_snapshot(root, SHORTCUTS)
    if source is None:
        return False
    if optional_present(root / 'keybindings.json'):
        raise ValueError('Two older shortcut formats are present. Review their assignments before saving a current map.')
    old, mapping = decode_shortcuts(source.raw), current_menu_map(manager)
    translated = {}
    for name, shortcut in old['overrides'].items():
        if name not in mapping:
            raise ValueError('An older shortcut command is unavailable: ' + name)
        identity, original = mapping[name]
        if old['originals'][name] != original:
            raise ValueError('The original shortcut changed for ' + name + '. Review it before assigning a new key.')
        if identity in translated:
            raise ValueError('Several canonical shortcuts would retarget the same command.')
        translated[identity] = shortcut
    manager.validate(translated)
    manager.apply_overrides(translated, source_guard=snapshot_guard(root, expected=source))
    manager.load_notice = 'Previous native-menu shortcuts imported; the original file was kept.'
    return True
