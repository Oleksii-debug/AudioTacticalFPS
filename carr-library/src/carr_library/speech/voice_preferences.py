"""Non-executable, per-library choices over the existing installed voice registry."""
from dataclasses import dataclass, replace
from hashlib import sha256
from itertools import islice
import os
from pathlib import Path
import re

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from ._io import text_value
from .runtime import Voice

MAX_LANGUAGES = 256
MAX_FAVORITES = 128
MAX_PREFERENCES_BYTES = 1024 * 1024
SCHEMA = 'carr-voice-preferences/1'


def language_key(value):
    """Normalize a declared identifier, without guessing language or dialect."""
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,7}', value):
        raise ValueError('Choose an installed voice with a declared language identifier.')
    value = value.replace('_', '-').lower()
    if value == 'und':
        raise ValueError('An unspecified language cannot have a language default.')
    return value


def voice_language(voice):
    try:
        return language_key(voice.language)
    except ValueError:
        return None


def _voice_id(value):
    text_value(value, 'saved voice ID', maximum=2048)
    if any(ord(char) < 32 for char in value):
        raise ValueError('Use an exact installed voice identity without control characters.')
    return value


def installed_voices(voices):
    values = tuple(islice(iter(voices), 10001))
    if len(values) > 10000 or any(not isinstance(voice, Voice) for voice in values):
        raise ValueError('Use the current bounded installed voice registry.')
    if len({voice.id for voice in values}) != len(values):
        raise ValueError('The installed voice identities are ambiguous.')
    return values


@dataclass(frozen=True)
class VoicePreferences:
    defaults: tuple[tuple[str, str], ...] = ()
    favorites: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.defaults, tuple) or len(self.defaults) > MAX_LANGUAGES:
            raise ValueError('Too many or invalid language defaults.')
        keys = set()
        for row in self.defaults:
            if not isinstance(row, tuple) or len(row) != 2:
                raise ValueError('A language default needs a language and voice identity.')
            language, identity = row
            if language_key(language) != language or language in keys:
                raise ValueError('Language defaults must have unique normalized identifiers.')
            _voice_id(identity); keys.add(language)
        if not isinstance(self.favorites, tuple) or len(self.favorites) > MAX_FAVORITES:
            raise ValueError('Too many or invalid favorite voices.')
        for identity in self.favorites:
            _voice_id(identity)
        if len(set(self.favorites)) != len(self.favorites):
            raise ValueError('Favorite voice identities must be unique.')
        object.__setattr__(self, 'defaults', tuple(sorted(self.defaults)))

    def to_dict(self):
        return {'schema': SCHEMA, 'defaults': dict(self.defaults), 'favorites': list(self.favorites)}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'defaults', 'favorites'}
                or value['schema'] != SCHEMA or type(value['defaults']) is not dict
                or type(value['favorites']) is not list):
            raise ValueError('Unsupported voice preferences; the saved file was preserved.')
        return cls(tuple(value['defaults'].items()), tuple(value['favorites']))

    def with_default(self, language, identity, voices):
        key = language_key(language)
        values = dict(self.defaults)
        if identity is None:
            values.pop(key, None)
        else:
            selected = next((voice for voice in installed_voices(voices) if voice.id == identity), None)
            if selected is None or voice_language(selected) != key:
                raise ValueError('Choose a currently installed voice declaring this exact language or dialect.')
            values[key] = selected.id
        return replace(self, defaults=tuple(values.items()))

    def with_favorite(self, identity, enabled, voices):
        _voice_id(identity)
        if type(enabled) is not bool:
            raise ValueError('Choose whether this voice is a favorite.')
        values = list(self.favorites)
        if enabled and identity not in values:
            if not any(voice.id == identity for voice in installed_voices(voices)):
                raise ValueError('Only a currently installed voice can be added as a favorite.')
            values.append(identity)
        elif not enabled and identity in values:
            values.remove(identity)
        return replace(self, favorites=tuple(values))

    def preferred(self, language, voices):
        key = language_key(language)
        identity = dict(self.defaults).get(key)
        if identity is None:
            return None
        selected = next((voice for voice in installed_voices(voices) if voice.id == identity), None)
        if selected is None or voice_language(selected) != key:
            raise ValueError(f'The saved default for {key} is unavailable or its declared language changed. '
                             'Choose a new installed voice explicitly; no fallback was selected.')
        return selected


def validate_voice_preferences(value):
    """Validate known backup state without probing engines, paths or credentials."""
    return VoicePreferences.from_dict(value).to_dict()


class VoicePreferenceStore:
    """Small existing speech sidecar with optimistic writes and normal backup coverage."""
    def __init__(self, library_root):
        self.library_root = local_path(library_root, exists=True)
        info = regular(self.library_root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.library_root / 'state' / 'speech' / 'voice-preferences.json'
        self.warning = ''

    def guard(self):
        root = local_path(self.library_root, exists=True)
        info = regular(root, directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The library directory changed. Reopen voice preferences.')
        local_path(self.path, root=self.library_root)

    def load(self):
        self.guard()
        if not os.path.lexists(self.path):
            return VoicePreferences(), None
        if regular(self.path).st_size > MAX_PREFERENCES_BYTES:
            raise ValueError('Saved voice preferences exceed their byte budget.')
        preferences = VoicePreferences.from_dict(read_json(self.path, maximum=MAX_PREFERENCES_BYTES))
        payload = encode(preferences.to_dict())
        if len(payload) > MAX_PREFERENCES_BYTES:
            raise ValueError('Saved voice preferences exceed their byte budget.')
        self.guard()
        return preferences, sha256(payload).hexdigest()

    def save(self, preferences, *, expected_token):
        if not isinstance(preferences, VoicePreferences):
            raise ValueError('Use validated local voice preferences.')
        payload = encode(preferences.to_dict())
        if len(payload) > MAX_PREFERENCES_BYTES:
            raise ValueError('Voice preferences exceed their byte budget.')
        self.guard()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.warning = ''
        published = False
        attempted = False
        token = sha256(payload).hexdigest()
        try:
            with state_lock(self.path.parent, 'voice-preferences'):
                _, current = self.load()
                if current != expected_token:
                    raise ValueError('Another window changed voice preferences. Reload before saving.')
                attempted = True
                atomic_json(self.path, preferences.to_dict(), replace=self.path.exists())
                published = True
        except (Exception, KeyboardInterrupt):
            # An attempted writer can fail after publication or before changing
            # an already equivalent value. Readback proves current state only;
            # without the writer's acknowledgement it cannot prove our commit.
            if not attempted:
                raise
            actual, actual_token = self.load()
            if actual != preferences or actual_token != token:
                raise
            if published:
                self.warning = ('Voice preferences were saved and checked. Closing the state lock did not '
                                'finish normally; reopen this window before another change.')
            else:
                self.warning = ('The intended voice preferences are present and checked. This save\'s '
                                'completion could not be confirmed; reopen and reload before another change.')
        return preferences, token
