"""Read-only compatibility data validation from checkpoint5580d4d.

No legacy engine, UI, provider transport or credential access is installed here.
The original formats remain inert until an explicit validated import exists.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from ..reader.reading_state import ReadingLocation, _location, _text, _timestamp, _unique


DIRECTORY = 'narration-projects'


MAX_TEXT_BYTES = 8 * 1024 * 1024


MAX_PROJECT_BYTES = 32 * 1024 * 1024


MAX_HISTORY = 50


MAX_RULES = 1000


MAX_PROJECTS = 1000


MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


_ID = re.compile(r'[0-9a-f]{32}')


_FILE = re.compile(r'([0-9a-f]{32})(\.draft)?\.json')


def text_digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _body(value):
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('Reading text must be Unicode text without NUL characters.')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise ValueError('Reading text contains invalid Unicode.') from exc
    if size > MAX_TEXT_BYTES:
        raise ValueError('This reading section exceeds the 8 MiB editing budget. Open a smaller section.')
    return value


@dataclass(frozen=True)
class PronunciationRule:
    written: str
    spoken: str
    language: str = ''

    def __post_init__(self):
        if not isinstance(self.written, str) or not self.written or len(self.written) > 1000:
            raise ValueError('Enter a literal phrase of 1 to 1000 characters to replace.')
        if not isinstance(self.spoken, str) or len(self.spoken) > 4000:
            raise ValueError('A pronunciation replacement must be at most 4000 characters.')
        _body(self.written)
        _body(self.spoken)
        if not isinstance(self.language, str) or len(self.language) > 64:
            raise ValueError('Invalid pronunciation language label.')
        if self.language:
            _text(self.language, 'pronunciation language', limit=64)


@dataclass(frozen=True)
class Revision:
    number: int
    text: str
    label: str
    created_at: str

    def __post_init__(self):
        if type(self.number) is not int or self.number < 0:
            raise ValueError('Invalid reading-copy revision number.')
        _body(self.text)
        _text(self.label, 'revision label', limit=200)
        _timestamp(self.created_at)


@dataclass(frozen=True)
class NarrationProject:
    id: str
    title: str
    original_text: str
    original_sha256: str
    source_location: ReadingLocation | None
    revisions: tuple[Revision, ...]
    cursor: int
    rules: tuple[PronunciationRule, ...] = ()

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Invalid reading-project identity.')
        _text(self.title, 'reading-copy title', limit=200)
        _body(self.original_text)
        if self.original_sha256 != text_digest(self.original_text):
            raise ValueError('Original reading text does not match its content digest.')
        if self.source_location is not None:
            if type(self.source_location) is not ReadingLocation:
                raise ValueError('A validated original source location is required.')
            if self.source_location.text_sha256 != self.original_sha256:
                raise ValueError('Original reading text does not match the selected source location.')
        if (type(self.revisions) is not tuple or not 1 <= len(self.revisions) <= MAX_HISTORY
                or any(type(item) is not Revision for item in self.revisions)
                or type(self.cursor) is not int or not 0 <= self.cursor < len(self.revisions)):
            raise ValueError('Invalid reading-copy revision history.')
        if self.revisions[0].number != 0 or self.revisions[0].text != self.original_text:
            raise ValueError('The first reading-copy revision must preserve the original text.')
        if any(a.number >= b.number for a, b in zip(self.revisions, self.revisions[1:])):
            raise ValueError('Reading-copy revisions must have increasing identities.')
        if (type(self.rules) is not tuple or len(self.rules) > MAX_RULES
                or any(type(rule) is not PronunciationRule for rule in self.rules)
                or len({rule.written for rule in self.rules}) != len(self.rules)):
            raise ValueError('Use distinct literal phrases in the pronunciation dictionary.')
        if sum(len(item.text.encode('utf-8')) for item in self.revisions) > MAX_PROJECT_BYTES // 2:
            raise ValueError('Reading-copy undo history exceeds its memory budget. Save a new copy of the text.')

    @property
    def current_text(self):
        return self.revisions[self.cursor].text

    @property
    def provenance(self):
        if self.source_location is None:
            return 'Independent reading copy; no authenticated library source is attached.'
        where = self.source_location
        return (f'Reading copy of source edition {where.edition}, revision {where.revision}, '
                f'{where.kind.replace("_", " ")} {where.index}. '
                'Edited text is not a verified quotation. The original source stays unchanged.')


def decode_project(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PROJECT_BYTES:
        raise ValueError('Invalid or oversized reading project.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        fields = {'kind', 'version', 'id', 'title', 'original_text', 'original_sha256',
                  'source_location', 'revisions', 'cursor', 'rules'}
        if (type(value) is not dict or set(value) != fields or value['kind'] != 'carr-reading-copy'
                or type(value['version']) is not int or value['version'] != 1
                or type(value['revisions']) is not list or len(value['revisions']) > MAX_HISTORY
                or type(value['rules']) is not list or len(value['rules']) > MAX_RULES):
            raise ValueError('Unsupported reading-project format.')
        location = None if value['source_location'] is None else _location(value['source_location'])
        revisions = tuple(Revision(**row) for row in value['revisions'])
        rules = tuple(PronunciationRule(**row) for row in value['rules'])
        return NarrationProject(value['id'], value['title'], value['original_text'], value['original_sha256'],
                                location, revisions, value['cursor'], rules)
    except (UnicodeError, TypeError, KeyError, RecursionError, ValueError) as exc:
        raise ValueError(f'The reading project is invalid; existing files were kept. {exc}') from exc


def validate_narration_payload(filename, raw):
    if isinstance(filename, str) and filename.startswith(DIRECTORY + '/'):
        filename = filename[len(DIRECTORY) + 1:]
    match = _FILE.fullmatch(filename) if isinstance(filename, str) else None
    if match is None:
        raise ValueError('Invalid reading-project filename.')
    project = decode_project(raw)
    if project.id != match.group(1):
        raise ValueError('Reading-project filename does not match its identity.')
    return project
