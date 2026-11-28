"""Explicit research-content merge of two verified common4 library backups.

The inputs are immutable. Output is always a new library and contains only the
published research graph and its required originals. Operational/audio/settings
sidecars are deliberately outside this named profile and must be reviewed.
"""
from contextlib import closing, contextmanager, ExitStack
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import threading
from tempfile import TemporaryDirectory
from time import monotonic

from .application import LibraryApp
from .backup_budget import (checked_budget, effective_budget, available_memory,
                            check_metadata_memory)
from . import library_backup
from .durable_state import local_path, regular
from .ingest.writer_guard import exclusive_writer
from .catalog.intake import source_snapshot
from .storage.revisions import (Storage, CURRENT_SCHEMA_VERSION,
                                _schema_signature, _expected_signature)

PROFILE = 'published-research-content/1'
_MAX_WORKSPACES = 10_000
_TABLES = (
    'workspace', 'workspace_meta', 'catalog_asset', 'catalog_work', 'catalog_author',
    'catalog_edition', 'catalog_collection', 'catalog_membership', 'revision',
    'page', 'sealed', 'active', 'source_metadata', 'source_page_review',
    'research_topic', 'research_evidence', 'research_saved_query', 'research_tag',
    'research_evidence_tag', 'academic_reference', 'import_publication',
)
_OMISSION_NOTICE = (
    'This is a research-content merge. It omits all operational/import queues, '
    'journals, audio projects/generated audio, reading drafts/resume, preferences '
    'and other sidecars. Full input backups remain unchanged. Required originals, '
    'sealed history, exact evidence, notes, topics, tags, saved queries and '
    'academic references of the chosen workspaces are retained. '
    'Choose one entire side for each conflicting workspace; no evidence is retargeted.')


def _check(cancelled):
    if not callable(cancelled):
        raise TypeError('A merge cancellation callback is required.')
    value = cancelled()
    if type(value) is not bool:
        raise ValueError('Merge cancellation must return a boolean.')
    if value:
        raise InterruptedError('Research-content merge cancelled before a completed result.')


def _binding(path):
    path = local_path(path, exists=True)
    identities = []
    for parent in (*reversed(path.parents), path):
        info = regular(parent, directory=True)
        identities.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
    return path, tuple(identities)


def _encode(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False,
                       sort_keys=True, separators=(',', ':')) + '\n').encode('ascii')


def _size(manifest):
    pending = manifest.get('pending') or {}
    return (manifest['database']['size_bytes']
            + sum(row['size_bytes'] for row in manifest['assets'])
            + sum(row['size_bytes'] for row in manifest['sidecars'])
            + sum(row['size_bytes'] for row in pending.get('files', ())))


def _omitted(manifest):
    values = [row['filename'] for row in manifest['sidecars']]
    values.extend('pending/' + row['filename']
                  for row in (manifest.get('pending') or {}).get('files', ()))
    return tuple(sorted(values))


@contextmanager
def _reader(path, check):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                isolation_level=None, timeout=0)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        failure = None
        def progress():
            nonlocal failure
            try:
                check()
            except BaseException as exc:
                failure = exc
                return 1
            return 0
        db.set_progress_handler(progress, 1000)
        try:
            if (db.execute('PRAGMA user_version').fetchone()[0] != CURRENT_SCHEMA_VERSION
                    or _schema_signature(db) != _expected_signature(CURRENT_SCHEMA_VERSION)):
                raise ValueError('Research merge requires the exact current canonical schema.')
            check()
            yield db
        except BaseException:
            if failure is not None:
                raise failure
            raise
        finally:
            db.set_progress_handler(None, 0)
            db.rollback()


def _columns(db, table):
    if table not in _TABLES:
        raise ValueError('Unknown research merge table.')
    info = tuple(db.execute('PRAGMA table_info("' + table + '")'))
    columns = tuple(row['name'] for row in info if not (table == 'page' and row['name'] == 'id'))
    keys = [row['name'] for row in sorted(info, key=lambda row: row['pk']) if row['pk']]
    if table == 'page':
        keys = ['revision', 'ordinal']
    if not columns or not keys:
        raise ValueError('The canonical research table is incomplete.')
    return columns, tuple(keys)


