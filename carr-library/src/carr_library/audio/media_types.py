"""Typed local-media choices; no command, filter graph, URL or runtime path."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath, PureWindowsPath
import re

from .capture_types import finite, integer
from .models import FORMATS, SAMPLE_RATES

DIRECTORY = 'media-conversion-jobs'
MAX_ITEMS = 1000
MAX_JOBS = 1000
MAX_METADATA_BYTES = 8 * 1024**2
MAX_RECEIPT_BYTES = 16 * 1024
MAX_SNAPSHOT_BYTES = 2**60
MAX_FILES = 100000
CONTAINERS = ('wav', 'flac', 'mp3', 'ogg', 'mov', 'matroska', 'aac', 'aiff', 'asf', 'avi', 'mpeg', 'mpegts')
STATUSES = ('prepared', 'running', 'paused', 'cancelled', 'failed', 'complete')


def text(value, label, maximum=500, *, empty=False):
    if (type(value) is not str or len(value) > maximum or not empty and not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(label + ' must be bounded readable text.')
    value.encode('utf-8')
    return value


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def metadata_reserve(definition):
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError('The prepared media definition exceeds its metadata allowance.')
    return len(encoded) + 65536 + MAX_RECEIPT_BYTES * len(definition['inputs'])


def identity(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Choose an existing media job identity.')
    return value


def checksum(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('Invalid media content checksum.')
    return value


@dataclass(frozen=True)
class ConversionPreset:
    format: str = 'wav'
    sample_rate: int = 48000
    channels: int = 2
    pcm_bits: int = 16
    bitrate_kbps: int = 192
    title: str = ''
    artist: str = ''
    album: str = ''

    def __post_init__(self):
        if self.format not in FORMATS or type(self.format) is not str:
            raise ValueError('Choose one of the existing supported audio formats.')
        if type(self.sample_rate) is not int or self.sample_rate not in SAMPLE_RATES:
            raise ValueError('Choose a supported output sample rate.')
        integer(self.channels, 'Output channels', 1, 2)
        if type(self.pcm_bits) is not int or self.pcm_bits not in (16, 24):
            raise ValueError('Choose 16-bit or 24-bit integer PCM.')
        if self.pcm_bits != 16 and self.format not in ('wav', 'aiff', 'flac'):
            raise ValueError('PCM bit depth applies only to WAV, AIFF or FLAC outputs.')
        integer(self.bitrate_kbps, 'Encoded bitrate', 32, 320)
        if self.format in ('opus', 'wma') and self.sample_rate != 48000:
            raise ValueError('The existing Opus and WMAv2 profiles require 48000 Hz; choose that rate explicitly.')
        for name in ('title', 'artist', 'album'):
            text(getattr(self, name), 'Output ' + name, empty=True)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unsupported media conversion preset.')
        return cls(**value)


@dataclass(frozen=True)
class ConversionAllowance:
    disk_bytes: int = 4 * 1024**3
    timeout_seconds: float = 3600
    threads: int = 2

    def __post_init__(self):
        integer(self.disk_bytes, 'Media job disk allowance', 1024**2, MAX_SNAPSHOT_BYTES)
        finite(self.timeout_seconds, 'Per-item time allowance', .05, 365 * 86400)
        integer(self.threads, 'FFmpeg threads', 1, 64)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unsupported media resource allowance.')
        return cls(**value)


@dataclass(frozen=True)
class AudioStream:
    index: int
    codec: str
    sample_rate: int
    channels: int
    duration_seconds: float | None = None
    language: str = ''
    title: str = ''
    pcm_bits: int = 0

    def __post_init__(self):
        integer(self.index, 'Audio stream index', 0, 65535)
        text(self.codec, 'Codec', 64)
        if re.fullmatch('[a-zA-Z0-9_]+', self.codec) is None:
            raise ValueError('Unsupported codec identity.')
        integer(self.sample_rate, 'Input sample rate', 1000, 768000)
        integer(self.channels, 'Input channel count', 1, 64)
        if self.duration_seconds is not None:
            finite(self.duration_seconds, 'Declared input duration', 0, 365 * 86400)
        text(self.language, 'Stream language tag', 64, empty=True)
        text(self.title, 'Stream title', empty=True)
        integer(self.pcm_bits, 'Reported PCM bit depth', 0, 64)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unsupported media stream description.')
        return cls(**value)


@dataclass(frozen=True)
class MediaInspection:
    path: str
    bytes: int
    sha256: str
    file_identity: tuple
    container: str
    has_video: bool
    streams: tuple
    processor: tuple

    def __post_init__(self):
        text(self.path, 'Selected local media path', 32768)
        integer(self.bytes, 'Selected media bytes', 1, MAX_SNAPSHOT_BYTES)
        checksum(self.sha256)
        if (type(self.file_identity) is not tuple or len(self.file_identity) != 5
                or any(type(v) is not int or v < 0 for v in self.file_identity)
                or self.file_identity[2] != self.bytes):
            raise ValueError('Invalid inspected media file identity.')
        if self.container not in CONTAINERS or type(self.has_video) is not bool:
            raise ValueError('Unsupported selected media container.')
        if (type(self.streams) is not tuple or not 1 <= len(self.streams) <= 64
                or any(type(v) is not AudioStream for v in self.streams)
                or len({v.index for v in self.streams}) != len(self.streams)):
            raise ValueError('Choose a file with a bounded, distinct audio stream inventory.')
        if type(self.processor) is not tuple or len(self.processor) != 2:
            raise ValueError('The inspected media needs its exact installed processor identity.')
        for value in self.processor:
            checksum(value)


@dataclass(frozen=True)
class MediaChoice:
    source: MediaInspection
    stream_index: int
    preset: ConversionPreset = ConversionPreset()

    def __post_init__(self):
        if type(self.source) is not MediaInspection or type(self.preset) is not ConversionPreset:
            raise ValueError('Inspect media and choose a typed output preset before preparing it.')
        integer(self.stream_index, 'Selected audio stream', 0, 65535)
        if self.stream_index not in {stream.index for stream in self.source.streams}:
            raise ValueError('Explicitly choose an inspected audio stream; video/data streams are not audio.')


def validate_definition(value, *, job_id=None):
    fields = {'schema', 'id', 'title', 'created', 'processor', 'allowance', 'inputs'}
    if type(value) is not dict or set(value) != fields or value['schema'] != 'carr-media-conversion/1':
        raise ValueError('Unsupported saved media conversion definition.')
    identity(value['id'])
    if job_id is not None and value['id'] != job_id:
        raise ValueError('The media definition belongs to a different job.')
    text(value['title'], 'Media job title')
    text(value['created'], 'Media job timestamp', 80)
    if type(value['processor']) is not dict or set(value['processor']) != {'ffmpeg_sha256', 'ffprobe_sha256'}:
        raise ValueError('Invalid saved media processor identity.')
    for item in value['processor'].values():
        checksum(item)
    allowance = ConversionAllowance.from_dict(value['allowance'])
    if type(value['inputs']) is not list or not 1 <= len(value['inputs']) <= MAX_ITEMS:
        raise ValueError('A media batch needs between one and 1000 explicitly selected items.')
    total = 0
    for index, row in enumerate(value['inputs'], 1):
        if (type(row) is not dict or set(row) != {'index', 'name', 'origin', 'retained', 'bytes', 'sha256',
                'container', 'has_video', 'streams', 'stream_index', 'preset'}
                or type(row['index']) is not int or row['index'] != index
                or row['retained'] != f'inputs/{index:06d}.media'):
            raise ValueError('Invalid retained media input record.')
        text(row['name'], 'Original filename', 255)
        text(row['origin'], 'Original media location', 32768)
        original = PureWindowsPath(row['origin']) if PureWindowsPath(row['origin']).is_absolute() else PurePosixPath(row['origin'])
        if (not original.is_absolute() or row['origin'].startswith(('\\\\', '//'))
                or '..' in original.parts or original.name != row['name']):
            raise ValueError('Original media provenance must identify the selected local file.')
        integer(row['bytes'], 'Retained input bytes', 1, allowance.disk_bytes)
        total += row['bytes']
        checksum(row['sha256'])
        if row['container'] not in CONTAINERS or type(row['has_video']) is not bool:
            raise ValueError('Invalid retained media format.')
        if type(row['streams']) is not list or not 1 <= len(row['streams']) <= 64:
            raise ValueError('Invalid retained audio stream inventory.')
        streams = tuple(AudioStream.from_dict(item) for item in row['streams'])
        if len({item.index for item in streams}) != len(streams):
            raise ValueError('Duplicate retained audio stream identity.')
        if type(row['stream_index']) is not int or row['stream_index'] not in {item.index for item in streams}:
            raise ValueError('The selected audio stream is outside its retained source inventory.')
        ConversionPreset.from_dict(row['preset'])
    if total + metadata_reserve(value) >= allowance.disk_bytes:
        raise ValueError('The retained inputs leave no space within the selected job allowance.')
    return value


def validate_job(value, definition=None, *, job_id=None):
    if (type(value) is not dict or set(value) != {'schema', 'id', 'definition_sha256', 'status',
            'completed', 'total', 'updated', 'run_id', 'message'} or value['schema'] != 'carr-media-job/1'):
        raise ValueError('Unsupported media conversion checkpoint.')
    identity(value['id'])
    checksum(value['definition_sha256'])
    if job_id is not None and value['id'] != job_id:
        raise ValueError('The media checkpoint belongs to another job.')
    if value['status'] not in STATUSES:
        raise ValueError('Invalid media job state.')
    integer(value['total'], 'Media item count', 1, MAX_ITEMS)
    integer(value['completed'], 'Completed media item count', 0, value['total'])
    if value['status'] == 'complete' and value['completed'] != value['total']:
        raise ValueError('An incomplete media batch cannot be marked complete.')
    if value['run_id'] is not None:
        identity(value['run_id'])
    text(value['updated'], 'Media checkpoint timestamp', 80)
    text(value['message'], 'Media checkpoint status', 500, empty=True)
    if definition is not None and (value['id'] != definition['id']
            or value['definition_sha256'] != digest(definition) or value['total'] != len(definition['inputs'])):
        raise ValueError('The media checkpoint differs from its immutable preparation.')
    return value


def validate_presets(value):
    if (type(value) is not dict or set(value) != {'schema', 'presets'}
            or value['schema'] != 'carr-media-presets/1'
            or type(value['presets']) is not dict or len(value['presets']) > 100):
        raise ValueError('Unsupported named media presets.')
    names = set()
    for name, preset in value['presets'].items():
        text(name, 'Preset name', 80)
        if name.casefold() in names:
            raise ValueError('Saved media preset names must be distinct.')
        names.add(name.casefold())
        ConversionPreset.from_dict(preset)
    return value


def validate_control(value, job_id):
    if (type(value) is not dict or set(value) != {'schema', 'id', 'run_id', 'action'}
            or value['schema'] != 'carr-media-control/1' or value['id'] != job_id
            or value['action'] not in ('pause', 'cancel')):
        raise ValueError('Unsupported media job control.')
    identity(value['run_id'])
    return value


def output_name(row):
    # Index is always included, so equal source basenames cannot collide. The
    # source name is display provenance, never directory authority.
    name = row['name'].rsplit('.', 1)[0]
    name = re.sub(r'[\\/:<>"|?*\x00-\x1f\x7f]', '_', name).strip(' .')[:70] or 'audio'
    return f"{row['index']:06d}-{name}.{row['preset']['format']}"


def validate_receipt(value, definition, row):
    if (type(value) is not dict or set(value) != {'schema', 'job', 'definition_sha256', 'index',
            'source', 'stream_index', 'preset', 'processor', 'output'}
            or value['schema'] != 'carr-media-output/1' or value['job'] != definition['id']
            or value['definition_sha256'] != digest(definition) or value['index'] != row['index']
            or type(value['index']) is not int
            or value['source'] != {'name': row['name'], 'bytes': row['bytes'], 'sha256': row['sha256']}
            or value['stream_index'] != row['stream_index'] or type(value['stream_index']) is not int
            or value['preset'] != row['preset'] or value['processor'] != definition['processor']):
        raise ValueError('A media result differs from its exact reviewed input and settings.')
    if (type(value['source']) is not dict or type(value['source'].get('bytes')) is not int
            or type(value['source'].get('name')) is not str
            or type(value['source'].get('sha256')) is not str):
        raise ValueError('Invalid exact media source receipt.')
    ConversionPreset.from_dict(value['preset'])
    if len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')) > MAX_RECEIPT_BYTES:
        raise ValueError('A media result exceeds its receipt allowance.')
    output = value['output']
    if (type(output) is not dict or set(output) != {'path', 'bytes', 'sha256', 'codec', 'sample_rate',
            'channels', 'pcm_bits', 'decoded_frames'} or output['path'] != output_name(row)):
        raise ValueError('Unsupported verified media output.')
    integer(output['bytes'], 'Output bytes', 1, definition['allowance']['disk_bytes'])
    checksum(output['sha256'])
    preset = ConversionPreset.from_dict(row['preset'])
    codec = {'wav': 'pcm_s%dle' % preset.pcm_bits, 'aiff': 'pcm_s%dbe' % preset.pcm_bits,
             'flac': 'flac', 'mp3': 'mp3', 'ogg': 'vorbis', 'opus': 'opus',
             'm4a': 'aac', 'm4b': 'aac', 'aac': 'aac', 'wma': 'wmav2'}[preset.format]
    if (output['codec'] != codec or type(output['sample_rate']) is not int
            or output['sample_rate'] != preset.sample_rate or type(output['channels']) is not int
            or output['channels'] != preset.channels or type(output['pcm_bits']) is not int
            or output['pcm_bits'] != (preset.pcm_bits if preset.format in ('wav', 'aiff', 'flac') else 0)):
        raise ValueError('The verified output does not have the reviewed encoding.')
    integer(output['decoded_frames'], 'Verified decoded frames', 1, 2**63 - 1)
    return value
