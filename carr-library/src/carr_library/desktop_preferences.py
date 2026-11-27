"""Passive desktop choices; observing clipboard and global-key admission are transient."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType

from .durable_state import atomic_bytes, state_lock
from .keyboard_preferences import _read
from .ui.command_registry import _identifier, normalize_shortcut

FILENAME = 'desktop-preferences.json'
MAX_BYTES = 8192


def global_shortcut(value):
    value = normalize_shortcut(value)
    if not value:
        return ''
    parts = value.split('+')
    if (not {'Ctrl', 'Shift'} <= set(parts[:-1])
            or re.fullmatch(r'[A-Z0-9]|F(?:[1-9]|1[01])', parts[-1]) is None):
        raise ValueError('Global shortcuts require Ctrl+Shift with a letter, digit or F1–F11; Alt is optional.')
    return value


@dataclass(frozen=True)
class DesktopPreferences:
    tray_icon: bool = False
    global_shortcuts: object = None

    def __post_init__(self):
        if type(self.tray_icon) is not bool:
            raise ValueError('The tray preference must be boolean.')
        raw = {} if self.global_shortcuts is None else self.global_shortcuts
        if not isinstance(raw, (dict, MappingProxyType)) or len(raw) > 16:
            raise ValueError('Choose at most 16 global command assignments.')
        normalized, used = {}, set()
        for identity, shortcut in raw.items():
            _identifier(identity, 'Command ID')
            value = global_shortcut(shortcut)
            if value:
                if value in used:
                    raise ValueError('Global shortcuts contain duplicate explicit assignments.')
                used.add(value)
                normalized[identity] = value
        object.__setattr__(self, 'global_shortcuts', MappingProxyType(normalized))

    def to_dict(self):
        return {'version': 1, 'tray_icon': self.tray_icon,
                'global_shortcuts': dict(self.global_shortcuts)}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Desktop preferences contain a duplicate field.')
        result[key] = value
    return result


def validate_desktop_preferences(raw):
    """Passively validate backup bytes; unknown command IDs remain inert data."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Desktop preferences need at most 8 KiB of UTF-8 JSON.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        if (type(value) is not dict or set(value) != {'version', 'tray_icon', 'global_shortcuts'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['global_shortcuts']) is not dict):
            raise ValueError('Unsupported desktop preference fields.')
        return DesktopPreferences(value['tray_icon'], value['global_shortcuts'])
    except (UnicodeError, TypeError, RecursionError) as exc:
        raise ValueError('Invalid desktop preference bytes.') from exc


class DesktopPreferenceStore:
    """Use the existing guarded reader, lock and atomic state publication."""
    def __init__(self, root):
        self.path = Path(root).absolute() / FILENAME
        self._loaded, self._observed = False, None

    @staticmethod
    def _digest(raw):
        return sha256(raw).digest() if raw is not None else None

    def load(self):
        raw = _read(self.path, maximum=MAX_BYTES)
        self._observed, self._loaded = self._digest(raw), True
        return DesktopPreferences() if raw is None else validate_desktop_preferences(raw)

    def save(self, preferences):
        if not isinstance(preferences, DesktopPreferences) or not self._loaded:
            raise ValueError('Load and explicitly review desktop preferences before saving.')
        raw = (json.dumps(preferences.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2) + '\n').encode('utf-8')
        validate_desktop_preferences(raw)
        with state_lock(self.path.parent, 'desktop-preferences'):
            if self._digest(_read(self.path, maximum=MAX_BYTES)) != self._observed:
                raise ValueError('Desktop preferences changed in another window. Reload before saving.')
            atomic_bytes(self.path, raw, replace=True)
            self._observed = self._digest(raw)
        return preferences
