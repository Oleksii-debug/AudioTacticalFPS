"""Passive desktop choices; observing clipboard and global-key admission are transient."""
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from types import MappingProxyType

from .durable_state import atomic_bytes, local_path, read_bytes_snapshot, regular, state_lock
from .keybindings import normalize_shortcut

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
            if type(identity) is not str or re.fullmatch(r'[a-z][a-z0-9_.-]{0,127}', identity) is None:
                raise ValueError('Use a bounded semantic global command identity.')
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
    """Use the shared bounded snapshot, CAS lock and atomic state publication."""
    def __init__(self, root):
        self.root = local_path(root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / FILENAME
        self._loaded, self._observed = False, None

    def _guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The desktop preference library changed.')
        local_path(self.path, root=self.root)

    def _snapshot(self):
        self._guard()
        try:
            self.path.lstat()
        except FileNotFoundError:
            return None, None
        raw, token = read_bytes_snapshot(self.path, max_bytes=MAX_BYTES)
        self._guard()
        return raw, token

    def load(self):
        raw, token = self._snapshot()
        self._observed, self._loaded = token, True
        return DesktopPreferences() if raw is None else validate_desktop_preferences(raw)

    def save(self, preferences):
        if not isinstance(preferences, DesktopPreferences) or not self._loaded:
            raise ValueError('Load and explicitly review desktop preferences before saving.')
        raw = (json.dumps(preferences.to_dict(), ensure_ascii=False, sort_keys=True,
                          indent=2) + '\n').encode('utf-8')
        validate_desktop_preferences(raw)
        self._guard()
        with state_lock(self.path.parent, 'desktop-preferences'):
            _, token = self._snapshot()
            if token != self._observed:
                raise ValueError('Desktop preferences changed in another window. Reload before saving.')
            atomic_bytes(self.path, raw, replace=token is not None)
            self._observed = sha256(raw).hexdigest()
        return preferences
