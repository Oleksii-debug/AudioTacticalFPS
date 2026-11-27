"""Presentation-only reader settings in the existing typed library state tree.

The released schema remains byte-compatible. Complete backup registers this one
bounded file; it never gathers neighboring UI state or installs a font file.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
import re
import stat

from ..audio.jobs import _open_json_file, _json_bytes, atomic_json
from ..backup_files import local_path, optional_present, regular
from ..ingest.writer_guard import exclusive_writer
from ..native_file_metadata import stream_change_time

FILENAME = 'state/ui/reader-appearance.json'
MAX_APPEARANCE_BYTES = 4096
LOCK_NAME = '.reader-appearance.lock'


@dataclass(frozen=True)
class ReaderFont:
    face: str
    points: float
    weight: int = 400
    style: str = 'normal'
    underlined: bool = False
    strikethrough: bool = False

    def __post_init__(self):
        if (type(self.face) is not str or not self.face.strip() or len(self.face) > 128
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in self.face)):
            raise ValueError('Choose an installed font with a readable face name.')
        if (type(self.points) not in (int, float) or not math.isfinite(self.points)
                or not 4 <= self.points <= 144):
            raise ValueError('Choose a font size between 4 and 144 points.')
        if type(self.weight) is not int or not 1 <= self.weight <= 1000:
            raise ValueError('The selected font weight is invalid.')
        if self.style not in ('normal', 'italic', 'slant'):
            raise ValueError('The selected font style is invalid.')
        if type(self.underlined) is not bool or type(self.strikethrough) is not bool:
            raise ValueError('The selected font effects are invalid.')


@dataclass(frozen=True)
class ReaderAppearancePreferences:
    font: ReaderFont | None = None
    foreground: str | None = None
    background: str | None = None
    zoom_percent: int = 100

    def __post_init__(self):
        if self.font is not None and type(self.font) is not ReaderFont:
            raise ValueError('Use a reviewed reader font or the system default.')
        for colour in (self.foreground, self.background):
            if colour is not None and (type(colour) is not str
                                      or re.fullmatch(r'#[0-9A-F]{6}', colour) is None):
                raise ValueError('Use an explicit RGB colour or the system default.')
        if type(self.zoom_percent) is not int or not 50 <= self.zoom_percent <= 400:
            raise ValueError('Choose reader zoom between 50 and 400 percent.')


@dataclass(frozen=True)
class AppearanceSnapshot:
    preferences: ReaderAppearancePreferences
    token: str | None
    warning: str = ''


def _path(root):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    return local_path(root / FILENAME, root=root)


def _bytes(path):
    if not optional_present(path):
        return None
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= MAX_APPEARANCE_BYTES:
        raise ValueError('Saved reader appearance exceeds its size limit.')
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    with _open_preferences(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or not 0 < opened.st_size <= MAX_APPEARANCE_BYTES
                or any(getattr(before, key) != getattr(opened, key) for key in fields)):
            raise ValueError('Reader appearance changed before its bounded file was opened.')
        native_before = stream_change_time(stream)
        raw = stream.read(MAX_APPEARANCE_BYTES + 1)
        closed = os.fstat(stream.fileno())
        if (stream_change_time(stream) != native_before
                or any(getattr(opened, key) != getattr(closed, key) for key in fields)
                or closed.st_nlink != 1):
            raise ValueError('Reader appearance changed while reading its bounded snapshot.')
    local_path(path, exists=True)
    after = regular(path)
    if (len(raw) != before.st_size or any(getattr(before, key) != getattr(opened, key)
            or getattr(before, key) != getattr(after, key) for key in fields)):
        raise ValueError('Reader appearance changed while opening. Reopen its settings.')
    return raw


def _open_preferences(path):
    if os.name == 'nt':
        # Reuse the native open-reparse-point/delete-sharing descriptor boundary.
        return _open_json_file(path)
    flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags | getattr(os, 'O_CLOEXEC', 0))
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate reader appearance field.')
        value[key] = item
    return value


def _preferences(raw):
    if raw is None:
        return ReaderAppearancePreferences()
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_APPEARANCE_BYTES:
        raise ValueError('Saved reader appearance is empty or exceeds its size limit.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite reader appearance.')))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Saved reader appearance could not be read.') from exc
    if (type(value) is not dict or set(value) != {'schema', 'preferences'}
            or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('Unsupported reader appearance schema.')
    choices = value['preferences']
    if type(choices) is not dict or set(choices) != {'font', 'foreground', 'background', 'zoom_percent'}:
        raise ValueError('Invalid reader appearance preferences.')
    choices = dict(choices)
    if choices['font'] is not None:
        font = choices['font']
        if type(font) is not dict or set(font) != {'face', 'points', 'weight', 'style', 'underlined', 'strikethrough'}:
            raise ValueError('Invalid saved reader font.')
        choices['font'] = ReaderFont(**font)
    return ReaderAppearancePreferences(**choices)


def validate_appearance(raw):
    """Pure validator shared by the live preference store and complete backup."""
    if raw is None:
        raise ValueError('A retained appearance file must contain explicit JSON bytes.')
    return _preferences(raw)


def _token(raw):
    return sha256(raw).hexdigest() if raw is not None else None


def load_appearance(root):
    raw = _bytes(_path(root))
    return AppearanceSnapshot(_preferences(raw), _token(raw))


@contextmanager
def snapshot_guard(root):
    """Take the released native preference lock under the application writer.

    Verification/restoration need no live lock. A source snapshot uses this
    exact lock, in the same root-writer then preference-file order as Save.
    """
    from filelock import FileLock, Timeout
    directory = _path(root).parent
    if not optional_present(directory):
        yield
        return
    regular(directory, directory=True)
    path = directory / LOCK_NAME
    if optional_present(path):
        regular(path)
    try:
        with FileLock(path, timeout=0, mode=0o600):
            local_path(directory, exists=True)
            regular(path)
            yield
    except Timeout as exc:
        raise RuntimeError('Reader appearance is being saved. Retry when idle.') from exc


def save_appearance(root, preferences, *, expected_token):
    """Save reviewed choices, rejecting another reader's intervening save."""
    if type(preferences) is not ReaderAppearancePreferences:
        raise ValueError('Review reader appearance before saving it.')
    if expected_token is not None and (type(expected_token) is not str
            or re.fullmatch(r'[0-9a-f]{64}', expected_token) is None):
        raise ValueError('Reopen reader appearance before saving it.')
    value = {'schema': 1, 'preferences': asdict(preferences)}
    raw = _json_bytes(value)
    validate_appearance(raw)
    path, warning = _path(root), ''
    with exclusive_writer(local_path(root, exists=True)):
        path = _path(root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with snapshot_guard(root):
            previous = _bytes(path)
            if _token(previous) != expected_token:
                raise ValueError('Another window changed reader appearance. Reopen the settings before saving.')
            # Even an explicit reset cannot replace invalid retained state.
            _preferences(previous)
            if previous != raw:
                try:
                    atomic_json(path, value)
                except OSError as exc:
                    # An atomic rename may have committed before directory
                    # sync failed. Report that only after checking exact bytes.
                    try:
                        confirmed = _bytes(path) == raw
                    except (OSError, ValueError, RuntimeError):
                        confirmed = False
                    if not confirmed:
                        raise OSError('Saving reader appearance could not be confirmed. '
                                      'Reopen the settings before retrying.') from exc
                    warning = 'Appearance was saved; storage cleanup or directory sync needs attention.'
    return AppearanceSnapshot(preferences, _token(raw), warning)
