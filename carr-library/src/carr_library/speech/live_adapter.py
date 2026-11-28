"""Small reading-controller adapter to the canonical installed-voice registry.

No discovery, renderer or output store is duplicated here. The reader's ratio
controls are mapped to SpeechSettings' discrete native controls explicitly.
"""
from dataclasses import dataclass
import math
from pathlib import Path
import sys
from time import monotonic

from ._io import check_cancelled, text_value
from .runtime import SpeechSettings, VoiceRegistry


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str
    language: str | None = None
    rate: float = 1.0
    pitch: float = 0.0
    volume: float = 1.0

    def __post_init__(self):
        text_value(self.text)
        text_value(self.voice_id, 'voice identity', maximum=2048)
        if self.language is not None:
            text_value(self.language, 'language', maximum=128)
        for name, lower, upper in (('rate', .5, 2), ('pitch', -1, 1), ('volume', 0, 1)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f'Reader {name} must be a finite number from {lower} to {upper}.')


def native_rate(ratio):
    """Nearest supported native step; 0.5 -> -10, 1 -> 0, 2 -> 10."""
    if type(ratio) not in (int, float) or not math.isfinite(ratio) or not .5 <= ratio <= 2:
        raise ValueError('Reader rate must be a finite ratio from 0.5 to 2.')
    return round(10 * math.log2(ratio))


def _registry(configuration_root, cancelled, *, timeout_seconds=60):
    if configuration_root is None:
        return VoiceRegistry.discover(cancelled=cancelled, timeout_seconds=timeout_seconds)
    from ..runtime_settings import voice_registry
    return voice_registry(Path(configuration_root) / 'state', cancelled=cancelled,
                          timeout_seconds=timeout_seconds)


def _close_registry(registry):
    """Keep saved speech truthful while the registry retains failed cleanup."""
    close = getattr(registry, 'close', None)
    if not callable(close):
        return
    message = None
    try:
        if close() is False:
            message = 'The local voice worker still awaits confirmed cleanup.'
    except (Exception, KeyboardInterrupt) as exc:
        message = f'Local voice cleanup needs attention: {exc}'
    if message is not None:
        try:
            print(message, file=sys.stderr)
        except (OSError, ValueError):
            pass


def discover_voices(*, cancelled=None, configuration_root=None):
    callback = cancelled if cancelled is not None else lambda: False
    check_cancelled(callback)
    registry = _registry(configuration_root, callback)
    try:
        return registry.voices()
    finally:
        _close_registry(registry)


def render_speech(request, target, *, cancelled=None, timeout_seconds=600,
                  max_output_bytes=512 * 1024 * 1024, configuration_root=None):
    if not isinstance(request, SpeechRequest):
        raise ValueError('A validated reading SpeechRequest is required.')
    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 86400):
        raise ValueError('Set a positive reading speech timeout of at most 86400 seconds.')
    options = SpeechSettings(rate=native_rate(request.rate), volume=round(request.volume * 100),
        pitch=None if request.pitch == 0 else round((request.pitch + 1) * 49.5),
        timeout_seconds=max(1, timeout_seconds), max_output_bytes=max_output_bytes)
    callback = cancelled if cancelled is not None else lambda: False
    deadline = monotonic() + timeout_seconds
    def check():
        check_cancelled(callback)
        if monotonic() >= deadline:
            raise TimeoutError('Reading speech exceeded its overall time budget.')
        return False
    check()
    registry = _registry(configuration_root, check, timeout_seconds=min(60, timeout_seconds))
    try:
        selected = registry.get(request.voice_id)
        if request.language is not None and request.language.lower().replace('_', '-') != selected.language.lower().replace('_', '-'):
            raise ValueError('The requested language differs from the selected installed voice. Select its explicit language profile.')
        return registry.synthesize(request.text, request.voice_id, target, settings=options, cancelled=check)
    finally:
        _close_registry(registry)
