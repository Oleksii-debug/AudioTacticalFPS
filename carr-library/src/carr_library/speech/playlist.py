"""Bounded PCM review and passive queue recovery; never autoplay.

Live PlaylistItems are process-local review results. SavedPlaylist contains
only relative retained-media identities and inert settings. Loading or backing
it up grants no playback, external-file, output-device or source authority.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import wave

from ..durable_state import atomic_json, encode, local_path, regular, state_lock, _open_json_file
from ._io import local_file

MAX_ITEMS = 256
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_REVIEW_SECONDS = 120
MAX_STATE_BYTES = 2 * 1024**2
NAME = 'state/speech/playback-queue.json'
SCHEMA = 'carr-playback-queue/2'
LEGACY_SCHEMA = 'carr-playback-queue/1'
_ASSET = re.compile(r'state/studio/inputs/([0-9a-f]{64})\.audio\Z')


@dataclass(frozen=True)
class PlaylistItem:
    path: Path
    identity: tuple
    sha256: str
    frames: int
    sample_rate: int
    channels: int
    sample_width: int
    label: str = ''

    def __post_init__(self):
        if (not isinstance(self.path, Path) or type(self.identity) is not tuple
                or len(self.identity) != 5 or any(type(value) is not int for value in self.identity)
                or not 1 <= self.identity[2] <= MAX_FILE_BYTES
                or type(self.sha256) is not str or re.fullmatch('[0-9a-f]{64}', self.sha256) is None
                or type(self.frames) is not int or self.frames <= 0
                or type(self.sample_rate) is not int or not 1 <= self.sample_rate <= 2**32 - 1
                or type(self.channels) is not int or self.channels not in (1, 2)
                or type(self.sample_width) is not int or self.sample_width not in (1, 2, 3, 4)
                or self.frames * self.channels * self.sample_width > self.identity[2] - 44):
            raise ValueError('Invalid reviewed playlist PCM identity.')
        if self.label:
            _label(self.label)
        elif type(self.label) is not str:
            raise ValueError('Invalid playlist label.')

    @property
    def display_name(self):
        return self.label or self.path.name

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _ReviewHeader:
    """Check cooperative limits even while wave scans optional RIFF chunks."""
    def __init__(self, stream, size, check):
        self.stream, self.size, self.check = stream, size, check

    def read(self, count):
        self.check()
        if type(count) is not int or not 0 <= count <= 65536:
            raise ValueError('The WAV header requested an unsupported read.')
        return self.stream.read(min(count, self.size - self.stream.tell() + 1))

    def seek(self, offset, whence=0):
        self.check()
        if type(offset) is not int or whence not in (0, 1, 2):
            raise ValueError('The WAV header requested an invalid position.')
        target = offset + (self.stream.tell() if whence == 1 else self.size if whence == 2 else 0)
        if not 0 <= target <= self.size:
            raise ValueError('The WAV header points outside the selected recording.')
        return self.stream.seek(target)

    def tell(self):
        self.check()
        return self.stream.tell()


def prepare_playlist(paths, *, cancelled=lambda: False):
    """Review at most 256 user-selected local PCM files with bounded memory."""
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_ITEMS:
        raise ValueError(f'Choose from 1 to {MAX_ITEMS} local PCM WAV files.')
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def check():
        if cancelled():
            raise InterruptedError('Playlist review was cancelled; no audio was started.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Playlist review exceeded two minutes. Choose fewer files.')
    if not callable(cancelled):
        raise ValueError('Playlist review needs a cancellation callback.')
    from .listening import _opened
    result, total = [], 0
    for path in paths:
        check()
        path = local_file(path)
        before = path.stat()
        identity = _stamp(before)
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError('A playlist recording must be nonempty and at most 4 GiB.')
        total += before.st_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('The selected playlist exceeds its 32 GiB review budget.')
        with _opened(path, identity) as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _stamp(opened) != identity or _stamp(local_file(path).stat()) != identity):
                raise ValueError('A selected playlist file changed before it could be reviewed.')
            with wave.open(_ReviewHeader(stream, before.st_size, check), 'rb') as audio:
                frames, rate = audio.getnframes(), audio.getframerate()
                channels, width = audio.getnchannels(), audio.getsampwidth()
                if (audio.getcomptype() != 'NONE' or frames <= 0 or rate <= 0
                        or channels not in (1, 2) or width not in (1, 2, 3, 4)
                        or frames * channels * width > before.st_size - stream.tell()):
                    raise ValueError('Playlists require complete nonempty mono or stereo PCM WAV files.')
            stream.seek(0)
            digest = sha256()
            remaining = before.st_size
            while remaining:
                check()
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('A selected playlist file was truncated during review.')
                digest.update(block)
                remaining -= len(block)
            check()
            if (stream.read(1) or _stamp(os.fstat(stream.fileno())) != identity
                    or _stamp(local_file(path).stat()) != identity):
                raise ValueError('A selected playlist file changed during review.')
        result.append(PlaylistItem(path, identity, digest.hexdigest(), frames, rate, channels, width))
    check()
    return tuple(result)


def retain_playlist(library_root, items, *, retain, cancelled=lambda: False):
    """Reuse the current Studio editor's owned, verified retention operation."""
    root = local_path(library_root, exists=True)
    root_identity = _stamp(regular(root, directory=True))[:2]
    inputs = local_path(root / 'state' / 'studio' / 'inputs', root=root, exists=True)
    if (not callable(retain) or not callable(cancelled)
            or not isinstance(items, (tuple, list)) or not 1 <= len(items) <= MAX_ITEMS
            or any(type(item) is not PlaylistItem for item in items)
            or sum(item.identity[2] for item in items) > MAX_TOTAL_BYTES):
        raise ValueError('Retain only explicitly reviewed playlist entries through the current Studio owner.')
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def stopped():
        if _stamp(regular(local_path(root, exists=True), directory=True))[:2] != root_identity:
            raise ValueError('The library folder changed during playlist retention.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Playlist retention exceeded two minutes. Choose fewer files.')
        return cancelled()
    result = []
    for item in items:
        if stopped():
            raise InterruptedError('Playlist retention was cancelled.')
        if _stamp(local_file(item.path).stat()) != item.identity:
            raise ValueError('A reviewed playlist source changed before retention.')
        target = retain(item.path, expected_sha256=item.sha256, cancelled=stopped)
        target = local_path(target, root=inputs, exists=True)
        if target.parent != inputs or target.name != item.sha256 + '.audio':
            raise ValueError('Studio retention returned a different media identity.')
        reviewed, = prepare_playlist((target,), cancelled=stopped)
        if reviewed.sha256 != item.sha256:
            raise ValueError('Retained playlist audio differs from its reviewed source.')
        label = item.display_name
        _label(label)
        result.append(replace(reviewed, label=label))
    if stopped():
        raise InterruptedError('Playlist retention was cancelled.')
    return tuple(result)


