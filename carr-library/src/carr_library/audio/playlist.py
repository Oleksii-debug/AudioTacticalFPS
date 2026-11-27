"""Retained PCM playlists over the existing WavePlayer and typed backup contract.

Adapted from PR152's passive playlist review model. Saved data grants no device,
external path, automatic playback or worker authority. Only explicit review and
Play use the current owned player.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from ..backup_files import local_path, regular, optional_present, sync_directory
from ..durable_state import atomic_json, encode, state_lock
from ..ingest.writer_guard import exclusive_writer
from ..operations import ResourceBroker, ResourceRequest
from .ffmpeg import wav_info
from .jobs import file_identity, publish_new, publish_noreplace, regular_file, verified_reader

DIRECTORY = 'playback-queue'
NAME = DIRECTORY + '/queue.json'
SCHEMA = 'carr-retained-playback-queue/1'
MAX_ITEMS = 256
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_STATE_BYTES = 2 * 1024**2
MAX_REVIEW_SECONDS = 120
_ASSET = re.compile(r'playback-queue/media/([0-9a-f]{64})\.wav\Z')


def _check(cancelled, deadline=None):
    if cancelled():
        raise InterruptedError('Playlist work was cancelled; retained originals are unchanged.')
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('Playlist review exceeded two minutes; select fewer recordings.')


def _independent(path):
    info = regular_file(path)
    if info.st_nlink != 1:
        raise ValueError('Retained playback data and selected inputs must not be hard links.')
    return info


def _label(value):
    if (type(value) is not str or not 1 <= len(value) <= 256 or not value.strip()
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('Use a playlist label of 1 to 256 printable characters.')


@dataclass(frozen=True)
class PlaylistItem:
    path: Path
    identity: tuple
    sha256: str
    frames: int
    sample_rate: int
    channels: int
    label: str

    def __post_init__(self):
        if (not isinstance(self.path, Path) or type(self.identity) is not tuple or len(self.identity) != 5
                or any(type(value) is not int for value in self.identity)
                or not 44 < self.identity[2] <= MAX_FILE_BYTES
                or type(self.sha256) is not str or re.fullmatch('[0-9a-f]{64}', self.sha256) is None
                or type(self.frames) is not int or self.frames <= 0
                or type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 192000
                or type(self.channels) is not int or self.channels not in (1, 2)
                or self.frames * self.channels * 2 > self.identity[2] - 44):
            raise ValueError('Use an exact bounded reviewed PCM identity.')
        _label(self.label)

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate


def _hash_stream(stream, size, cancelled, deadline=None):
    digest, count = sha256(), 0
    while block := stream.read(min(1024 * 1024, size + 1 - count)):
        _check(cancelled, deadline)
        count += len(block)
        if count > size:
            raise ValueError('A playlist input grew during verification.')
        digest.update(block)
    _check(cancelled, deadline)
    if count != size:
        raise ValueError('A playlist input was truncated during verification.')
    return digest.hexdigest()


def prepare_playlist(paths, *, cancelled=lambda: False):
    """Review explicit PCM16 RIFF/RF64 inputs using the existing verified reader."""
    if type(paths) not in (list, tuple) or not 1 <= len(paths) <= MAX_ITEMS:
        raise ValueError('Select from 1 to 256 local PCM WAV or RF64 recordings.')
    deadline, total, result = time.monotonic() + MAX_REVIEW_SECONDS, 0, []
    for selected in paths:
        _check(cancelled, deadline)
        path = local_path(selected, exists=True)
        before = _independent(path)
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError('A playlist recording must be nonempty and at most 4 GiB.')
        total += before.st_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('The playlist exceeds its 32 GiB review budget.')
        _label(path.name)
        identity = file_identity(before)
        with verified_reader(path, expected_identity=identity) as stream:
            info = wav_info(path)
            digest = _hash_stream(stream, before.st_size, cancelled, deadline)
        if file_identity(_independent(path)) != identity:
            raise ValueError('Selected PCM identity changed after review.')
        result.append(PlaylistItem(path, identity, digest, info.frames, info.rate, info.channels, path.name))
    _check(cancelled, deadline)
    return tuple(result)


@dataclass(frozen=True)
class SavedPlaylistItem:
    filename: str
    label: str
    sha256: str
    size_bytes: int
    frames: int
    sample_rate: int
    channels: int

    def __post_init__(self):
        match = _ASSET.fullmatch(self.filename) if type(self.filename) is str else None
        if match is None or type(self.sha256) is not str or match[1] != self.sha256:
            raise ValueError('A saved queue can reference only its exact retained PCM identity.')
        _label(self.label)
        if (type(self.size_bytes) is not int or not 44 < self.size_bytes <= MAX_FILE_BYTES
                or type(self.frames) is not int or not 1 <= self.frames <= MAX_FILE_BYTES
                or type(self.sample_rate) is not int or not 8000 <= self.sample_rate <= 192000
                or type(self.channels) is not int or self.channels not in (1, 2)
                or self.frames * self.channels * 2 > self.size_bytes - 44):
            raise ValueError('Invalid retained PCM dimensions or byte budget.')

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unknown or missing saved playlist item fields.')
        return cls(**value)

    @classmethod
    def reviewed(cls, root, item):
        if type(item) is not PlaylistItem:
            raise ValueError('Use the actual reviewed retained PCM item.')
        path = local_path(item.path, root=root, exists=True)
        info = _independent(path)
        if file_identity(info) != item.identity:
            raise ValueError('Retained playlist media changed after review.')
        return cls(path.relative_to(root).as_posix(), item.label, item.sha256,
                   info.st_size, item.frames, item.sample_rate, item.channels)


@dataclass(frozen=True)
class SavedPlaylist:
    items: tuple[SavedPlaylistItem, ...] = ()
    index: int | None = None
    frame: int = 0
    skips: tuple[float, float] = (10, 30)
    accuracy: str = 'manual'

    def __post_init__(self):
        if (type(self.items) is not tuple or len(self.items) > MAX_ITEMS
                or any(type(item) is not SavedPlaylistItem for item in self.items)
                or sum(item.size_bytes for item in self.items) > MAX_TOTAL_BYTES):
            raise ValueError('Invalid saved playlist order or byte budget.')
        if self.index is not None and (type(self.index) is not int or not 0 <= self.index < len(self.items)):
            raise ValueError('Saved selection is outside the playlist.')
        if (type(self.frame) is not int or self.frame < 0
                or self.index is None and self.frame != 0
                or self.index is not None and self.frame > self.items[self.index].frames):
            raise ValueError('Saved cursor is outside its exact original PCM frames.')
        if self.accuracy not in ('manual', 'driver_reported', 'completed_buffers'):
            raise ValueError('Saved cursor must be manual or reported by the actual PCM owner.')
        if (type(self.skips) is not tuple or len(self.skips) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value)
                       or not .1 <= value <= 600 for value in self.skips)):
            raise ValueError('Playlist skips must be finite values from 0.1 to 600 seconds.')

    def to_dict(self):
        return dict(schema=SCHEMA, items=[asdict(item) for item in self.items],
                    index=self.index, frame=self.frame, skips=list(self.skips), accuracy=self.accuracy)

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'items', 'index', 'frame', 'skips', 'accuracy'}
                or value['schema'] != SCHEMA or type(value['items']) is not list
                or len(value['items']) > MAX_ITEMS or type(value['skips']) is not list):
            raise ValueError('Unsupported saved playback queue; existing bytes were preserved.')
        return cls(tuple(SavedPlaylistItem.from_dict(row) for row in value['items']),
                   value['index'], value['frame'], tuple(value['skips']), value['accuracy'])

    def review(self, root, *, cancelled=lambda: False):
        """Open only confined retained media after an explicit review action."""
        root = local_path(root, exists=True)
        if not self.items:
            return ()
        checked = prepare_playlist(tuple(local_path(root / row.filename, root=root, exists=True)
                                         for row in self.items), cancelled=cancelled)
        result = []
        for saved, item in zip(self.items, checked):
            item = replace(item, label=saved.label)
            if SavedPlaylistItem.reviewed(root, item) != saved:
                raise ValueError('Saved playlist media changed; it was not admitted.')
            result.append(item)
        return tuple(result)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate saved playlist field.')
        result[key] = value
    return result


def decode_playlist(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_STATE_BYTES:
        raise ValueError('Saved playlist exceeds its bounded state allowance.')
    return SavedPlaylist.from_dict(json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite playlist value.'))))


def _load(path):
    if not optional_present(path):
        return None, None
    info = _independent(path)
    if not 0 < info.st_size <= MAX_STATE_BYTES:
        raise ValueError('Saved playlist exceeds its bounded state allowance.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) != info.st_size:
            raise ValueError('The saved playlist changed during reading.')
    return decode_playlist(raw), sha256(raw).hexdigest()


@contextmanager
def snapshot_guard(root):
    """Borrow the outer application writer; use the same namespace lock as saving."""
    base = local_path(Path(root) / DIRECTORY, root=root)
    if not optional_present(base):
        yield
        return
    regular(base, directory=True)
    with state_lock(base, 'queue'):
        yield


def state_paths(root, *, cancelled=lambda: False):
    root = local_path(root, exists=True)
    base = root / DIRECTORY
    if not optional_present(base):
        return ()
    local_path(base, root=root, exists=True)
    regular(base, directory=True)
    result, count, total, media_count = [], 0, 0, 0
    with os.scandir(base) as children:
        top = []
        for entry in children:
            if len(top) >= MAX_ITEMS + 3:
                raise ValueError('Unknown playback queue namespace.')
            top.append(Path(entry.path))
    for path in top:
        _check(cancelled)
        if path.name == '.queue.lock':
            _independent(path)
            continue
        if re.fullmatch(r'\.pending-[A-Za-z0-9_-]{8}', path.name):
            if _independent(path).st_size > MAX_STATE_BYTES:
                raise ValueError('Interrupted playlist state exceeds its allowance.')
            continue
        if path.name == 'queue.json':
            info = _independent(path)
            if not 0 < info.st_size <= MAX_STATE_BYTES:
                raise ValueError('Saved playlist exceeds its state allowance.')
            result.append(path)
            continue
        if path.name != 'media':
            raise ValueError('Unknown playback queue member.')
        regular(path, directory=True)
        with os.scandir(path) as media:
            for entry in media:
                _check(cancelled)
                count += 1
                if count > MAX_ITEMS * 2:
                    raise ValueError('Too many retained playback media entries.')
                selected = Path(entry.path)
                info = _independent(selected)
                total += info.st_size
                if not 0 <= info.st_size <= MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise ValueError('Retained playlist media exceeds its byte allowance.')
                if re.fullmatch(r'\.(?:publish-[A-Za-z0-9_-]{8}|capture-[0-9a-f]{32})', entry.name):
                    continue  # A private interrupted copy is not a completed retained item.
                if _ASSET.fullmatch(selected.relative_to(root).as_posix()) is None:
                    raise ValueError('Unknown retained playback media.')
                media_count += 1
                if not 0 < info.st_size <= MAX_FILE_BYTES or media_count > MAX_ITEMS:
                    raise ValueError('Retained playlist media exceeds its count or byte allowance.')
                result.append(selected)
    return tuple(sorted(result))


def validate_state_tree(root, *, cancelled=lambda: False):
    """Authenticate the producer's actual PCM and the passive queue references."""
    root = local_path(root, exists=True)
    inventory = state_paths(root, cancelled=cancelled)
    expected = {}
    deadline = None  # Complete-backup caller supplies its own cancellation/budget.
    for path in inventory:
        _check(cancelled)
        if path.name == 'queue.json':
            continue
        info = _independent(path)
        with verified_reader(path, expected_identity=file_identity(info)) as stream:
            wave = wav_info(path)
            digest = _hash_stream(stream, info.st_size, cancelled, deadline)
        if digest != path.stem:
            raise ValueError('Retained playback media hash differs from its exact identity.')
        expected[path.relative_to(root).as_posix()] = (digest, info.st_size, wave.frames, wave.rate, wave.channels)
    saved, _ = _load(root / NAME)
    if saved is not None:
        for row in saved.items:
            if expected.get(row.filename) != (row.sha256, row.size_bytes, row.frames, row.sample_rate, row.channels):
                raise ValueError('Saved playlist references missing or changed retained audio.')
    return saved


