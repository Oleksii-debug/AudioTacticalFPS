"""Validated, portable keyboard preferences for native menu commands.

Menu paths identify commands, not transient wx IDs. Preferences contain no
executable expressions or machine paths and live with the private library.
Only main-window shortcuts are remapped; modal editor/navigation keys retain
their documented contextual behavior.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer

FILENAME = 'keybindings.json'
MAX_BYTES = 128 * 1024
MAX_COMMANDS = 500
_KEYS = frozenset(('Space', 'Enter', 'Home', 'End', 'PageUp', 'PageDown',
                  'Left', 'Right', 'Up', 'Down', 'Insert', 'Delete', 'Backspace'))
_FORBIDDEN = frozenset(('Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Y', 'Ctrl+Z',
                       'Alt+Tab', 'Alt+F4', 'Ctrl+Alt+N', 'F10', 'Shift+F10', 'Ctrl+Alt+Delete', 'Ctrl+Tab', 'Ctrl+Shift+Tab'))


def normalize_shortcut(value):
    """Return wx menu accelerator spelling; empty text removes a shortcut."""
    if not isinstance(value, str) or len(value) > 48:
        raise ValueError('Enter a keyboard shortcut of at most 48 characters.')
    value = value.strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split('+')]
    if len(parts) > 4 or any(not part for part in parts):
        raise ValueError('Use a shortcut such as Ctrl+Shift+S, Alt+F8, or F6.')
    if any(part.casefold().replace(' ', '') in ('insert', 'ins', 'capslock', 'caps', 'nvda') for part in parts):
        raise ValueError('Insert and Caps Lock belong to screen-reader commands and cannot be assigned.')
    modifiers = []
    for part in parts[:-1]:
        modifier = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}.get(part.lower())
        if modifier is None or modifier in modifiers:
            raise ValueError('Use Ctrl, Alt and Shift at most once each.')
        modifiers.append(modifier)
    key = parts[-1]
    names = {name.lower(): name for name in _KEYS}
    if re.fullmatch('[a-zA-Z0-9]', key):
        key = key.upper()
    elif re.fullmatch(r'(?i)f(?:[1-9]|1[0-9]|2[0-4])', key):
        key = key.upper()
    elif key.lower() in names:
        key = names[key.lower()]
    else:
        raise ValueError('Use a letter, digit, F1–F24, or a named navigation key.')
    if not re.fullmatch(r'F\d+', key) and not ({'Ctrl', 'Alt'} & set(modifiers)):
        raise ValueError('Letters and navigation keys need Ctrl or Alt so ordinary typing remains available.')
    result = '+'.join([*(m for m in ('Ctrl', 'Alt', 'Shift') if m in modifiers), key])
    if result in _FORBIDDEN or (modifiers == ['Alt'] and len(key) == 1):
        raise ValueError('This key is reserved for editing, system controls, or Alt-menu navigation.')
    return result


@dataclass(frozen=True)
class Command:
    identity: str
    label: str
    shortcut: str = ''
    configurable: bool = True

    def __post_init__(self):
        if (not isinstance(self.identity, str) or not 0 < len(self.identity) <= 500
                or any(ord(ch) < 32 for ch in self.identity)
                or not isinstance(self.label, str) or not self.label.strip()):
            raise ValueError('Invalid keyboard command.')
        # An existing fixed platform command, e.g. Alt+F4, remains intact.
        if self.configurable:
            normalize_shortcut(self.shortcut)


def validate_bindings(commands, overrides):
    commands = tuple(commands)
    if len(commands) > MAX_COMMANDS or len({c.identity for c in commands}) != len(commands):
        raise ValueError('The keyboard command inventory is invalid.')
    if not isinstance(overrides, dict) or len(overrides) > MAX_COMMANDS:
        raise ValueError('Invalid keyboard preferences.')
    by_id = {command.identity: command for command in commands}
    if set(overrides) - by_id.keys():
        raise ValueError('These preferences include a command unavailable in this version.')
    bindings, used = {}, {}
    for command in commands:
        value = overrides.get(command.identity, command.shortcut)
        if not command.configurable:
            if value != command.shortcut:
                raise ValueError(f'{command.label} uses a fixed system shortcut.')
        else:
            value = normalize_shortcut(value)
        if value:
            collision = used.get(value.casefold())
            if collision:
                raise ValueError(f'{value} is already assigned to {collision}. Clear that assignment first.')
            used[value.casefold()] = command.label
        bindings[command.identity] = value
    return bindings


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate keyboard preference field.')
        result[key] = value
    return result


def decode_preferences(raw):
    """Strict structural validation, also used by checked library backups."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Keyboard preferences are empty or too large.')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid keyboard preferences JSON.') from exc
    if (type(value) is not dict or set(value) != {'version', 'kind', 'overrides'}
            or type(value['version']) is not int or value['version'] != 1
            or value['kind'] != 'carr-keybindings' or type(value['overrides']) is not dict
            or len(value['overrides']) > MAX_COMMANDS):
        raise ValueError('Unsupported keyboard preferences format.')
    result = {}
    for identity, shortcut in value['overrides'].items():
        if (not isinstance(identity, str) or not 0 < len(identity) <= 500
                or any(ord(ch) < 32 for ch in identity)):
            raise ValueError('Invalid keyboard command identity.')
        result[identity] = normalize_shortcut(shortcut)
    return result


def encode_preferences(overrides):
    raw = (json.dumps(dict(version=1, kind='carr-keybindings', overrides=overrides),
                      ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    decode_preferences(raw)
    return raw


def read_preferences(path):
    path = Path(path)
    _regular_if_present(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Keyboard preferences are empty or too large.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) != size:
        raise ValueError('Keyboard preferences changed while reading.')
    return decode_preferences(raw)


class KeybindingStore:
    def __init__(self, root):
        self.root = _private_root(root)
        self.path = self.root / FILENAME

    def load(self):
        return read_preferences(self.path) if os.path.lexists(self.path) else {}

    def save(self, commands, overrides):
        validate_bindings(commands, overrides)
        # Fixed defaults need not be serialized; imported edits can never
        # remap the platform's close command or ordinary copy/paste keys.
        configurable = {command.identity for command in commands if command.configurable}
        raw = encode_preferences({key: value for key, value in overrides.items() if key in configurable})
        with exclusive_writer(self.root):
            _regular_if_present(self.path)
            temporary = None
            try:
                with NamedTemporaryFile(dir=self.root, prefix='.keybindings-', suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name != 'nt':
                    fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return raw