def _label(value):
    if (type(value) is not str or not value.strip() or len(value) > 256
            or any(ord(char) < 32 or 127 <= ord(char) < 160
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError('Use a playlist label of 1 to 256 printable characters.')


@dataclass(frozen=True)
class SavedPlaylistItem:
    filename: str
    label: str
    sha256: str
    size_bytes: int
    frames: int
    sample_rate: int
    channels: int
    sample_width: int

    def __post_init__(self):
        match = _ASSET.fullmatch(self.filename) if type(self.filename) is str else None
        if match is None or type(self.sha256) is not str or match[1] != self.sha256:
            raise ValueError('A saved playlist can reference only its exact retained audio identity.')
        _label(self.label)
        if (type(self.size_bytes) is not int or not 1 <= self.size_bytes <= MAX_FILE_BYTES
                or type(self.frames) is not int or not 1 <= self.frames <= MAX_FILE_BYTES
                or type(self.sample_rate) is not int or not 1 <= self.sample_rate <= 2**32 - 1
                or type(self.channels) is not int or self.channels not in (1, 2)
                or type(self.sample_width) is not int or self.sample_width not in (1, 2, 3, 4)
                or self.frames * self.channels * self.sample_width > self.size_bytes - 44):
            raise ValueError('Invalid saved PCM dimensions or byte budget.')

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
            raise ValueError('Unknown or missing saved playlist item fields.')
        return cls(**value)

    @classmethod
    def reviewed(cls, root, item):
        path = local_path(item.path, root=root, exists=True)
        info = regular(path)
        if _stamp(info) != item.identity:
            raise ValueError('A retained playlist file changed after review.')
        return cls(path.relative_to(root).as_posix(), item.display_name, item.sha256,
                   info.st_size, item.frames, item.sample_rate, item.channels, item.sample_width)


def _legacy_output(value):
    """Parse the former passive shape without granting current device authority."""
    fields = {'index', 'name', 'interface', 'manufacturer', 'product', 'driver', 'channels'}
    if type(value) is not dict or set(value) != fields:
        raise ValueError('Invalid legacy playback output declaration.')
    if type(value['index']) is not int or not 0 <= value['index'] < 128:
        raise ValueError('Invalid legacy output index.')
    for key, maximum in (('name', 32), ('interface', 32768)):
        text = value[key]
        if (type(text) is not str or not text or len(text) > maximum
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text)):
            raise ValueError('Invalid legacy output identity.')
    for key, maximum in (('manufacturer', 65535), ('product', 65535),
                         ('driver', 2**32 - 1), ('channels', 65535)):
        if type(value[key]) is not int or not 0 <= value[key] <= maximum:
            raise ValueError('Invalid legacy output capabilities.')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate playlist state key.')
        result[key] = value
    return result


def _read_snapshot(path):
    """Hash and parse the same bounded, ordinary captured state bytes."""
    path = local_path(path, exists=True)
    before = regular(path)
    parent = _stamp(regular(path.parent, directory=True))[:2]
    if not 0 < before.st_size <= MAX_STATE_BYTES:
        raise ValueError('The saved playlist exceeds its state byte budget.')
    raw = bytearray()
    with _open_json_file(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or _stamp(opened) != _stamp(before)):
            raise ValueError('The saved queue changed before its bytes were captured.')
        local_path(path, exists=True)
        while True:
            block = stream.read(min(65536, before.st_size - len(raw) + 1))
            if not block:
                break
            raw.extend(block)
            if len(raw) > before.st_size:
                raise ValueError('The saved queue grew while being captured.')
        if (len(raw) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(regular(local_path(path, exists=True))) != _stamp(before)
                or _stamp(regular(path.parent, directory=True))[:2] != parent):
            raise ValueError('The saved queue changed while being captured.')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite playlist state.')))
    return value, sha256(raw).hexdigest()


@dataclass(frozen=True)
class SavedPlaylist:
    items: tuple[SavedPlaylistItem, ...] = ()
    index: int | None = None
    frame: int = 0
    volume: float = 100
    muted: bool = False
    balance: float = 0
    skips: tuple[float, float] = (10, 30)
    output: object = None
    schema: str = SCHEMA

    def __post_init__(self):
        if (type(self.items) is not tuple or len(self.items) > MAX_ITEMS
                or any(not isinstance(item, SavedPlaylistItem) for item in self.items)
                or sum(item.size_bytes for item in self.items) > MAX_TOTAL_BYTES):
            raise ValueError('Invalid saved playlist order or total byte budget.')
        if (self.index is not None and
                (type(self.index) is not int or not 0 <= self.index < len(self.items))):
            raise ValueError('The saved playlist selection is outside its order.')
        if (type(self.frame) is not int or self.frame < 0
                or self.index is None and self.frame != 0
                or self.index is not None and self.frame > self.items[self.index].frames):
            raise ValueError('The saved playback cursor is outside its exact original PCM frames.')
        if (type(self.volume) not in (float, int) or not 0 <= self.volume <= 100
                or type(self.muted) is not bool
                or type(self.balance) not in (float, int) or not -1 <= self.balance <= 1):
            raise ValueError('Invalid saved volume, mute or balance settings.')
        if self.schema not in (SCHEMA, LEGACY_SCHEMA):
            raise ValueError('Unsupported saved playback queue schema.')
        minimum, maximum = (.1, 600) if self.schema == LEGACY_SCHEMA else (1, 3600)
        if (type(self.skips) is not tuple or len(self.skips) != 2
                or any(type(value) not in (float, int) or not minimum <= value <= maximum
                       for value in self.skips)):
            raise ValueError(f'Saved skips must be between {minimum} and {maximum} seconds.')
        if self.output is not None:
            if self.schema == LEGACY_SCHEMA:
                _legacy_output(self.output)
            else:
                from ..audio.output_devices import OutputDevice
                if (type(self.output) is not dict or
                        set(self.output) != set(OutputDevice.__dataclass_fields__)):
                    raise ValueError('Invalid passive playback output declaration.')
                OutputDevice(**self.output)

    def to_dict(self):
        return {'schema': self.schema, 'items': [asdict(item) for item in self.items],
                'index': self.index, 'frame': self.frame,
                'volume': self.volume, 'muted': self.muted, 'balance': self.balance,
                'skips': list(self.skips), 'output': None if self.output is None else dict(self.output)}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) !=
                {'schema', 'items', 'index', 'frame', 'volume', 'muted', 'balance', 'skips', 'output'}
                or value['schema'] not in (SCHEMA, LEGACY_SCHEMA) or type(value['items']) is not list
                or len(value['items']) > MAX_ITEMS or type(value['skips']) is not list
                or len(value['skips']) != 2):
            raise ValueError('Unsupported saved playback queue; its existing bytes were preserved.')
        return cls(tuple(SavedPlaylistItem.from_dict(row) for row in value['items']),
                   value['index'], value['frame'], value['volume'], value['muted'],
                   value['balance'], tuple(value['skips']), value['output'], value['schema'])

    @classmethod
    def capture(cls, root, player, *, skips=(10, 30)):
        root = local_path(root, exists=True)
        items = tuple(SavedPlaylistItem.reviewed(root, item) for item in player.playlist)
        index = player.playlist_index
        frame = 0
        if index is not None:
            if type(index) is not int or not 0 <= index < len(items):
                raise ValueError('The current playback selection is invalid.')
            if player.path != player.playlist[index].path:
                raise ValueError('The current recording is not the selected playlist entry.')
            seconds = player.position_seconds
            if (type(seconds) not in (float, int) or not math.isfinite(seconds)
                    or not 0 <= seconds <= items[index].frames / items[index].sample_rate):
                raise ValueError('The current playback cursor is unavailable or outside its original.')
            frame = min(items[index].frames, round(seconds * items[index].sample_rate))
        levels, output = player.levels, player.output_device
        return cls(items, index, frame, levels.volume, levels.muted, levels.balance,
                   tuple(skips), None if output is None else asdict(output))

    def review(self, root, *, cancelled=lambda: False):
        """Explicit worker review opens only confined retained copies, not original paths."""
        root = local_path(root, exists=True)
        if not self.items:
            return ()
        paths = [local_path(root / item.filename, root=root, exists=True) for item in self.items]
        checked = prepare_playlist(paths, cancelled=cancelled)
        result = []
        for saved, item in zip(self.items, checked):
            named = replace(item, label=saved.label)
            if SavedPlaylistItem.reviewed(root, named) != saved:
                raise ValueError('Saved playlist audio changed. The queue was not admitted.')
            result.append(named)
        return tuple(result)

    def apply(self, player, reviewed, *, use_current_controls=False):
        """Adopt an explicitly reviewed queue silently, preserving exact original cursor."""
        if (type(use_current_controls) is not bool or type(reviewed) not in (list, tuple)
                or len(reviewed) != len(self.items)):
            raise ValueError('Use the exact reviewed saved queue.')
        for saved, item in zip(self.items, reviewed):
            if (not isinstance(item, PlaylistItem) or item.sha256 != saved.sha256
                    or item.frames != saved.frames or item.sample_rate != saved.sample_rate
                    or item.channels != saved.channels or item.sample_width != saved.sample_width
                    or item.identity[2] != saved.size_bytes):
                raise ValueError('The reviewed queue no longer matches its saved source identity.')
        if not use_current_controls:
            if self.schema == LEGACY_SCHEMA and self.output is not None:
                raise ValueError('This legacy queue uses a different output identity. Review with current controls explicitly.')
            from ._wave_output import PlaybackLevels
            if not player.live_mix_available and PlaybackLevels(self.volume, self.muted, self.balance) != PlaybackLevels():
                raise ValueError('Saved live levels require Windows. Choose review with current controls explicitly.')
            from ..audio.output_devices import OutputDevice, resolve_output_device
            selected = None if self.output is None else OutputDevice(**self.output)
            if selected is not None:
                resolve_output_device(selected)  # Fresh identity; no ordinal fallback.
            if player.close() is False:
                raise RuntimeError('The current playback device has not confirmed closure.')
            player.select_output(selected)
        player.set_playlist(reviewed)
        if not use_current_controls:
            if player.live_mix_available:
                player.set_levels(self.volume, muted=self.muted, balance=self.balance)
        if self.index is not None:
            player.select_queue(self.index)
            player.seek(self.frame / self.items[self.index].sample_rate, start_if_stopped=False)
        return self.skips


