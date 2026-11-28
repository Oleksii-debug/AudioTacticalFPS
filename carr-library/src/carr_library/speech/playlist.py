"""Bounded PCM review and passive queue recovery; never autoplay.

Live PlaylistItems are process-local review results. SavedPlaylist contains
only relative retained-media identities and inert settings. Loading or backing
it up grants no playback, external-file, output-device or source authority.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import stat
import time
import wave
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, regular, state_lock
from ..backup_sidecars import read_regular, _json
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
from ._io import local_file

MAX_ITEMS = 256
MAX_FILE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**3
MAX_REVIEW_SECONDS = 120
MAX_STATE_BYTES = 2 * 1024**2
NAME = 'state/speech/playback-queue.json'
SCHEMA = 'carr-playback-queue/1'
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

    @property
    def display_name(self):
        return self.label or self.path.name

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


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
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or _stamp(opened) != identity or _stamp(local_file(path).stat()) != identity):
                raise ValueError('A selected playlist file changed before it could be reviewed.')
            with wave.open(stream, 'rb') as audio:
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


def _items(items):
    if (not isinstance(items, (tuple, list)) or not 1 <= len(items) <= MAX_ITEMS
            or any(not isinstance(item, PlaylistItem) or type(item.identity) is not tuple
                   or len(item.identity) != 5
                   or any(type(part) is not int for part in item.identity)
                   or not 0 < item.identity[2] <= MAX_FILE_BYTES for item in items)
            or sum(item.identity[2] for item in items) > MAX_TOTAL_BYTES):
        raise ValueError('Retain only explicitly reviewed playlist entries.')
    for item in items:
        _label(item.display_name)
    return tuple(items)


@contextmanager
def _admission(library_root, paths, *, retain, cancelled):
    """Use the current cooperative policy for one bounded review/copy operation."""
    if (not isinstance(paths, (tuple, list)) or not 1 <= len(paths) <= MAX_ITEMS
            or not callable(cancelled)):
        raise ValueError('Choose a bounded playlist and its cancellation owner.')
    root = local_path(library_root, exists=True)
    info = regular(root, directory=True)
    identity = info.st_dev, info.st_ino
    deadline = time.monotonic() + MAX_REVIEW_SECONDS
    def check():
        if cancelled():
            raise InterruptedError('Playlist review or retention was cancelled.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Playlist review exceeded two minutes. Choose fewer files.')
        current = regular(local_path(root, exists=True), directory=True)
        if (current.st_dev, current.st_ino) != identity:
            raise ValueError('The library folder changed during playlist review.')
        return False
    check()
    chosen = tuple(local_file(path) for path in paths)
    stamps = tuple(_stamp(path.stat()) for path in chosen)
    if (any(not 0 < info[2] <= MAX_FILE_BYTES for info in stamps)
            or sum(info[2] for info in stamps) > MAX_TOTAL_BYTES):
        raise ValueError('The selected playlist exceeds its file or total byte allowance.')
    check()
    broker = ResourceBroker.for_app(root, cancelled=check)
    check()
    disk = sum(info[2] for info in stamps) + 64 * 1024**2 if retain else 0
    request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                              disk_bytes=disk, disk_path=root if disk else None)
    try:
        with broker.reserve(request, operation_id='playback-review:' + uuid4().hex,
                cancelled=check, timeout=60, run_timeout=MAX_REVIEW_SECONDS) as lease:
            def admitted():
                lease.checkpoint()
                return check()
            admitted()
            if any(_stamp(local_file(path).stat()) != expected
                   for path, expected in zip(chosen, stamps)):
                raise ValueError('Selected playlist audio changed while waiting for resources.')
            yield root, admitted
            admitted()
    except ResourceCancelled as error:
        raise InterruptedError('Playlist review was cancelled while waiting for resources.') from error


def _retain_playlist(root, items, *, cancelled):
    from ..studio.clips import retain_audio_input
    items = _items(items)
    if cancelled():
        raise InterruptedError('Playlist retention was cancelled.')
    for item in items:
        if _stamp(local_file(item.path).stat()) != item.identity:
            raise ValueError('A reviewed playlist source changed before retention.')
    inputs = local_path(root / 'state' / 'studio' / 'inputs', root=root)
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    local_path(inputs, root=root, exists=True)
    def inspect(path, *, cancelled):
        prepare_playlist((path,), cancelled=cancelled)
    result = []
    for item in items:
        if cancelled():
            raise InterruptedError('Playlist retention was cancelled.')
        if _stamp(local_file(item.path).stat()) != item.identity:
            raise ValueError('A reviewed playlist source changed before retention.')
        target = retain_audio_input(inputs, item.path, inspect=inspect,
            expected_sha256=item.sha256, cancelled=cancelled)
        reviewed, = prepare_playlist((target,), cancelled=cancelled)
        result.append(replace(reviewed, label=item.display_name))
    if cancelled():
        raise InterruptedError('Playlist retention was cancelled.')
    return tuple(result)


def retain_playlist(library_root, items, *, cancelled=lambda: False):
    """Reuse Studio's verified copy under the selected library resource policy."""
    items = _items(items)
    with _admission(library_root, tuple(item.path for item in items),
                    retain=True, cancelled=cancelled) as (root, check):
        return _retain_playlist(root, items, cancelled=check)