def _rows(db, table, workspace):
    columns, keys = _columns(db, table)
    quoted = ','.join('t."' + name + '"' for name in columns)
    order = ','.join('t."' + name + '"' for name in keys)
    if table == 'workspace':
        predicate = 't.id=?'
    elif table == 'catalog_asset':
        predicate = 'EXISTS(SELECT 1 FROM catalog_edition e WHERE e.workspace=? AND e.asset=t.digest)'
    elif table in ('page', 'sealed', 'import_publication'):
        predicate = 'EXISTS(SELECT 1 FROM revision r WHERE r.workspace=? AND r.id=t.revision)'
    else:
        predicate = 't.workspace=?'
    return db.execute('SELECT ' + quoted + ' FROM "' + table + '" t WHERE '
                      + predicate + ' ORDER BY ' + order, (workspace,)), columns


def _workspace(db, identity, check):
    digest, counts = sha256(), {}
    for table in _TABLES:
        check()
        cursor, columns = _rows(db, table, identity)
        count = 0
        with closing(cursor):
            for row in cursor:
                check()
                # Hash each native typed value without ASCII-escaping a whole
                # potentially large raw page into an intermediate JSON record.
                digest.update(_encode([table, columns]))
                for value in row:
                    if value is None:
                        kind, wire = b'n', b''
                    elif type(value) is int:
                        kind, wire = b'i', str(value).encode('ascii')
                    elif type(value) is str:
                        kind, wire = b's', value.encode('utf-8')
                    else:
                        raise ValueError('A research value has an unsupported native type.')
                    digest.update(kind)
                    digest.update(len(wire).to_bytes(8, 'big'))
                    digest.update(wire)
                count += 1
        counts[table] = count
    meta = db.execute('SELECT name,description,archived FROM workspace_meta WHERE workspace=?',
                      (identity,)).fetchone()
    if meta is None:
        raise ValueError('The selected research workspace has no presentation record.')
    return dict(id=identity, name=meta['name'], archived=bool(meta['archived']),
                sha256=digest.hexdigest(), counts=counts)


def _summaries(db, check, policy):
    result, wire_bytes = [], 0
    with closing(db.execute('SELECT id FROM workspace ORDER BY id LIMIT ?',
                            (_MAX_WORKSPACES + 1,))) as cursor:
        for (identity,) in cursor:
            check()
            if len(result) == _MAX_WORKSPACES:
                raise ValueError('One research merge supports at most 10000 workspaces.')
            row = _workspace(db, identity, check)
            wire_bytes += len(_encode(row))
            check_metadata_memory(wire_bytes, len(result) + 1, policy)
            if wire_bytes > policy.manifest_bytes:
                raise ValueError('Workspace review exceeds the selected metadata allowance.')
            result.append(row)
    return tuple(result)


def _cross_input_conflicts(readers, check, policy):
    """Report global identities which whole-workspace choices cannot remap."""
    left, right = readers
    result, wire_bytes = [], 0
    def add(kind, identity, left_ids, right_ids):
        nonlocal wire_bytes
        check()
        value = dict(kind=kind, identity=identity,
                     left_workspaces=list(left_ids), right_workspaces=list(right_ids))
        wire_bytes += len(_encode(value))
        check_metadata_memory(wire_bytes, len(result) + 1, policy)
        if wire_bytes > policy.manifest_bytes:
            raise ValueError('Stable-identity conflicts exceed the selected review allowance.')
        result.append(value)
    with closing(left.execute('SELECT id,workspace FROM revision ORDER BY id')) as cursor:
        for row in cursor:
            check()
            other = right.execute('SELECT workspace FROM revision WHERE id=?', (row['id'],)).fetchone()
            if other is not None and other['workspace'] != row['workspace']:
                add('revision', row['id'], (row['workspace'],), (other['workspace'],))
    with closing(left.execute(
            'SELECT p.request_id,p.attempt,r.workspace FROM import_publication p '
            'JOIN revision r ON r.id=p.revision ORDER BY p.request_id,p.attempt')) as cursor:
        for row in cursor:
            check()
            other = right.execute(
                'SELECT r.workspace FROM import_publication p JOIN revision r ON r.id=p.revision '
                'WHERE p.request_id=? AND p.attempt=?', (row['request_id'], row['attempt'])).fetchone()
            if other is not None and other['workspace'] != row['workspace']:
                add('publication_attempt', [row['request_id'], row['attempt']],
                    (row['workspace'],), (other['workspace'],))
    with closing(left.execute('SELECT digest,mime,size FROM catalog_asset ORDER BY digest')) as cursor:
        for row in cursor:
            check()
            other = right.execute('SELECT mime,size FROM catalog_asset WHERE digest=?',
                                  (row['digest'],)).fetchone()
            if other is None or (row['mime'], row['size']) == tuple(other):
                continue
            owners = []
            for db in readers:
                values = []
                with closing(db.execute('SELECT DISTINCT workspace FROM catalog_edition '
                                        'WHERE asset=? ORDER BY workspace', (row['digest'],))) as selected:
                    for (workspace,) in selected:
                        check()
                        if len(values) >= _MAX_WORKSPACES:
                            raise ValueError('An original has too many workspace owners to review.')
                        values.append(workspace)
                owners.append(values)
            if owners[0] and owners[1]:
                add('original_metadata', row['digest'], *owners)
    return result


