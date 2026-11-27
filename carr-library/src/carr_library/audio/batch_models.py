"""Typed durable plans for existing audio conversion and Studio producers.

These records contain inert local source identities and settings. Reopening a
record never chooses an executable, resumes a worker, or follows an old output
directory. Every output name is derived before execution and is never replaced.
"""
from dataclasses import asdict, dataclass, fields
from hashlib import sha256
import json
import math
import re
import unicodedata

from .models import FORMATS, SAMPLE_RATES
from .paths import filename_component

SCHEMA = 'carr-audio-batch/1'
MAX_ITEMS = 128
MAX_INPUT_BYTES = 8 * 1024**3
MAX_STATE_BYTES = 16 * 1024**2
MAX_OUTPUTS = 10000
AUDIO_DEMUXERS = {
    '.wav': 'wav', '.flac': 'flac', '.mp3': 'mp3',
    '.m4a': 'mov', '.m4b': 'mov', '.aac': 'aac',
    '.aif': 'aiff', '.aiff': 'aiff', '.ogg': 'ogg',
    '.opus': 'ogg', '.wma': 'asf',
}
VIDEO_DEMUXERS = {
    '.mp4': 'mov', '.m4v': 'mov', '.mov': 'mov',
    '.mkv': 'matroska', '.webm': 'matroska', '.avi': 'avi',
}
ITEM_STATES = frozenset(('pending', 'running', 'paused', 'cancelled', 'failed', 'complete'))
BATCH_STATES = frozenset(('queued', 'running', 'paused', 'cancelled', 'failed', 'complete'))


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(',', ':')) + '\n').encode('utf-8')


def digest(value):
    return sha256(canonical(value)).hexdigest()


def exact(value, names, label):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError('Unsupported ' + label + ' fields.')
    return value


