"""Typed immutable book reviews; ordinary Studio projects hold all render state."""
from contextlib import contextmanager
from dataclasses import asdict
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from ..backup_files import optional_present
from ..ingest.writer_guard import exclusive_writer
from ..reader.legacy_reading import validate_anchor, validate_anchor_database
from ..reader.narration_text import NarrationDraft
from .book_plan import BookPlan, REFERENCE_KIND, encode, validate_reference, _identity
from .book_batch import BookBatch, validate_plans
from .jobs import (checked_directory, regular_file, read_json, project_lock,
                   publish_noreplace, _sync_directory)

DIRECTORY = 'state/audio/books'
MAX_FILES = 4096
MAX_PLAN_BYTES = 32 * 1024**2
MAX_BATCH_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 128 * 1024**2
_PATH = re.compile(r'state/audio/books/(plans|batches)/[0-9a-f]{32}\.json\Z')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Book review snapshot cancelled.')


def is_state_path(relative):
    return type(relative) is str and _PATH.fullmatch(relative) is not None


def state_limit(relative):
    if not is_state_path(relative):
        raise ValueError('Unsupported book review path.')
    return MAX_PLAN_BYTES if '/plans/' in relative else MAX_BATCH_BYTES


def _ordinary(path, directory=False):
    if directory:
        return checked_directory(path)
    info = regular_file(path)
    if info.st_nlink != 1:
        raise ValueError('Book reviews cannot be aliases.')
    return info


def state_paths(root, *, cancelled=lambda: False):
    """Bounded exact durable namespace. Runtime controls and approvals are absent."""
    root = Path(root)
    base = root / DIRECTORY
    if not optional_present(base):
        return ()
    _ordinary(base, True)
    result, total, entries = [], 0, 0
    children = []
    with os.scandir(base) as iterator:
        for entry in iterator:
            _check(cancelled)
            if len(children) >= 3 or entry.name not in ('plans', 'batches', '.books.lock'):
                raise ValueError('Unknown entry in the bounded book review namespace.')
            children.append(base / entry.name)
    for child in sorted(children):
        _check(cancelled)
        entries += 1
        if child.name == '.books.lock':
            info = _ordinary(child)
            if info.st_size not in (0, 1):
                raise ValueError('Invalid book review native lock.')
            continue
        if child.name not in ('plans', 'batches'):
            raise ValueError('Unknown entry in the book review namespace.')
        _ordinary(child, True)
        with os.scandir(child) as iterator:
            for item in iterator:
                _check(cancelled)
                entries += 1
                if entries > MAX_FILES + 8:
                    raise ValueError('Book review inventory exceeds its entry budget.')
                path = child / item.name
                if re.fullmatch(r'\.book-write-[a-z0-9_]{8}', item.name):
                    _ordinary(path)
                    continue
                relative = path.relative_to(root).as_posix()
                if not is_state_path(relative):
                    raise ValueError('Unknown durable book review file.')
                info = _ordinary(path)
                total += info.st_size
                if (not 0 < info.st_size <= state_limit(relative) or total > MAX_SNAPSHOT_BYTES
                        or len(result) >= MAX_FILES):
                    raise ValueError('Book review snapshot exceeds its bounded metadata budget.')
                result.append(path)
    return tuple(sorted(result))


@contextmanager
def snapshot_guard(root, *, cancelled=lambda: False):
    """Caller holds the application writer. Verification never calls this."""
    base = Path(root) / DIRECTORY
    if not optional_present(base):
        yield
        return
    before = state_paths(root, cancelled=cancelled)
    with project_lock(base, lock_name='.books.lock'):
        if state_paths(root, cancelled=cancelled) != before:
            raise ValueError('Book review files changed before snapshot.')
        yield