@contextmanager
def _checked_sql(db, check):
    """Keep the existing SQLite copy/rebuild cancellable and retain its cause."""
    failure = None
    def progress():
        nonlocal failure
        try:
            check()
        except BaseException as exc:
            failure = exc
            return 1
        return 0
    db.set_progress_handler(progress, 1000)
    try:
        check()
        yield
        check()
    except BaseException:
        if failure is not None:
            raise failure
        raise
    finally:
        db.set_progress_handler(None, 0)


@dataclass(frozen=True)
class MergePreview:
    payload: bytes
    _owner: object = field(repr=False, compare=False)

    @property
    def sha256(self):
        return sha256(self.payload).hexdigest()

    @property
    def text(self):
        return json.dumps(json.loads(self.payload), ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class MergeResult:
    path: Path
    profile: str
    preview_sha256: str
    snapshot_sha256: str
    choices: tuple
    omitted: tuple


class ResearchContentMerge:
    """One issued preview and explicit choices; no background owner or live merge."""
    def __init__(self, left, left_sha256, right, right_sha256, destination, *, backup_budget=None):
        checked_budget(backup_budget)
        self.backup_budget = backup_budget
        self.policy = effective_budget(backup_budget)
        self.inputs = (_binding(left), _binding(right))
        self.checksums = (left_sha256, right_sha256)
        for checksum in self.checksums:
            if (type(checksum) is not str or len(checksum) != 64
                    or any(char not in '0123456789abcdef' for char in checksum)):
                raise ValueError('Supply each complete independently retained backup SHA-256.')
        self.destination = library_backup._destination(destination, self.inputs[0][0])
        library_backup._destination(self.destination, self.inputs[1][0])
        self.parent = _binding(self.destination.parent)
        self._preview = None
        self._guard = threading.Lock()

    @contextmanager
    def _exclusive(self):
        if not self._guard.acquire(blocking=False):
            raise ValueError('A research merge operation is already active for this preview owner.')
        try:
            yield
        finally:
            self._guard.release()

    def _bound(self):
        for expected in (*self.inputs, self.parent):
            if _binding(expected[0]) != expected:
                raise ValueError('A reviewed input or destination parent changed. Prepare a new preview.')
        if os.path.lexists(self.destination):
            raise FileExistsError('Research-content merge requires a new destination.')

    def _operation(self, cancelled):
        deadline = monotonic() + self.policy.seconds
        def check():
            _check(cancelled)
            if monotonic() >= deadline:
                raise TimeoutError('Research-content merge exceeded its explicit work allowance.')
        check()
        self._bound()
        return check

    def _manifests(self, check):
        manifests = []
        for (path, _), checksum in zip(self.inputs, self.checksums):
            check()
            manifest = library_backup._read_manifest(path, checksum,
                cancelled=lambda: (check() or False), backup_budget=self.backup_budget)
            if (manifest['version'] != 4 or manifest['schema_version'] != CURRENT_SCHEMA_VERSION
                    or library_backup._historical_layout(manifest)):
                raise ValueError('This selective merge accepts current common4 research backups only.')
            manifests.append(manifest)
        # Both restored inputs, merged content, checked output backup and final
        # restore may coexist. Charge full input bytes even for omitted state.
        expected = 6 * sum(_size(value) for value in manifests) + 64 * 1024**2
        if expected > self.policy.staging_bytes:
            raise ValueError('The merged snapshots exceed the selected staging-byte allowance.')
        if shutil.disk_usage(self.parent[0]).free < expected:
            raise ValueError('Insufficient current free disk for all merge snapshots and publication.')
        if available_memory() < self.policy.memory_bytes:
            raise ValueError('Insufficient current free memory for the selected merge policy.')
        check()
        self._bound()
        return tuple(manifests)

    @contextmanager
    def _inputs(self, scratch, manifests, check):
        with ExitStack() as stack:
            roots, readers = [], []
            for index, ((path, _), checksum) in enumerate(zip(self.inputs, self.checksums)):
                check()
                root = scratch / ('input-' + str(index))
                library_backup.restore_backup(path, root, checksum,
                    cancelled=lambda: (check() or False), backup_budget=self.backup_budget)
                roots.append(root)
                readers.append(stack.enter_context(_reader(root / 'library.sqlite', check)))
            yield tuple(roots), tuple(readers)

    def _description(self, manifests, readers, check):
        summaries = [_summaries(db, check, self.policy) for db in readers]
        sides = [dict((row['id'], row) for row in rows) for rows in summaries]
        rows = []
        for identity in sorted(set(sides[0]) | set(sides[1])):
            left, right = sides[0].get(identity), sides[1].get(identity)
            state = ('left_only' if right is None else 'right_only' if left is None
                     else 'identical' if left['sha256'] == right['sha256'] else 'conflict')
            rows.append(dict(id=identity, state=state, left=left, right=right))
        result = dict(schema='carr-research-merge-preview/1', profile=PROFILE,
            notice=_OMISSION_NOTICE, input_sha256=list(self.checksums),
            destination=str(self.destination), workspaces=rows,
            incompatible_stable_ids=_cross_input_conflicts(readers, check, self.policy),
            omitted={'left': list(_omitted(manifests[0])), 'right': list(_omitted(manifests[1]))},
            originals='Only originals referenced by the chosen workspaces are copied; all other bytes remain in input backups.',
            internal_rowids='Only internal page rowids are reassigned; logical IDs and exact evidence payloads remain unchanged.',
            resources=self.policy.to_dict())
        raw = _encode(result)
        if len(raw) > self.policy.manifest_bytes:
            raise ValueError('The complete merge preview exceeds the selected metadata allowance.')
        check_metadata_memory(len(raw), len(rows), self.policy)
        return raw

    def prepare(self, *, cancelled=lambda: False):
        with self._exclusive():
            return self._prepare(cancelled=cancelled)

    def _prepare(self, *, cancelled):
        self._preview = None
        check = self._operation(cancelled)
        manifests = self._manifests(check)
        with TemporaryDirectory(prefix='.carr-research-merge-preview-', dir=self.parent[0]) as temporary:
            with self._inputs(Path(temporary), manifests, check) as (_, readers):
                raw = self._description(manifests, readers, check)
            check()
            self._bound()
        self._preview = MergePreview(raw, self)
        return self._preview

    def _choices(self, preview, choices, acknowledged_omissions):
        if (preview is not self._preview or type(preview) is not MergePreview
                or preview._owner is not self):
            raise ValueError('Use the current exact preview issued by this merge action.')
        if acknowledged_omissions is not True:
            raise ValueError('Explicitly acknowledge every omitted operational/audio/settings sidecar.')
        rows = json.loads(preview.payload)['workspaces']
        if type(choices) is not dict or set(choices) != {row['id'] for row in rows}:
            raise ValueError('Choose one whole side or omit for every displayed workspace.')
        for row in rows:
            side = choices[row['id']]
            if side not in ('left', 'right', 'omit') or (side != 'omit' and row[side] is None):
                raise ValueError('A workspace choice must name an available complete side.')
        if not any(side != 'omit' for side in choices.values()):
            raise ValueError('Select at least one published research workspace.')
        for conflict in json.loads(preview.payload)['incompatible_stable_ids']:
            if (any(choices.get(identity) == 'left' for identity in conflict['left_workspaces'])
                    and any(choices.get(identity) == 'right' for identity in conflict['right_workspaces'])):
                raise ValueError('The reviewed choices retain incompatible stable identities. '
                                 'Choose one side or omit an affected workspace; IDs are never renamed.')
        return tuple((row['id'], choices[row['id']]) for row in rows)

    def merge(self, preview, choices, *, acknowledged_omissions=False, cancelled=lambda: False):
        with self._exclusive():
            return self._merge(preview, choices, acknowledged_omissions=acknowledged_omissions,
                               cancelled=cancelled)

    def _merge(self, preview, choices, *, acknowledged_omissions, cancelled):
        selected = self._choices(preview, choices, acknowledged_omissions)
        check = self._operation(cancelled)
        manifests = self._manifests(check)
        with TemporaryDirectory(prefix='.carr-research-merge-', dir=self.parent[0]) as temporary:
            scratch = Path(temporary)
            with self._inputs(scratch, manifests, check) as (roots, readers):
                if self._description(manifests, readers, check) != preview.payload:
                    raise ValueError('The verified research snapshots changed. Prepare a new preview.')
                merged = LibraryApp(scratch / 'merged')
                try:
                    with exclusive_writer(merged.root), closing(Storage(
                            merged.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
                        with _checked_sql(storage.db, check):
                            with storage.transaction():
                                for identity, side in selected:
                                    check()
                                    if side == 'omit':
                                        continue
                                    incoming = readers[0 if side == 'left' else 1]
                                    self._copy_workspace(storage.db, incoming, identity, check)
                            # Existing scalar-row rebuild and complete V5 validator.
                            storage.rebuild()
                    self._copy_originals(merged, roots, check)
                    check()
                    self._bound()
                    bundle = scratch / 'merged-backup'
                    checksum = library_backup.create_backup(merged, bundle,
                        cancelled=lambda: (check() or False), backup_budget=self.backup_budget)
                    check()
                    self._bound()
                    # Restore performs its own independent complete verification
                    # and incomplete-marker/no-replace publication. No new writer.
                    output = library_backup.restore_backup(bundle, self.destination, checksum,
                        cancelled=lambda: (check() or False), backup_budget=self.backup_budget)
                    result = MergeResult(output, PROFILE, preview.sha256, checksum, selected,
                        (('left', _omitted(manifests[0])), ('right', _omitted(manifests[1]))))
                finally:
                    if not merged.close_search():
                        raise RuntimeError('The private merge search owner has not finished cleanup.')
            # A completed restore wins over a later cancellation request.
            return result

    def _copy_workspace(self, destination, incoming, workspace, check):
        for table in _TABLES:
            cursor, columns = _rows(incoming, table, workspace)
            if table == 'research_topic':
                cursor.close()
                quoted = ','.join('t."' + name + '"' for name in columns)
                cursor = incoming.execute(
                    'WITH RECURSIVE tree(id,depth) AS ('
                    'SELECT id,0 FROM research_topic WHERE workspace=? AND parent IS NULL '
                    'UNION ALL SELECT t.id,tree.depth+1 FROM research_topic t JOIN tree '
                    'ON t.parent=tree.id WHERE t.workspace=?) '
                    'SELECT ' + quoted + ' FROM tree JOIN research_topic t '
                    'ON t.id=tree.id WHERE t.workspace=? ORDER BY tree.depth,t.position,t.id',
                    (workspace, workspace, workspace))
            statement = ('INSERT INTO "' + table + '"('
                         + ','.join('"' + name + '"' for name in columns)
                         + ') VALUES(' + ','.join('?' for _ in columns) + ')')
            with closing(cursor):
                for row in cursor:
                    check()
                    values = tuple(row)
                    if table == 'catalog_asset':
                        existing = destination.execute(
                            'SELECT digest,mime,size FROM catalog_asset WHERE digest=?', (row['digest'],)).fetchone()
                        if existing is not None:
                            if tuple(existing) != values:
                                raise ValueError('A shared original digest has conflicting catalog metadata.')
                            continue
                    try:
                        destination.execute(statement, values)
                    except sqlite3.IntegrityError as exc:
                        raise ValueError('Chosen complete workspaces contain incompatible stable identities; '
                                         'omit one conflicting workspace and preview again.') from exc

    def _copy_originals(self, merged, roots, check):
        from .ingest.catalog_asset_copy import catalog_inventory, verify_catalog_assets
        limits = dict(include_all_workspaces=True, max_assets=self.policy.assets,
            max_bytes=self.policy.asset_bytes, max_source_bytes=self.policy.original_bytes,
            metadata_budget=self.policy)
        with merged.read() as db:
            entries = catalog_inventory(db, cancelled=lambda: (check() or False), **limits)
            for entry in entries:
                check()
                candidates = [root / 'originals' / entry.filename for root in roots]
                source = next((path for path in candidates if path.is_file()), None)
                if source is None:
                    raise ValueError('A chosen research source has no required original.')
                captured = source_snapshot(source.parent, source.name, file_backed=True,
                    max_bytes=self.policy.original_bytes, cancelled=lambda: (check() or False))
                if captured.manifest() != dict(sha256=entry.digest, mime=entry.mime, size=entry.size):
                    raise ValueError('A required original differs from the chosen research identity.')
                merged.assets.retain_snapshot(captured, max_bytes=self.policy.original_bytes,
                    cancelled=lambda: (check() or False))
            verify_catalog_assets(db, merged.assets, cancelled=lambda: (check() or False), **limits)
