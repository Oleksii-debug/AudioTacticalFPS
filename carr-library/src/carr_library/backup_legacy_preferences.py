"""Pure validators for inert preferences from the canonical backup producer.

Selected validation code is reused from canonical 966e5ccf. These functions
preserve old data; they do not install a second keyboard registry, apply a
shortcut, change progress feedback, or read/write any external path.
"""
from dataclasses import dataclass
import json
import math
import re
from types import SimpleNamespace

_KEYMAP_MAX_BYTES = 64 * 1024
_PROGRESS_MAX_BYTES = 8192

MAX_COMMANDS = 512
MAX_SHORTCUT_LENGTH = 64
_IDENTIFIER = re.compile(r'[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z')
_MODIFIERS = {'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift'}
_MODIFIER_ORDER = ('Ctrl', 'Alt', 'Shift')
_KEY_NAMES = {
    'pgup': 'PageUp', 'pageup': 'PageUp',
    'pgdn': 'PageDown', 'pgdown': 'PageDown', 'pagedown': 'PageDown',
    'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down',
    'home': 'Home', 'end': 'End', 'space': 'Space', 'spacebar': 'Space',
    'tab': 'Tab', 'esc': 'Escape', 'escape': 'Escape',
    'enter': 'Enter', 'return': 'Enter', 'del': 'Delete', 'delete': 'Delete',
    'back': 'Backspace', 'backspace': 'Backspace',
    'ins': 'Insert', 'insert': 'Insert', 'capslock': 'CapsLock',
}
_CURSOR_KEYS = {'Left', 'Right', 'Up', 'Down', 'Home', 'End', 'PageUp', 'PageDown'}


class ShortcutError(ValueError):
    """An invalid, conflicting or reserved application shortcut."""


def _identifier(value, field):
    if (not isinstance(value, str) or len(value) > 100
            or _IDENTIFIER.fullmatch(value) is None):
        raise ShortcutError(f'{field} must be a stable lowercase command identifier.')
    return value


def normalize_shortcut(value):
    """Return an unambiguous shortcut spelling, or reject protected keys.

    Empty text means unassigned. Bare function keys are supported; ordinary
    characters and navigation keys require modifiers. Native editing, menu
    mnemonics, focus navigation, OS window controls and common screen-reader
    modifier keys remain outside the configurable application map.
    """
    if not isinstance(value, str) or len(value) > MAX_SHORTCUT_LENGTH:
        raise ShortcutError('A shortcut must be text of at most 64 characters.')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ShortcutError('A shortcut cannot contain control characters.')
    value = value.strip()
    if not value:
        return ''
    parts = [part.strip() for part in value.split('+')]
    if not all(parts) or len(parts) > 4:
        raise ShortcutError('Use a shortcut such as Ctrl+Shift+K, or leave it empty.')
    modifiers = set()
    for part in parts[:-1]:
        modifier = _MODIFIERS.get(part.lower())
        if modifier is None:
            raise ShortcutError('Only Ctrl, Alt and Shift modifiers are supported.')
        if modifier in modifiers:
            raise ShortcutError('A shortcut cannot repeat a modifier.')
        modifiers.add(modifier)
    raw_key = parts[-1]
    if len(raw_key) == 1 and raw_key.isascii() and raw_key.isalnum():
        key = raw_key.upper()
    elif re.fullmatch(r'[fF](?:[1-9]|1[0-2])', raw_key):
        key = raw_key.upper()
    else:
        key = _KEY_NAMES.get(raw_key.lower())
        if key is None:
            raise ShortcutError('Unsupported key. Use a letter, digit, F1–F12 or a named navigation key.')

    protected = False
    if key in {'Insert', 'CapsLock', 'Tab', 'Escape', 'Enter'}:
        protected = True
    elif not modifiers and not re.fullmatch(r'F(?:[1-9]|1[0-2])', key):
        protected = True
    elif modifiers == {'Shift'} and len(key) == 1:
        protected = True
    elif key == 'F10' and modifiers <= {'Shift'}:
        protected = True
    elif 'Alt' in modifiers and key in {'F4', 'Space', 'Backspace'}:
        protected = True
    elif modifiers == {'Alt'} and key in {'Up', 'Down'}:
        protected = True
    elif modifiers <= {'Alt', 'Shift'} and 'Alt' in modifiers and len(key) == 1:
        protected = True
    elif modifiers <= {'Ctrl', 'Shift'} and 'Ctrl' in modifiers:
        # Ctrl+PageUp/Down intentionally remain available to the modal reader.
        protected = (key in {'A', 'C', 'X', 'V', 'Z', 'Y', 'Backspace', 'Delete',
                             'Left', 'Right', 'Up', 'Down', 'Home', 'End'})
    elif modifiers == {'Shift'} and key in _CURSOR_KEYS | {'Delete', 'Backspace', 'Space'}:
        protected = True
    elif modifiers >= {'Ctrl', 'Alt'} and key == 'Delete':
        protected = True
    if protected:
        raise ShortcutError('This key combination is reserved for native editing, navigation, '
                            'screen-reader or operating-system controls.')
    return '+'.join([modifier for modifier in _MODIFIER_ORDER if modifier in modifiers] + [key])


