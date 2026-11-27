"""Versioned reading copies, separate from source and quotation authority.

All transformations are explicit previews. These files contain no assertion
that edited words occur in the original source, and never update source SQL.
Only unchanged prefix/suffix spans receive an exact offset map; changed spans
are honestly reported as derivatives. Local saves use the library writer lock
and a content-digest lease, including separate recoverable autosave files.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ..ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer
from ..backup_files import optional_present
from ..research.export_file import write_export
from .reading_state import ReadingLocation, _file_identity, _location, _text, _timestamp, _unique
from .legacy_reading import LegacyReadingOrigin, decode_origin


DIRECTORY = 'narration-projects'
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_HISTORY = 50
MAX_RULES = 1000
MAX_RULE_CHARACTERS = 64 * 1024
MAX_MATCH_STEPS = 20_000_000
MAX_DICTIONARY_BYTES = 1024 * 1024
MAX_REGEX_MATCHES = 10000
MAX_REGEX_RESPONSE = 900 * 1024
MAX_PROJECTS = 1000
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_ID = re.compile(r'[0-9a-f]{32}')
_FILE = re.compile(r'([0-9a-f]{32})(\.draft)?\.json')
READING_REFERENCE_KIND = 'saved-reading-copy'


def text_digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _original_path(value):
    """Validate portable provenance without opening or requiring the source.

    A restored project may refer to a disconnected drive or another operating
    system. This field only prevents source overwrites; it never grants access.
    """
    if value is None:
        return None
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(char) < 32 for char in value)
            or value.startswith(('//', '\\\\'))):
        raise ValueError('The original text path must be an absolute local path.')
    windows = PureWindowsPath(value)
    if not PurePosixPath(value).is_absolute() and not windows.is_absolute():
        raise ValueError('The original text path must be an absolute local path.')
    if windows.drive and (not re.fullmatch('[A-Za-z]:', windows.drive) or ':' in value[2:]):
        raise ValueError('The original text path must not use a network path or alternate data stream.')
    return value


def protect_original_path(original_path, destination):
    """Reject an original filename and aliases, even after a project is reopened."""
    original_path = _original_path(original_path)
    if original_path is None:
        return
    original, selected = Path(original_path), Path(destination).absolute()
    # Foreign-platform provenance remains intact on restore. It is not a local
    # pathname and must not be interpreted relative to the current directory.
    if not original.is_absolute():
        return
    if (selected.resolve() == original.resolve()
            or (selected.exists() and original.exists() and selected.samefile(original))):
        raise ValueError('The original source file is immutable. Choose a separate reading-copy or project filename.')


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


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


@dataclass(frozen=True)
class PronunciationRule:
    written: str
    spoken: str
    language: str = ''
    voice_id: str = ''
    regex: bool = False
    ignore_case: bool = False
    language_scope: bool = False

    def __post_init__(self):
        if not isinstance(self.written, str) or not self.written or len(self.written) > 1000:
            raise ValueError('Enter a written phrase or pattern of 1 to 1000 characters.')
        if not isinstance(self.spoken, str) or len(self.spoken) > 4000:
            raise ValueError('A pronunciation replacement must be at most 4000 characters.')
        _body(self.written)
        _body(self.spoken)
        if not isinstance(self.language, str) or len(self.language) > 64:
            raise ValueError('Invalid pronunciation language label.')
        if self.language:
            _text(self.language, 'pronunciation language', limit=64)
        if not isinstance(self.voice_id, str) or len(self.voice_id) > 2048:
            raise ValueError('Invalid pronunciation voice identity.')
        if self.voice_id:
            _text(self.voice_id, 'pronunciation voice identity', limit=2048)
        if any(type(value) is not bool for value in (self.regex, self.ignore_case, self.language_scope)):
            raise ValueError('Pronunciation flags must be booleans.')
        if self.language_scope and not self.language:
            raise ValueError('A language-scoped rule needs an explicit language identifier.')


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
    original_path: str | None = None
    legacy_origin: LegacyReadingOrigin | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Invalid reading-project identity.')
        _text(self.title, 'reading-copy title', limit=200)
        _body(self.original_text)
        if self.original_sha256 != text_digest(self.original_text):
            raise ValueError('Original reading text does not match its content digest.')
        _original_path(self.original_path)
        if self.legacy_origin is not None:
            if (type(self.legacy_origin) is not LegacyReadingOrigin
                    or decode_origin(asdict(self.legacy_origin), self.original_text) != self.legacy_origin
                    or self.source_location is not None):
                raise ValueError('Invalid or mixed imported reading-copy source identity.')
        if self.source_location is not None and self.original_path is not None:
            raise ValueError('A reading copy cannot replace its library source with an external file.')
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
        if type(self.rules) is not tuple:
            raise ValueError('Use a validated pronunciation dictionary.')
        validate_pronunciation_rules(self.rules)
        if sum(len(item.text.encode('utf-8')) for item in self.revisions) > MAX_PROJECT_BYTES // 2:
            raise ValueError('Reading-copy undo history exceeds its memory budget. Save a new copy of the text.')

    @property
    def current_text(self):
        return self.revisions[self.cursor].text

    @property
    def workspace(self):
        if self.source_location is not None:
            return self.source_location.workspace
        anchor = None if self.legacy_origin is None else self.legacy_origin.source_anchor
        return None if anchor is None else anchor.workspace

    @property
    def provenance(self):
        if self.legacy_origin is not None:
            origin = self.legacy_origin
            anchor = origin.source_anchor
            source = ('Independent archived reading history.' if anchor is None else
                      f'Source edition {anchor.edition}, revision {anchor.revision}, '
                      f'{len(anchor.positions)} exact source positions.')
            return (f'Imported historical reading revision {origin.selected_revision}. {source} '
                    f'Complete compact history: {origin.archive_sha256}. '
                    'Edits remain derivatives; archived positions never grant quotation authority.')
        if self.source_location is None:
            if self.original_path is not None:
                return (f'Reading copy imported from {self.original_path}. '
                        'The original file stays unchanged. Edited text is not a verified quotation.')
            return 'Independent reading copy; no authenticated library source is attached.'
        where = self.source_location
        return (f'Reading copy of source edition {where.edition}, revision {where.revision}, '
                f'{where.kind.replace("_", " ")} {where.index}. '
                'Edited text is not a verified quotation. The original source stays unchanged.')


def new_project(original_text='', *, title='Reading copy', source_location=None, original_path=None,
                legacy_origin=None):
    text = _body(original_text)
    first = Revision(0, text, 'Original text', _now())
    return NarrationProject(uuid4().hex, title, text, text_digest(text), source_location,
                            (first,), 0, original_path=original_path, legacy_origin=legacy_origin)


def revise(project, text, *, label='Edited reading text'):
    _body(text)
    if text == project.current_text:
        return project
    history = project.revisions[:project.cursor + 1]
    if len(history) >= MAX_HISTORY:
        raise ValueError('The 50-revision undo budget is full. Export the reading text and start a new copy.')
    next_revision = Revision(max(item.number for item in project.revisions) + 1, text, label, _now())
    return replace(project, revisions=history + (next_revision,), cursor=len(history))


def undo(project):
    if project.cursor == 0:
        raise ValueError('There is no earlier reading revision.')
    return replace(project, cursor=project.cursor - 1)


def redo(project):
    if project.cursor + 1 == len(project.revisions):
        raise ValueError('There is no later reading revision.')
    return replace(project, cursor=project.cursor + 1)


def normalization_preview(text):
    """Normalize spacing only; never guess punctuation, stress or missing words."""
    _body(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = '\n'.join(re.sub(r'[ \t]+', ' ', line).rstrip(' ') for line in text.split('\n'))
    return re.sub(r'\n{3,}', '\n\n', text)


def _rule_key(rule):
    return (rule.written, rule.regex, rule.ignore_case,
            rule.language.casefold() if rule.language_scope else '', rule.voice_id)


def validate_pronunciation_rules(rules):
    rules = tuple(rules)
    if (len(rules) > MAX_RULES or any(type(rule) is not PronunciationRule for rule in rules)
            or len({_rule_key(rule) for rule in rules}) != len(rules)):
        raise ValueError('Invalid or duplicate pronunciation phrases and scopes.')
    if sum(len(rule.written) + len(rule.spoken) for rule in rules) > MAX_RULE_CHARACTERS:
        raise ValueError('Pronunciation dictionary exceeds its character budget. Use a smaller project dictionary.')
    return rules


def _pronunciation_context(language, voice_id):
    for value, label, limit in ((language, 'selected pronunciation language', 64),
                                (voice_id, 'selected pronunciation voice', 2048)):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError('Invalid ' + label + '.')
        if value:
            _text(value, label, limit=limit)
    return language.casefold(), voice_id


def _check_pronunciation_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A pronunciation cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Pronunciation preview cancelled. The reading text is unchanged.')


def _pattern_matches(text, selected, *, timeout, cancelled):
    """Run only reviewed package code, with pattern/text sent as bounded data."""
    from ..audio.engines import _run, EngineUnavailable
    from ..audio.model_manifest import offline_environment
    message = dict(version=1, text=text, rules=[
        dict(index=index, pattern=rule.written, replacement=rule.spoken,
             regex=rule.regex, ignore_case=rule.ignore_case)
        for index, rule in selected])
    raw = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError('Pronunciation request exceeds its byte budget.')
    try:
        reply = _run([sys.executable, '-I', '-B', '-S',
                      str(Path(__file__).with_name('pronunciation_worker.py'))],
                     data=raw, cancelled=cancelled, timeout=timeout, env=offline_environment())
    except TimeoutError:
        raise TimeoutError('Pronunciation patterns exceeded their time budget. The reading text is unchanged.') from None
    except EngineUnavailable:
        raise ValueError('The isolated pronunciation worker could not complete. The reading text is unchanged.') from None
    if len(reply) > MAX_REGEX_RESPONSE:
        raise ValueError('Pronunciation matches exceed their response budget.')
    try:
        value = json.loads(reply.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('The pronunciation worker returned an invalid bounded response.') from None
    if type(value) is dict and set(value) == {'error'}:
        reasons = {'invalid-pattern': 'A pronunciation pattern or replacement is invalid.',
                   'work-limit': 'Pronunciation matches exceed their work or output budget.',
                   'invalid-request': 'The pronunciation worker rejected its bounded request.'}
        reason = value['error']
        raise ValueError(reasons.get(reason, 'The pronunciation worker could not complete.')
                         if type(reason) is str else 'The pronunciation worker returned an invalid error.')
    if (type(value) is not dict or set(value) != {'matches'} or type(value['matches']) is not list
            or len(value['matches']) > MAX_REGEX_MATCHES):
        raise ValueError('The pronunciation worker returned an invalid match inventory.')
    allowed = {index for index, _ in selected}
    seen, rows = set(), []
    for row in value['matches']:
        _check_pronunciation_cancelled(cancelled)
        if (type(row) is not list or len(row) != 4
                or any(type(part) is not int for part in row[:2] + row[3:])
                or not 0 <= row[0] <= row[1] <= len(text) or row[3] not in allowed
                or type(row[2]) is not str):
            raise ValueError('The pronunciation worker returned an invalid match.')
        key = row[0], row[1], row[3]
        if key in seen:
            raise ValueError('The pronunciation worker repeated a match.')
        seen.add(key)
        _body(row[2])
        rows.append(row)
    return rows


def pronunciation_preview(text, rules, *, language='', voice_id='', cancelled=lambda: False, timeout=1.0):
    """One atomic, non-cascading pass; patterns run in a bounded local child.

    Legacy language labels remain descriptive unless language_scope is True.
    Scoped rules match the explicitly selected language/voice only. At each
    source position the longest match wins, then voice/language specificity,
    then dictionary order. No replacement is scanned again.
    """
    _body(text)
    rules = validate_pronunciation_rules(rules)
    language, voice_id = _pronunciation_context(language, voice_id)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 10:
        raise ValueError('Pronunciation time budget must be between 0.05 and 10 seconds.')
    _check_pronunciation_cancelled(cancelled)
    selected = [(index, rule) for index, rule in enumerate(rules)
                if (not rule.language_scope or rule.language.casefold() == language)
                and (not rule.voice_id or rule.voice_id == voice_id)]
    if not selected:
        return text, 0
    rank = {index: (2 * bool(rule.voice_id) + bool(rule.language_scope), -index)
            for index, rule in selected}
    patterns = [(index, rule) for index, rule in selected if rule.regex or rule.ignore_case]
    matches = _pattern_matches(text, patterns, timeout=timeout, cancelled=cancelled) if patterns else ()
    by_start = {}
    for start, end, replacement, index in matches:
        candidate = (end, replacement, index)
        prior = by_start.get(start)
        if prior is None or (end, *rank[index]) > (prior[0], *rank[prior[2]]):
            by_start[start] = candidate
    tree = {}
    for index, rule in selected:
        if rule.regex or rule.ignore_case:
            continue
        node = tree
        for char in rule.written:
            node = node.setdefault(char, {})
        prior = node.get(None)
        if prior is None or rank[index] > rank[prior[1]]:
            node[None] = rule.spoken, index
    pieces, cursor, position, size, count, steps = [], 0, 0, 0, 0, 0
    while position <= len(text):
        if position % 1024 == 0:
            _check_pronunciation_cancelled(cancelled)
        node, end, candidate = tree, position, by_start.get(position)
        while end < len(text):
            steps += 1
            if steps > MAX_MATCH_STEPS:
                raise ValueError('Pronunciation matching exceeded its work budget. Use a smaller text section.')
            if steps % 1024 == 0:
                _check_pronunciation_cancelled(cancelled)
            node = node.get(text[end])
            if node is None:
                break
            end += 1
            if None in node:
                replacement, index = node[None]
                if candidate is None or (end, *rank[index]) > (candidate[0], *rank[candidate[2]]):
                    candidate = end, replacement, index
        if candidate is None:
            position += 1
            continue
        matched_end, replacement, _ = candidate
        unchanged = text[cursor:position]
        size += len(unchanged.encode('utf-8')) + len(replacement.encode('utf-8'))
        if size > MAX_TEXT_BYTES:
            raise ValueError('Pronunciation expansion exceeds the reading-section budget.')
        pieces.extend((unchanged, replacement))
        cursor, position, count = matched_end, max(position + 1, matched_end), count + 1
    pieces.append(text[cursor:])
    _check_pronunciation_cancelled(cancelled)
    return _body(''.join(pieces)), count


def _dictionary_value(value):
    canonical = False
    if type(value) is list:
        items = value  # Existing unversioned literal-editor interchange.
    elif type(value) is dict and set(value) == {'schema', 'rules'} and value['schema'] == 'carr.pronunciations.v1':
        items, canonical = value['rules'], True
    elif (type(value) is dict and set(value) == {'kind', 'version', 'rules'}
          and value['kind'] == 'carr-pronunciation-dictionary' and type(value['version']) is int
          and value['version'] == 1):
        items = value['rules']
    else:
        raise ValueError('Unsupported pronunciation dictionary format.')
    if type(items) is not list or len(items) > MAX_RULES:
        raise ValueError('Invalid pronunciation dictionary entry count.')
    rules = []
    for row in items:
        if type(row) is not dict:
            raise ValueError('Invalid pronunciation dictionary entry.')
        if canonical:
            if not {'pattern', 'replacement'} <= row.keys() or row.keys() - {
                    'pattern', 'replacement', 'language', 'voice_id', 'regex', 'ignore_case'}:
                raise ValueError('Invalid canonical pronunciation dictionary entry.')
            row = dict(written=row['pattern'], spoken=row['replacement'], language=row.get('language', ''),
                       voice_id=row.get('voice_id', ''), regex=row.get('regex', False),
                       ignore_case=row.get('ignore_case', False), language_scope=bool(row.get('language', '')))
        try:
            rules.append(PronunciationRule(**row))
        except TypeError as exc:
            raise ValueError('Invalid pronunciation dictionary fields.') from exc
    return validate_pronunciation_rules(rules)


def decode_pronunciation_dictionary(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DICTIONARY_BYTES:
        raise ValueError('Pronunciation dictionary exceeds its editing byte budget.')
    def constant(_):
        raise ValueError('Nonfinite pronunciation dictionary value.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=constant)
        return _dictionary_value(value)
    except (UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError('Invalid pronunciation dictionary.') from exc


def encode_pronunciation_dictionary(rules):
    value = dict(kind='carr-pronunciation-dictionary', version=1,
                 rules=[asdict(rule) for rule in validate_pronunciation_rules(rules)])
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(raw) > MAX_DICTIONARY_BYTES:
        raise ValueError('Pronunciation dictionary exceeds its interchange byte budget.')
    return raw


def load_pronunciation_dictionary(path):
    from ..audio.jobs import read_json
    return _dictionary_value(read_json(path, limit=MAX_DICTIONARY_BYTES))


def source_spans(project):
    """Linear-time conservative alignment: exact edges and one derived middle.

    Ranges use Python Unicode offsets [start, end). No source offset is claimed
    for the middle, even if it happens to contain unchanged smaller phrases.
    """
    old, new = project.original_text, project.current_text
    if project.legacy_origin is not None:
        # A canonical edit may rewrite exactly the same characters. Do not
        # infer recovered exactness by matching those words to the original.
        # The complete, explicit legacy offset map remains in its archive.
        if project.revisions[project.cursor].number != 0:
            return (('derived', 0, len(old), 0, len(new)),) if old or new else ()
        anchor = project.legacy_origin.source_anchor
        if anchor is not None:
            spans, cursor = [], 0
            for row in anchor.positions:
                if row.start > cursor:
                    spans.append(('derived', cursor, row.start, cursor, row.start))
                if row.end > row.start:
                    spans.append(('unchanged', row.start, row.end, row.start, row.end))
                cursor = row.end
            return tuple(spans)
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-suffix-1] == new[-suffix-1]:
        suffix += 1
    result = []
    if prefix:
        result.append(('unchanged', 0, prefix, 0, prefix))
    if prefix != len(old) or prefix != len(new):
        result.append(('derived', prefix, len(old)-suffix, prefix, len(new)-suffix))
    if suffix:
        result.append(('unchanged', len(old)-suffix, len(old), len(new)-suffix, len(new)))
    return tuple(result)


def comparison_text(project):
    lines = [project.provenance, f'Original SHA256: {project.original_sha256}',
             f'Reading revision: {project.revisions[project.cursor].number}',
             'Offsets are Unicode characters, starting from zero.']
    if project.current_text == project.original_text and project.legacy_origin is None:
        lines.append('The reading copy is identical to the original text.')
    else:
        for kind, a, b, c, d in source_spans(project):
            lines.append(f'{kind.capitalize()}: original [{a}, {b}); reading copy [{c}, {d}).')
        lines.append('The derived range has no claim of exact source-word alignment.')
    return '\n'.join(lines)


def encode_project(project):
    if type(project) is not NarrationProject:
        raise ValueError('A validated reading project is required.')
    advanced = any(rule.voice_id or rule.regex or rule.ignore_case or rule.language_scope for rule in project.rules)
    version = 4 if project.legacy_origin is not None else (3 if advanced else 2)
    value = dict(kind='carr-reading-copy', version=version, **asdict(project))
    if project.legacy_origin is None:
        value.pop('legacy_origin')
    if version == 2:
        value['rules'] = [dict(written=rule.written, spoken=rule.spoken, language=rule.language)
                          for rule in project.rules]
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_PROJECT_BYTES:
        raise ValueError('Reading project exceeds its storage budget.')
    return raw


def decode_project(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PROJECT_BYTES:
        raise ValueError('Invalid or oversized reading project.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
        fields = {'kind', 'version', 'id', 'title', 'original_text', 'original_sha256',
                  'source_location', 'revisions', 'cursor', 'rules'}
        if type(value) is dict and value.get('version') in (2, 3, 4):
            fields.add('original_path')
        if type(value) is dict and value.get('version') == 4:
            fields.add('legacy_origin')
        if (type(value) is not dict or set(value) != fields or value['kind'] != 'carr-reading-copy'
                or type(value['version']) is not int or value['version'] not in (1, 2, 3, 4)
                or type(value['revisions']) is not list or len(value['revisions']) > MAX_HISTORY
                or type(value['rules']) is not list or len(value['rules']) > MAX_RULES):
            raise ValueError('Unsupported reading-project format.')
        location = None if value['source_location'] is None else _location(value['source_location'])
        revisions = tuple(Revision(**row) for row in value['revisions'])
        if value['version'] in (1, 2):
            if any(type(row) is not dict or not {'written', 'spoken'} <= row.keys()
                   or row.keys() - {'written', 'spoken', 'language'} for row in value['rules']):
                raise ValueError('Legacy reading rules cannot contain unversioned pattern or scope fields.')
        elif any(type(row) is not dict or set(row) != {'written', 'spoken', 'language', 'voice_id',
                 'regex', 'ignore_case', 'language_scope'} for row in value['rules']):
            raise ValueError('Version 3/4 reading rules require their complete scoped fields.')
        rules = tuple(PronunciationRule(**row) for row in value['rules'])
        return NarrationProject(value['id'], value['title'], value['original_text'], value['original_sha256'],
                                location, revisions, value['cursor'], rules, value.get('original_path'),
                                decode_origin(value['legacy_origin'], value['original_text'])
                                if value['version'] == 4 else None)
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


def _read_bytes(path, *, allow_empty=False):
    _regular_if_present(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, 'st_file_attributes', 0) & 0x400):
        raise ValueError('Reading-project path must be a regular file without links.')
    if not (0 if allow_empty else 1) <= before.st_size <= MAX_PROJECT_BYTES:
        raise ValueError('Reading-project file is empty or oversized.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
            raise ValueError('Reading project changed while opening.')
        raw = stream.read(MAX_PROJECT_BYTES + 1)
    if len(raw) != before.st_size or _file_identity(path.lstat()) != _file_identity(before):
        raise ValueError('Reading project changed while reading.')
    return raw


def _directory(root, *, create=False):
    root = _private_root(root)
    path = root / DIRECTORY
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if optional_present(path):
        _private_root(path)
    return path


def snapshot_narration_files(root):
    """Return strict validated relative filenames/bytes; caller holds writer lock."""
    path = _directory(root)
    if not optional_present(path):
        return {}
    result, total = {}, 0
    for file in path.iterdir():
        # Interrupted private staging files are not recoverable committed drafts.
        if file.name.startswith('.reading-copy-') and file.name.endswith('.tmp'):
            _regular_if_present(file)
            continue
        if len(result) >= MAX_PROJECTS * 2:
            raise ValueError('Too many reading projects for this backup budget.')
        raw = _read_bytes(file)
        validate_narration_payload(file.name, raw)
        total += len(raw)
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError('Reading projects exceed the backup memory budget.')
        result[DIRECTORY + '/' + file.name] = raw
    return result


def _validate_retained_origin(root, project):
    if project.legacy_origin is None:
        return
    from .legacy_reading import LegacyReadingStore
    origin = project.legacy_origin
    snapshot = LegacyReadingStore(root).load(origin.archive_sha256,
                                             revision=origin.selected_revision)
    if snapshot.source_text != project.original_text or snapshot.origin != origin:
        raise ValueError('The reading project does not match its complete retained source history.')


class NarrationStore:
    def __init__(self, root):
        self.root = _private_root(root)

    def _path(self, project_id, draft=False, *, create=False):
        if not isinstance(project_id, str) or not _ID.fullmatch(project_id):
            raise ValueError('Invalid reading-project identity.')
        return _directory(self.root, create=create) / (project_id + ('.draft' if draft else '') + '.json')

    def load(self, project_id, *, draft=False):
        path = self._path(project_id, draft)
        raw = _read_bytes(path)
        project = validate_narration_payload(path.name, raw)
        _validate_retained_origin(self.root, project)
        return project, sha256(raw).hexdigest()

    def save(self, project, *, expected_digest=None, draft=False):
        raw = encode_project(project)
        if expected_digest is not None and (not isinstance(expected_digest, str)
                or not re.fullmatch('[0-9a-f]{64}', expected_digest)):
            raise ValueError('Invalid reading-project save lease.')
        with exclusive_writer(self.root):
            _validate_retained_origin(self.root, project)
            path = self._path(project.id, draft, create=True)
            protect_original_path(project.original_path, path)
            try:
                previous = _read_bytes(path)
            except FileNotFoundError:
                previous = None
            digest = None if previous is None else sha256(previous).hexdigest()
            if digest != expected_digest:
                raise ValueError('This reading copy changed elsewhere. Reopen it or save a new copy.')
            if previous is not None:
                prior = validate_narration_payload(path.name, previous)
                if (prior.original_text, prior.source_location, prior.original_path, prior.legacy_origin) != (
                        project.original_text, project.source_location, project.original_path, project.legacy_origin):
                    raise ValueError('The original source of a reading project cannot be replaced.')
            temporary = None
            try:
                with NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.reading-copy-',
                                        suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Cooperating writers are locked. Detect ordinary external edits
                # before publication too; this is not a hostile-filesystem CAS.
                try:
                    current = _read_bytes(path)
                except FileNotFoundError:
                    current = None
                if current != previous:
                    raise ValueError('Reading project changed before publication; nothing was replaced.')
                os.replace(temporary, path)
                temporary = None
                if os.name != 'nt':
                    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return sha256(raw).hexdigest()

    def clear_draft(self, project_id, *, expected_digest):
        with exclusive_writer(self.root):
            path = self._path(project_id, True)
            try:
                raw = _read_bytes(path)
            except FileNotFoundError:
                return
            if sha256(raw).hexdigest() != expected_digest:
                raise ValueError('A different recovery draft exists; it was kept.')
            path.unlink()

    def list_projects(self, workspace=None):
        with exclusive_writer(self.root):
            files = snapshot_narration_files(self.root)
        projects = {}
        for filename, raw in files.items():
            project = validate_narration_payload(filename, raw)
            if workspace is not None and project.workspace is not None:
                if project.workspace != workspace:
                    continue
            row = projects.setdefault(project.id, {'project': project, 'saved': False, 'draft': False})
            is_draft = filename.endswith('.draft.json')
            row['draft' if is_draft else 'saved'] = True
            if not is_draft:
                row['project'] = project
        return tuple(sorted(projects.values(), key=lambda row: (row['project'].title.casefold(), row['project'].id)))


def import_text(path, *, title=None):
    path = Path(_original_path(str(Path(path).absolute())))
    raw = _read_bytes(path, allow_empty=True)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError('The UTF-8 text file exceeds the reading-section budget.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('Choose a UTF-8 text file.') from exc
    return new_project(text, title=title or path.stem[:200] or 'Imported reading copy',
                       original_path=str(path.resolve(strict=True)))


def export_text(project, path, *, private_root, overwrite=False, approval=None):
    protect_original_path(project.original_path, path)
    if Path(path).exists() and not overwrite:
        raise FileExistsError('Choose another filename or explicitly approve replacing the existing export.')
    write_export(path, project.current_text, private_root=private_root,
                 require_absent=not overwrite, approval=approval)


def save_as(project, *, title=None):
    """Fork a project identity while keeping immutable source/history provenance."""
    return replace(project, id=uuid4().hex, title=title or project.title)


def reading_project_reference(project, *, saved_digest):
    """Describe one explicitly saved reading revision for a one-way handoff."""
    raw = encode_project(project)
    if saved_digest != sha256(raw).hexdigest():
        raise ValueError('Save this exact reading revision before preparing Audio Studio.')
    reference = dict(kind=READING_REFERENCE_KIND, project_id=project.id,
                revision=project.revisions[project.cursor].number,
                project_sha256=saved_digest, original_sha256=project.original_sha256,
                reading_sha256=text_digest(project.current_text),
                source_location=None if project.source_location is None else asdict(project.source_location),
                original_path=project.original_path)
    if project.legacy_origin is not None:
        reference['legacy_origin'] = asdict(project.legacy_origin)
    return reference


def validate_reading_project_reference(value, *, original_text, source_id):
    """Validate stored handoff provenance without treating it as quotation authority.

    reading_sha256 identifies the saved handoff revision. Later Studio edits or
    undo can intentionally change the current derivative, so it is not compared
    to the Studio draft's current text.
    """
    fields = {'kind', 'project_id', 'revision', 'project_sha256', 'original_sha256',
              'reading_sha256', 'source_location', 'original_path'}
    if type(value) is dict and 'legacy_origin' in value:
        fields.add('legacy_origin')
    if (type(value) is not dict or set(value) != fields or value['kind'] != READING_REFERENCE_KIND
            or not isinstance(value['project_id'], str) or not _ID.fullmatch(value['project_id'])
            or type(value['revision']) is not int or value['revision'] < 0):
        raise ValueError('Invalid saved reading-copy reference.')
    for field in ('project_sha256', 'original_sha256', 'reading_sha256'):
        if not isinstance(value[field], str) or not re.fullmatch('[0-9a-f]{64}', value[field]):
            raise ValueError('Invalid saved reading-copy digest.')
    if (text_digest(original_text) != value['original_sha256']
            or source_id != f"reading-project:{value['project_id']}:revision:{value['revision']}"):
        raise ValueError('The reading-copy reference does not match its immutable original.')
    _original_path(value['original_path'])
    location = None if value['source_location'] is None else _location(value['source_location'])
    if location is not None and (location.text_sha256 != value['original_sha256'] or value['original_path'] is not None):
        raise ValueError('The reading-copy source location does not match its original.')
    if 'legacy_origin' in value:
        decode_origin(value['legacy_origin'], original_text)
        if location is not None:
            raise ValueError('A saved reading copy cannot mix two source identity formats.')
    return value
