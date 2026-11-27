"""Passive reading-copy bookmarks and reversible paragraph operations."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import re
from uuid import uuid4

MAX_MARKS = 256
_HEX = re.compile(r'[0-9a-f]{32}\Z')
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def text_digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class ReadingBookmark:
    id: str
    label: str
    text_sha256: str
    offset: int

    def __post_init__(self):
        if type(self.id) is not str or _HEX.fullmatch(self.id) is None:
            raise ValueError('Invalid reading-copy bookmark identity.')
        if (type(self.label) is not str or not self.label.strip() or len(self.label) > 80
                or any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xD800 <= ord(c) <= 0xDFFF
                       for c in self.label)):
            raise ValueError('Use a bookmark name of 1 to 80 printable characters.')
        if type(self.text_sha256) is not str or _DIGEST.fullmatch(self.text_sha256) is None:
            raise ValueError('A bookmark needs the exact reading-text checksum.')
        if type(self.offset) is not int or not 0 <= self.offset <= 2_000_000:
            raise ValueError('Bookmark offset exceeds the reading-copy limit.')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def create(cls, label, text, offset):
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise ValueError('Choose a cursor within the current reading copy.')
        return cls(uuid4().hex, label, text_digest(text), offset)


def validate_bookmarks(value):
    if type(value) is not list or len(value) > MAX_MARKS:
        raise ValueError('Keep at most 256 bookmarks in one reading copy.')
    result = []
    for row in value:
        if type(row) is not dict or set(row) != {'id', 'label', 'text_sha256', 'offset'}:
            raise ValueError('Invalid reading-copy bookmark fields.')
        result.append(ReadingBookmark(**row))
    if len({item.id for item in result}) != len(result):
        raise ValueError('Reading-copy bookmark identities must be unique.')
    return tuple(result)


def split_paragraph(document, offset):
    """One explicit history edit; source text and source authority stay intact."""
    document._range(offset, offset)
    text = document.text
    if not 0 < offset < len(text) or text[offset - 1] in '\r\n' or text[offset] in '\r\n':
        raise ValueError('Place the cursor inside a paragraph before splitting it.')
    newline = '\r\n' if '\r\n' in text and '\n' not in text.replace('\r\n', '') else '\n'
    if not document.replace(offset, offset, newline * 2, label='Split reading paragraph'):
        raise ValueError('No paragraph boundary was added.')
    return offset + len(newline) * 2


def join_next_break(document, offset):
    """Join the next line or paragraph break with one explicit undoable edit."""
    document._range(offset, offset)
    text = document.text
    if 0 < offset < len(text) and text[offset - 1:offset + 1] == '\r\n':
        raise ValueError('Place the cursor outside a CRLF line ending.')
    match = re.compile(r'[ \t]*(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))*[ \t]*').search(text, offset)
    if match is None:
        raise ValueError('There is no following line or paragraph break to join.')
    start, end = match.span()
    if not start or end == len(text):
        raise ValueError('Choose a break with reading text on both sides.')
    separator = '' if text[start - 1].isspace() or text[end].isspace() else ' '
    document.replace(start, end, separator, label='Join reading line or paragraph')
    return min(offset, len(document.text))