class BookStore:
    def __init__(self, root):
        self.library_root = checked_directory(root)
        self.root = self.library_root / DIRECTORY

    def path(self, identity, kind):
        if kind not in ('plans', 'batches'):
            raise ValueError('Unknown book review type.')
        return self.root / kind / (_identity(identity) + '.json')

    def load(self, identity, kind='plans'):
        path = self.path(identity, kind)
        value = read_json(path, limit=state_limit(path.relative_to(self.library_root).as_posix()))
        if type(value) is not dict or set(value) != {'value', 'sha256'}:
            raise ValueError('Invalid retained book review envelope.')
        result = (BookPlan if kind == 'plans' else BookBatch).from_dict(value['value'])
        if result.id != identity or result.fingerprint != value['sha256']:
            raise ValueError('The retained book review identity changed.')
        return result

    def save(self, value, *, cancelled=lambda: False, guard=lambda: None):
        kind = 'plans' if isinstance(value, BookPlan) else 'batches' if isinstance(value, BookBatch) else None
        if kind is None:
            raise ValueError('Use a typed immutable book plan or selection.')
        path = self.path(value.id, kind)
        payload = encode({'value': value.to_dict(), 'sha256': value.fingerprint})
        if len(payload) > state_limit(path.relative_to(self.library_root).as_posix()):
            raise ValueError('The reviewed book metadata exceeds its byte budget.')
        with exclusive_writer(self.library_root):
            checked_directory(self.root, create=True)
            for directory in (self.root.parent, self.root.parent.parent, self.library_root):
                _sync_directory(directory)
            with project_lock(self.root, lock_name='.books.lock'):
                guard()
                _check(cancelled)
                files = state_paths(self.library_root, cancelled=cancelled)
                if optional_present(path):
                    if self.load(value.id, kind).fingerprint != value.fingerprint:
                        raise ValueError('This saved identity already belongs to another immutable review.')
                    # Retry the durability acknowledgement after a previous
                    # publish succeeded but parent-directory flush failed.
                    _sync_directory(path.parent)
                    return value
                if len(files) >= MAX_FILES or sum(p.stat().st_size for p in files) + len(payload) > MAX_SNAPSHOT_BYTES:
                    raise ValueError('The saved book review budget is full.')
                checked_directory(path.parent, create=True)
                _sync_directory(self.root)
                temporary = None
                try:
                    with NamedTemporaryFile(prefix='.book-write-', dir=path.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    def stopping():
                        guard()
                        return bool(cancelled())
                    publish_noreplace(temporary, path, cancelled=stopping)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        return value

    def saved(self, *, workspace=None, batches=False, cancelled=lambda: False):
        kind = 'batches' if batches else 'plans'
        result = []
        for path in state_paths(self.library_root, cancelled=cancelled):
            _check(cancelled)
            if path.parent.name != kind:
                continue
            value = self.load(path.stem, kind)
            scope = value.workspace if batches else value.source['source_anchor']['workspace']
            if workspace is None or workspace == scope:
                result.append(value)
        return tuple(result)


def part_plan(root, payload, *, library=None, current=False, exact=True, cancelled=lambda: False):
    """Resolve the exact current plan; the document cannot retarget its source."""
    draft = NarrationDraft.from_dict(payload['draft'])
    reference = validate_reference(payload['source_reference'], draft)
    plan = BookStore(root).load(reference['plan'])
    index = reference['index']
    expected = plan.document(index)
    # Managed output is rebound by the existing Studio opener after restore.
    # It is never part of the executable source or export authority.
    candidate = dict(payload, output_dir=None)
    if (reference != expected['source_reference'] or payload['project_id'] != expected['project_id']
            or draft.source_text != expected['draft']['source_text']):
        raise ValueError('The book part was rebound to a different retained source.')
    if exact and candidate != expected:
        raise ValueError('This book part was edited. Review a new book plan or an independent reading copy.')
    if current:
        from ..application import open_existing_library
        from ..reader.book_source import validate_source
        library = library or open_existing_library(root, cancelled=cancelled)
        source = plan.source
        validate_source(library, source['text'], source['source_anchor'],
                        source['source_metadata'], source['source_topic'], cancelled=cancelled)
        if plan.landmarks_json:
            from .book_landmarks import validate_current
            validate_current(library, source, plan.landmarks, cancelled=cancelled)
    return plan, index


def validate_state_tree(db, root, *, cancelled=lambda: False):
    """Passive historical source validation; never restore runtime approval."""
    root = Path(root)
    store = BookStore(root)
    paths = state_paths(root, cancelled=cancelled)
    plans, batches = {}, []
    for path in paths:
        _check(cancelled)
        value = store.load(path.stem, path.parent.name)
        if isinstance(value, BookPlan):
            source = value.source
            anchor = validate_anchor(source['source_anchor'], source['text'])
            validate_anchor_database(db, anchor, cancelled=cancelled)
            row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                'WHERE workspace=? AND edition=? AND revision=?',
                (anchor.workspace, anchor.edition, anchor.revision)).fetchone()
            if row is None or dict(zip(('author', 'title', 'volume', 'edition'), row)) != source['source_metadata']:
                raise ValueError('The saved book source metadata disagrees with its historical revision.')
            for position in anchor.positions:
                _check(cancelled)
                expected = source['text'][position.start:position.end]
                size = db.execute('SELECT length(raw),typeof(raw) FROM page WHERE revision=? AND ordinal=?',
                                  (anchor.revision, position.ordinal)).fetchone()
                if size is None or size[1] != 'text' or size[0] != len(expected):
                    raise ValueError('The saved book page bytes differ from their sealed source.')
                actual = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=?',
                                    (anchor.revision, position.ordinal)).fetchone()
                if actual is None or actual[0] != expected:
                    raise ValueError('The saved book page bytes differ from their sealed source.')
            plans[value.id] = value
        else:
            batches.append(value)
    for batch in batches:
        try:
            validate_plans(batch, tuple(plans[item.plan_id] for item in batch.items))
        except KeyError as exc:
            raise ValueError('The saved batch refers to a missing book plan.') from exc
    projects = root / 'audio-projects'
    if not optional_present(projects):
        return
    _ordinary(projects, True)
    count = 0
    with os.scandir(projects) as iterator:
        for entry in iterator:
            _check(cancelled)
            count += 1
            if count > 1000:
                raise ValueError('Too many managed projects for book provenance validation.')
            directory = projects / entry.name
            _ordinary(directory, True)
            for name in ('project.json', 'project.json.recovery.json'):
                path = directory / 'studio' / name
                if not optional_present(path):
                    continue
                value = read_json(path, limit=64 * 1024**2)
                reference = value.get('source_reference') if type(value) is dict else None
                if type(reference) is dict and reference.get('kind') == REFERENCE_KIND:
                    if reference.get('plan') not in plans:
                        raise ValueError('A managed book part has no retained source plan.')
                    part_plan(root, value, exact=False)
