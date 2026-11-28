"""Typed reading appearance, separate from document text and engine authority."""
from dataclasses import asdict, dataclass
import re

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock


MAX_BYTES = 8192
STATE_PATH = 'speech/presentation.json'


@dataclass(frozen=True)
class ReadingFont:
    face: str
    points: int
    weight: int = 400
    style: str = 'normal'
    underlined: bool = False

    def __post_init__(self):
        if (type(self.face) is not str or not self.face.strip() or len(self.face) > 128
                or any(ord(char) < 32 for char in self.face)):
            raise ValueError('Choose an installed font with a valid name.')
        self.face.encode('utf-8', 'strict')
        if type(self.points) is not int or not 6 <= self.points <= 72:
            raise ValueError('Font size must be from 6 to 72 points.')
        if type(self.weight) is not int or not 100 <= self.weight <= 1000:
            raise ValueError('Invalid font weight.')
        if type(self.style) is not str or self.style not in ('normal', 'italic', 'slant') or type(self.underlined) is not bool:
            raise ValueError('Invalid font style.')


@dataclass(frozen=True)
class ReadingAppearance:
    font: ReadingFont | None = None
    foreground: str | None = None
    background: str | None = None
    zoom: int = 100

    def __post_init__(self):
        if self.font is not None and type(self.font) is not ReadingFont:
            raise ValueError('A validated reading font is required.')
        for value in (self.foreground, self.background):
            if value is not None and (type(value) is not str or not re.fullmatch(r'#[0-9A-F]{6}', value)):
                raise ValueError('Use a colour such as #FFFFFF, or leave it empty for the system colour.')
        if type(self.zoom) is not int or not 50 <= self.zoom <= 400:
            raise ValueError('Reading zoom must be from 50 to 400 percent.')

    def to_dict(self):
        return {'schema': 'carr.reading-appearance.v1', **asdict(self)}

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict or set(value) != {'schema', 'font', 'foreground', 'background', 'zoom'}
                or value['schema'] != 'carr.reading-appearance.v1'):
            raise ValueError('Unsupported reading appearance settings.')
        font = value['font']
        if font is not None:
            if type(font) is not dict or set(font) != set(ReadingFont.__dataclass_fields__):
                raise ValueError('Unsupported reading font settings.')
            font = ReadingFont(**font)
        return cls(font, value['foreground'], value['background'], value['zoom'])


class ReadingAppearanceStore:
    """Use the existing atomic state contract and detect concurrent preferences."""
    def __init__(self, library_root):
        self.root = local_path(library_root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = info.st_dev, info.st_ino
        self.path = self.root / 'state' / STATE_PATH

    def _guard(self):
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise ValueError('The library changed. Reopen reading appearance settings.')
        local_path(self.path)

    def load(self):
        self._guard()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return ReadingAppearance()
        if not 0 < info.st_size <= MAX_BYTES:
            raise ValueError('Reading appearance settings exceed their size limit.')
        value = read_json(self.path)
        if len(encode(value)) > MAX_BYTES:
            raise ValueError('Reading appearance settings exceed their size limit.')
        self._guard()
        return ReadingAppearance.from_dict(value)

    def save(self, appearance, *, expected, guard=lambda: None):
        if type(appearance) is not ReadingAppearance or type(expected) is not ReadingAppearance:
            raise ValueError('Validated reading appearance settings are required.')
        guard()
        self._guard()
        with state_lock(self.path.parent, 'presentation'):
            if self.load() != expected:
                raise ValueError('Reading appearance changed in another window. Reopen these settings.')
            guard()
            self._guard()
            atomic_json(self.path, appearance.to_dict(), replace=True)
        return appearance