class KeymapError(ValueError):
    """Keyboard preferences could not be safely read or saved."""


class KeymapChangedError(KeymapError):
    """Another process changed the keymap after this instance loaded it."""


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KeymapError('The keyboard settings contain a duplicate JSON key.')
        result[key] = value
    return result


def _decode(raw):
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)
    except KeymapError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise KeymapError('The keyboard settings are not valid UTF-8 JSON.') from exc
    if (not isinstance(value, dict) or set(value) != {'version', 'bindings'}
            or type(value['version']) is not int or value['version'] != 1):
        raise KeymapError('The keyboard settings format is unsupported.')
    bindings = value['bindings']
    if not isinstance(bindings, dict) or len(bindings) > MAX_COMMANDS:
        raise KeymapError('The keyboard settings must contain at most 512 command bindings.')
    try:
        return {_identifier(command_id, 'Command ID'): normalize_shortcut(shortcut)
                for command_id, shortcut in bindings.items()}
    except ShortcutError as exc:
        raise KeymapError(str(exc)) from exc


def validate_keymap(raw):
    """Validate a bounded JSON payload for backup or import without executing it.

    Unknown future-module IDs are retained as inert strings. Applying the map
    still requires CommandRegistry.validate to check installed commands and
    context conflicts. This helper neither reads nor writes the filesystem.
    """
    if not isinstance(raw, bytes):
        raise KeymapError('Keyboard settings validation requires immutable bytes.')
    if len(raw) > _KEYMAP_MAX_BYTES:
        raise KeymapError('The keyboard settings file exceeds 64 KiB.')
    return _decode(raw)


class ProgressPreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ProgressPreferences:
    mode: str = 'off'
    interval_seconds: float = 5.0
    volume: int = 30
    muted: bool = False
    allow_background: bool = False
    quiet_during_media: bool = True

    def __post_init__(self):
        if self.mode not in ('off', 'beep', 'speech', 'beep_speech'):
            raise ProgressPreferenceError('Choose Off, Beep, Speech or Beep and Speech.')
        if (isinstance(self.interval_seconds, bool)
                or not isinstance(self.interval_seconds, (int, float))
                or not 1 <= self.interval_seconds <= 60 or not math.isfinite(self.interval_seconds)):
            raise ProgressPreferenceError('The progress interval must be from 1 to 60 seconds.')
        if type(self.volume) is not int or not 0 <= self.volume <= 100:
            raise ProgressPreferenceError('Progress volume must be an integer from 0 to 100.')
        if any(type(value) is not bool for value in (self.muted, self.allow_background, self.quiet_during_media)):
            raise ProgressPreferenceError('Progress mute, background and quiet settings must be boolean.')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProgressPreferenceError('Duplicate progress-preference field.')
        result[key] = value
    return result


def validate_progress_preferences(raw):
    """Validate inert backup/import bytes; no voice, sound or network is used."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _PROGRESS_MAX_BYTES:
        raise ProgressPreferenceError('Progress preferences require at most 8 KiB of UTF-8 JSON.')
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if (type(document) is not dict or set(document) != {'version', 'preferences'}
                or type(document['version']) is not int or document['version'] != 1):
            raise ProgressPreferenceError('Unsupported progress-preference format.')
        preferences = document['preferences']
        if type(preferences) is not dict or set(preferences) != set(ProgressPreferences.__dataclass_fields__):
            raise ProgressPreferenceError('Invalid progress-preference fields.')
        return ProgressPreferences(**preferences)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ProgressPreferenceError('Progress preferences are invalid; the existing file was kept. ' + str(exc)) from exc



KEYBOARD = SimpleNamespace(MAX_BYTES=_KEYMAP_MAX_BYTES, validate_keymap=validate_keymap)
PROGRESS = SimpleNamespace(MAX_BYTES=_PROGRESS_MAX_BYTES,
                           validate_progress_preferences=validate_progress_preferences)
