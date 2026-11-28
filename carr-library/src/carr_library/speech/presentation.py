"""Non-executable native reader appearance, separate from text and voice state."""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
import stat

from ..durable_state import _open_json_read, atomic_json, encode, local_path, regular, state_lock

SCHEMA = 'carr-reading-presentation/1'
MAX_PROFILE_BYTES = 8192


@dataclass(frozen=True)
class ReadingFont:
    face: str
    points: float
    weight: int = 400
    style: str = 'normal'

    def __post_init__(self):
        if (not isinstance(self.face, str) or not self.face or len(self.face) > 128
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in self.face)):
            raise ValueError('Choose one installed font with a bounded face name.')
        if (type(self.points) not in (int, float) or not math.isfinite(self.points)
                or not 6 <= self.points <= 72):
            raise ValueError('Choose a base font size from 6 to 72 points.')
        if type(self.weight) is not int or not 1 <= self.weight <= 1000:
            raise ValueError('Invalid native font weight.')
        if self.style not in ('normal', 'italic', 'slant'):
            raise ValueError('Unsupported native font style.')
        object.__setattr__(self, 'points', float(self.points))

    def to_dict(self):
        return {'face': self.face, 'points': self.points, 'weight': self.weight, 'style': self.style}

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {'face', 'points', 'weight', 'style'}:
            raise ValueError('Unsupported saved reader font.')
        return cls(**value)


def _colour(value):
    if value is not None and (type(value) is not tuple or len(value) != 3
            or any(type(part) is not int or not 0 <= part <= 255 for part in value)):
        raise ValueError('Choose an opaque RGB color or the OS theme default.')


@dataclass(frozen=True)
class ReadingPresentation:
    font: ReadingFont | None = None
    foreground: tuple[int, int, int] | None = None
    background: tuple[int, int, int] | None = None
    zoom: int = 100

    def __post_init__(self):
        if self.font is not None and not isinstance(self.font, ReadingFont):
            raise ValueError('Use a validated native reader font.')
        _colour(self.foreground); _colour(self.background)
        if type(self.zoom) is not int or not 50 <= self.zoom <= 300:
            raise ValueError('Choose reading zoom from 50 to 300 percent.')

    def to_dict(self):
        return {'schema': SCHEMA, 'font': self.font.to_dict() if self.font else None,
                'foreground': list(self.foreground) if self.foreground is not None else None,
                'background': list(self.background) if self.background is not None else None,
                'zoom': self.zoom}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'font', 'foreground', 'background', 'zoom'}
                or value['schema'] != SCHEMA):
            raise ValueError('Unsupported reading appearance profile; saved bytes were preserved.')
        colors = []
        for key in ('foreground', 'background'):
            item = value[key]
            if item is not None and type(item) is not list:
                raise ValueError('A saved color needs three RGB components.')
            colors.append(tuple(item) if item is not None else None)
        return cls(ReadingFont.from_dict(value['font']) if value['font'] is not None else None,
                   *colors, value['zoom'])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate reading appearance field.')
        result[key] = value
    return result


class PresentationStore:
    """Bounded sidecar with root identity, atomic writes and compare-before-save."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / 'state' / 'speech' / 'reading-presentation.json'

    def guard(self):
        root = local_path(self.root, exists=True)
        info = regular(root, directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The library directory changed. Reopen reading appearance.')
        local_path(self.path, root=self.root)

    def _raw(self):
        self.guard()
        if not os.path.lexists(self.path):
            return None
        before = regular(self.path)
        if not 0 < before.st_size <= MAX_PROFILE_BYTES:
            raise ValueError('Reading appearance exceeds its 8 KiB byte budget.')
        # Use the common delete-sharing reader with the profile's smaller bound.
        # A concurrent replace is a stale snapshot, not permission to overwrite it.
        with _open_json_read(self.path) as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or getattr(opened, 'st_file_attributes', 0) & 0x400
                    or opened.st_nlink > 1 or opened.st_size > MAX_PROFILE_BYTES
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('Reading appearance changed during load. Reopen the dialog.')
            self.guard()
            raw = stream.read(MAX_PROFILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) != opened.st_size or len(raw) > MAX_PROFILE_BYTES
                or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise ValueError('Reading appearance changed during load. Reopen the dialog.')
        self.guard()
        return raw

    def load(self):
        raw = self._raw()
        if raw is None:
            return ReadingPresentation(), None
        try:
            value = json.loads(raw, object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite appearance value.')))
        except (UnicodeError, RecursionError) as exc:
            raise ValueError('The reading appearance profile could not be decoded.') from exc
        return ReadingPresentation.from_dict(value), sha256(raw).hexdigest()

    def save(self, profile, *, expected_token):
        if not isinstance(profile, ReadingPresentation):
            raise ValueError('Use a validated reading appearance profile.')
        payload = encode(profile.to_dict())
        if len(payload) > MAX_PROFILE_BYTES:
            raise ValueError('Reading appearance exceeds its 8 KiB byte budget.')
        self.guard()
        with state_lock(self.path.parent, 'reading-presentation'):
            self.guard()
            _, token = self.load()
            if token != expected_token:
                raise ValueError('Another window changed reading appearance. Reload before applying.')
            atomic_json(self.path, profile.to_dict(), replace=self.path.exists())
        return profile, sha256(payload).hexdigest()
