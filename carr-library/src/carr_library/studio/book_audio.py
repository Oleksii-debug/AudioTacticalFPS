"""Reviewed book splits backed by existing Studio projects and render jobs.

A saved plan is immutable review metadata, not a second execution queue. Every
item has a deterministic Project/ProductionService identity. Original source
text remains literal data, including strings that resemble Studio directions.
"""
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from functools import cached_property
import json
import os
from pathlib import Path
import re
from string import Formatter
import sys
import threading
from uuid import uuid4

from .book_source import capture_source, source_context, validate_source
from ..audio.cleanup import retained_state_lock
from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from ..reader.ebook_contents import open_ebook
from ..speech._io import run_process
from ..speech import reading
from .project import Line, Project, Role, Scene
from .render import ProductionService, _production_output_name

MAX_CHARACTERS = 2_000_000
MAX_PARTS = 256
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_REVIEW_BYTES = 64 * 1024 * 1024
MAX_REVIEW_CHARACTERS = 8_000_000
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
        if type(self.pattern) is not str or len(self.pattern) > 4096 or '\x00' in self.pattern:
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
        return self.name_template.format(index=index, book=book[:160], part=label[:160])[:512]


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
                or not self.role.voice_id or self.format not in FORMATS
                or self.sample_rate not in (16000, 22050, 24000, 44100, 48000)):
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
        value = {'schema': 1, 'id': self.id, 'source': self.source,
                'options': asdict(self.options), 'role': self.role.to_dict(),
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
        if type(value['schema']) is not int or value['schema'] != 1 or type(value['parts']) is not list:
            raise ValueError('Unsupported book-audio plan.')
        try:
            return cls(value['id'], encode(value['source']).decode('utf-8'),
                       SplitOptions(**value['options']), Role.from_dict(value['role']),
                       tuple(BookPart(**part) for part in value['parts']),
                       value['format'], value['sample_rate'],
                       encode(value['landmarks']).decode('utf-8') if 'landmarks' in value else '')
        except (TypeError, KeyError) as exc:
            raise ValueError('Invalid saved book-audio plan.') from exc

    def job_id(self, index):
        if type(index) is not int or not 1 <= index <= len(self.parts):
            raise ValueError('Choose a part in this saved plan.')
        return sha256(encode(['book-audio-job', self.id, self.fingerprint, index])).hexdigest()[:32]

    def project(self, index):
        job_id = self.job_id(index)
        part = self.parts[index - 1]
        source = self.source
        selected = source['text'][part.start:part.end]
        lines = []
        for offset in range(0, len(selected), 100000):
            literal = selected[offset:offset + 100000]
            if not literal.strip():
                raise ValueError('This source contains an excessive whitespace-only narration block.')
            reference = SOURCE_PREFIX + ':'.join((self.id, self.fingerprint, str(index),
                                                  str(part.start + offset),
                                                  str(part.start + offset + len(literal))))
            lines.append(Line('line-' + str(len(lines) + 1), self.role.id, literal, 0, reference))
        # Do not use parse_script: [Music: ...], role names and file-like source
        # text are spoken literally, with no generated cue or script command.
        return Project(job_id, self.options.title(index, source['title'], part.label),
                       (self.role,), (Scene('part', part.label, tuple(lines)),),
                       original_script=selected, mode='faithful', sample_rate=self.sample_rate,
                       schema=3 if self.role.effects else 1)

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
        boundaries = list(range(options.characters, len(value), options.characters))
    elif options.mode in ('paragraphs', 'blank-lines', 'sentences'):
        pattern = {'paragraphs': r'\r\n|\r|\n',
                   'blank-lines': r'(?:\r?\n)[ \t]*(?:\r?\n)(?:[ \t]*\r?\n)*',
                   'sentences': r'[.!?…。！？]+(?:["”’»)\]]*)[ \t\r\n]*'}[options.mode]
        boundaries = [match.end() for match in re.finditer(pattern, value)]
    else:
        # Reuse the already installed, isolated and bounded reading-regex worker.
        raw = encode({'text': value, 'pattern': options.pattern,
                      'replacement': '', 'ignore_case': options.ignore_case})
        answer, _, _ = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-S',
                              str(Path(reading.__file__).with_name('regex_worker.py'))],
                             input_bytes=raw, cancelled=cancelled, timeout=1,
                             max_output=1024 * 1024)
        try:
            if len(answer) > 1024 * 1024:
                raise ValueError('The split expression produced too many boundaries.')
            result = json.loads(answer.decode('utf-8'))
            matches = result['matches']
            if (type(result) is not dict or set(result) != {'matches'} or type(matches) is not list
                    or len(matches) > 10000 or any(type(row) is not list or len(row) != 3
                        or type(row[0]) is not int or type(row[1]) is not int
                        or not 0 <= row[0] <= row[1] <= len(value) or row[2] != '' for row in matches)):
                raise ValueError('Invalid bounded regex result.')
            boundaries = [row[1] for row in matches]
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError('The bounded split expression was not accepted.') from exc
    cursor, ranges = 0, []
    for end in sorted(set([*boundaries, len(value)])):
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


