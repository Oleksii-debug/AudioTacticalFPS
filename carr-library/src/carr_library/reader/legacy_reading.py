"""Read canonical reading-copy histories without creating another editor.

The original compact JSON is retained once by its byte hash. Its source anchor
is a locator, never quotation authority. Opening a selected old revision creates
an explicit derivative in the existing NarrationStore; it does not retarget old
source positions or replace the original file.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from ..backup_files import local_path, regular, optional_present
from ..ingest.writer_guard import _private_root, exclusive_writer
from .reading_state import ReadingLocation, _unique

DIRECTORY = 'narration-imports'
MAX_ARCHIVES = 1000
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
SESSION_DIRECTORY = 'state/speech'
MAX_SESSIONS = 1000
MAX_SESSION_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_CHARACTERS = 2_000_000
MAX_EDITS = 500
MAX_POSITIONS = 10000
_MAX_RUNS = MAX_POSITIONS * 2 + MAX_EDITS * 2
_DIGEST = re.compile(r'[0-9a-f]{64}')
_FILE = re.compile(r'([0-9a-f]{64})\.json')
_SESSION_FILE = re.compile(r'([0-9a-f]{64})\.reader\.json')
ORIGIN_KIND = 'canonical-reading-copy-origin'
_POSITION_FIELDS = {'ordinal', 'kind', 'index', 'start', 'end', 'text_sha256',
                    'text_verified_at_capture'}
_ANCHOR_FIELDS = {'workspace', 'edition', 'revision', 'asset_sha256', 'positions',
                  'quotation_authority'}


def _cancel(cancelled):
    if not callable(cancelled):
        raise ValueError('A reading-import cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Reading-copy import cancelled.')


def _text(value, label, *, maximum=MAX_CHARACTERS, empty=True):
    if (type(value) is not str or len(value) > maximum or '\x00' in value
            or (not empty and not value.strip())):
        raise ValueError('Invalid ' + label + '.')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Invalid Unicode in ' + label + '.') from exc
    return value


def _digest(value):
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ValueError('Invalid reading-copy digest.')
    return value


@dataclass(frozen=True)
class SourcePosition:
    ordinal: int
    kind: str
    index: int
    start: int
    end: int
    text_sha256: str
    text_verified_at_capture: bool


@dataclass(frozen=True)
class SourceAnchor:
    workspace: str
    edition: str
    revision: str
    asset_sha256: str
    positions: tuple[SourcePosition, ...]
    quotation_authority: bool = False


@dataclass(frozen=True)
class LegacyReadingOrigin:
    kind: str
    archive_sha256: str
    selected_revision: int
    selected_sha256: str
    source_anchor: SourceAnchor | None


def anchor_locations(anchor):
    if anchor is None:
        return ()
    return tuple(ReadingLocation(anchor.workspace, anchor.edition, anchor.revision,
        row.kind, row.index, row.text_sha256, anchor.asset_sha256)
        for row in anchor.positions)


def validate_anchor(value, text):
    """Validate every exact slice, preserving historical verification as data."""
    if (type(value) is not dict or set(value) != _ANCHOR_FIELDS
            or value['quotation_authority'] is not False
            or type(value['positions']) not in (list, tuple)
            or not 1 <= len(value['positions']) <= MAX_POSITIONS):
        raise ValueError('Invalid reading-copy source anchor.')
    previous, positions = None, []
    for item in value['positions']:
        if (type(item) is not dict or set(item) != _POSITION_FIELDS
                or type(item['ordinal']) is not int or not 1 <= item['ordinal'] <= MAX_POSITIONS
                or type(item['start']) is not int or type(item['end']) is not int
                or not 0 <= item['start'] <= item['end'] <= len(text)
                or type(item['text_verified_at_capture']) is not bool):
            raise ValueError('Invalid reading-copy source position.')
        row = SourcePosition(**item)
        ReadingLocation(value['workspace'], value['edition'], value['revision'],
                        row.kind, row.index, row.text_sha256, value['asset_sha256'])
        if previous is None:
            if row.start != 0:
                raise ValueError('The first source position must begin the reading snapshot.')
        elif (row.ordinal != previous.ordinal + 1 or row.kind != previous.kind
              or row.index <= previous.index or row.start != previous.end + 2
              or text[previous.end:row.start] != '\n\n'):
            raise ValueError('Invalid source position order or narration separator.')
        if sha256(text[row.start:row.end].encode('utf-8')).hexdigest() != row.text_sha256:
            raise ValueError('Reading-copy source position checksum mismatch.')
        positions.append(row)
        previous = row
    if previous.end != len(text):
        raise ValueError('Unmapped text follows the last source position.')
    return SourceAnchor(value['workspace'], value['edition'], value['revision'],
                        value['asset_sha256'], tuple(positions))


def _old_anchor(value, text, *, flag):
    fields = {'schema', 'workspace', 'edition', 'revision', 'asset_sha256',
              'positions', 'derived_separators', 'quotation_authority'}
    if (type(value) is not dict or set(value) != fields
            or value['schema'] != 'carr.narration-source.v1'
            or type(value['positions']) is not list
            or not 1 <= len(value['positions']) <= MAX_POSITIONS):
        raise ValueError('Unsupported legacy reading-copy source format.')
    positions = []
    for row in value['positions']:
        if type(row) is not dict or set(row) != (_POSITION_FIELDS - {'text_verified_at_capture'}) | {flag}:
            raise ValueError('Invalid or mixed legacy reading-copy source position.')
        positions.append({**{key: part for key, part in row.items() if key != flag},
                          'text_verified_at_capture': row[flag]})
    anchor = validate_anchor({
        **{key: value[key] for key in ('workspace', 'edition', 'revision',
                                     'asset_sha256', 'quotation_authority')},
        'positions': positions}, text)
    separators = value['derived_separators']
    expected = [{'start': first.end, 'end': second.start}
                for first, second in zip(anchor.positions, anchor.positions[1:])]
    if (type(separators) is not list or len(separators) != len(expected)
            or any(type(row) is not dict or set(row) != {'start', 'end'}
                   or type(row['start']) is not int or type(row['end']) is not int
                   for row in separators) or separators != expected):
        raise ValueError('Invalid legacy narration separators.')
    return anchor


def decode_origin(value, original_text):
    fields = {'kind', 'archive_sha256', 'selected_revision', 'selected_sha256', 'source_anchor'}
    if (type(value) is not dict or set(value) != fields or value['kind'] != ORIGIN_KIND
            or type(value['selected_revision']) is not int
            or not 0 <= value['selected_revision'] <= MAX_EDITS):
        raise ValueError('Invalid imported reading-copy origin.')
    anchor = (None if value['source_anchor'] is None
              else validate_anchor(value['source_anchor'], original_text))
    return LegacyReadingOrigin(ORIGIN_KIND, _digest(value['archive_sha256']),
        value['selected_revision'], _digest(value['selected_sha256']), anchor)


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str
    label: str


@dataclass(frozen=True)
class MappedRun:
    text: str
    source_start: int | None
    source_end: int | None
    exact: bool


def _split(runs, offset):
    before, after, cursor = [], [], 0
    for run in runs:
        end = cursor + len(run.text)
        if end <= offset:
            before.append(run)
        elif cursor >= offset:
            after.append(run)
        else:
            at = offset - cursor
            if run.exact:
                before.append(MappedRun(run.text[:at], run.source_start, run.source_start + at, True))
                after.append(MappedRun(run.text[at:], run.source_start + at, run.source_end, True))
            else:
                before.append(MappedRun(run.text[:at], run.source_start, run.source_end, False))
                after.append(MappedRun(run.text[at:], run.source_start, run.source_end, False))
        cursor = end
    return tuple(before), tuple(after)


def _initial_runs(text, anchor):
    if anchor is None:
        return (MappedRun(text, 0, len(text), True),) if text else ()
    result, cursor = [], 0
    for row in anchor.positions:
        if row.start > cursor:
            result.append(MappedRun(text[cursor:row.start], None, None, False))
        if row.end > row.start:
            result.append(MappedRun(text[row.start:row.end], row.start, row.end, True))
        cursor = row.end
    return tuple(result)


def _replay(source, anchor, edits, selected, cancelled):
    """Keep at most the selected revision and current piece sequence in memory."""
    runs = _initial_runs(source, anchor)
    chosen = runs if selected == 0 else None
    length = len(source)
    for index, edit in enumerate(edits, 1):
        _cancel(cancelled)
        if not 0 <= edit.start <= edit.end <= length:
            raise ValueError('The legacy edit does not address its exact preceding revision.')
        before, tail = _split(runs, edit.start)
        removed, after = _split(tail, edit.end - edit.start)
        if ''.join(run.text for run in removed) == edit.replacement:
            raise ValueError('Legacy reading history contains a no-op edit.')
        size = length - (edit.end - edit.start) + len(edit.replacement)
        if size > MAX_CHARACTERS:
            raise ValueError('Legacy reading history exceeds the character budget.')
        known = [run for run in removed if run.source_start is not None]
        start = min((run.source_start for run in known), default=None)
        end = max((run.source_end for run in known), default=None)
        middle = (MappedRun(edit.replacement, start, end, False),) if edit.replacement else ()
        runs, length = before + middle + after, size
        if len(runs) > _MAX_RUNS:
            raise ValueError('Legacy reading history exceeds the source-map budget.')
        if index == selected:
            chosen = runs
    _cancel(cancelled)
    return chosen


@dataclass(frozen=True)
class RecoveredSession:
    """Inert historical presentation data; never a live voice/runtime selection."""
    voice_id: str
    rate: int
    volume: int
    cursor: int
    audio_seconds: float


@dataclass(frozen=True)
class LegacySnapshot:
    raw: bytes
    archive_sha256: str
    title: str
    source_text: str
    source_sha256: str
    source_anchor: SourceAnchor | None
    edits: tuple[Edit, ...]
    saved_revision: int
    selected_revision: int
    runs: tuple[MappedRun, ...]
    session: RecoveredSession | None = None

    @property
    def text(self):
        return ''.join(run.text for run in self.runs)

    @property
    def origin(self):
        return LegacyReadingOrigin(ORIGIN_KIND, self.archive_sha256, self.selected_revision,
            sha256(self.text.encode('utf-8')).hexdigest(), self.source_anchor)

    @property
    def revision_labels(self):
        return ('0 — Original snapshot',) + tuple(
            f'{index} — {edit.label}' for index, edit in enumerate(self.edits, 1))

    def at(self, revision, *, cancelled=lambda: False):
        return decode_snapshot(self.raw, revision=revision, cancelled=cancelled)


def decode_snapshot(raw, *, revision=None, cancelled=lambda: False):
    _cancel(cancelled)
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_ARCHIVE_BYTES:
        raise ValueError('Invalid or oversized canonical reading-copy file.')
    def constant(_):
        raise ValueError('Invalid JSON number in a reading copy.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique, parse_constant=constant)
        session = None
        if type(value) is dict and value.get('schema') == 'carr.reader-session.v1':
            session_fields = {'schema', 'document', 'voice_id', 'rate', 'volume', 'cursor', 'audio_seconds'}
            if set(value) != session_fields or type(value['document']) is not dict:
                raise ValueError('Invalid canonical reader-session envelope.')
            voice = _text(value['voice_id'], 'historical session voice', maximum=2048)
            if (type(value['rate']) is not int or not -10 <= value['rate'] <= 10
                    or type(value['volume']) is not int or not 0 <= value['volume'] <= 100
                    or type(value['cursor']) is not int or value['cursor'] < 0
                    or type(value['audio_seconds']) not in (int, float)
                    or not math.isfinite(value['audio_seconds'])
                    or not 0 <= value['audio_seconds'] <= 31_536_000):
                raise ValueError('Invalid canonical reader-session presentation state.')
            session = RecoveredSession(voice, value['rate'], value['volume'],
                                       value['cursor'], value['audio_seconds'])
            value = value['document']
        fields = {'schema', 'title', 'source_text', 'source_sha256', 'edits', 'revision'}
        if type(value) is not dict or value.get('schema') not in ('carr.reading-copy.v1', 'carr.reading-copy.v2'):
            raise ValueError('Choose a canonical reading-copy v1 or v2 JSON file.')
        version2 = value['schema'] == 'carr.reading-copy.v2'
        if version2:
            extras = set(value) - fields
            if extras not in ({'source_anchor'}, {'source_origin'}):
                raise ValueError('A version 2 reading copy requires one exact source identity.')
            fields |= extras
        if set(value) != fields:
            raise ValueError('Unsupported canonical reading-copy fields.')
        title = _text(value['title'], 'legacy reading title', maximum=512, empty=False)
        source = _text(value['source_text'], 'legacy source snapshot')
        if _digest(value['source_sha256']) != sha256(source.encode('utf-8')).hexdigest():
            raise ValueError('The legacy source snapshot checksum does not match.')
        anchor = None
        if version2:
            if 'source_origin' in value:
                anchor = _old_anchor(value['source_origin'], source, flag='text_verified')
            elif type(value['source_anchor']) is dict and 'schema' in value['source_anchor']:
                anchor = _old_anchor(value['source_anchor'], source, flag='text_verified_at_read')
            else:
                anchor = validate_anchor(value['source_anchor'], source)
        if type(value['edits']) is not list or len(value['edits']) > MAX_EDITS:
            raise ValueError('Invalid legacy reading history length.')
        edits = []
        for row in value['edits']:
            _cancel(cancelled)
            if (type(row) is not dict or set(row) != {'start', 'end', 'replacement', 'label'}
                    or type(row['start']) is not int or type(row['end']) is not int):
                raise ValueError('Invalid legacy reading edit.')
            edits.append(Edit(row['start'], row['end'],
                _text(row['replacement'], 'legacy replacement'),
                _text(row['label'], 'legacy edit label', maximum=256, empty=False)))
        saved = value['revision']
        selected = saved if revision is None else revision
        if any(type(position) is not int or not 0 <= position <= len(edits) for position in (saved, selected)):
            raise ValueError('Choose an existing historical reading revision.')
        edits = tuple(edits)
        runs = _replay(source, anchor, edits, selected, cancelled)
        if session is not None:
            saved_runs = runs if selected == saved else _replay(source, anchor, edits, saved, cancelled)
            if session.cursor > sum(len(run.text) for run in saved_runs):
                raise ValueError('The historical reader-session cursor is outside its saved revision.')
        return LegacySnapshot(raw, sha256(raw).hexdigest(), title, source,
                              value['source_sha256'], anchor, edits, saved, selected, runs, session)
    except (UnicodeError, RecursionError, KeyError, TypeError) as exc:
        raise ValueError('Invalid canonical reading-copy data; the original file was kept.') from exc


def read_snapshot(path, *, cancelled=lambda: False):
    from .narration import _read_bytes
    _cancel(cancelled)
    path = local_path(path, exists=True)
    raw = _read_bytes(path)
    _cancel(cancelled)
    return decode_snapshot(raw, cancelled=cancelled)


def is_archive_path(name):
    return (type(name) is str and name.startswith(DIRECTORY + '/')
            and _FILE.fullmatch(name[len(DIRECTORY) + 1:]) is not None)


def validate_archive_payload(name, raw, *, cancelled=lambda: False):
    if not is_archive_path(name):
        raise ValueError('Invalid retained reading-history filename.')
    value = decode_snapshot(raw, cancelled=cancelled)
    if name != DIRECTORY + '/' + value.archive_sha256 + '.json':
        raise ValueError('Retained reading-history bytes do not match their filename.')
    return value


def _directory(root, *, create=False):
    root = _private_root(root)
    path = root / DIRECTORY
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if optional_present(path):
        local_path(path, root=root, exists=True)
        regular(path, directory=True)
    return path


def snapshot_import_files(root, *, cancelled=lambda: False):
    """Typed byte inventory; the caller owns the application writer lock."""
    from .narration import _read_bytes
    path = _directory(root)
    result, total = {}, 0
    if not optional_present(path):
        return result
    for count, item in enumerate(path.iterdir()):
        _cancel(cancelled)
        if count >= MAX_ARCHIVES * 2:
            raise ValueError('Too many retained reading-history namespace entries.')
        regular(item)
        if re.fullmatch(r'\.reading-import-[a-z0-9_]{8}\.tmp', item.name):
            continue
        if len(result) >= MAX_ARCHIVES:
            raise ValueError('Too many retained reading histories.')
        raw = _read_bytes(item)
        name = DIRECTORY + '/' + item.name
        validate_archive_payload(name, raw, cancelled=cancelled)
        total += len(raw)
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError('Retained reading histories exceed their backup byte budget.')
        result[name] = raw
    return result


def is_session_path(name):
    return (type(name) is str and name.startswith(SESSION_DIRECTORY + '/')
            and _SESSION_FILE.fullmatch(name[len(SESSION_DIRECTORY) + 1:]) is not None)


def validate_session_payload(name, raw, *, cancelled=lambda: False):
    if not is_session_path(name):
        raise ValueError('Invalid canonical reader-session filename.')
    snapshot = decode_snapshot(raw, cancelled=cancelled)
    if snapshot.session is None:
        raise ValueError('A reader-session file must contain its exact typed envelope.')
    return snapshot


def session_paths(root, *, cancelled=lambda: False):
    """Bounded original recovery inventory; runtime profiles are never collected."""
    root = local_path(root, exists=True)
    directory = local_path(root / SESSION_DIRECTORY, root=root)
    if not optional_present(directory):
        return ()
    regular(directory, directory=True)
    result = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            _cancel(cancelled)
            if index >= MAX_SESSIONS * 3:
                raise ValueError('Too many canonical reader-session namespace entries.')
            path = Path(entry.path)
            regular(path)
            if (re.fullmatch(r'\.[0-9a-f]{64}\.lock', path.name)
                    or re.fullmatch(r'\.pending-[a-z0-9_]{8}', path.name)):
                continue
            if not _SESSION_FILE.fullmatch(path.name) or len(result) >= MAX_SESSIONS:
                raise ValueError('Unexpected or excessive canonical reader-session files.')
            result.append(path)
    return tuple(sorted(result))


@contextmanager
def session_snapshot_guard(root, *, cancelled=lambda: False):
    """Hold each released ReaderDraftStore native lock under the root writer."""
    from filelock import FileLock, Timeout
    before = session_paths(root, cancelled=cancelled)
    with ExitStack() as stack:
        for path in before:
            _cancel(cancelled)
            key = _SESSION_FILE.fullmatch(path.name).group(1)
            lock = path.parent / ('.' + key + '.lock')
            if optional_present(lock):
                regular(lock)
            try:
                stack.enter_context(FileLock(lock, timeout=0, mode=0o600))
            except Timeout as exc:
                raise RuntimeError('A canonical reading draft is being saved. Retry backup when idle.') from exc
        if session_paths(root, cancelled=cancelled) != before:
            raise ValueError('Canonical reading drafts changed before snapshot.')
        yield


def snapshot_session_files(root, *, cancelled=lambda: False):
    from .narration import _read_bytes
    root = local_path(root, exists=True)
    result, total = {}, 0
    for path in session_paths(root, cancelled=cancelled):
        _cancel(cancelled)
        raw = _read_bytes(path)
        name = path.relative_to(root).as_posix()
        validate_session_payload(name, raw, cancelled=cancelled)
        total += len(raw)
        if total > MAX_SESSION_SNAPSHOT_BYTES:
            raise ValueError('Canonical reader sessions exceed their snapshot byte budget.')
        result[name] = raw
    return result


def origin_archive(origin, original_text, archives, *, cancelled=lambda: False):
    """Require the whole compact history, not just an unauthenticated pointer."""
    if type(origin) is not LegacyReadingOrigin:
        raise ValueError('A validated imported reading origin is required.')
    checked = decode_origin(asdict(origin), original_text)
    name = DIRECTORY + '/' + checked.archive_sha256 + '.json'
    try:
        raw = archives[name]
    except KeyError as exc:
        raise ValueError('The complete retained reading history is missing.') from exc
    snapshot = validate_archive_payload(name, raw, cancelled=cancelled).at(
        checked.selected_revision, cancelled=cancelled)
    if snapshot.source_text != original_text or snapshot.origin != checked:
        raise ValueError('The imported reading origin differs from its retained history.')
    return snapshot


def validate_anchor_database(db, anchor, *, cancelled=lambda: False):
    """Resolve exact historical sealed positions; never select an active revision."""
    if anchor is None:
        return
    for row, location in zip(anchor.positions, anchor_locations(anchor)):
        _cancel(cancelled)
        found = db.execute('''SELECT 1 FROM revision r
            JOIN sealed s ON s.revision=r.id
            JOIN page p ON p.revision=r.id
            JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition AND e.asset=r.asset
            WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?
                AND p.ordinal=? AND p.digest=?
                AND ((?='physical_page' AND p.physical=?) OR
                     (?='logical_segment' AND p.physical IS NULL AND p.ordinal=?))''',
            (location.workspace, location.edition, location.revision, location.asset_sha256,
             row.ordinal, location.text_sha256, location.kind, location.index,
             location.kind, location.index)).fetchone()
        if found is None:
            raise ValueError('The imported reading history does not match this library source.')


def authorize_snapshot(library, workspace, snapshot, *, cancelled=lambda: False):
    """Recheck access, sealed page bytes and the retained original in one snapshot."""
    from ..citations.anchors import Identity
    anchor = snapshot.source_anchor
    if anchor is not None and anchor.workspace != workspace:
        raise ValueError('The imported reading copy belongs to a different library.')
    with library._search_read(workspace, 60.0, cancelled) as (db, budget):
        validate_anchor_database(db, anchor, cancelled=budget.stopped)
        if anchor is not None:
            identity = Identity(anchor.workspace, anchor.edition, anchor.asset_sha256, anchor.revision)
            library._retained_source(db, workspace, identity, cancelled=budget.stopped)
            for row in anchor.positions:
                budget.check()
                expected = snapshot.source_text[row.start:row.end]
                size = db.execute('SELECT length(raw),typeof(raw) FROM page WHERE revision=? AND ordinal=?',
                                  (anchor.revision, row.ordinal)).fetchone()
                if size is None or size[1] != 'text' or size[0] != len(expected):
                    raise ValueError('The retained reading source text has changed.')
                actual = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=?',
                                    (anchor.revision, row.ordinal)).fetchone()
                if actual is None or actual[0] != expected:
                    raise ValueError('The retained reading source text has changed.')
        budget.check()


class LegacyReadingStore:
    def __init__(self, root):
        self.root = _private_root(root)

    def load(self, archive_sha256, *, revision=None, cancelled=lambda: False):
        from .narration import _read_bytes
        name = _digest(archive_sha256) + '.json'
        raw = _read_bytes(_directory(self.root) / name)
        value = validate_archive_payload(DIRECTORY + '/' + name, raw, cancelled=cancelled)
        return value if revision is None else value.at(revision, cancelled=cancelled)

    def retain(self, raw, *, cancelled=lambda: False):
        """Publish immutable original bytes once. A failed later save keeps history."""
        from ..audio.jobs import publish_noreplace, _sync_directory
        from .narration import _read_bytes
        snapshot = decode_snapshot(raw, cancelled=cancelled)
        with exclusive_writer(self.root):
            inventory = snapshot_import_files(self.root, cancelled=cancelled)
            relative = DIRECTORY + '/' + snapshot.archive_sha256 + '.json'
            if relative in inventory:
                if inventory[relative] != raw:
                    raise ValueError('A retained reading history has conflicting bytes.')
                # A prior no-replace move may have committed before its
                # directory flush failed. A successful retry must complete
                # that same durability boundary before publishing a project.
                _sync_directory(self.root / DIRECTORY)
                return snapshot.archive_sha256
            if len(inventory) >= MAX_ARCHIVES or sum(map(len, inventory.values())) + len(raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError('Retained reading histories exceed their storage budget.')
            path = _directory(self.root, create=True) / (snapshot.archive_sha256 + '.json')
            temporary = None
            try:
                with NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.reading-import-',
                                        suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _cancel(cancelled)
                publish_noreplace(temporary, path)
                temporary = None
                if _read_bytes(path) != raw:
                    raise ValueError('Retained reading history changed during publication.')
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return snapshot.archive_sha256


def project_from_snapshot(snapshot, *, title, original_path=None):
    """Create a new current project; compact legacy undo/redo stays in the archive."""
    from dataclasses import replace
    from .narration import new_project, Revision, _now
    project = new_project(snapshot.source_text, title=title, original_path=original_path,
                          legacy_origin=snapshot.origin)
    # Even a historical edit that happens to reproduce the same words remains
    # a derivative. A dedicated revision preserves that distinction.
    if snapshot.selected_revision:
        selected = Revision(1, snapshot.text,
                            'Imported historical revision ' + str(snapshot.selected_revision), _now())
        project = replace(project, revisions=project.revisions + (selected,), cursor=1)
    return project


def history_description(snapshot):
    anchor = snapshot.source_anchor
    lines = [snapshot.title, 'Historical revision: ' + str(snapshot.selected_revision),
             'Saved cursor: ' + str(snapshot.saved_revision),
             'Complete history: ' + str(len(snapshot.edits)) + ' edits',
             'Original file SHA256: ' + snapshot.archive_sha256,
             'Quotation authority: none. Captured verification flags remain historical data.']
    if snapshot.session is not None:
        session = snapshot.session
        lines.extend(('Retained reader-session envelope: exact original bytes are kept.',
            'Historical text cursor: ' + str(session.cursor),
            'Historical audio position in seconds: ' + str(session.audio_seconds),
            'Saved voice/rate/volume are inert. Select current playback settings explicitly.'))
    if anchor is not None:
        lines.extend(('Library: ' + anchor.workspace, 'Edition: ' + anchor.edition,
                      'Source revision: ' + anchor.revision,
                      'Source positions: ' + str(len(anchor.positions))))
    else:
        lines.append('Independent legacy copy: no library source anchor.')
    offset = 0
    for run in snapshot.runs:
        end = offset + len(run.text)
        label = 'Exact snapshot offsets' if run.exact else 'Derived text'
        source = ('none' if run.source_start is None
                  else f'[{run.source_start}, {run.source_end})')
        lines.append(f'{label}: reading [{offset}, {end}); source {source}.')
        offset = end
    return '\n'.join(lines)