def review_and_retain(library_root, paths, *, review=prepare_playlist,
                      cancelled=lambda: False):
    """Run review, exact copy and final hashing inside the same shared lease."""
    if not callable(review):
        raise ValueError('A bounded PCM review is required.')
    with _admission(library_root, paths, retain=True, cancelled=cancelled) as (root, check):
        reviewed = review(paths, cancelled=check)
        return _retain_playlist(root, reviewed, cancelled=check)


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
        if (type(self.skips) is not tuple or len(self.skips) != 2
                or any(type(value) not in (float, int) or not .1 <= value <= 600
                       for value in self.skips)):
            raise ValueError('Saved skips must be between 0.1 and 600 seconds.')
        if self.output is not None:
            from .playback_devices import PlaybackDevice
            if (type(self.output) is not dict or
                    set(self.output) != set(PlaybackDevice.__dataclass_fields__)):
                raise ValueError('Invalid passive playback output declaration.')
            device = PlaybackDevice(**self.output)
            if not device.selectable:
                raise ValueError('Saved playback outputs need an exact interface identity.')
            device.name.encode('utf-8', errors='strict')
            device.interface.encode('utf-8', errors='strict')

    def to_dict(self):
        return {'schema': SCHEMA, 'items': [asdict(item) for item in self.items],
                'index': self.index, 'frame': self.frame,
                'volume': self.volume, 'muted': self.muted, 'balance': self.balance,
                'skips': list(self.skips), 'output': None if self.output is None else dict(self.output)}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) !=
                {'schema', 'items', 'index', 'frame', 'volume', 'muted', 'balance', 'skips', 'output'}
                or value['schema'] != SCHEMA or type(value['items']) is not list
                or type(value['skips']) is not list):
            raise ValueError('Unsupported saved playback queue; its existing bytes were preserved.')
        return cls(tuple(SavedPlaylistItem.from_dict(row) for row in value['items']),
                   value['index'], value['frame'], value['volume'], value['muted'],
                   value['balance'], tuple(value['skips']), value['output'])

    @classmethod
    def capture(cls, root, player):
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
                   tuple(player.skip_intervals), None if output is None else asdict(output))

    def review(self, root, *, cancelled=lambda: False):
        """Explicit worker review opens only confined retained copies, not original paths."""
        root = local_path(root, exists=True)
        if not self.items:
            return ()
        paths = [local_path(root / item.filename, root=root, exists=True) for item in self.items]
        with _admission(root, paths, retain=False, cancelled=cancelled) as (_, check):
            checked = prepare_playlist(paths, cancelled=check)
            result = []
            for saved, item in zip(self.items, checked):
                named = replace(item, label=saved.label)
                if SavedPlaylistItem.reviewed(root, named) != saved:
                    raise ValueError('Saved playlist audio changed. The queue was not admitted.')
                result.append(named)
            return tuple(result)

    def apply(self, player, reviewed, *, use_current_controls=False):
        """Adopt an explicitly reviewed queue silently, preserving exact original cursor."""
        SavedPlaylist.from_dict(self.to_dict())
        if type(use_current_controls) is not bool or len(reviewed) != len(self.items):
            raise ValueError('Use the exact reviewed saved queue.')
        for saved, item in zip(self.items, reviewed):
            if (not isinstance(item, PlaylistItem) or item.sha256 != saved.sha256
                    or item.frames != saved.frames or item.sample_rate != saved.sample_rate
                    or item.channels != saved.channels or item.sample_width != saved.sample_width
                    or item.identity[2] != saved.size_bytes):
                raise ValueError('The reviewed queue no longer matches its saved source identity.')
        if not use_current_controls:
            from ._wave_output import PlaybackLevels
            if not player.live_mix_available and PlaybackLevels(self.volume, self.muted, self.balance) != PlaybackLevels():
                raise ValueError('Saved live levels require Windows. Choose review with current controls explicitly.')
            from .playback_devices import PlaybackDevice
            # resolve_output runs inside select_output: no stale ordinal or fallback.
            player.select_output(None if self.output is None else PlaybackDevice(**self.output))
        player.set_playlist(reviewed)
        if not use_current_controls:
            player.set_skip_intervals(*self.skips)
            if player.live_mix_available:
                player.set_levels(self.volume, muted=self.muted, balance=self.balance)
        if self.index is not None:
            player.select_queue(self.index)
            player.seek(self.frame / self.items[self.index].sample_rate, start_if_stopped=False)


