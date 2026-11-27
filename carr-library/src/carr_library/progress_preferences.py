"""Explicit local progress-feedback preferences and backup validation."""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout

# Use the selected keyboard settings writer's reviewed filesystem guards.
# These helpers reject links/reparse points/hardlinks and detect changed reads.
from .keyboard_preferences import _check_path, _read


FILENAME = 'progress-preferences.json'
MAX_BYTES = 8192
_UNOBSERVED = object()


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
                or not math.isfinite(self.interval_seconds) or not 1 <= self.interval_seconds <= 60):
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
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
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


def _digest(raw):
    return hashlib.sha256(raw).digest() if raw is not None else None


class ProgressPreferenceStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self._observed = _UNOBSERVED

    def load(self):
        try:
            raw = _read(self.path)
            self._observed = _digest(raw)
            return ProgressPreferences() if raw is None else validate_progress_preferences(raw)
        except (ValueError, OSError) as exc:
            raise ProgressPreferenceError(str(exc)) from exc

    def save(self, preferences):
        if not isinstance(preferences, ProgressPreferences):
            raise ProgressPreferenceError('A validated ProgressPreferences value is required.')
        raw = (json.dumps({'version': 1, 'preferences': asdict(preferences)},
                          sort_keys=True, indent=2) + '\n').encode('utf-8')
        validate_progress_preferences(raw)
        temporary = self.path.with_name('.' + self.path.name + '-' + uuid4().hex + '.tmp')
        lock = self.path.with_name(self.path.name + '.lock')
        try:
            _check_path(self.path, regular=True)
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_path(lock, regular=True)
            with FileLock(lock, timeout=5, mode=0o600):
                current = _read(self.path)
                if self._observed is not _UNOBSERVED and _digest(current) != self._observed:
                    raise ProgressPreferenceError('Progress preferences changed in another window. Reload before saving.')
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _digest(_read(self.path)) != _digest(current):
                    raise ProgressPreferenceError('Progress preferences changed before saving. Reload before saving.')
                os.replace(temporary, self.path)
                self._observed = _digest(raw)
                if hasattr(os, 'O_DIRECTORY'):
                    try:
                        descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    except OSError:
                        pass  # Atomic file publication already committed.
        except Timeout as exc:
            raise ProgressPreferenceError('Progress preferences are being saved by another window.') from exc
        except (ValueError, OSError) as exc:
            raise ProgressPreferenceError(str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return preferences