@dataclass(frozen=True)
class BookReview:
    plan_sha256: str
    output_parent: str
    destinations: tuple[str, ...]
    prepared: bool

    @property
    def fingerprint(self):
        return sha256(encode(asdict(self))).hexdigest()


class BookAudioService:
    """One immutable book plan referring to the existing production store."""
    def __init__(self, library, workspace, *, production=None):
        self.library = library
        self.workspace = _short(workspace, 'library identity', 1024)
        self.library_root = local_path(library.root, exists=True)
        identity = regular(self.library_root, directory=True)
        self._library_identity = (identity.st_dev, identity.st_ino)
        self.root = local_path(library.root / 'state' / 'studio')
        self.root.mkdir(parents=True, exist_ok=True)
        self.plans = self.root / 'book-plans' / sha256(workspace.encode('utf-8')).hexdigest()
        self.plans.mkdir(parents=True, exist_ok=True)
        if production is not None and (not isinstance(production, ProductionService)
                                       or production.root != self.root):
            raise ValueError('Use the existing Studio service for this library.')
        self.production = production or ProductionService(self.root)
        self._owned = production is None
        self._closed = threading.Event()

    def close(self):
        self._closed.set()
        if self._owned:
            self.production.close()

    def _check_root(self):
        current = local_path(self.library.root, exists=True)
        identity = regular(current, directory=True)
        if current != self.library_root or (identity.st_dev, identity.st_ino) != self._library_identity:
            raise ValueError('The owning library folder changed. Reopen Book audio.')

    def load(self, plan_id):
        self._check_root()
        path = self.plans / (_identity(plan_id) + '.json')
        if regular(path).st_size > MAX_PLAN_BYTES:
            raise ValueError('The saved book review exceeds its bounded metadata size.')
        saved = read_json(path)
        if len(encode(saved)) > MAX_PLAN_BYTES:
            raise ValueError('The saved book review grew beyond its metadata size.')
        if type(saved) is not dict or set(saved) != {'plan', 'fingerprint'}:
            raise ValueError('Invalid saved book review.')
        plan = BookPlan.from_dict(saved['plan'])
        if (plan.id != plan_id or plan.fingerprint != saved['fingerprint']
                or plan.source['source_anchor']['workspace'] != self.workspace):
            raise ValueError('The saved book review identity changed.')
        return plan

    def saved(self):
        # Inspect only bounded typed plan metadata; authorization is rechecked
        # by ReadingDesk before opening, preparing or running the selection.
        with self.library.read(self.workspace):
            pass
        paths = sorted(self.plans.glob('*.json'))
        if len(paths) > 4096:
            raise ValueError('Too many saved book plans; use their exact identities.')
        total_bytes = sum(regular(path).st_size for path in paths)
        if total_bytes > MAX_REVIEW_BYTES:
            raise ValueError('Saved reviews exceed the bounded inventory. Open an exact plan identity.')
        plans, characters = [], 0
        for path in paths:
            plan = self.load(path.stem)
            characters += len(plan.source['text'])
            if characters > MAX_REVIEW_CHARACTERS:
                raise ValueError('Saved review text exceeds the bounded inventory. Open an exact plan identity.')
            plans.append(plan)
        return tuple(plans)

    def _retain(self, plan):
        if len(encode({'plan': plan.to_dict(), 'fingerprint': plan.fingerprint})) > MAX_PLAN_BYTES:
            raise ValueError('The book plan exceeds its bounded metadata size.')
        path = self.plans / (plan.id + '.json')
        if os.path.lexists(path):
            if self.load(plan.id).fingerprint != plan.fingerprint:
                raise ValueError('This book identity already belongs to another reviewed plan.')
        else:
            atomic_json(path, {'plan': plan.to_dict(), 'fingerprint': plan.fingerprint})

    def save(self, plan, *, cancelled=lambda: False, guard=lambda: None):
        """Retain an exact source review without preparing or starting jobs."""
        if not isinstance(plan, BookPlan):
            raise ValueError('A typed immutable book plan is required.')
        with state_lock(self.plans, 'book-' + plan.id):
            self._validate(plan, cancelled, guard)
            self._retain(plan)
        return plan

    def discover_voices(self, *, cancelled=lambda: False):
        """Use this ProductionService's one owned/borrowed configured registry."""
        self._check_root()
        _check(cancelled, self.production._check_open)
        registry = self.production.voices
        _check(cancelled, self.production._check_open)
        return registry

    def fresh(self, plan, *, cancelled=lambda: False, guard=lambda: None):
        """Review new job identities without changing historical output receipts."""
        if not isinstance(plan, BookPlan):
            raise ValueError('A typed immutable book plan is required.')
        self._validate(plan, cancelled, guard)
        return replace(plan, id=uuid4().hex)

    def _validate(self, plan, cancelled, guard):
        self._check_root()
        def stopped():
            return self._closed.is_set() or bool(cancelled())
        _check(stopped, guard)
        source = plan.source
        if source['source_anchor']['workspace'] != self.workspace:
            raise ValueError('This book plan belongs to another library scope.')
        try:
            validate_source(self.library, source['text'], source['source_anchor'],
                            source['source_metadata'], source['source_topic'],
                            cancelled=stopped, guard=guard)
            if plan.landmarks_json:
                from .book_landmarks import validate_current
                validate_current(self.library, source, plan.landmarks, cancelled=stopped, guard=guard)
        except (ValueError, OSError, RuntimeError) as exc:
            # ReadingDesk/native retained-source checks may surface their own
            # bounded-read error when interrupted. Only an actual stop signal
            # acknowledges cancellation; all other source failures stay errors.
            if stopped():
                raise InterruptedError('Book source validation was cancelled; saved items are retained.') from exc
            raise
        _check(stopped, guard)

    def _parent(self, value):
        parent = local_path(value, exists=True)
        if not parent.is_dir() or parent.is_relative_to(local_path(self.library.root, exists=True)):
            raise ValueError('Choose an existing output folder outside private library data.')
        return parent

    def _job(self, plan, index):
        job_id = plan.job_id(index)
        path = self.production.jobs_root / job_id / 'job.json'
        if not os.path.lexists(path):
            return None
        state = self.production.get(job_id)
        if 'restore_requires_new_output' in state and type(state['restore_requires_new_output']) is not bool:
            raise ValueError('Invalid restored Book output authorization.')
        if (state['fingerprint'] != plan.project(index).fingerprint
                or state['format'] != plan.format or state['stems'] is not False):
            raise ValueError('A book item differs from its immutable reviewed production plan.')
        return state

    def review(self, plan, destination, *, cancelled=lambda: False, guard=lambda: None):
        if not isinstance(plan, BookPlan):
            raise ValueError('A typed immutable book plan is required.')
        self._validate(plan, cancelled, guard)
        parent, destinations, prepared = self._parent(destination), [], True
        for index in range(1, len(plan.parts) + 1):
            state = self._job(plan, index)
            if state is None or state.get('restore_requires_new_output') is True:
                prepared = False
                output = parent / _production_output_name(plan.project(index).title, plan.job_id(index))
            else:
                output = local_path(state['destination'])
                if output.parent != parent:
                    raise ValueError('This saved item belongs to another reviewed output folder. Choose it or create a new plan.')
            destinations.append(str(output))
        if len({Path(value).name.casefold() for value in destinations}) != len(destinations):
            raise ValueError('The reviewed part names collide on a case-insensitive filesystem.')
        return BookReview(plan.fingerprint, str(parent), tuple(destinations), prepared)

    def prepare(self, plan, destination, *, approval, cancelled=lambda: False, guard=lambda: None):
        if not isinstance(plan, BookPlan):
            raise ValueError('A typed immutable book plan is required.')
        with state_lock(self.plans, 'book-' + plan.id):
            reviewed = self.review(plan, destination, cancelled=cancelled, guard=guard)
            if approval != reviewed.fingerprint:
                raise ValueError('The book split or output names changed. Review them again.')
            self._retain(plan)
            for index in range(1, len(plan.parts) + 1):
                self._validate(plan, cancelled, guard)
                state = self._job(plan, index)
                if state is not None and state.get('restore_requires_new_output') is True:
                    raise ValueError('This restored Book plan requires a new output review. '
                                     'Create a fresh review, then explicitly Preview and Prepare; '
                                     'historical jobs and their former paths remain unchanged.')
                elif state is None:
                    self.production.submit(plan.project(index), reviewed.output_parent,
                                           format=plan.format, stems=False, operation_id=plan.job_id(index),
                                           defer_voice_validation=True)
            return self.review(plan, reviewed.output_parent, cancelled=cancelled, guard=guard)

    def run(self, plan_id, destination, *, approval, cancelled=lambda: False,
            guard=lambda: None, progress=None):
        with retained_state_lock(self.plans, 'book-' + _identity(plan_id)):
            plan = self.load(plan_id)
            review = self.review(plan, destination, cancelled=cancelled, guard=guard)
            if not review.prepared or approval != review.fingerprint:
                raise ValueError('Prepare and explicitly review the exact saved item destinations before Run.')
            results = []
            observer = progress

            def notify(message):
                nonlocal observer
                if observer is not None:
                    try:
                        observer(message)
                    except Exception:
                        observer = None  # Optional feedback cannot relabel a durable result.

            for index, expected_output in enumerate(review.destinations, 1):
                def source_guard(index=index, expected_output=expected_output):
                    self._validate(plan, cancelled, guard)
                    if self.load(plan.id).fingerprint != plan.fingerprint:
                        raise ValueError('The saved book plan changed during rendering.')
                    current = self._job(plan, index)
                    if (current is None or current['destination'] != expected_output
                            or current.get('restore_requires_new_output') is True):
                        raise ValueError('The reviewed book output changed during rendering.')
                    return plan.provenance(index)

                source_guard()
                # ProductionService itself verifies completed artifacts before
                # returning a reuse result; no batch-level status shortcut exists.
                result = self.production.run(plan.job_id(index),
                    cancelled=lambda: self._closed.is_set() or bool(cancelled()),
                    progress=notify, source_guard=source_guard)
                results.append({'index': index, 'job': result['id'], 'status': result['status'],
                                'destination': result['destination']})
                if result['status'] != 'complete':
                    # A paused, cancelled or failed item never implicitly
                    # advances to another part. A new explicit Run resumes it.
                    break
            return tuple(results)

    def _measured_publication(self, plan_id, index, output, *, maximum_ms, name_template,
                              publish, approval, budget, cancelled, guard, progress):
        from time import monotonic
        from ..audio import AudioBudget, AudioCancelled
        from ..audio.cleanup import retained_state_lock
        from .publishing import PublicationService
        selected_budget = budget or AudioBudget()
        if not isinstance(selected_budget, AudioBudget):
            raise TypeError('Use the existing AudioBudget for measured publication.')
        deadline = monotonic() + selected_budget.timeout
        def stopped():
            return (self._closed.is_set() or self.production._closed
                    or bool(cancelled()) or monotonic() >= deadline)
        try:
            with retained_state_lock(self.plans, 'book-' + _identity(plan_id)):
                plan = self.load(plan_id)
                def original_source():
                    self._validate(plan, stopped, guard)
                    if self.load(plan.id).fingerprint != plan.fingerprint:
                        raise ValueError('The saved Book plan changed during measured publication.')
                    state = self._job(plan, index)
                    if (state is None or state['status'] != 'complete'
                            or state.get('restore_requires_new_output') is True):
                        raise ValueError('Run and verify this exact Book part before measured audio splitting.')
                    return plan.provenance(index)
                original_source()
                publisher = PublicationService(self.production, budget=selected_budget)
                options = dict(maximum_ms=maximum_ms, name_template=name_template,
                    source_guard=original_source, cancelled=stopped, progress=progress)
                if publish:
                    return publisher.export_pcm_parts(plan.job_id(index), output,
                                                      review_sha256=approval, **options)
                return publisher.review_pcm_parts(plan.job_id(index), output, **options)
        except (InterruptedError, AudioCancelled) as exc:
            if monotonic() >= deadline and not cancelled():
                raise TimeoutError('Measured audio publication exceeded its selected time budget.') from exc
            raise

    def review_duration(self, plan_id, index, output, *, maximum_ms,
                        name_template='{index:04d} - {book} - {part}', budget=None,
                        cancelled=lambda: False, guard=lambda: None, progress=None):
        """Review a post-render PCM split without another speech call."""
        return self._measured_publication(plan_id, index, output, maximum_ms=maximum_ms,
            name_template=name_template, publish=False, approval=None, budget=budget,
            cancelled=cancelled, guard=guard, progress=progress)

    def publish_duration(self, plan_id, index, output, *, maximum_ms, approval,
                         name_template='{index:04d} - {book} - {part}', budget=None,
                         cancelled=lambda: False, guard=lambda: None, progress=None):
        """Publish only the exact separately approved completed PCM ranges."""
        return self._measured_publication(plan_id, index, output, maximum_ms=maximum_ms,
            name_template=name_template, publish=True, approval=approval, budget=budget,
            cancelled=cancelled, guard=guard, progress=progress)