def is_state_path(name):
    return name == NAME or type(name) is str and _ASSET.fullmatch(name) is not None


def state_limit(name):
    if name == NAME:
        return MAX_STATE_BYTES
    if type(name) is str and _ASSET.fullmatch(name):
        return MAX_FILE_BYTES
    raise ValueError('Unknown playback queue state file.')



def _recover_private_stages(root, *, cancelled):
    """Caller holds writer and namespace lock; only unpublished owned files qualify."""
    state_paths(root, cancelled=cancelled)  # Refuse unknown or aliased members first.
    base = Path(root) / DIRECTORY
    changed = []
    for directory, pattern in ((base, r'\.pending-[A-Za-z0-9_-]{8}'),
                               (base / 'media', r'\.(?:publish-[A-Za-z0-9_-]{8}|capture-[0-9a-f]{32})')):
        if not optional_present(directory):
            continue
        with os.scandir(directory) as entries:
            paths = []
            for entry in entries:
                if len(paths) >= MAX_ITEMS * 2 + 3:
                    raise ValueError('Interrupted playlist staging exceeds its bounded inventory.')
                paths.append(Path(entry.path))
        for path in paths:
            _check(cancelled)
            if re.fullmatch(pattern, path.name):
                _independent(path)
                path.unlink()
                if directory not in changed:
                    changed.append(directory)
    for directory in changed:
        sync_directory(directory)