def text(value, label, limit=256):
    if (type(value) is not str or not value.strip() or len(value) > limit
            or '\x00' in value or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8')
    return value


def identity(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid audio batch item or project identity.')
    return value


def hash_value(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid audio source or receipt digest.')
    return value


def count(value, label, minimum=0, maximum=2**63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(label + ' is outside its integer allowance.')
    return value


def seconds(value, label, maximum=86400):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not .05 <= value <= maximum):
        raise ValueError(label + ' must be a positive bounded number of seconds.')
    return float(value)


@dataclass(frozen=True)
class AudioPreset:
    label: str = 'Speech delivery'
    output_format: str = 'mp3'
    sample_rate: int = 48000
    channels: int = 2
    bitrate_kbps: int = 192
    split: str = 'whole'
    max_seconds: float = 3600.0
    timeout_seconds: float = 3600.0

    def __post_init__(self):
        text(self.label, 'preset name', 80)
        if self.output_format not in FORMATS:
            raise ValueError('Select one of the existing supported audio formats.')
        if type(self.sample_rate) is not int or self.sample_rate not in SAMPLE_RATES:
            raise ValueError('Select a supported numeric sample rate.')
        count(self.channels, 'Channel count', 1, 2)
        count(self.bitrate_kbps, 'Encoded bitrate in kbps', 32, 320)
        if self.output_format in ('opus', 'wma') and self.sample_rate != 48000:
            raise ValueError('Opus and WMA batch presets require an explicit 48,000 Hz rate.')
        if self.split not in ('whole', 'chapters'):
            raise ValueError('Choose one whole programme or one file per existing chapter.')
        object.__setattr__(self, 'max_seconds', seconds(self.max_seconds, 'Maximum input duration'))
        object.__setattr__(self, 'timeout_seconds', seconds(self.timeout_seconds, 'Worker timeout'))

    @property
    def pcm_budget(self):
        return math.ceil((self.max_seconds + 1) * self.sample_rate * self.channels * 2) + 1024**2

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**exact(value, (field.name for field in fields(cls)), 'audio preset'))


@dataclass(frozen=True)
class Selection:
    kind: str
    source: str
    title: str
    preset: AudioPreset
    audio_stream: int = 0

    def __post_init__(self):
        if self.kind not in ('audio', 'video', 'book'):
            raise ValueError('Select local audio, local video, or a saved library Studio project.')
        text(self.source, 'selected local input', 32768)
        text(self.title, 'item title')
        if not isinstance(self.preset, AudioPreset):
            raise ValueError('An explicit typed preset is required for every item.')
        count(self.audio_stream, 'Selected audio stream', 0, 63)
        if self.kind == 'book':
            identity(self.source)
            if self.audio_stream != 0:
                raise ValueError('A Studio book uses its existing programme mix.')
        elif self.preset.split != 'whole':
            raise ValueError('Chapter splitting requires a saved Studio book with explicit chapters.')

    @classmethod
    def from_dict(cls, value):
        exact(value, (field.name for field in fields(cls)), 'selected batch item')
        return cls(**{**value, 'preset': AudioPreset.from_dict(value['preset'])})


@dataclass(frozen=True)
class RuntimeIdentity:
    sha256: str
    version: str
    version_sha256: str

    def __post_init__(self):
        hash_value(self.sha256)
        hash_value(self.version_sha256)
        text(self.version, 'FFmpeg version', 4096)

    @classmethod
    def from_dict(cls, value):
        return cls(**exact(value, (field.name for field in fields(cls)), 'selected runtime'))


@dataclass(frozen=True)
class InputAsset:
    name: str
    file: str
    bytes: int
    sha256: str

    def __post_init__(self):
        filename_component(self.name)
        filename_component(self.file)
        if not self.name.lower().endswith('.wav') or not self.file.endswith('.wav'):
            raise ValueError('Studio inputs must remain their existing retained WAV assets.')
        count(self.bytes, 'Retained asset bytes', 1, MAX_INPUT_BYTES)
        hash_value(self.sha256)

    @classmethod
    def from_dict(cls, value):
        return cls(**exact(value, (field.name for field in fields(cls)), 'retained Studio input'))


@dataclass(frozen=True)
class InputSnapshot:
    file: str
    bytes: int
    sha256: str
    original_name: str
    project_id: str | None = None
    child_document_sha256: str | None = None
    assets: tuple = ()

    def __post_init__(self):
        filename_component(self.file)
        filename_component(self.original_name)
        count(self.bytes, 'Retained input bytes', 1, MAX_INPUT_BYTES)
        hash_value(self.sha256)
        if (self.project_id is None) != (self.child_document_sha256 is None):
            raise ValueError('A saved book needs both project and child-document bindings.')
        if self.project_id is not None:
            identity(self.project_id)
            hash_value(self.child_document_sha256)
        if type(self.assets) is not tuple or len(self.assets) > 4096:
            raise ValueError('The retained Studio asset inventory is not bounded.')
        if any(not isinstance(asset, InputAsset) for asset in self.assets):
            raise ValueError('Studio asset records must be typed.')
        names = [asset.name.casefold() for asset in self.assets]
        if len(names) != len(set(names)) or len({asset.file for asset in self.assets}) != len(self.assets):
            raise ValueError('Retained Studio inputs contain a portable filename collision.')

    @classmethod
    def from_dict(cls, value):
        exact(value, (field.name for field in fields(cls)), 'retained batch input')
        if type(value['assets']) is not list:
            raise ValueError('Retained Studio inputs must be a list.')
        return cls(**{**value, 'assets': tuple(InputAsset.from_dict(x) for x in value['assets'])})


@dataclass(frozen=True)
class BatchItem:
    id: str
    ordinal: int
    kind: str
    title: str
    preset: AudioPreset
    audio_stream: int
    source: InputSnapshot
    outputs: tuple
    state: str = 'pending'
    attempt: int = 0
    error: str | None = None

    def __post_init__(self):
        identity(self.id)
        count(self.ordinal, 'Item position', 1, MAX_ITEMS)
        text(self.title, 'item title')
        if self.kind not in ('audio', 'video', 'book') or self.state not in ITEM_STATES:
            raise ValueError('Invalid audio queue item kind or state.')
        if not isinstance(self.preset, AudioPreset) or not isinstance(self.source, InputSnapshot):
            raise ValueError('An item requires its typed preset and retained source.')
        count(self.audio_stream, 'Selected audio stream', 0, 63)
        count(self.attempt, 'Attempt count', 0, 1000000)
        if self.error is not None:
            text(self.error, 'item error', 2048)
        if self.state != 'failed' and self.error is not None:
            raise ValueError('Only a failed item can retain an error.')
        if type(self.outputs) is not tuple or not 1 <= len(self.outputs) <= MAX_OUTPUTS:
            raise ValueError('The output preview must be a bounded nonempty list.')
        for name in self.outputs:
            filename_component(name)
            if not name.endswith('.' + self.preset.output_format):
                raise ValueError('An output name disagrees with its selected format.')
        if len(set(name.casefold() for name in self.outputs)) != len(self.outputs):
            raise ValueError('Output names collide on a portable filesystem.')
        if output_names(self.id, self.ordinal, self.title, self.preset, len(self.outputs)) != self.outputs:
            raise ValueError('The deterministic output-name preview was modified.')
        if self.kind == 'book':
            if self.source.project_id is None or self.source.file != self.id + '.studio.json' or self.audio_stream != 0:
                raise ValueError('A saved book lost its exact managed-project binding.')
        else:
            demuxers = AUDIO_DEMUXERS if self.kind == 'audio' else VIDEO_DEMUXERS
            suffix = '.' + self.source.file.rsplit('.', 1)[-1]
            if (suffix not in demuxers or self.source.file != self.id + suffix
                    or self.source.project_id is not None or self.source.assets
                    or self.preset.split != 'whole' or len(self.outputs) != 1):
                raise ValueError('A local media input has an invalid reviewed format or source binding.')

    def immutable(self):
        value = asdict(self)
        for name in ('state', 'attempt', 'error'):
            del value[name]
        return value

    @property
    def fingerprint(self):
        return digest(self.immutable())

    @classmethod
    def from_dict(cls, value):
        exact(value, (field.name for field in fields(cls)), 'audio queue item')
        if type(value['outputs']) is not list:
            raise ValueError('The output preview must be a list.')
        return cls(**{**value, 'preset': AudioPreset.from_dict(value['preset']),
                      'source': InputSnapshot.from_dict(value['source']), 'outputs': tuple(value['outputs'])})


def output_names(item_id, ordinal, title, preset, chapters=1):
    identity(item_id)
    count(chapters, 'Chapter count', 1, MAX_OUTPUTS)
    normalized = unicodedata.normalize('NFKC', text(title, 'item title'))
    slug = '-'.join(''.join(c if c.isalnum() else ' ' for c in normalized).split())[:48] or 'audio'
    base = f'{ordinal:04d}-{slug}-{item_id}'
    if preset.split == 'whole':
        return (base + '.' + preset.output_format,)
    return tuple(f'{base}-chapter-{index:04d}.{preset.output_format}' for index in range(1, chapters + 1))


@dataclass(frozen=True)
class BatchState:
    schema: str
    id: str
    title: str
    runtime: RuntimeIdentity
    items: tuple
    plan_sha256: str
    status: str = 'queued'
    control_sequence: int = 0

    def __post_init__(self):
        if self.schema != SCHEMA or self.status not in BATCH_STATES:
            raise ValueError('Unsupported audio batch state.')
        identity(self.id)
        text(self.title, 'batch title')
        if not isinstance(self.runtime, RuntimeIdentity):
            raise ValueError('The selected runtime identity is missing.')
        if (type(self.items) is not tuple or not 1 <= len(self.items) <= MAX_ITEMS
                or any(not isinstance(item, BatchItem) for item in self.items)):
            raise ValueError('The audio queue has an invalid bounded item inventory.')
        if ([item.ordinal for item in self.items] != list(range(1, len(self.items) + 1))
                or len(set(item.id for item in self.items)) != len(self.items)):
            raise ValueError('The audio queue order or identity is ambiguous.')
        if len({name.casefold() for item in self.items for name in item.outputs}) != sum(len(item.outputs) for item in self.items):
            raise ValueError('Audio queue output names collide.')
        count(self.control_sequence, 'Acknowledged control sequence')
        hash_value(self.plan_sha256)
        if self.plan_sha256 != digest(self.plan()):
            raise ValueError('The saved audio plan changed; create a new reviewed queue.')
        if self.status == 'complete' and any(item.state != 'complete' for item in self.items):
            raise ValueError('A batch cannot complete before every item is verified.')
        if len(canonical(self.to_dict())) > MAX_STATE_BYTES:
            raise ValueError('Audio queue metadata exceeds its allowance.')

    def plan(self):
        return {'schema': self.schema, 'id': self.id, 'title': self.title,
                'runtime': asdict(self.runtime), 'items': [item.immutable() for item in self.items]}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def create(cls, identity, title, runtime, items):
        plan = {'schema': SCHEMA, 'id': identity, 'title': title, 'runtime': asdict(runtime),
                'items': [item.immutable() for item in items]}
        return cls(SCHEMA, identity, title, runtime, tuple(items), digest(plan))

    @classmethod
    def from_dict(cls, value):
        exact(value, (field.name for field in fields(cls)), 'audio batch')
        if type(value['items']) is not list:
            raise ValueError('Audio queue items must be a list.')
        return cls(**{**value, 'runtime': RuntimeIdentity.from_dict(value['runtime']),
                      'items': tuple(BatchItem.from_dict(item) for item in value['items'])})


@dataclass(frozen=True)
class BatchControl:
    schema: str = 'carr-audio-batch-control/1'
    batch_id: str = ''
    sequence: int = 0
    action: str = 'continue'

    def __post_init__(self):
        if self.schema != 'carr-audio-batch-control/1' or self.action not in ('continue', 'pause', 'cancel'):
            raise ValueError('Invalid durable audio batch control.')
        identity(self.batch_id)
        count(self.sequence, 'Control sequence')

    @classmethod
    def from_dict(cls, value):
        return cls(**exact(value, (field.name for field in fields(cls)), 'audio batch control'))


def receipt_value(value, batch, item):
    """Validate identity and shape before checking the owned output bytes."""
    exact(value, ('schema', 'batch_id', 'item_id', 'plan_sha256', 'item_sha256',
                  'source_sha256', 'runtime_sha256', 'files', 'render'), 'audio output receipt')
    expected = {'schema': 'carr-audio-batch-receipt/1', 'batch_id': batch.id, 'item_id': item.id,
                'plan_sha256': batch.plan_sha256, 'item_sha256': item.fingerprint,
                'source_sha256': item.source.sha256, 'runtime_sha256': batch.runtime.sha256}
    if any(value[key] != expected_value for key, expected_value in expected.items()):
        raise ValueError('An output receipt belongs to another source, item, preset, or runtime.')
    files = value['files']
    if type(files) is not list or [row.get('name') for row in files if type(row) is dict] != list(item.outputs):
        raise ValueError('The completed output inventory differs from the preview.')
    for row in files:
        exact(row, ('name', 'bytes', 'sha256', 'sample_rate', 'channels', 'frames'), 'verified audio output')
        filename_component(row['name'])
        hash_value(row['sha256'])
        count(row['bytes'], 'Verified output bytes', 1, 32 * 1024**3)
        count(row['frames'], 'Verified decoded frames', 1, math.ceil((item.preset.max_seconds + 1) * item.preset.sample_rate))
        if (type(row['sample_rate']) is not int or type(row['channels']) is not int
                or row['sample_rate'] != item.preset.sample_rate or row['channels'] != item.preset.channels):
            raise ValueError('The verified output differs from its selected rate or channel count.')
    if item.kind == 'book':
        render = exact(value['render'], ('run_key', 'plan_sha256', 'source_sha256'), 'Studio render binding')
        for field in render.values():
            hash_value(field)
    elif value['render'] is not None:
        raise ValueError('Local media output cannot claim a Studio speech render.')
    return value
