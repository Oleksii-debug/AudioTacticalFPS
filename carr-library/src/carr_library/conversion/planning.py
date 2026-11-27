"""Reviewed text boundaries and portable names over the existing reading copy.

Every character belongs to exactly one part. A plan never changes a reading
copy, interprets its prose as Studio commands, or approves source quotations.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import re
from string import Formatter
import unicodedata

from ..durable_state import encode
from ..research.export_file import _filename
from ..speech._io import check_cancelled, text_value
from ..speech.reading import ReadingDocument
from ..state_backup import relative_name

MAX_PARTS = 10000
MODES = ('whole', 'paragraphs', 'blank-lines', 'sentences', 'characters',
         'literal', 'regex', 'sections', 'chapters', 'pages', 'bookmarks',
         'estimated-duration')
_KINDS = ('section', 'chapter', 'page', 'bookmark')
_FIELDS = {'title', 'source', 'heading', 'first_line', 'index', 'part', 'total', 'document'}
_NUMBERS = {'index', 'part', 'total', 'document'}


@dataclass(frozen=True)
class SplitOptions:
    mode: str = 'whole'
    size: int = 5000
    marker: str = ''
    ignore_case: bool = False
    seconds: float = 300
    words_per_minute: int = 150

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError('Choose a supported split method.')
        if type(self.size) is not int or not 1 <= self.size <= 2_000_000:
            raise ValueError('The character limit must be from 1 to 2,000,000.')
        text_value(self.marker, 'split marker', maximum=1024, empty=True)
        if self.mode in ('literal', 'regex') and not self.marker:
            raise ValueError('Enter a nonempty split marker.')
        if type(self.ignore_case) is not bool:
            raise ValueError('Case matching must be a boolean choice.')
        if (type(self.seconds) not in (int, float) or not math.isfinite(self.seconds)
                or not 1 <= self.seconds <= 86400):
            raise ValueError('Estimated part duration must be from 1 to 86,400 seconds.')
        if type(self.words_per_minute) is not int or not 30 <= self.words_per_minute <= 1000:
            raise ValueError('The explicit estimate must use 30 to 1,000 whitespace words per minute.')


@dataclass(frozen=True)
class TextBoundary:
    start: int
    label: str
    kind: str = 'section'

    def __post_init__(self):
        if type(self.start) is not int or self.start < 0 or self.kind not in _KINDS:
            raise ValueError('Invalid supplied text boundary.')
        text_value(self.label, 'boundary label', maximum=512, empty=True)


@dataclass(frozen=True)
class TextPart:
    start: int
    end: int
    sha256: str
    label: str
    name: str
    source_start: int | None
    source_end: int | None
    source_exact: bool
    estimated_seconds: float | None = None


@dataclass(frozen=True)
class SplitPlan:
    reading_sha256: str
    text_sha256: str
    source_name: str
    template: str
    options: SplitOptions
    parts: tuple[TextPart, ...]
    notices: tuple[str, ...]

    def to_dict(self):
        return {'schema': 'carr-text-split/1', 'reading_sha256': self.reading_sha256,
                'text_sha256': self.text_sha256, 'source_name': self.source_name,
                'template': self.template, 'options': asdict(self.options),
                'parts': [asdict(part) for part in self.parts], 'notices': list(self.notices)}

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value):
        fields = {'schema', 'reading_sha256', 'text_sha256', 'source_name',
                  'template', 'options', 'parts', 'notices'}
        if type(value) is not dict or set(value) != fields or value['schema'] != 'carr-text-split/1':
            raise ValueError('Invalid reviewed split plan.')
        if (type(value['parts']) is not list or not 1 <= len(value['parts']) <= MAX_PARTS
                or type(value['notices']) is not list or len(value['notices']) > 16):
            raise ValueError('The split plan exceeds its bounded part/notice budget.')
        try:
            return cls(value['reading_sha256'], value['text_sha256'], value['source_name'],
                       value['template'], SplitOptions(**value['options']),
                       tuple(TextPart(**part) for part in value['parts']), tuple(value['notices']))
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid reviewed split-plan fields.') from exc


def reading_identity(document):
    if not isinstance(document, ReadingDocument):
        raise ValueError('Choose an existing reading copy.')
    return sha256(encode(document.to_dict())).hexdigest()


def portable_name(value):
    """One portable basename, including conservative byte and UTF-16 limits."""
    text_value(value, 'output filename', maximum=240)
    _filename(value)
    if (len(relative_name(value).parts) != 1 or value.startswith('.')
            or len(value.encode('utf-8')) > 240 or len(value.encode('utf-16-le')) > 480):
        raise ValueError('Choose one portable filename of at most 240 encoded bytes.')
    return value


def filename_key(value):
    portable_name(value)
    return unicodedata.normalize('NFC', value).casefold()


def _component(value):
    value = ' '.join(unicodedata.normalize('NFC', value).split())
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', '_', value)
    value = ' '.join(value.split()).strip(' .') or 'untitled'
    # Substituted text is visibly shortened, not interpreted as another path.
    while len(value.encode('utf-8')) > 72:
        value = value[:-1]
    return value


def validate_template(template):
    text_value(template, 'output naming template', maximum=512)
    try:
        pieces = tuple(Formatter().parse(template))
    except ValueError as exc:
        raise ValueError('Invalid braces in the naming template.') from exc
    for literal, field, specification, conversion in pieces:
        if any(char in '/\\\x00\r\n' for char in literal):
            raise ValueError('The naming template cannot contain a directory or newline.')
        if field is None:
            continue
        if field not in _FIELDS or conversion is not None:
            raise ValueError('Use only title, source, heading, first_line, index, part, total and document fields.')
        if specification and (field not in _NUMBERS or not re.fullmatch(r'0?[1-6]?d', specification)):
            raise ValueError('Only sequence fields accept a bounded integer format such as 04d.')
    return pieces


def render_name(template, *, title, source, heading, first_line, index, part, total, document=1):
    values = {key: _component(value) for key, value in
              (('title', title), ('source', source), ('heading', heading), ('first_line', first_line))}
    values.update(index=index, part=part, total=total, document=document)
    result = []
    for literal, field, specification, _ in validate_template(template):
        result.append(literal)
        if field is not None:
            result.append(format(values[field], specification))
    return portable_name(''.join(result) + '.txt')


def _markers(document, options, cancelled):
    # Reuse the accepted isolated regex protocol and result validation. It
    # imports no wx runtime; neither a regex nor source text can run in-process.
    from ..ui.find_replace_dialog import FindQuery, find_matches
    from ..ui.pronunciation_dialog import ReadingSnapshot
    snapshot = ReadingSnapshot.capture(document, (0, len(document.text)))
    results = find_matches(snapshot, FindQuery(options.marker, regex=options.mode == 'regex',
                                             ignore_case=options.ignore_case), cancelled=cancelled)
    return [start for start, _, _ in results.matches]


def source_boundaries(document):
    """Use actual retained positions only while their exact text map is intact."""
    anchor = document.source_anchor
    if anchor is None:
        return ()
    if document.text != document.source_text:
        raise ValueError('The reading copy was edited. Its original page boundaries need a new explicit review.')
    return tuple(TextBoundary(row['start'], f"{row['kind']} {row['index']}",
                              'page' if row['kind'] == 'physical_page' else 'section')
                 for row in anchor['positions'])


def plan_split(document, options=None, *, template='{index:04d}-{source}-{part:03d}',
               source_name='reading-copy', boundaries=(), first_index=1, document_index=1,
               cancelled=lambda: False):
    check_cancelled(cancelled)
    identity = reading_identity(document)
    options = options or SplitOptions()
    if not isinstance(options, SplitOptions):
        raise ValueError('Choose typed split settings.')
    validate_template(template)
    text_value(source_name, 'source name', maximum=512)
    if (type(first_index) is not int or not 1 <= first_index <= 1_000_000
            or type(document_index) is not int or not 1 <= document_index <= 1_000_000):
        raise ValueError('Output sequence numbers must be positive integers.')
    text = document.text
    if not text or not text.strip():
        raise ValueError('Choose nonempty reading text before planning outputs.')
    offsets, labels = {0, len(text)}, {}
    notices = ['Parts preserve the exact character stream; source quotations are not approved.']
    mode = options.mode
    if mode == 'characters':
        if (len(text) + options.size - 1) // options.size > MAX_PARTS:
            raise ValueError('Choose fewer boundaries; one plan supports at most 10,000 parts.')
        offsets.update(range(options.size, len(text), options.size))
    elif mode in ('paragraphs', 'blank-lines', 'sentences'):
        pattern = {'paragraphs': r'\r\n|\r|\n',
                   # A CRLF is one line break even when the regex backtracks.
                   'blank-lines': r'(?:\r\n|\r(?!\n)|(?<!\r)\n)[ \t]*(?:(?:\r\n|\r(?!\n)|(?<!\r)\n)[ \t]*)+',
                   'sentences': r'[.!?…。！？]+["”’\)\]]*(?:\s+|$)'}[mode]
        for match in re.finditer(pattern, text):
            check_cancelled(cancelled)
            offsets.add(match.end())
            if len(offsets) > MAX_PARTS + 1:
                raise ValueError('Choose fewer boundaries; one plan supports at most 10,000 parts.')
        notices.append('Sentence/paragraph boundaries are explicit punctuation or stored line-break rules, not linguistic inference.')
    elif mode in ('literal', 'regex'):
        offsets.update(_markers(document, options, cancelled))
        notices.append('Markers remain literal text at the beginning of the following part.')
    elif mode in ('sections', 'chapters', 'pages', 'bookmarks'):
        selected = []
        for row in boundaries:
            check_cancelled(cancelled)
            if not isinstance(row, TextBoundary) or len(selected) >= MAX_PARTS:
                raise ValueError('Supply a bounded list of actual source boundaries.')
            selected.append(row)
        selected = selected or source_boundaries(document)
        wanted = {'sections': 'section', 'chapters': 'chapter', 'pages': 'page', 'bookmarks': 'bookmark'}[mode]
        if len(selected) > MAX_PARTS or any(not isinstance(row, TextBoundary) for row in selected):
            raise ValueError('Supply a bounded list of actual source boundaries.')
        selected = [row for row in selected if row.kind == wanted]
        if not selected:
            raise ValueError(f'No actual {mode} were supplied for this reading copy; choose another method.')
        for row in selected:
            check_cancelled(cancelled)
            if not 0 <= row.start < len(text) or row.start in labels:
                raise ValueError('Source boundaries must be distinct existing character positions.')
            offsets.add(row.start)
            labels[row.start] = row.label
        notices.append('Named boundaries use supplied source positions; logical segments are never called printed pages.')
    elif mode == 'estimated-duration':
        words = max(1, math.floor(options.seconds * options.words_per_minute / 60))
        for index, match in enumerate(re.finditer(r'\S+\s*', text), 1):
            check_cancelled(cancelled)
            if index % words == 0:
                offsets.add(match.end())
            if len(offsets) > MAX_PARTS + 1:
                raise ValueError('Choose a larger estimated duration or fewer source words.')
        notices.append(f'ESTIMATE ONLY: {options.words_per_minute} whitespace words/minute; no audio has been measured. '
                       'Languages without word spaces and different voices may differ substantially.')
    ordered = sorted(offsets)
    if len(ordered) - 1 > MAX_PARTS:
        raise ValueError('Choose fewer boundaries; one plan supports at most 10,000 parts.')
    parts, names = [], set()
    for number, (start, end) in enumerate(zip(ordered, ordered[1:]), 1):
        check_cancelled(cancelled)
        part = text[start:end]
        label = labels.get(start, f'Part {number}')
        first_line = next((line.strip() for line in part.splitlines() if line.strip()), 'empty text')
        name = render_name(template, title=document.title, source=source_name, heading=label,
                           first_line=first_line, index=first_index + number - 1, part=number,
                           total=len(ordered) - 1, document=document_index)
        key = filename_key(name)
        if key in names:
            raise ValueError('The naming template produces duplicate portable filenames; include index or part.')
        names.add(key)
        origin = document.source_span(start, end)
        estimate = (round(len(re.findall(r'\S+', part)) * 60 / options.words_per_minute, 3)
                    if mode == 'estimated-duration' else None)
        parts.append(TextPart(start, end, sha256(part.encode('utf-8')).hexdigest(), label, name,
                              origin.start, origin.end, origin.exact, estimate))
    check_cancelled(cancelled)
    return SplitPlan(identity, sha256(text.encode('utf-8')).hexdigest(), source_name, template,
                     options, tuple(parts), tuple(notices))


def verify_plan(document, plan, *, cancelled=lambda: False):
    """Validate persisted rows rather than trusting a prior preview or receipt."""
    check_cancelled(cancelled)
    if (not isinstance(plan, SplitPlan) or reading_identity(document) != plan.reading_sha256
            or sha256(document.text.encode('utf-8')).hexdigest() != plan.text_sha256
            or not 1 <= len(plan.parts) <= MAX_PARTS):
        raise ValueError('The reading copy changed after its split preview.')
    SplitOptions(**asdict(plan.options))
    validate_template(plan.template)
    text_value(plan.source_name, 'source name', maximum=512)
    if any(not isinstance(value, str) or len(value) > 2048 for value in plan.notices):
        raise ValueError('Invalid split-plan notice.')
    offset, names = 0, set()
    for part in plan.parts:
        check_cancelled(cancelled)
        if (type(part.start) is not int or type(part.end) is not int
                or part.start != offset or not offset < part.end <= len(document.text)):
            raise ValueError('Split parts must cover the original stream without gaps or overlaps.')
        raw = document.text[part.start:part.end]
        span = document.source_span(part.start, part.end)
        if (sha256(raw.encode('utf-8')).hexdigest() != part.sha256
                or type(part.source_exact) is not bool
                or (part.source_start, part.source_end, part.source_exact) != (span.start, span.end, span.exact)):
            raise ValueError('Split text or its source mapping changed.')
        text_value(part.label, 'part label', maximum=512, empty=True)
        if part.estimated_seconds is not None and (plan.options.mode != 'estimated-duration'
                or type(part.estimated_seconds) not in (int, float) or not math.isfinite(part.estimated_seconds)
                or part.estimated_seconds < 0):
            raise ValueError('An unmeasured duration cannot be presented as measured timing.')
        key = filename_key(part.name)
        if key in names:
            raise ValueError('Split output names collide on a portable filesystem.')
        names.add(key)
        offset = part.end
    if offset != len(document.text):
        raise ValueError('The split plan omitted the final source characters.')
    return plan


def studio_project(document, plan, *, project_id, voice_id, language=''):
    """Create canonical spoken lines directly: imported prose is never a script."""
    from ..speech.runtime import split_utterances
    from ..studio.project import Line, Project, Role, Scene
    verify_plan(document, plan)
    text_value(voice_id, 'explicit local voice', maximum=2048)
    role = Role('narrator', 'Narrator', voice_id=voice_id, language=language)
    scenes = []
    for part_index, part in enumerate(plan.parts, 1):
        selected = document.text[part.start:part.end]
        if not selected.strip():
            raise ValueError('A reviewed part contains only whitespace. Choose fewer boundaries before narration; '
                             'text export can retain that part exactly without inventing audio timing.')
        lines, offset = [], part.start
        # This existing execution chunker also preserves every character.
        pieces = split_utterances(selected, maximum=7000)
        for line_index, text in enumerate(pieces, 1):
            end = offset + len(text)
            ref = json.dumps({'kind': 'reading-copy', 'reading_sha256': plan.reading_sha256,
                              'text_sha256': plan.text_sha256, 'start': offset, 'end': end,
                              'quotation_authority': False}, separators=(',', ':'))
            lines.append(Line(f'part-{part_index}-line-{line_index}', role.id, text, pause_after=0, source_ref=ref))
            offset = end
        scenes.append(Scene(f'part-{part_index}', part.label or f'Part {part_index}', tuple(lines)))
    return Project(project_id, document.title, (role,), tuple(scenes),
                   original_script=document.text, mode='faithful')
