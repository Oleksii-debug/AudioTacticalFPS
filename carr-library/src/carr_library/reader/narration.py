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
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from uuid import uuid4

from ..ingest.writer_guard import _private_root, _regular_if_present, exclusive_writer
from ..research.export_file import write_export
from .reading_state import ReadingLocation, _file_identity, _location, _text, _timestamp, _unique


DIRECTORY = 'narration-projects'
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_PROJECT_BYTES = 32 * 1024 * 1024
MAX_HISTORY = 50
MAX_RULES = 1000
MAX_RULE_CHARACTERS = 64 * 1024
MAX_MATCH_STEPS = 20_000_000
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


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


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


def new_project(original_text='', *, title='Reading copy', source_location=None):
    text = _body(original_text)
    first = Revision(0, text, 'Original text', _now())
    return NarrationProject(uuid4().hex, title, text, text_digest(text), source_location, (first,), 0)


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


def pronunciation_preview(text, rules):
    """One non-cascading literal pass with an explicit character-work budget.

    A trie selects the longest match. Neither written nor spoken text becomes
    a regular expression, so overlapping phrases cannot create regex backtracking.
    """
    _body(text)
    rules = tuple(rules)
    if (len(rules) > MAX_RULES or any(type(rule) is not PronunciationRule for rule in rules)
            or len({rule.written for rule in rules}) != len(rules)):
        raise ValueError('Invalid or duplicate pronunciation phrases.')
    if not rules:
        return text, 0
    if sum(len(rule.written) + len(rule.spoken) for rule in rules) > MAX_RULE_CHARACTERS:
        raise ValueError('Pronunciation dictionary exceeds its character budget. Use a smaller project dictionary.')
    tree = {}
    for rule in rules:
        node = tree
        for char in rule.written:
            node = node.setdefault(char, {})
        node[None] = rule.spoken
    pieces, cursor, position, size, count, steps = [], 0, 0, 0, 0, 0
    while position < len(text):
        node, end, matched_end, replacement = tree, position, None, None
        while end < len(text):
            steps += 1
            if steps > MAX_MATCH_STEPS:
                raise ValueError('Pronunciation matching exceeded its work budget. Use a smaller text section.')
            node = node.get(text[end])
            if node is None:
                break
            end += 1
            if None in node:
                matched_end, replacement = end, node[None]
        if matched_end is None:
            position += 1
            continue
        unchanged = text[cursor:position]
        size += len(unchanged.encode('utf-8')) + len(replacement.encode('utf-8'))
        if size > MAX_TEXT_BYTES:
            raise ValueError('Pronunciation expansion exceeds the reading-section budget.')
        pieces.extend((unchanged, replacement))
        cursor, position, count = matched_end, matched_end, count + 1
    pieces.append(text[cursor:])
    result = ''.join(pieces)
    return _body(result), count


def regex_preview(text, pattern, replacement, *, ignore_case=False, timeout=2.0,
                  cancelled=lambda: False):
    """Compute a bounded isolated proposal; applying it is an explicit edit."""
    from .regex_preview import preview
    return preview(text, pattern, replacement, ignore_case=ignore_case,
                   timeout=timeout, cancelled=cancelled)


def source_spans(project):
    """Linear-time conservative alignment: exact edges and one derived middle.

    Ranges use Python Unicode offsets [start, end). No source offset is claimed
    for the middle, even if it happens to contain unchanged smaller phrases.
    """
    old, new = project.original_text, project.current_text
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
    if project.current_text == project.original_text:
        lines.append('The reading copy is identical to the original text.')
    else:
        for kind, a, b, c, d in source_spans(project):
            lines.append(f'{kind.capitalize()}: original [{a}, {b}); reading copy [{c}, {d}).')
        lines.append('The derived range has no claim of exact source-word alignment.')
    return '\n'.join(lines)


def encode_project(project):
    if type(project) is not NarrationProject:
        raise ValueError('A validated reading project is required.')
    value = dict(kind='carr-reading-copy', version=1, **asdict(project))
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
    if path.exists() or path.is_symlink():
        _private_root(path)
    return path


def snapshot_narration_files(root):
    """Return strict validated relative filenames/bytes; caller holds writer lock."""
    path = _directory(root)
    if not path.exists():
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
        return validate_narration_payload(path.name, raw), sha256(raw).hexdigest()

    def save(self, project, *, expected_digest=None, draft=False):
        raw = encode_project(project)
        if expected_digest is not None and (not isinstance(expected_digest, str)
                or not re.fullmatch('[0-9a-f]{64}', expected_digest)):
            raise ValueError('Invalid reading-project save lease.')
        with exclusive_writer(self.root):
            path = self._path(project.id, draft, create=True)
            try:
                previous = _read_bytes(path)
            except FileNotFoundError:
                previous = None
            digest = None if previous is None else sha256(previous).hexdigest()
            if digest != expected_digest:
                raise ValueError('This reading copy changed elsewhere. Reopen it or save a new copy.')
            if previous is not None:
                prior = validate_narration_payload(path.name, previous)
                if (prior.original_text, prior.source_location) != (project.original_text, project.source_location):
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
            if workspace is not None and project.source_location is not None:
                if project.source_location.workspace != workspace:
                    continue
            row = projects.setdefault(project.id, {'project': project, 'saved': False, 'draft': False})
            is_draft = filename.endswith('.draft.json')
            row['draft' if is_draft else 'saved'] = True
            if not is_draft:
                row['project'] = project
        return tuple(sorted(projects.values(), key=lambda row: (row['project'].title.casefold(), row['project'].id)))


def import_text(path, *, title=None):
    raw = _read_bytes(Path(path), allow_empty=True)
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError('The UTF-8 text file exceeds the reading-section budget.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('Choose a UTF-8 text file.') from exc
    return new_project(text, title=title or Path(path).stem[:200] or 'Imported reading copy')


def export_text(project, path, *, private_root, overwrite=False):
    if Path(path).exists() and not overwrite:
        raise FileExistsError('Choose another filename or explicitly approve replacing the existing export.')
    write_export(path, project.current_text, private_root=private_root)


def save_as(project, *, title=None):
    """Fork a project identity while keeping immutable source/history provenance."""
    return replace(project, id=uuid4().hex, title=title or project.title)
