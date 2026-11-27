"""Bounded playback positions, never filesystem or quotation capabilities.

Only an already authorized source range or an owned Studio project can name a
position. Reopening requires a freshly prepared WAV with the same byte digest.
No executable, external path, voice credential or inferred text timing is saved.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from ..ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer
from ..reader.reading_state import ReadingLocation, _location, _unique
from .ffmpeg import wav_info
from .jobs import regular_file

FILENAME = 'playback-resume.json'
MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 2000
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def _hash(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError('Invalid playback content identity.')
    return value


def _number(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError('Invalid playback position or audio format.')
    return value


@dataclass(frozen=True)
class PlaybackOwner:
    kind: str
    source: ReadingLocation | None = None
    start: int | None = None
    end: int | None = None
    project: str | None = None
    revision: str | None = None

    def __post_init__(self):
        if self.kind == 'source':
            if type(self.source) is not ReadingLocation or self.project is not None or self.revision is not None:
                raise ValueError('Playback needs an exact source location.')
            _number(self.start, 0, 64 * 1024 * 1024)
            _number(self.end, self.start + 1, 64 * 1024 * 1024)
        elif self.kind == 'project':
            if (self.source is not None or self.start is not None or self.end is not None
                    or not isinstance(self.project, str) or not re.fullmatch('[0-9a-f]{32}', self.project)):
                raise ValueError('Playback needs an owned Studio project identity.')
            _hash(self.revision)
        else:
            raise ValueError('Unsupported playback owner.')

    def to_dict(self):
        if self.kind == 'source':
            return dict(kind=self.kind, source=asdict(self.source), start=self.start, end=self.end)
        return dict(kind=self.kind, project=self.project, revision=self.revision)

    @property
    def key(self):
        return sha256(_encode(self.to_dict())).hexdigest()


def owner_from_dict(value):
    if type(value) is not dict:
        raise ValueError('Invalid playback owner.')
    if set(value) == {'kind', 'source', 'start', 'end'} and value['kind'] == 'source':
        return PlaybackOwner('source', source=_location(value['source']), start=value['start'], end=value['end'])
    if set(value) == {'kind', 'project', 'revision'} and value['kind'] == 'project':
        return PlaybackOwner(**value)
    raise ValueError('Invalid playback owner fields.')


@dataclass(frozen=True)
class AudioIdentity:
    sha256: str
    frames: int
    rate: int
    channels: int

    def __post_init__(self):
        _hash(self.sha256)
        _number(self.frames, 1, 2**63 - 1)
        _number(self.rate, 1000, 384000)
        _number(self.channels, 1, 64)


@dataclass(frozen=True)
class ResumePoint:
    owner: PlaybackOwner
    audio: AudioIdentity
    frame: int

    def __post_init__(self):
        if type(self.owner) is not PlaybackOwner or type(self.audio) is not AudioIdentity:
            raise ValueError('Invalid playback resume binding.')
        _number(self.frame, 0, self.audio.frames)

    def to_dict(self):
        return dict(owner=self.owner.to_dict(), audio=asdict(self.audio), frame=self.frame)


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')) + '\n').encode('utf-8')


def validate_bytes(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Playback resume exceeds its byte budget.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number.')))
        if (type(value) is not dict or set(value) != {'kind', 'version', 'entries'}
                or value['kind'] != 'playback-resume' or type(value['version']) is not int
                or value['version'] != 1 or type(value['entries']) is not list or len(value['entries']) > MAX_ENTRIES):
            raise ValueError('Unsupported playback resume document.')
        rows, keys = [], set()
        for row in value['entries']:
            if type(row) is not dict or set(row) != {'owner', 'audio', 'frame'}:
                raise ValueError('Invalid playback resume entry.')
            if type(row['audio']) is not dict or set(row['audio']) != {'sha256', 'frames', 'rate', 'channels'}:
                raise ValueError('Invalid playback audio identity.')
            point = ResumePoint(owner_from_dict(row['owner']), AudioIdentity(**row['audio']), row['frame'])
            if point.owner.key in keys:
                raise ValueError('Duplicate playback resume owner.')
            keys.add(point.owner.key)
            rows.append(point)
        return tuple(rows)
    except (UnicodeError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError('Invalid playback resume data.') from exc


def validate_inventory(raw, names):
    """A restored project position remains inert until fresh audio verification."""
    names = set(names)
    for point in validate_bytes(raw):
        if point.owner.kind == 'project' and f'audio-projects/{point.owner.project}/studio/project.json' not in names:
            raise ValueError('Playback resume refers to an absent owned Studio project.')


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _check(guard, cancelled):
    guard()
    if cancelled():
        raise InterruptedError('Playback preparation cancelled.')


@dataclass(frozen=True)
class PreparedAudio:
    path: Path
    identity: tuple
    audio: AudioIdentity
    owner: PlaybackOwner | None

    def check(self):
        if _identity(regular_file(self.path)) != self.identity:
            raise ValueError('The recording changed. Prepare its current version again.')


def prepare_audio(path, *, owner=None, guard=lambda: None, cancelled=lambda: False):
    if owner is not None and type(owner) is not PlaybackOwner:
        raise ValueError('Invalid playback owner.')
    _check(guard, cancelled)
    path = Path(path).absolute()
    before = _identity(regular_file(path))
    info = wav_info(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb', buffering=0) as stream:
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError('The recording changed while opening it.')
        def read_digest():
            digest, remaining = sha256(), before[2]
            while True:
                block = stream.read(min(1024 * 1024, remaining + 1))
                _check(guard, cancelled)
                if not block:
                    break
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError('The recording grew while verifying it.')
                digest.update(block)
            if remaining or _identity(os.fstat(stream.fileno())) != before:
                raise ValueError('The recording changed while reading it.')
            return digest.hexdigest()
        checksum = read_digest()
        stream.seek(0)
        if read_digest() != checksum:
            raise ValueError('The recording content changed while verifying it.')
    _check(guard, cancelled)
    result = PreparedAudio(path, before, AudioIdentity(checksum, info.frames, info.rate, info.channels), owner)
    result.check()
    return result


class ResumeStore:
    def __init__(self, root):
        self.root = _private_root(Path(root).absolute())
        info = self.root.stat()
        self._root_identity = info.st_dev, info.st_ino

    def check(self):
        root = _private_root(self.root)
        info = root.stat()
        if (info.st_dev, info.st_ino) != self._root_identity:
            raise ValueError('The playback data folder changed. Open the player again.')

    def _read(self):
        self.check()
        path = self.root / FILENAME
        _regular_if_present(path)
        if not path.exists():
            return (), None
        before = _identity(path.stat())
        if not 0 < before[2] <= MAX_BYTES:
            raise ValueError('Playback resume exceeds its byte budget.')
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            if _identity(os.fstat(stream.fileno())) != before:
                raise ValueError('Playback resume changed while opening.')
            raw = stream.read(MAX_BYTES + 1)
            if _identity(os.fstat(stream.fileno())) != before:
                raise ValueError('Playback resume changed while reading.')
        _regular_if_present(path)
        if _identity(path.stat()) != before:
            raise ValueError('Playback resume changed while reading.')
        self.check()
        return validate_bytes(raw), before

    def position(self, owner, audio):
        rows, _ = self._read()
        point = next((row for row in rows if row.owner == owner and row.audio == audio), None)
        return None if point is None else point.frame

    def remember(self, owner, audio, frame, *, guard=lambda: None, cancelled=lambda: False):
        point = ResumePoint(owner, audio, frame)
        with exclusive_writer(self.root):
            _check(guard, cancelled)
            rows, before = self._read()
            rows = tuple(row for row in rows if row.owner.key != owner.key) + (point,)
            raw = _encode(dict(kind='playback-resume', version=1, entries=[row.to_dict() for row in rows]))
            validate_bytes(raw)
            descriptor, name = tempfile.mkstemp(prefix='.playback-resume-', suffix='.tmp', dir=self.root)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check(guard, cancelled)
                self.check()
                path = self.root / FILENAME
                _regular_if_present(path)
                now = _identity(path.stat()) if path.exists() else None
                if now != before:
                    raise ValueError('Playback resume changed before saving.')
                os.replace(name, path)
                if os.name == 'posix':
                    fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
