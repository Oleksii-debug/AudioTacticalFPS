"""Immutable literal book plans adapted from canonical PR152.

The current ProductionPlan, NarrationDraft and owned regex worker remain the
only production and text-editing contracts. A saved plan grants no output or
runtime permission.
"""
from dataclasses import asdict, dataclass, replace
from functools import cached_property
from hashlib import sha256
from itertools import chain
import json
import re
from string import Formatter
from uuid import uuid4

from ..reader.book_source import capture_source, source_context, validate_capture
from ..reader.ebook_contents import open_ebook
from .models import Role, canonical


def encode(value):
    return (canonical(value) + '\n').encode('utf-8')


def _display(value):
    return ''.join(' ' if ord(c) < 32 or 127 <= ord(c) < 160 else c for c in value).strip()[:256] or 'Book part'


MAX_CHARACTERS = 2_000_000
PLAN_SCHEMA = 'carr-book-audio/2'
REFERENCE_KIND = 'book-audio-part'
MAX_BOUNDARIES = 10000
MAX_PARTS = 256
SOURCE_PREFIX = 'carr-book-audio:'
FORMATS = ('wav', 'flac', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aiff', 'wma')
MODES = ('document', 'chapters', 'pages', 'paragraphs', 'sentences',
         'characters', 'blank-lines', 'regex', 'headings', 'bookmarks')


def _identity(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid book-audio plan identity.')
    return value


def _check(cancelled, guard):
    if cancelled():
        raise InterruptedError('Book audio stopped; existing Studio jobs are retained.')
    guard()
    if cancelled():
        raise InterruptedError('Book audio stopped; existing Studio jobs are retained.')


def _short(value, label, maximum=512):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or '\x00' in value):
        raise ValueError('Invalid ' + label + '.')
    value.encode('utf-8', 'strict')
    return value


@dataclass(frozen=True)
class SplitOptions:
    mode: str = 'document'
    characters: int = 10000
    pattern: str = ''
    ignore_case: bool = False
    name_template: str = '{index:04d} - {book} - {part}'

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError('Choose a supported deterministic text split; duration estimates are unavailable.')
        if type(self.characters) is not int or not 1 <= self.characters <= 100000:
            raise ValueError('The character split must be between 1 and 100000 Unicode characters.')
        if type(self.pattern) is not str or len(self.pattern) > 1000 or '\x00' in self.pattern:
            raise ValueError('The regular expression exceeds its limit.')
        if self.mode == 'regex' and not self.pattern:
            raise ValueError('Enter the reviewed regular expression.')
        if type(self.ignore_case) is not bool:
            raise ValueError('Choose an explicit regex case option.')
        _short(self.name_template, 'name template')
        if any(char in self.name_template for char in '/\\:<>"|?*\x00\r\n'):
            # A colon is allowed only inside the supported index format below.
            without_index = re.sub(r'\{index:0?[1-4]d\}', '', self.name_template)
            if any(char in without_index for char in '/\\:<>"|?*\x00\r\n'):
                raise ValueError('Names must be one local filename component.')
        seen = set()
        try:
            for _, field, spec, conversion in Formatter().parse(self.name_template):
                if field is None:
                    continue
                if field not in ('index', 'book', 'part') or conversion is not None:
                    raise ValueError('Name fields are index, book and part only.')
                if (field == 'index' and spec and re.fullmatch('0?[1-4]d', spec) is None
                        or field != 'index' and spec):
                    raise ValueError('Only index supports a one-to-four-digit decimal format.')
                seen.add(field)
        except (ValueError, IndexError) as exc:
            raise ValueError('Use a name such as {index:04d} - {book} - {part}.') from exc
        if 'index' not in seen:
            raise ValueError('Include {index} in each reviewed part name.')

    def title(self, index, book, label):
        # Path authority comes only from the separately selected output parent.
        # The exact sanitized directory name is shown in the review.
        return _display(self.name_template.format(index=index, book=book[:160], part=label[:160]))


@dataclass(frozen=True)
class BookPart:
    start: int
    end: int
    label: str

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or not 0 <= self.start < self.end <= MAX_CHARACTERS:
            raise ValueError('Invalid exact book-part range.')
        _short(self.label, 'book-part label', 512)


@dataclass(frozen=True)
class BookPlan:
    id: str
    source_json: str
    options: SplitOptions
    role: Role
    parts: tuple[BookPart, ...]
    format: str = 'wav'
    sample_rate: int = 24000
    landmarks_json: str = ''

    def __post_init__(self):
        _identity(self.id)
        if (not isinstance(self.options, SplitOptions) or not isinstance(self.role, Role)
                or not self.role.voice or self.role.name != 'Narrator' or self.format not in FORMATS
                or self.sample_rate not in (8000, 16000, 22050, 24000, 44100, 48000, 96000)):
            raise ValueError('Choose a typed local voice, supported format and sample rate.')
        if type(self.source_json) is not str or len(self.source_json) > 12_000_000:
            raise ValueError('Invalid bounded source snapshot.')
        try:
            source = json.loads(self.source_json)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid book source snapshot.') from exc
        if (type(source) is not dict or set(source) != {
                'text', 'title', 'warnings', 'source_anchor', 'source_metadata', 'source_topic'}
                or type(source['text']) is not str or not source['text'].strip()
                or len(source['text']) > MAX_CHARACTERS):
            raise ValueError('Choose a nonempty bounded library source.')
        _short(source['title'], 'source title', 16384)
        if (type(source['warnings']) is not list or len(source['warnings']) > 10000
                or any(type(item) is not str or len(item) > 4096 for item in source['warnings'])):
            raise ValueError('Invalid reading warnings.')
        if source_context(source['text'], source['source_anchor'],
                          source['source_metadata'], source['source_topic']) is None:
            raise ValueError('Book audio requires the exact existing library source.')
        if encode(source).decode('utf-8') != self.source_json:
            raise ValueError('The immutable source snapshot is not canonical.')
        if (type(self.parts) is not tuple or not 1 <= len(self.parts) <= MAX_PARTS
                or not all(isinstance(part, BookPart) for part in self.parts)):
            raise ValueError('A book plan must contain between 1 and 256 typed parts.')
        cursor = 0
        for part in self.parts:
            if part.start != cursor or not source['text'][part.start:part.end].strip():
                raise ValueError('Book parts must preserve every source character exactly once.')
            cursor = part.end
        if cursor != len(source['text']):
            raise ValueError('The split does not cover the exact selected text.')
        if type(self.landmarks_json) is not str:
            raise ValueError('Invalid immutable landmark snapshot.')
        if self.options.mode in ('headings', 'bookmarks'):
            from .book_landmarks import entries
            snapshot = self.landmarks
            entries(source, snapshot)
            if (snapshot['kind'] != self.options.mode
                    or encode(snapshot).decode('utf-8') != self.landmarks_json
                    or split_parts(source, self.options, landmarks=snapshot) != self.parts):
                raise ValueError('The reviewed landmark split differs from its captured source intervals.')
        elif self.landmarks_json:
            raise ValueError('Landmark metadata applies only to explicit heading or bookmark splits.')

    @property
    def source(self):
        return json.loads(self.source_json)

    @property
    def landmarks(self):
        if not self.landmarks_json:
            return None
        try:
            return json.loads(self.landmarks_json)
        except (TypeError, ValueError) as exc:
            raise ValueError('Invalid captured book landmark metadata.') from exc

    @cached_property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    def to_dict(self):
        value = {'schema': PLAN_SCHEMA, 'id': self.id, 'source': self.source,
                'options': asdict(self.options), 'role': asdict(self.role),
                'parts': [asdict(part) for part in self.parts], 'format': self.format,
                'sample_rate': self.sample_rate}
        if self.landmarks_json:
            value['landmarks'] = self.landmarks
        return value

    @classmethod
    def from_dict(cls, value):
        required = {'schema', 'id', 'source', 'options', 'role', 'parts', 'format', 'sample_rate'}
        if type(value) is not dict or not required <= set(value) <= required | {'landmarks'}:
            raise ValueError('Invalid saved book-audio plan fields.')
        if (value['schema'] != PLAN_SCHEMA or type(value['parts']) is not list
                or not 1 <= len(value['parts']) <= MAX_PARTS
                or type(value['options']) is not dict or type(value['role']) is not dict):
            raise ValueError('Unsupported book-audio plan.')
        # Validate bounded leaves before serializing shared Python containers.
        # A file byte cap alone cannot bound a pure API caller's repeated list.
        source = validate_capture(value['source'])
        if (len(source['text']) + sum(len(item) for item in source['warnings'])
                + sum(len(encode(item)) for item in source['source_anchor']['positions'])
                + sum(len(item or '') for item in source['source_metadata'].values()) > 12_000_000):
            raise ValueError('The exact book source snapshot exceeds its metadata work budget.')
        try:
            options = SplitOptions(**value['options'])
            role = Role.from_dict(value['role'])
            parts = tuple(BookPart(**part) for part in value['parts'])
            landmarks = ''
            if 'landmarks' in value:
                from .book_landmarks import entries
                entries(source, value['landmarks'])
                landmarks = encode(value['landmarks']).decode('utf-8')
            return cls(value['id'], encode(source).decode('utf-8'),
                       options, role, parts, value['format'], value['sample_rate'], landmarks)
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid saved book-audio plan.') from exc

    def job_id(self, index):
        if type(index) is not int or not 1 <= index <= len(self.parts):
            raise ValueError('Choose a part in this saved plan.')
        return sha256(encode(['book-audio-job', self.id, self.fingerprint, index])).hexdigest()[:32]

    def project(self, index):
        """Return the current literal, source-spanned ProductionPlan."""
        self.job_id(index)
        part = self.parts[index - 1]
        source = self.source
        selected = source['text'][part.start:part.end]
        from .production import plan_narration
        plan = plan_narration(selected,
            title=self.options.title(index, source['title'], part.label),
            voice=self.role.voice, language=self.role.language)
        return replace(plan, roles=(self.role,), output_format=self.format,
                       sample_rate=self.sample_rate, export_stems=False)

    def reference(self, index):
        part = self.parts[index - 1] if type(index) is int and 1 <= index <= len(self.parts) else None
        if part is None:
            raise ValueError('Choose a part in this reviewed plan.')
        return {'kind': REFERENCE_KIND, 'plan': self.id, 'plan_sha256': self.fingerprint,
                'project_id': self.job_id(index), 'index': index,
                'start': part.start, 'end': part.end,
                'text_sha256': sha256(self.source['text'][part.start:part.end].encode('utf-8')).hexdigest()}

    def document(self, index):
        """Ordinary managed Studio document; no external output permission."""
        from ..reader.narration_text import NarrationDraft
        project = self.project(index)
        reference = self.reference(index)
        draft = NarrationDraft(project.original_script, source_id=reference_source_id(reference))
        return {'kind': 'research-audio-studio', 'version': 2, 'mode': 'LOCAL ONLY',
                'project_id': reference['project_id'], 'title': project.title,
                'voice': self.role.voice, 'language': self.role.language,
                'input_kind': 'narration', 'output_dir': None,
                'source_reference': reference, 'draft': draft.to_dict()}

    def provenance(self, index):
        part = self.parts[index - 1]
        source = self.source
        value = {'schema': 'carr-book-audio-source/1', 'plan': self.id,
                'plan_sha256': self.fingerprint,
                'source_anchor': source['source_anchor'], 'source_metadata': source['source_metadata'],
                'source_topic': source['source_topic'], 'warnings': source['warnings'],
                'source_sha256': sha256(source['text'].encode('utf-8')).hexdigest(),
                'part': {'index': index, 'start': part.start, 'end': part.end,
                         'text_sha256': sha256(source['text'][part.start:part.end].encode('utf-8')).hexdigest()},
                'notice': 'A literal reading derivative; this anchor never authorizes a quotation.'}
        if self.landmarks_json:
            from .book_landmarks import entries
            value['landmarks'] = {'kind': self.options.mode,
                'review_sha256': sha256(self.landmarks_json.encode('utf-8')).hexdigest(),
                'boundaries': [entry for entry in entries(source, self.landmarks)
                               if part.start <= entry['start'] < part.end]}
        return value


def split_parts(source, options, *, chapter_labels=None, landmarks=None, cancelled=lambda: False):
    if not isinstance(options, SplitOptions):
        raise ValueError('Validated split options are required.')
    value = source['text']
    if not value.strip() or len(value) > MAX_CHARACTERS:
        raise ValueError('Choose a nonempty source within the reading limit.')
    positions = source['source_anchor']['positions']
    landmark_rows = ()
    if options.mode == 'document':
        boundaries = []
    elif options.mode in ('chapters', 'pages'):
        if options.mode == 'chapters' and (chapter_labels is None
                or any(position['ordinal'] not in chapter_labels for position in positions)):
            raise ValueError('Chapter splitting requires the actual EPUB chapter inventory.')
        if options.mode == 'pages' and any(position['kind'] != 'physical_page' for position in positions):
            raise ValueError('This source has logical segments, not physical pages. Choose another split.')
        boundaries = [position['start'] for position in positions[1:]]
    elif options.mode in ('headings', 'bookmarks'):
        from .book_landmarks import entries
        landmark_rows = entries(source, landmarks)
        if landmarks['kind'] != options.mode:
            raise ValueError('Choose the exact reviewed landmark type.')
        boundaries = [row['start'] for row in landmark_rows]
    elif options.mode == 'characters':
        boundaries = range(options.characters, len(value), options.characters)
    elif options.mode in ('paragraphs', 'blank-lines', 'sentences'):
        pattern = {'paragraphs': r'\r\n|\r|\n',
                   'blank-lines': r'(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*\r?\n)*',
                   'sentences': r'[.!?…。！？]+(?:["”’»)\]]*)[ \t\r\n]*'}[options.mode]
        boundaries = (match.end() for match in re.finditer(pattern, value))
    else:
        # Current bounded process/worker, including pre-allocation expansion
        # guard and owned native child cleanup; never Python re on the UI thread.
        from ..reader.narration import PronunciationRule, _pattern_matches
        rule = PronunciationRule(options.pattern, '', regex=True, ignore_case=options.ignore_case)
        matches = _pattern_matches(value, [(0, rule)], timeout=1, cancelled=cancelled)
        boundaries = (row[1] for row in matches)
    cursor, ranges = 0, []
    for boundary_count, end in enumerate(chain(boundaries, (len(value),)), 1):
        if boundary_count > MAX_BOUNDARIES:
            raise ValueError('The split exceeds its bounded boundary scan; choose larger parts or a smaller range.')
        if cancelled():
            raise InterruptedError('The book split was cancelled.')
        if end <= cursor:
            continue
        if value[cursor:end].strip():
            ranges.append((cursor, end))
            cursor = end
            if len(ranges) > MAX_PARTS:
                raise ValueError('The split exceeds 256 parts. Choose larger parts or a smaller source range.')
    if cursor < len(value):
        start, _ = ranges[-1]
        ranges[-1] = (start, len(value))
    parts = []
    for index, (start, end) in enumerate(ranges, 1):
        if options.mode in ('chapters', 'pages'):
            covered = [row for row in positions if row['start'] < end and row['end'] > start]
            if options.mode == 'chapters':
                label = chapter_labels[covered[0]['ordinal']][:480]
                if len(covered) > 1:
                    label += ' (through chapter %d)' % covered[-1]['ordinal']
            else:
                label = 'Page %s' % covered[0]['index']
                if len(covered) > 1:
                    label += ' to %s' % covered[-1]['index']
        elif options.mode in ('headings', 'bookmarks'):
            labels = list(dict.fromkeys(row['label'] for row in landmark_rows if start <= row['start'] < end))
            label = '; '.join(labels)[:480] if labels else 'Opening text'
        else:
            label = 'Part %d' % index
        parts.append(BookPart(start, end, label))
    return tuple(parts)


def capture_plan(library, workspace, edition, *, options, role, format='wav', sample_rate=24000,
                 topic=None, first=None, last=None, chapter=None, revision=None,
                 bookmark_ids=None,
                 cancelled=lambda: False, guard=lambda: None):
    _check(cancelled, guard)
    if not isinstance(options, SplitOptions) or not isinstance(role, Role):
        raise ValueError('Validated split and voice options are required.')
    source = capture_source(library, workspace, edition, topic=topic, first=first, last=last,
                            chapter=chapter, revision=revision, cancelled=cancelled, guard=guard)
    labels = None
    if options.mode == 'chapters':
        contents = open_ebook(library, workspace, edition, authorized_topic=topic, cancelled=cancelled)
        if contents['revision'] != source['source_anchor']['revision']:
            raise ValueError('The EPUB chapter inventory changed during review.')
        labels = {row['ordinal']: row['title'] for row in contents['chapters']}
    landmarks = None
    if options.mode in ('headings', 'bookmarks'):
        from .book_landmarks import capture
        landmarks = capture(library, source, options.mode, bookmark_ids=bookmark_ids,
                            cancelled=cancelled, guard=guard)
    elif bookmark_ids is not None:
        raise ValueError('Bookmark identities apply only to bookmark splitting.')
    parts = split_parts(source, options, chapter_labels=labels, landmarks=landmarks, cancelled=cancelled)
    _check(cancelled, guard)
    plan = BookPlan(uuid4().hex, encode(source).decode('utf-8'), options, role, parts, format, sample_rate,
                    encode(landmarks).decode('utf-8') if landmarks is not None else '')
    # Bound and validate every literal Project before presenting an executable plan.
    for index in range(1, len(parts) + 1):
        plan.project(index)
    return plan



def reference_source_id(reference):
    return 'book-audio:' + ':'.join(str(reference[name]) for name in
                                  ('plan', 'plan_sha256', 'index', 'start', 'end'))


def validate_reference(reference, draft):
    keys = {'kind', 'plan', 'plan_sha256', 'project_id', 'index', 'start', 'end', 'text_sha256'}
    if (type(reference) is not dict or set(reference) != keys
            or reference['kind'] != REFERENCE_KIND):
        raise ValueError('Invalid current book-part reference.')
    _identity(reference['plan'])
    _identity(reference['project_id'])
    for name in ('plan_sha256', 'text_sha256'):
        if type(reference[name]) is not str or re.fullmatch('[0-9a-f]{64}', reference[name]) is None:
            raise ValueError('Invalid book-part fingerprint.')
    if (type(reference['index']) is not int or not 1 <= reference['index'] <= MAX_PARTS
            or type(reference['start']) is not int or type(reference['end']) is not int
            or not 0 <= reference['start'] < reference['end'] <= MAX_CHARACTERS
            or draft.source_id != reference_source_id(reference)
            or draft.source_sha256 != reference['text_sha256']
            or len(draft.source_text) != reference['end'] - reference['start']):
        raise ValueError('The managed part no longer matches its immutable book source.')
    draft.assert_source()
    return reference
