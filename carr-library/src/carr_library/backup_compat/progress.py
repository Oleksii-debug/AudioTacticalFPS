"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import asdict, dataclass
import json
import math


FILENAME = 'progress-preferences.json'


MAX_BYTES = 8192


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