def retain_playlist(root, items, *, cancelled=lambda: False):
    """Publish independent verified PCM copies through the existing audio publisher."""
    root = local_path(root, exists=True)
    root_info = regular(root, directory=True)
    root_identity = root_info.st_dev, root_info.st_ino
    if (type(items) not in (tuple, list) or not 1 <= len(items) <= MAX_ITEMS
            or any(type(item) is not PlaylistItem for item in items)
            or sum(item.identity[2] for item in items) > MAX_TOTAL_BYTES):
        raise ValueError('Retain an explicit bounded reviewed playlist.')
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def stopped():
        info = regular(local_path(root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != root_identity:
            raise ValueError('The library changed during playlist retention.')
        _check(cancelled, deadline)
        return False
    request = ResourceRequest(cpu_slots=1, memory_bytes=4 * 1024**2, disk_path=root,
                              disk_bytes=sum(item.identity[2] for item in items) + 16 * 1024**2)
    broker = ResourceBroker.for_app(root, cancelled=stopped)
    with broker.reserve(request, operation_id='document:' + uuid4().hex, cancelled=stopped, timeout=60) as lease:
        def check():
            stopped()
            lease.checkpoint()
            return False
        with exclusive_writer(root):
            base = local_path(root / DIRECTORY, root=root)
            base.mkdir(mode=0o700, exist_ok=True)
            with snapshot_guard(root):
                media = local_path(base / 'media', root=root)
                media.mkdir(mode=0o700, exist_ok=True)
                _recover_private_stages(root, cancelled=check)
                current = state_paths(root, cancelled=check)
                retained = {path.name: _independent(path).st_size for path in current if path.parent == media}
                total = sum(retained.values())
                result = []
                for item in items:
                    check()
                    target = media / (item.sha256 + '.wav')
                    if file_identity(_independent(item.path)) != item.identity:
                        raise ValueError('A selected playlist source changed after its review.')
                    if not optional_present(target):
                        if len(retained) >= MAX_ITEMS or total + item.identity[2] > MAX_TOTAL_BYTES:
                            raise ValueError('The retained playback media allowance is full.')
                        stage = media / ('.capture-' + uuid4().hex)
                        try:
                            publish_new(item.path, stage, cancelled=check, expected_identity=item.identity)
                            captured, = prepare_playlist((stage,), cancelled=check)
                            if (captured.sha256, captured.frames, captured.sample_rate, captured.channels) != (
                                    item.sha256, item.frames, item.sample_rate, item.channels):
                                raise ValueError('Selected PCM changed during independent capture.')
                            publish_noreplace(stage, target, cancelled=check)
                        finally:
                            stage.unlink(missing_ok=True)
                        retained[target.name] = item.identity[2]
                        total += item.identity[2]
                    actual, = prepare_playlist((target,), cancelled=check)
                    if (actual.sha256, actual.frames, actual.sample_rate, actual.channels) != (
                            item.sha256, item.frames, item.sample_rate, item.channels):
                        raise ValueError('A retained playlist copy differs from its reviewed original.')
                    result.append(replace(actual, label=item.label))
                check()
                sync_directory(media)
                sync_directory(base)
                return tuple(result)


@dataclass(frozen=True)
class SaveReceipt:
    token: str
    saved: SavedPlaylist
    confirmed: bool = True
    warning: str | None = None


class PlaylistStore:
    """One CAS snapshot under the same writer/namespace order as complete backup."""
    def __init__(self, root):
        self.root = local_path(root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / NAME

    def guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The playback library folder changed.')
        local_path(self.path, root=self.root)

    def load(self):
        self.guard()
        value = _load(self.path)
        self.guard()
        return value

    def save(self, value, *, expected_token, cancelled=lambda: False):
        if type(value) is not SavedPlaylist:
            raise ValueError('Use a validated saved playlist.')
        raw = encode(value.to_dict())
        decode_playlist(raw)
        expected = sha256(raw).hexdigest()
        self.guard()
        with exclusive_writer(self.root):
            base = local_path(self.root / DIRECTORY, root=self.root)
            base.mkdir(mode=0o700, exist_ok=True)
            with snapshot_guard(self.root):
                self.guard()
                _recover_private_stages(self.root, cancelled=cancelled)
                _, token = self.load()
                if token != expected_token:
                    raise ValueError('Another window changed the saved queue; reload before replacing it.')
                value.review(self.root, cancelled=cancelled)
                _check(cancelled)
                self.guard()
                try:
                    atomic_json(self.path, value.to_dict(), replace=token is not None)
                except Exception:
                    # Publication can commit before a final directory flush fails.
                    # Preserve the acknowledged durable data and make retry explicit.
                    actual, actual_token = self.load()
                    if actual == value and actual_token == expected:
                        return SaveReceipt(expected, value, False, 'The queue was written; retry Save to confirm the final directory flush.')
                    raise
                actual, actual_token = self.load()
                if actual != value or actual_token != expected:
                    raise ValueError('The saved playlist could not be confirmed after publication.')
                self.guard()
                return SaveReceipt(expected, value)