def _read_snapshot(path):
    raw = read_regular(path, MAX_STATE_BYTES)
    try:
        value = _json(raw)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('The saved playlist JSON is invalid or too deeply nested.') from error
    return value, sha256(raw).hexdigest()


class PlaylistSaveCommitted(OSError):
    """Exact queue bytes are visible; durability confirmation still needs retry."""
    def __init__(self, token):
        super().__init__('Queue bytes were published, but durable storage could not be confirmed. '
                         'Keep this window open and retry Save.')
        self.token = token


class PlaylistStore:
    """One passive CAS document alongside existing speech preferences and drafts."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
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
        if not os.path.lexists(self.path):
            return None, None
        value, token = _read_snapshot(self.path)
        self.guard()
        return SavedPlaylist.from_dict(value), token

    def save(self, value, *, expected_token):
        if not isinstance(value, SavedPlaylist):
            raise ValueError('Use a validated passive playlist snapshot.')
        if expected_token is not None and (type(expected_token) is not str
                or re.fullmatch(r'[0-9a-f]{64}', expected_token) is None):
            raise ValueError('Use the exact prior saved-queue token.')
        payload = value.to_dict()
        SavedPlaylist.from_dict(payload)
        raw = encode(payload)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError('The saved playlist exceeds its state byte budget.')
        self.guard()
        with state_lock(self.path.parent, 'playback-queue'):
            self.guard()
            _, token = self.load()
            if token != expected_token:
                raise ValueError('Another window changed the saved queue. Reload before replacing it.')
            for item in value.items:
                path = local_path(self.root / item.filename, root=self.root, exists=True)
                if regular(path).st_size != item.size_bytes:
                    raise ValueError('The reviewed retained playlist media is unavailable or changed.')
            try:
                atomic_json(self.path, payload, replace=token is not None)
            except FileExistsError:
                raise  # A competing create is never our committed publication.
            except OSError:
                try:
                    visible = read_regular(self.path, MAX_STATE_BYTES)
                except (OSError, ValueError):
                    raise
                if visible == raw:
                    raise PlaylistSaveCommitted(sha256(raw).hexdigest()) from None
                raise
        return sha256(raw).hexdigest()


def validate_collection(stage, entries, *, cancelled=lambda: False):
    """Compatibility entry for the one copied-state passive queue validator."""
    from ..backup_playback_queue import validate_collection as validate
    return validate(Path(stage) / 'state',
        ({**entry, 'filename': entry['filename'][6:]}
         for entry in entries if entry['filename'].startswith('state/')),
        cancelled=cancelled)
