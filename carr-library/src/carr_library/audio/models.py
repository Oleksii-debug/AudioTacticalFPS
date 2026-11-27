"""Typed, serializable values shared by local audio production tools.

No value in this module is a shell command, FFmpeg filter expression, URL, or
model executable.  Numeric controls deliberately describe capabilities which
the runtime can validate, rather than accepting arbitrary vendor arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class AudioError(RuntimeError):
    """A local audio operation failed without publishing an output."""


class AudioCancelled(AudioError):
    """An operation was cancelled; previous completed outputs are preserved."""


class AudioUnavailable(AudioError):
    """An optional installed engine or verified capability is unavailable."""


def finite(value, label, minimum=None, maximum=None):
    if type(value) not in (int, float):
        raise ValueError(f'{label} must be a finite number.')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} must be a finite number.') from None
    if not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number.')
    if minimum is not None and value < minimum:
        raise ValueError(f'{label} must be at least {minimum}.')
    if maximum is not None and value > maximum:
        raise ValueError(f'{label} must be at most {maximum}.')
    return float(value)


def integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}.')
    return value


@dataclass(frozen=True)
class AudioBudget:
    """Explicit local resource allowances, independent of project duration.

    Output bytes include staging files.  Increase the budget intentionally for
    larger jobs; there is no ten-minute (or similar) project-duration ceiling.
    The FFmpeg per-allocation limit is not a whole-process memory sandbox.
    """
    timeout: float = 3600
    max_output_bytes: int = 32 * 1024**3
    max_log_bytes: int = 2 * 1024**2
    reserve_disk_bytes: int = 32 * 1024**2
    max_inputs: int = 64
    threads: int = 2
    max_alloc_bytes: int = 256 * 1024**2
    max_staging_files: int = 10000

    def __post_init__(self):
        finite(self.timeout, 'Timeout', .05, 365 * 86400)
        integer(self.max_output_bytes, 'Output budget', 4096, 2**63 - 1)
        integer(self.max_log_bytes, 'Log budget', 1024, 128 * 1024**2)
        integer(self.reserve_disk_bytes, 'Free disk reserve', 0, 2**63 - 1)
        integer(self.max_inputs, 'Simultaneous inputs', 1, 1024)
        integer(self.threads, 'Worker threads', 1, 64)
        integer(self.max_alloc_bytes, 'Allocation budget', 1024**2, 2**31 - 1)
        integer(self.max_staging_files, 'Staging file budget', 1, 1000000)


@dataclass(frozen=True, init=False)
class Effect:
    kind: str
    parameters: Mapping[str, float]
    enabled: bool

    def __init__(self, kind, parameters=None, *, enabled=True, **controls):
        if not isinstance(kind, str) or not kind or len(kind) > 40:
            raise ValueError('Effect kind must be a supported name.')
        if type(enabled) is not bool:
            raise ValueError('Effect enabled must be a boolean.')
        if parameters is not None and not isinstance(parameters, Mapping):
            raise ValueError('Effect parameters must be a mapping.')
        values = dict(parameters or {})
        if set(values) & set(controls):
            raise ValueError('Duplicate effect parameters.')
        values.update(controls)
        if len(values) > 20 or any(not isinstance(k, str) for k in values):
            raise ValueError('Invalid effect parameters.')
        object.__setattr__(self, 'kind', kind)
        object.__setattr__(self, 'parameters', MappingProxyType(values))
        object.__setattr__(self, 'enabled', enabled)

    def to_dict(self):
        return {'kind': self.kind, 'parameters': dict(self.parameters), 'enabled': self.enabled}


@dataclass(frozen=True)
class Track:
    path: str | Path
    start: float = 0
    gain_db: float = 0
    pan: float = 0
    role: str = 'voice'
    trim_start: float = 0
    duration: float | None = None
    fade_in: float = 0
    fade_out: float = 0
    effects: tuple[Effect, ...] = ()
    routing: str = 'pan'

    def __post_init__(self):
        if not isinstance(self.path, (str, Path)) or not str(self.path):
            raise ValueError('Track requires a local audio path.')
        finite(self.start, 'Track start', 0)
        finite(self.gain_db, 'Track gain in dB', -120, 24)
        finite(self.pan, 'Pan', -1, 1)
        finite(self.trim_start, 'Trim start', 0)
        if self.duration is not None:
            finite(self.duration, 'Track duration', .000001)
        finite(self.fade_in, 'Fade-in', 0)
        finite(self.fade_out, 'Fade-out', 0)
        if self.role not in ('voice', 'music', 'ambience', 'sfx', 'microphone'):
            raise ValueError('Unsupported track role.')
        if self.routing not in ('pan', 'left', 'right', 'stereo'):
            raise ValueError('Routing must be pan, left, right, or stereo.')
        if not isinstance(self.effects, (tuple, list)) or len(self.effects) > 32:
            raise ValueError('A track supports at most 32 typed effects.')
        if any(not isinstance(effect, Effect) for effect in self.effects):
            raise ValueError('Track effects must be typed Effect values.')
        object.__setattr__(self, 'effects', tuple(self.effects))


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    duration: float
    sample_rate: int
    channels: int
    codec: str
    format: str
    frames: int | None = None
    size: int = 0
    sha256: str | None = None

    def to_dict(self):
        return {'path': str(self.path), 'duration': self.duration,
                'sample_rate': self.sample_rate, 'channels': self.channels,
                'codec': self.codec, 'format': self.format, 'frames': self.frames,
                'size': self.size, 'sha256': self.sha256}


@dataclass(frozen=True)
class LoudnessInfo:
    integrated_lufs: float | None
    true_peak_dbfs: float | None
    loudness_range_lu: float | None
    threshold_lufs: float | None
    silent: bool

    def to_dict(self):
        return {'integrated_lufs': self.integrated_lufs,
                'true_peak_dbfs': self.true_peak_dbfs,
                'loudness_range_lu': self.loudness_range_lu,
                'threshold_lufs': self.threshold_lufs, 'silent': self.silent}


@dataclass(frozen=True)
class MusicSpec:
    duration: float
    mood: str = 'calm'
    tempo: float = 90
    seed: int = 0
    engine: str = 'procedural'
    key: str = 'C'
    intensity: float = .45
    prompt: str = ''
    fade_in: float = .15
    fade_out: float = .4

    def __post_init__(self):
        finite(self.duration, 'Music duration', .01)
        if self.mood not in ('calm', 'suspense', 'hopeful', 'sad', 'energetic'):
            raise ValueError('Music mood must be calm, suspense, hopeful, sad, or energetic.')
        finite(self.tempo, 'Tempo', 20, 300)
        integer(self.seed, 'Seed', 0, 2**31 - 1)
        if self.engine not in ('procedural', 'midi', 'musicgen', 'ace-step'):
            raise ValueError('Unknown local music engine.')
        if self.key not in ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'):
            raise ValueError('Music key must use a chromatic pitch name such as C or F#.')
        finite(self.intensity, 'Intensity', 0, 1)
        if not isinstance(self.prompt, str) or len(self.prompt) > 8000 or '\x00' in self.prompt:
            raise ValueError('Music prompt must contain at most 8000 text characters.')
        self.prompt.encode('utf-8')
        finite(self.fade_in, 'Music fade-in', 0, self.duration)
        finite(self.fade_out, 'Music fade-out', 0, self.duration)


@dataclass(frozen=True)
class LocalModelConfig:
    """An explicit local installation; never a downloadable model identifier.

    The manifest is verified before launching trusted, user-installed model
    code.  Only a noncommercial private profile enables MusicGen weights.
    """
    engine: str
    python: Path
    directory: Path
    manifest: Path
    device: str = 'cpu'
    private_noncommercial: bool = True
    max_chunk_seconds: float = 30

    def __post_init__(self):
        if self.engine not in ('musicgen', 'ace-step'):
            raise ValueError('Unknown local neural music engine.')
        if self.device not in ('cpu', 'cuda', 'mps'):
            raise ValueError('Unsupported model device.')
        if type(self.private_noncommercial) is not bool:
            raise ValueError('A use profile must be explicitly selected.')
        finite(self.max_chunk_seconds, 'Model chunk allowance', 1, 600)
        for name in ('python', 'directory', 'manifest'):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(f'Local model {name} must be an absolute path.')
            object.__setattr__(self, name, value)
