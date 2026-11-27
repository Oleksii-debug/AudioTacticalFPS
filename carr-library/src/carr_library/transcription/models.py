"""Typed operation choices for the existing job-directory protocol."""
from dataclasses import asdict, dataclass
import math
import re
from threading import Event


PROTOCOL = 'whisper-transcribe-job-dir-v43'
FORMATS = ('txt', 'srt', 'vtt', 'json')
INPUT_SUFFIXES = frozenset(('.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.opus', '.flac',
                           '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'))


def exact_keys(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f'Invalid {label} fields.')
    return value


def checksum(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('An exact SHA256 is required.')
    return value


def identity(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid transcription job identity.')
    return value


def positive(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'Invalid transcription {label}.')
    return value


@dataclass(frozen=True)
class TranscriptionOptions:
    language: str = 'auto'
    task: str = 'transcribe'
    formats: tuple[str, ...] = ('txt',)
    device: str = 'cpu'
    compute_type: str = 'int8'
    beam_size: int = 1
    vad_filter: bool = True
    condition_on_previous_text: bool = True

    def __post_init__(self):
        if not isinstance(self.language, str) or re.fullmatch('[a-z]{2,3}|auto', self.language) is None:
            raise ValueError('Choose an explicit advertised language code or auto detection.')
        if self.task not in ('transcribe', 'translate'):
            raise ValueError('Choose transcription or Whisper translation into English.')
        if (type(self.formats) is not tuple or not self.formats or len(set(self.formats)) != len(self.formats)
                or any(value not in FORMATS for value in self.formats)):
            raise ValueError('Choose one or more distinct TXT, SRT, VTT or JSON outputs.')
        if self.device not in ('cpu', 'cuda'):
            raise ValueError('Choose an explicit CPU or CUDA device; no silent device fallback.')
        if self.compute_type not in ('int8', 'int8_float16', 'int8_float32', 'float16', 'float32'):
            raise ValueError('Choose an explicit advertised compute type.')
        positive(self.beam_size, 'beam size', 10)
        if type(self.vad_filter) is not bool or type(self.condition_on_previous_text) is not bool:
            raise ValueError('Transcription options require exact booleans.')

    def to_dict(self):
        return {**asdict(self), 'formats': list(self.formats)}

    @classmethod
    def from_dict(cls, value):
        exact_keys(value, cls.__dataclass_fields__, 'transcription option')
        if type(value['formats']) is not list:
            raise ValueError('Invalid transcript output choices.')
        return cls(**{**value, 'formats': tuple(value['formats'])})

    def worker_settings(self, model):
        # JSON is always retained for independent structural/ownership checks.
        return {'output_mode': 'separate', 'task': self.task, 'translation_target': 'en',
                'language': self.language, 'model': model, 'device': self.device,
                'compute_type': self.compute_type, 'beam_size': self.beam_size,
                'parallel_files': 1, 'vad_filter': self.vad_filter, 'include_headers': False,
                **{'output_' + fmt: fmt == 'json' or fmt in self.formats for fmt in FORMATS},
                'multilingual': False, 'condition_on_previous_text': self.condition_on_previous_text}


@dataclass(frozen=True)
class TranscriptionBudget:
    max_sources: int = 1000
    max_input_bytes: int = 512 * 1024**3
    max_job_bytes: int = 1024**4
    max_output_bytes: int = 64 * 1024**2
    max_files: int = 100000
    max_seconds: float = 86400.0
    stop_grace_seconds: float = 2.0

    def __post_init__(self):
        positive(self.max_sources, 'source count', 10000)
        positive(self.max_input_bytes, 'input byte budget', 16 * 1024**4)
        positive(self.max_job_bytes, 'job byte budget', 32 * 1024**4)
        positive(self.max_output_bytes, 'single output byte budget', 256 * 1024**2)
        positive(self.max_files, 'file count', 1000000)
        for value, label, low, high in ((self.max_seconds, 'deadline', .05, 7 * 86400),
                (self.stop_grace_seconds, 'stop grace', 0, 10)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid transcription {label}.')
        if self.max_input_bytes > self.max_job_bytes:
            raise ValueError('The job budget must include its retained source snapshots.')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**exact_keys(value, cls.__dataclass_fields__, 'transcription budget'))


class TranscriptionControl:
    """An operation-owned signal. Pause is observed between native inference calls."""
    def __init__(self):
        self._paused = Event()
        self._stopped = Event()

    @property
    def paused(self):
        return self._paused.is_set()

    @property
    def stopped(self):
        return self._stopped.is_set()

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stopped.set()


@dataclass(frozen=True)
class PreparedTranscription:
    id: str
    request_sha256: str
    workspace: str
    source_count: int
    source_bytes: int
    model_id: str
    model_revision: str
    options: TranscriptionOptions
    source_names: tuple[str, ...]
    profile_path: str
    profile_sha256: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranscriptionProgress:
    phase: str
    completed: int
    total: int
    state: str = 'running'


@dataclass(frozen=True)
class TranscriptionReceipt:
    id: str
    request_sha256: str
    receipt_sha256: str
    sources: int
    outputs: tuple[str, ...]
    quotation_authority: bool = False
    warnings: tuple[str, ...] = ()