class PlaylistStore:
    """One passive CAS document alongside existing speech preferences and drafts."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / NAME
        self.notice = ''

    def guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The playback library folder changed.')
        local_path(self.path, root=self.root)

    def load(self):
        self.guard()
        if not os.path.lexists(self.path):
            return None, None
        value, token = _read_snapshot(self.path)
        self.guard()
        return SavedPlaylist.from_dict(value), token

    def save(self, value, *, expected_token):
        if type(value) is not SavedPlaylist:
            raise ValueError('Use a validated passive playlist snapshot.')
        if (expected_token is not None and
                (type(expected_token) is not str or re.fullmatch('[0-9a-f]{64}', expected_token) is None)):
            raise ValueError('Use the exact saved-queue version returned by this store.')
        payload = value.to_dict()
        SavedPlaylist.from_dict(payload)
        raw = encode(payload)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('The saved playlist exceeds its state byte budget.')
        self.guard()
        attempted, expected = False, sha256(raw).hexdigest()
        self.notice = ''
        try:
            with state_lock(self.path.parent, 'playback-queue'):
                self.guard()
                _, token = self.load()
                if token != expected_token:
                    raise ValueError('Another window changed the saved queue. Reload before replacing it.')
                for item in value.items:
                    path = local_path(self.root / item.filename, root=self.root, exists=True)
                    if regular(path).st_size != item.size_bytes:
                        raise ValueError('The reviewed retained playlist media is unavailable or changed.')
                attempted = True
                atomic_json(self.path, payload, replace=token is not None)
        except BaseException:
            if attempted:
                try:
                    current, token = self.load()
                    present = current is not None and current.to_dict() == payload and token == expected
                except BaseException:
                    present = False
                if present:
                    self.notice = ('The exact saved queue bytes are present; completion remains unconfirmed. '
                                   'Reopen the queue before another edit.')
                    return expected
            raise
        return expected


def validate_collection(stage, entries):
    """Bind passive queue rows to bytes already verified by the full backup."""
    inventory = {entry['filename']: entry for entry in entries}
    if NAME not in inventory:
        return
    raw, _ = _read_snapshot(stage / NAME)
    saved = SavedPlaylist.from_dict(raw)
    for item in saved.items:
        entry = inventory.get(item.filename)
        if entry is None or entry['sha256'] != item.sha256 or entry['size_bytes'] != item.size_bytes:
            raise ValueError('Saved playlist references missing or changed retained audio.')
