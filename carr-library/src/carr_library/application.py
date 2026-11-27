"""Local application services composed from the reviewed CARR components.

Every public operation opens a fresh workspace-authorized transaction. Originals,
historical quotations and mutable research notes remain separate. No network is
used by this module; model installation is an explicit build-time operation.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from .catalog.intake import snapshot
from .catalog.membership import Catalog, _metadata
from .catalog.read_views import active_collections, browse_editions, get_edition
from .catalog.registration import Registration, register_batch
from .citations.anchors import Identity, Locator, Metadata, make_anchor, format_citation
from .citations.historical import historical_loader
from .citations.text_attestation import strict_text_attestor
from .citations.pdf_review_attestation import ReviewedPage, pdf_review_attestor
from .citations.printed_label_attestation import printed_label_guard
from .ingest.pipeline import Ingest, canonical
from .ingest.retained_assets import RetainedAssets, MAX_BYTES as MAX_SOURCE_BYTES, SUFFIXES
from .ingest.writer_guard import exclusive_writer, _regular_if_present
from .reader.context import open_context
from .research.saved_evidence import SavedEvidenceStore
from .research.saved_topic_query import SavedTopicQueries
from .research.anchored_export import export_saved
from .research.topic_tree import TopicTree
from .search import literal, thematic
from .storage.revisions import CURRENT_SCHEMA, Storage, Page, _schema_signature, _expected_signature
from .data_paths import guard_data_directory
from .extract.document_formats import EXTENDED_MIMES, FORMAT_BY_MIME, ISOLATED_SUFFIXES


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    description: str
    archived: bool


@dataclass(frozen=True)
class SearchResult:
    workspace: str
    ref: str
    revision: str
    edition_id: str
    title: str
    author: str
    edition: str
    page: str
    quote: str
    citation: str
    verified: bool
    reason: str = ''


@dataclass(frozen=True)
class _Selection:
    workspace: str
    edition: str
    revision: str
    ordinal: int
    start: int
    end: int
    collection: str | None


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or '\x00' in value:
        raise ValueError('Enter a name of 1 to 512 characters.')
    return value.strip()


class _SearchBudget:
    """One cooperative deadline, including source loading and verification."""
    def __init__(self, timeout, cancelled):
        if (type(timeout) not in (int, float) or not 0 < timeout <= 60
                or not callable(cancelled)):
            raise ValueError('Invalid search operation budget.')
        self.timeout = timeout
        self.deadline = monotonic() + timeout
        self.cancelled = cancelled
        self.latched = False

    def stopped(self):
        self.latched = self.latched or bool(self.cancelled()) or monotonic() >= self.deadline
        return self.latched

    def check(self):
        if self.stopped():
            raise TimeoutError('Search cancelled or timed out.')

    def remaining(self):
        self.check()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            self.latched = True
            raise TimeoutError('Search cancelled or timed out.')
        # A coarse clock can return the same value twice; floating-point
        # subtraction must not expand the caller's validated time allowance.
        return min(remaining, self.timeout)


class _CatalogPublisher:
    """Keep the existing ingest journal, with one atomic catalog publication."""
    def __init__(self, storage, registration):
        self.storage, self.record = storage, registration
        self.db = storage.db
        self.review_records = lambda: ()
        self.published_here = False
        self.before_commit = lambda workspace, revision: None

    def publish_revision(self, workspace, edition, revision, asset, config, pages):
        if edition != self.record.edition_id or asset != self.record.manifest['sha256']:
            raise ValueError('Import identity changed.')
        with self.storage.transaction():
            LibraryApp._authorize(self.db, workspace)
            register_batch(Catalog(self.db, workspace), (self.record,))
            self.storage.stage_revision(workspace, edition, revision, asset, config, pages)
            self.db.execute('INSERT INTO source_metadata '
                '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                (workspace, edition, revision, '; '.join(self.record.authors) or None,
                 self.record.title or 'Unknown title', self.record.volume, self.record.edition))
            for review in self.review_records():
                if (review.get('workspace') != workspace or review.get('edition') != edition
                        or review.get('job') != revision or review.get('asset_sha256') != asset):
                    raise ValueError('Reviewed import publication identity changed.')
                row = self.db.execute('SELECT digest FROM page WHERE revision=? AND physical=?',
                                      (revision, review['physical'])).fetchone()
                if row is None or row[0] != review['raw_sha256']:
                    raise ValueError('Reviewed source text changed before publication.')
                self.db.execute('INSERT INTO source_page_review VALUES(?,?,?,?,?,?,?,?,1)',
                    (workspace, edition, revision, review['physical'], review['raw_sha256'],
                     review['render_sha256'], review['renderer'], review['reviewer']))
            # Reject inconsistent candidates while this publication can still
            # roll back every catalog, source, review and search-index write.
            self.storage._check()
            self.before_commit(workspace, revision)
        self.published_here = True

    def check(self):
        return self.storage.check()


class LibraryApp:
    """One private data directory, persistent workspaces and fresh scoped services."""
    def __init__(self, data_dir, *, semantic_ranker=None):
        self.root = guard_data_directory(data_dir)
        if (self.root / '.restore-incomplete').exists():
            raise ValueError('This restore is incomplete. Restore again to a new directory.')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError('Choose a local data directory, not a symbolic link.')
        self.root = self.root.resolve()
        self.database = self.root / 'library.sqlite'
        self.assets_dir = self.root / 'originals'
        self.assets_dir.mkdir(mode=0o700, exist_ok=True)
        self.assets = RetainedAssets(self.assets_dir)
        self.semantic_ranker = semantic_ranker
        self.semantic_cache = None
        self._selections = {}
        with exclusive_writer(self.root):
            self._check_database_paths()
            with closing(Storage(self.database, schema_version=CURRENT_SCHEMA)) as storage:
                storage.check()
        if semantic_ranker is not None:
            from .semantic import OfflineSemanticRanker
            if isinstance(semantic_ranker, OfflineSemanticRanker):
                self.semantic_cache = semantic_ranker.cache_for(self.root)

    def _check_database_paths(self):
        for path in (self.database, self.root / 'imports.sqlite'):
            _regular_if_present(path)
            for suffix in ('-wal', '-shm', '-journal'):
                _regular_if_present(Path(str(path) + suffix))

    @staticmethod
    def _authorize(db, workspace):
        if not isinstance(workspace, str) or not workspace or not db.execute(
                'SELECT 1 FROM workspace_meta WHERE workspace=? AND archived=0',
                (workspace,)).fetchone():
            raise ValueError('The selected library is missing or archived.')

    @contextmanager
    def read(self, workspace=None, *, budget=None):
        with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True,
                                     isolation_level=None, timeout=0 if budget is not None else 5)) as db:
            db.row_factory = sqlite3.Row
            if budget is not None:
                # Schema validation and authorization belong to the same
                # cancellable operation as retrieval. A SQLite busy handler
                # would otherwise sleep for five seconds without checking it.
                db.set_progress_handler(lambda: int(budget.stopped()), 1000)
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('PRAGMA recursive_triggers=ON')
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                if (db.execute('PRAGMA user_version').fetchone()[0] != CURRENT_SCHEMA
                        or _schema_signature(db) != _expected_signature(CURRENT_SCHEMA)):
                    raise ValueError('The library schema has changed.')
                if workspace is not None:
                    self._authorize(db, workspace)
                if budget is not None:
                    budget.check()
                yield db
            finally:
                if budget is not None:
                    # Cleanup must work even when cancellation remains latched.
                    db.set_progress_handler(None, 0)
                db.rollback()

    @contextmanager
    def write(self, workspace=None):
        with exclusive_writer(self.root):
            self._check_database_paths()
            with closing(Storage(self.database, schema_version=CURRENT_SCHEMA)) as storage:
                with storage.transaction():
                    if workspace is not None:
                        self._authorize(storage.db, workspace)
                    yield storage.db

    @contextmanager
    def _search_read(self, workspace, timeout, cancelled):
        budget = _SearchBudget(timeout, cancelled)
        budget.check()
        try:
            with self.read(workspace, budget=budget) as db:
                budget.check()
                yield db, budget
                budget.check()
        except InterruptedError as exc:
            if budget.stopped():
                raise TimeoutError('Search cancelled or timed out.') from exc
            raise
        except sqlite3.OperationalError as exc:
            # Include SQLite's extended BUSY/LOCKED codes in the same retry
            # policy while preserving unrelated database errors.
            code = (getattr(exc, 'sqlite_errorcode', 0) or 0) & 0xff
            if budget.stopped() or code == sqlite3.SQLITE_INTERRUPT:
                raise TimeoutError('Search cancelled or timed out.') from exc
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise RuntimeError('The library is busy. Retry the search after the writer finishes.') from exc
            raise

    def workspaces(self, *, include_archived=False):
        with self.read() as db:
            return tuple(Workspace(r[0], r[1], r[2], bool(r[3])) for r in db.execute(
                'SELECT workspace,name,description,archived FROM workspace_meta '
                'WHERE archived=0 OR ? ORDER BY name,workspace', (int(include_archived),)))

    def create_workspace(self, name, description=''):
        name, description = _name(name), _metadata(description)
        identity = uuid4().hex
        with self.write() as db:
            db.execute('INSERT INTO workspace(id) VALUES(?)', (identity,))
            db.execute('INSERT INTO workspace_meta(workspace,name,description,archived) VALUES(?,?,?,0)',
                       (identity, name, description or ''))
        return identity

    def rename_workspace(self, workspace, name, description=''):
        with self.write(workspace) as db:
            db.execute('UPDATE workspace_meta SET name=?,description=? WHERE workspace=?',
                       (_name(name), _metadata(description) or '', workspace))

    def archive_workspace(self, workspace):
        with self.write(workspace) as db:
            db.execute('UPDATE workspace_meta SET archived=1 WHERE workspace=?', (workspace,))
        self._selections = {k: v for k, v in self._selections.items() if v.workspace != workspace}

    def restore_workspace(self, workspace):
        with self.write() as db:
            if db.execute('UPDATE workspace_meta SET archived=0 WHERE workspace=?', (workspace,)).rowcount != 1:
                raise ValueError('Unknown library.')

    @contextmanager
    def catalog_transaction(self, workspace):
        with self.write(workspace) as db:
            yield Catalog(db, workspace)

    @contextmanager
    def topic_transaction(self, workspace):
        with self.write(workspace) as db:
            yield TopicTree(db, workspace)

    def collections(self, workspace):
        with self.read(workspace) as db:
            return active_collections(Catalog(db, workspace))

    def create_collection(self, workspace, name):
        ident = uuid4().hex
        with self.catalog_transaction(workspace) as catalog:
            catalog.create_collection(ident, _name(name))
        return ident

    def browse(self, workspace, **filters):
        with self.read(workspace) as db:
            return browse_editions(Catalog(db, workspace), **filters)

    def prepare_edition_file(self, workspace, edition_id, *, cancelled=lambda: False):
        """Open a catalog-linked original without inventing a quotation.

        A zero-width, view-only selection reuses the original-file authorization
        and byte-verification boundary. It cannot be copied as source evidence.
        Existing search selections and unsaved research notes remain available.
        """
        if cancelled():
            raise InterruptedError('Opening the linked document was cancelled.')
        with self.read(workspace) as db:
            get_edition(Catalog(db, workspace), edition_id)
            row = db.execute('SELECT revision,ordinal FROM active_page '
                'WHERE workspace=? AND edition=? ORDER BY ordinal LIMIT 1',
                (workspace, edition_id)).fetchone()
            if row is None:
                raise ValueError('The linked document has no completed source revision.')
            revision, ordinal = row
            selection = _Selection(workspace, edition_id, revision, ordinal, 0, 0, None)
            self._resolve_selection(db, workspace, selection, revision, cancelled=cancelled)
            ref = uuid4().hex
            self._selections[ref] = selection
        from .reader.source_file import prepare_source_file
        try:
            return prepare_source_file(self, workspace, ref, revision, cancelled=cancelled)
        except BaseException:
            self._selections.pop(ref, None)
            raise

    def export_bibliography(self, workspace, *, collection=None, format='csl-json',
                            cancelled=lambda: False):
        """Use the shared catalog exporter for the Bibliography desktop command."""
        return self.export_catalog(workspace, collection=collection, format=format,
                                   cancelled=cancelled)

    def edition_metadata(self, workspace, edition_id):
        """Read one exact edition in a fresh authorized catalog snapshot."""
        with self.read(workspace) as db:
            return get_edition(Catalog(db, workspace), edition_id)

    def lookup_doi(self, workspace, doi, *, online_allowed=False, providers=('crossref',),
                   unpaywall_email='', cancelled=lambda: False):
        """Preview exact DOI metadata without transmitting sources or private notes."""
        from .acquisition.service import service_for
        return service_for(self).lookup(workspace, doi, online_allowed=online_allowed,
            providers=providers, unpaywall_email=unpaywall_email, cancelled=cancelled)

    def lookup_isbn(self, workspace, isbn, *, online_allowed=False, cancelled=lambda: False):
        from .acquisition.service import service_for
        return service_for(self).lookup_isbn(workspace, isbn, online_allowed=online_allowed,
            cancelled=cancelled)

    def save_doi_reference(self, workspace, preview, *, cancelled=lambda: False):
        from .acquisition.service import service_for
        return service_for(self).save_reference(workspace, preview, cancelled=cancelled)

    def apply_doi_metadata(self, workspace, edition_id, preview, *, expected_metadata,
                           cancelled=lambda: False):
        from .acquisition.service import service_for
        return service_for(self).apply_metadata(workspace, edition_id, preview,
            expected_metadata=expected_metadata, cancelled=cancelled)

    def acquire_doi_source(self, workspace, preview, source_index, destination, *,
                           online_allowed=False, rights_confirmed=False, collection=None,
                           cancelled=lambda: False):
        from .acquisition.service import service_for
        return service_for(self).acquire(workspace, preview, source_index, destination,
            online_allowed=online_allowed, rights_confirmed=rights_confirmed,
            collection=collection, cancelled=cancelled)

    def catalog_report(self, workspace, *, collection=None, cancelled=lambda: False):
        """Inspect catalog completeness without granting source verification."""
        from .catalog.catalog_reports import catalog_report
        with self.read(workspace) as db:
            return catalog_report(db, workspace, collection=collection,
                                  cancelled=cancelled, timeout=60.0)

    def export_catalog(self, workspace, *, collection=None, format='csl-json',
                       cancelled=lambda: False):
        """Export current source metadata; quotations and private notes stay out."""
        from .catalog.bibliography import export_bibliography, export_inventory
        with self._search_read(workspace, 60.0, cancelled) as (db, budget):
            if format == 'csv':
                return export_inventory(db, workspace, collection=collection,
                                        cancelled=budget.stopped)
            return export_bibliography(db, workspace, collection=collection,
                                       format=format, cancelled=budget.stopped)

    def export_review_matrix(self, workspace, *, collection=None,
                             cancelled=lambda: False):
        """Export source-verified quotations, topic paths, notes and tags to CSV."""
        from .research.review_matrix import export_review_matrix
        with self._search_read(workspace, 60.0, cancelled) as (db, budget):
            load, metadata, _ = self._loaders(db, workspace, cancelled=budget.stopped)
            return export_review_matrix(db, workspace, load, metadata,
                                        collection=collection, cancelled=budget.stopped)

    def update_metadata(self, workspace, edition_id, *, title, authors=(),
                        language=None, edition=None, volume=None, expected_metadata=None,
                        selected_fields=None, cancelled=lambda: False):
        """Publish a metadata revision without changing any original source bytes.

        Already saved evidence keeps the previous metadata and source revision.
        New searches use the new snapshot. PDF reviews are never transferred.
        Title and authors belong to a work, so all its active editions receive
        new citation snapshots when those fields change. Edition-specific fields
        change only on the selected edition. An optional prior EditionView guards
        interactive edits against overwriting a concurrently changed catalog.
        Partial imports must supply both selected_fields and expected_metadata.
        Unselected values are retained exactly from the locked current catalog;
        only explicitly selected values receive the usual edit validation.
        """
        def check_cancelled():
            if cancelled():
                raise InterruptedError('Metadata update cancelled; no catalog changes were committed.')

        check_cancelled()
        allowed_fields = frozenset(('title', 'authors', 'language', 'edition', 'volume'))
        if selected_fields is None:
            fields = allowed_fields
        else:
            if (not isinstance(selected_fields, (tuple, list, set, frozenset))
                    or not selected_fields
                    or any(not isinstance(field, str) for field in selected_fields)
                    or expected_metadata is None):
                raise ValueError('A partial metadata edit requires selected fields and the reviewed metadata.')
            fields = frozenset(selected_fields)
            if not fields <= allowed_fields:
                raise ValueError('Select only supported metadata fields.')
        if 'title' in fields:
            title = _name(title)
        if 'authors' in fields:
            if not isinstance(authors, (tuple, list)) or len(authors) > 100:
                raise ValueError('authors must be a list or tuple of at most 100 names.')
            authors = tuple(_name(name) for name in authors)
        if 'language' in fields:
            language = _metadata(language)
        if 'edition' in fields:
            edition = _metadata(edition)
        if 'volume' in fields:
            volume = _metadata(volume)
        revision = uuid4().hex
        with exclusive_writer(self.root):
            check_cancelled()
            self._check_database_paths()
            with closing(Storage(self.database, schema_version=CURRENT_SCHEMA)) as storage:
                with storage.transaction():
                    db = storage.db
                    self._authorize(db, workspace)
                    current = get_edition(Catalog(db, workspace), edition_id)
                    if expected_metadata is not None and current != expected_metadata:
                        raise ValueError('Book metadata changed. Refresh the catalog before editing again.')
                    if selected_fields is not None:
                        title = title if 'title' in fields else current.title
                        authors = authors if 'authors' in fields else current.authors
                        language = language if 'language' in fields else current.language
                        edition = edition if 'edition' in fields else current.edition
                        volume = volume if 'volume' in fields else current.volume
                    row = db.execute('SELECT e.work,e.asset,a.revision,r.config FROM catalog_edition e '
                        'JOIN active a ON a.workspace=e.workspace AND a.edition=e.id '
                        'JOIN revision r ON r.id=a.revision WHERE e.workspace=? AND e.id=?',
                        (workspace, edition_id)).fetchone()
                    if row is None:
                        raise ValueError('Select an imported edition in this library.')
                    work = row['work']
                    shared_changed = current.title != title or current.authors != authors
                    affected = tuple(db.execute(
                        'SELECT e.id,e.asset,a.revision,r.config,e.edition,e.volume FROM catalog_edition e '
                        'JOIN active a ON a.workspace=e.workspace AND a.edition=e.id '
                        'JOIN revision r ON r.id=a.revision WHERE e.workspace=? AND e.work=? '
                        'AND (? OR e.id=?) ORDER BY e.id',
                        (workspace, work, int(shared_changed), edition_id)))
                    db.execute('UPDATE catalog_work SET title=? WHERE workspace=? AND id=?', (title, workspace, work))
                    db.execute('DELETE FROM catalog_author WHERE workspace=? AND work=?', (workspace, work))
                    db.executemany('INSERT INTO catalog_author(workspace,work,position,name) VALUES(?,?,?,?)',
                        ((workspace, work, n, name) for n, name in enumerate(authors)))
                    db.execute('UPDATE catalog_edition SET language=?,edition=?,volume=? WHERE workspace=? AND id=?',
                        (language, edition, volume, workspace, edition_id))
                    for affected_row in affected:
                        check_cancelled()
                        eid, asset, previous, config, previous_label, previous_volume = affected_row
                        source_pages = tuple(db.execute(
                            'SELECT * FROM page WHERE revision=? ORDER BY ordinal', (previous,)))
                        if any(not isinstance(r['raw'], str) or
                               sha256(r['raw'].encode('utf-8')).hexdigest() != r['digest']
                               for r in source_pages):
                            raise ValueError('Stored source text has been damaged; metadata was not changed.')
                        pages = tuple(Page(r['raw'], r['physical'], r['printed'], bool(r['verified']))
                                      for r in source_pages)
                        next_revision = revision if eid == edition_id else uuid4().hex
                        next_label = edition if eid == edition_id else previous_label
                        next_volume = volume if eid == edition_id else previous_volume
                        storage.stage_revision(workspace, eid, next_revision, asset, config, pages)
                        db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                            (workspace, eid, next_revision, '; '.join(authors) or None,
                             title, next_volume, next_label))
                    check_cancelled()
        return revision

    def rename_collection(self, workspace, collection, name):
        with self.catalog_transaction(workspace) as catalog:
            catalog._active_collection(collection)
            catalog.db.execute('UPDATE catalog_collection SET name=? WHERE workspace=? AND id=?',
                               (_name(name), workspace, collection))

    def topics(self, workspace, *, include_archived=False):
        with self.read(workspace) as db:
            return TopicTree(db, workspace).list_topics(include_archived=include_archived)

    def create_topic(self, workspace, name, parent=None):
        ident = uuid4().hex
        with self.topic_transaction(workspace) as tree:
            tree.create(ident, _name(name), parent=parent)
        return ident

    def list_scopes(self):
        rows = []
        for workspace in self.workspaces():
            rows.append((workspace.id, None, workspace.name))
            topics = self.topics(workspace.id)
            by_id = {topic.id: topic for topic in topics}
            for topic in topics:
                names, seen, current = [], set(), topic
                while current is not None:
                    if current.id in seen:
                        raise ValueError('A research topic cycle was detected.')
                    seen.add(current.id)
                    names.append(current.name)
                    if current.parent is not None and current.parent not in by_id:
                        raise ValueError('A research topic parent is unavailable.')
                    current = by_id.get(current.parent)
                rows.append((workspace.id, topic.id, workspace.name + ': ' + ' / '.join(reversed(names))))
        return tuple(rows)

    def import_file(self, workspace, path, *, title=None, authors=(), language=None,
                    edition=None, volume=None, collections=(), work_id=None,
                    edition_id=None, cancelled=lambda: False, config=None, timeout=120.0):
        """Isolate document parsing; retain the exact strict UTF-8 TXT path inline."""
        from .extract.page_ocr import Config
        if not isinstance(authors, (tuple, list)) or len(authors) > 100:
            raise ValueError('invalid authors')
        if not isinstance(collections, (tuple, list)) or len(collections) > 100:
            raise ValueError('invalid collections')
        path = Path(path).absolute()
        metadata = dict(title=title, authors=tuple(authors), language=language,
                        edition=edition, volume=volume, collections=tuple(collections),
                        work_id=work_id, edition_id=edition_id)
        if path.suffix.casefold() in ISOLATED_SUFFIXES:
            from .ingest.worker import run_request
            with self.read(workspace):
                pass
            return run_request(self.root, workspace, path, metadata, config or Config(),
                               cancelled=cancelled, timeout=timeout)
        return self._import_file(workspace, path, **metadata, cancelled=cancelled, config=config)

    def resume_import(self, workspace, request_id, *, cancelled=lambda: False, timeout=120.0):
        """Resume a durable native import from retained bytes and its existing job."""
        from .ingest.worker import load_request, run_request
        with self.read(workspace):
            request = load_request(self.root, request_id)
            if request.get('workspace') != workspace:
                raise ValueError('Import request belongs to another library.')
        return run_request(self.root, workspace, request['source'], {}, None,
                           request_id=request_id, cancelled=cancelled, timeout=timeout)

    def import_requests(self, workspace):
        """List resumable native requests without exposing paths as source authority."""
        from .ingest.worker import load_request
        with self.read(workspace):
            directory = self.root / 'import-requests'
            if not directory.exists():
                return ()
            rows = []
            for path in sorted(directory.glob('*.json')):
                request = load_request(self.root, path.stem)
                if request.get('workspace') == workspace:
                    rows.append(dict(request_id=request['request_id'],
                                     file=Path(request['source']).name,
                                     state=request.get('state', 'pending'),
                                     result=request.get('result')))
            return tuple(rows)

    def plan_import_batch(self, workspace, paths, *, collections=(), recursive=True,
                          config=None, cancelled=lambda: False):
        """Inspect selected files and persist a resumable private import queue."""
        from .ingest.batches import ImportBatchStore
        return ImportBatchStore(self).plan(workspace, paths, collections=collections,
            recursive=recursive, config=config, cancelled=cancelled)

    def import_batches(self, workspace):
        from .ingest.batches import ImportBatchStore
        return ImportBatchStore(self).list(workspace)

    def import_batch(self, workspace, batch_id):
        from .ingest.batches import ImportBatchStore
        return ImportBatchStore(self).get(workspace, batch_id)

    def run_import_batch(self, workspace, batch_id, *, cancelled=lambda: False,
                         timeout=120.0, retry_failed=False, progress=None):
        from .ingest.batches import ImportBatchStore
        return ImportBatchStore(self).run(workspace, batch_id, cancelled=cancelled,
            timeout=timeout, retry_failed=retry_failed, progress=progress)

    def set_import_batch_row(self, workspace, batch_id, row_id, *, included):
        from .ingest.batches import ImportBatchStore
        return ImportBatchStore(self).set_row(workspace, batch_id, row_id, included=included)

    def pending_review(self, workspace, request_id):
        """Return the exact quarantined page for an explicit human comparison."""
        from .ingest.worker import pending_review
        with self.read(workspace):
            return pending_review(self.root, workspace, request_id)

    def confirm_import_review(self, workspace, request_id, reviewer, *, confirmed=False,
                              expected_checksum=None):
        """Approve only the displayed exact checkpoint; resume is a separate action."""
        from .ingest.worker import confirm_review
        with self.read(workspace):
            return confirm_review(self.root, workspace, request_id, reviewer,
                                  confirmed=confirmed, expected_checksum=expected_checksum)

    def _import_file(self, workspace, path, *, title=None, authors=(), language=None,
                     edition=None, volume=None, collections=(), work_id=None,
                     edition_id=None, cancelled=lambda: False, config=None,
                     _request_id=None, _resume_job=None, _expected_manifest=None,
                     _publication_attempt=None):
        """Retain original bytes and reuse resumable ingest with atomic publication.

        A completed TXT import is independently verified against original bytes.
        PDF extraction stays a candidate until independently reviewed. Quarantined
        OCR is reported explicitly and never silently exposed as verified text.
        """
        from .extract.page_ocr import Config
        path = Path(path).absolute()
        collections = tuple(collections)
        with self.read(workspace) as db:
            catalog = Catalog(db, workspace)
            for collection in collections:
                catalog._active_collection(collection)
        if _expected_manifest is None:
            asset = snapshot(path.parent, path.name)
        else:
            from .ingest.batch_scan import planned_snapshot
            asset = planned_snapshot(path)
        if _expected_manifest is not None and asset.manifest() != _expected_manifest:
            raise ValueError('Source changed after import planning; plan this file again.')
        record = Registration(work_id or uuid4().hex, edition_id or uuid4().hex,
            asset.manifest(), _metadata(title) or path.stem, tuple(authors), language,
            edition, volume, tuple(collections))
        with exclusive_writer(self.root):
            self._check_database_paths()
            with closing(Storage(self.database, schema_version=CURRENT_SCHEMA)) as storage:
                self._authorize(storage.db, workspace)
                for collection in collections:
                    Catalog(storage.db, workspace)._active_collection(collection)
                retained = self.assets.retain(asset.data, asset.mime, asset.digest)
                from .ingest.worker import (RuntimeBoundIngest, load_reviews, record_progress,
                                            record_publication_candidate)
                record_progress(self.root, _request_id, asset_sha256=asset.digest)
                publisher = _CatalogPublisher(storage, record)
                publisher.before_commit = lambda scope, revision: record_publication_candidate(
                    self.root, _request_id, storage.db, scope, revision,
                    publication_attempt=_publication_attempt, config=config or Config())
                if asset.mime in EXTENDED_MIMES or asset.mime in (
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        'application/epub+zip'):
                    if asset.mime == 'application/epub+zip':
                        from .extract.epub_ordered import extract_epub
                        from .reader.ebook_contents import extraction_settings
                        extracted = extract_epub(asset.data)
                        source_format = 'EPUB'
                        settings = extraction_settings(extracted)
                    elif asset.mime == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
                        from .extract.docx_ordered import extract_docx
                        extracted = extract_docx(asset.data)
                        source_format = 'DOCX'
                        # Preserve the original configuration and revision identities.
                        settings = canonical({'adapter': 'python-docx-ordered-v1', 'kind': 'logical_segment',
                                              'provenance': extracted['provenance']})
                    else:
                        from .extract.document_formats import extract_document, extraction_settings
                        extracted = extract_document(asset.data, SUFFIXES[asset.mime], cancelled=cancelled)
                        source_format = FORMAT_BY_MIME[asset.mime].name
                        settings = canonical(extraction_settings(extracted, asset.mime))
                    if cancelled():
                        raise InterruptedError('Import cancelled.')
                    revision = sha256(canonical([workspace, record.edition_id, asset.digest, settings])
                                      .encode('utf-8')).hexdigest()
                    prior = storage.db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                                               (revision,)).fetchone()
                    if prior is None:
                        publisher.publish_revision(workspace, record.edition_id, revision, asset.digest, settings,
                            tuple(Page(r['raw_text']) for r in extracted['records']))
                    else:
                        if tuple(prior) != (workspace, record.edition_id, asset.digest, settings):
                            raise ValueError(f'Published {source_format} import identity changed.')
                        pages = storage.db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                            'FROM page WHERE revision=? ORDER BY ordinal', (revision,)).fetchall()
                        expected = tuple((i, None, None, 0, r['raw_text'], r['text_sha256'])
                                         for i, r in enumerate(extracted['records'], 1))
                        if (tuple(tuple(p) for p in pages) != expected or not storage.db.execute(
                                'SELECT 1 FROM sealed WHERE revision=?', (revision,)).fetchone()):
                            raise ValueError(f'Published {source_format} import differs from retained source bytes.')
                        with storage.transaction():
                            register_batch(Catalog(storage.db, workspace), (record,))
                            # Replays may add memberships; validate before exposing them.
                            storage._check()
                    publisher.check()
                    return {'state': 'complete', 'edition': record.edition_id, 'id': revision,
                            'completed': len(extracted['records']), 'verified': False,
                            'format': source_format, 'warnings': extracted['warnings'],
                            'message': f'{source_format} imported as unverified logical segments; '
                                       'printed pages are unknown. ' + (
                                           FORMAT_BY_MIME[asset.mime].limitations if asset.mime in EXTENDED_MIMES else '')}
                engine = RuntimeBoundIngest if asset.mime == 'application/pdf' else Ingest
                extra = (dict(reviews=load_reviews(self.root, _request_id), review_asset=asset.data)
                         if asset.mime == 'application/pdf' else {})
                with closing(engine(self.root / 'imports.sqlite', publisher, **extra)) as ingest:
                    if isinstance(ingest, RuntimeBoundIngest):
                        publisher.review_records = lambda: tuple(ingest.validated_reviews.values())
                    settings = config or Config()
                    if _resume_job is not None:
                        previous = ingest.inspect(workspace, _resume_job)
                        if previous['edition'] != record.edition_id or previous['asset'] != asset.digest:
                            raise ValueError('Import resume identity differs from the retained source.')
                        job = _resume_job
                        if previous['state'] in ('failed', 'cancelled', 'quarantined'):
                            ingest.retry(workspace, job)
                    else:
                        job = ingest.start(workspace, record.edition_id, self.assets_dir, retained.name, settings)
                    record_progress(self.root, _request_id, job=job)
                    result = ingest.run(workspace, job, self.assets_dir, retained.name, settings, cancelled=cancelled)
                    if result['state'] in ('complete', 'superseded') and not publisher.published_here:
                        if cancelled():
                            raise InterruptedError('Import cancelled before catalog registration.')
                        # Recovery validates the retained asset/configuration and
                        # checkpoint/page identity, but does not replay catalog
                        # registration. Apply the same atomic membership and
                        # metadata-conflict gate as the DOCX/EPUB replay path.
                        # A journal's terminal state alone is not proof that its
                        # source was actually published. Never reactivate an old
                        # revision when current metadata has superseded it.
                        with storage.transaction():
                            self._authorize(storage.db, workspace)
                            published = storage.db.execute(
                                'SELECT r.workspace,r.edition,r.asset,a.revision FROM revision r '
                                'JOIN sealed s ON s.revision=r.id LEFT JOIN active a '
                                'ON a.workspace=r.workspace AND a.edition=r.edition WHERE r.id=?',
                                (job,)).fetchone()
                            if (published is None or tuple(published[:3]) !=
                                    (workspace, record.edition_id, asset.digest)
                                    or published[3] is None):
                                raise ValueError('Completed import is missing its sealed source revision.')
                            if (published[3] == job) != (result['state'] == 'complete'):
                                raise ValueError('Completed import active revision changed.')
                            register_batch(Catalog(storage.db, workspace), (record,))
                            storage._check()
                            if cancelled():
                                raise InterruptedError('Import registration cancelled before commit.')
                    result['verified'] = asset.mime == 'text/plain' and result['state'] == 'complete'
                    return result

    def import_files(self, workspace, paths, *, progress=None, **metadata):
        """Import sequentially and report actual per-file outcomes.

        Reporting is an optional observer, outside every import transaction.
        It cannot change a committed result or cause a file to be retried.
        A single file and unsized iterables stay indeterminate: file counts do
        not measure page extraction, OCR, or elapsed time within one file.
        """
        total = None
        if progress is not None:
            try:
                count = len(paths)
                total = count if count > 1 else None
            except Exception:
                pass  # Do not consume an iterable merely to count it.

        def report(completed):
            nonlocal progress
            if progress is not None:
                try:
                    progress(completed, total, 'Processing files')
                except Exception:
                    progress = None

        cancelled = metadata.get('cancelled', lambda: False)
        report(0)
        results = []
        for path in paths:
            try:
                if cancelled():
                    raise InterruptedError('Import cancelled before processing this file; remaining files were not attempted.')
                results.append({'file': Path(path).name, **self.import_file(workspace, path, **metadata)})
            except InterruptedError as exc:
                results.append({'file': Path(path).name, 'state': 'cancelled', 'message': str(exc)})
            except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                results.append({'file': Path(path).name, 'state': 'failed', 'message': str(exc)})
            report(len(results))
            if results[-1].get('state') == 'cancelled':
                break
        return tuple(results)

    def add_text(self, workspace, title, text, **metadata):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Enter original source text.')
        with TemporaryDirectory(prefix='.manual-', dir=self.root) as tmp:
            path = Path(tmp) / 'original.txt'
            path.write_bytes(text.encode('utf-8'))
            return self.import_file(workspace, path, title=_name(title), **metadata)

    def _retained_source(self, db, workspace, identity, *, checked_assets=None,
                         cancelled=lambda: False):
        """Authenticate source identity independently of text-review authority.

        The optional cache belongs only to one search snapshot and holds paths,
        not source bytes. Authorization is still checked for every edition.
        Missing or changed originals must never become ordinary preview results.
        """
        self._authorize(db, workspace)
        if identity.workspace != workspace:
            raise ValueError('Source is outside the selected library.')
        row = db.execute('SELECT a.mime,a.size FROM catalog_edition e '
            'JOIN catalog_asset a ON a.digest=e.asset '
            'WHERE e.workspace=? AND e.id=? AND e.asset=?',
            (workspace, identity.edition_id, identity.asset_sha256)).fetchone()
        if row is None:
            raise ValueError('Source is outside the selected library.')
        mime, size = row
        if mime not in SUFFIXES or type(size) is not int or not 0 < size <= MAX_SOURCE_BYTES:
            raise ValueError('Invalid original source catalog manifest.')
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        key = identity.asset_sha256, mime, size
        if checked_assets is not None and key in checked_assets:
            return checked_assets[key], mime, size
        # Existing retention resolution checks regular-file/link rules, the
        # 64 MiB size bound, and the complete original-byte SHA256.
        path = self.assets.resolve(identity.asset_sha256)
        if path.suffix != SUFFIXES[mime] or path.stat().st_size != size:
            raise ValueError('The original source differs from its catalog manifest.')
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        if checked_assets is not None:
            checked_assets[key] = path
        return path, mime, size

    def _asset_bytes(self, db, workspace, identity, *, cancelled=lambda: False):
        path, mime, size = self._retained_source(db, workspace, identity, cancelled=cancelled)
        original = snapshot(self.assets_dir, path.name, max_bytes=size)
        if (original.digest, original.mime, original.size) != (identity.asset_sha256, mime, size):
            raise ValueError('The original source differs from its catalog manifest.')
        if cancelled():
            raise InterruptedError('Source verification cancelled.')
        return original.data

    def _loaders(self, db, workspace, *, cancelled=lambda: False):
        resolve_asset = lambda identity: self._asset_bytes(db, workspace, identity, cancelled=cancelled)
        text = strict_text_attestor(db, workspace, resolve_asset)

        def review(identity, physical):
            row = db.execute('SELECT raw_sha256,render_sha256,renderer,reviewer FROM source_page_review '
                'WHERE workspace=? AND edition=? AND revision=? AND physical=? AND active=1',
                (workspace, identity.edition_id, identity.revision, physical)).fetchone()
            return ReviewedPage(identity, physical, *row) if row else None

        from .ingest.worker import isolated_render_fingerprint
        pdf = pdf_review_attestor(workspace, resolve_asset, review,
            renderer=lambda data, physical: isolated_render_fingerprint(data, physical, cancelled=cancelled))

        def attest(identity, locator, raw_hash):
            if cancelled():
                raise InterruptedError('Source verification cancelled.')
            verified = text(identity, locator, raw_hash) or pdf(identity, locator, raw_hash)
            if cancelled():
                raise InterruptedError('Source verification cancelled.')
            return verified

        def metadata(identity, value):
            if identity.workspace != workspace:
                return False
            row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                'WHERE workspace=? AND edition=? AND revision=?',
                (workspace, identity.edition_id, identity.revision)).fetchone()
            return row is not None and Metadata(*row) == value

        guarded = printed_label_guard(workspace, attest, lambda *_: None)
        return historical_loader(db, workspace, guarded), metadata, guarded

    def _selected(self, db, workspace, ref, revision, *, cancelled=lambda: False):
        selected = self._selections.get(ref)
        return self._resolve_selection(db, workspace, selected, revision, cancelled=cancelled)

    def _resolve_selection(self, db, workspace, selected, revision, *, checked_assets=None,
                           cancelled=lambda: False):
        resolved = self._stored_selection(db, workspace, selected, revision)
        self._retained_source(db, workspace, resolved[1], checked_assets=checked_assets,
                              cancelled=cancelled)
        return resolved

    def _stored_selection(self, db, workspace, selected, revision):
        """Validate stored identity for withdrawing authority, without reading originals.

        This cannot establish source authenticity. Search, preview and evidence
        operations must use the retained-source guard in _resolve_selection.
        """
        if not selected or selected.workspace != workspace or selected.revision != revision:
            raise ValueError('Select a result in the active library again.')
        if selected.collection is not None:
            catalog = Catalog(db, workspace)
            catalog._active_collection(selected.collection)
            if not db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? AND collection=? AND edition=?',
                (workspace, selected.collection, selected.edition)).fetchone():
                raise ValueError('The source has left the selected collection. Search again.')
        row = db.execute('SELECT p.*,r.asset FROM page p JOIN revision r ON r.id=p.revision '
            'JOIN sealed s ON s.revision=r.id WHERE r.workspace=? AND r.edition=? AND r.id=? AND p.ordinal=?',
            (workspace, selected.edition, revision, selected.ordinal)).fetchone()
        if row is None:
            raise ValueError('The selected source is unavailable.')
        if sha256(row['raw'].encode('utf-8')).hexdigest() != row['digest']:
            raise ValueError('The stored source text has been damaged.')
        identity = Identity(workspace, selected.edition, row['asset'], revision)
        locator = (Locator('physical_page', row['physical'], row['printed'], bool(row['verified']))
                   if row['physical'] is not None else Locator('logical_segment', row['ordinal']))
        meta = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
            'WHERE workspace=? AND edition=? AND revision=?', (workspace, selected.edition, revision)).fetchone()
        if meta is None:
            raise ValueError('Historical source metadata is missing.')
        return selected, identity, locator, row['raw'], Metadata(*meta)

    def search(self, workspace, query, *, mode='phrase', collection=None, title=None,
               author=None, language=None, edition=None, limit=100, timeout=60.0,
               cancelled=lambda: False, authorized_topic=None, on_status=None,
               result_mode=None, offset=0):
        if mode not in ('word', 'phrase', 'terms', 'thematic', 'semantic', 'advanced'):
            raise ValueError('Unknown search mode.')
        if result_mode not in (None, 'short', 'thematic'):
            raise ValueError('Choose Short quotations or Thematic excerpts.')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid search operation budget.')
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid search page offset.')
        if offset and mode not in ('terms', 'advanced'):
            raise ValueError('Result pages are available for All words and Advanced searches.')
        if on_status is not None and not callable(on_status):
            raise ValueError('Search status callback must be callable.')
        with self._search_read(workspace, timeout, cancelled) as (db, budget):
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            catalog = Catalog(db, workspace)
            views = browse_editions(catalog, collection=collection, title=title, author=author,
                                    language=language, edition=edition, limit=500)
            budget.check()
            if len(views) == 500:
                more = browse_editions(catalog, collection=collection, title=title, author=author,
                    language=language, edition=edition, after=views[-1].edition_id, limit=1)
                budget.check()
                if more:
                    raise ValueError('Narrow the search to at most 500 editions using filters.')
            allowed = {v.edition_id: v.work_id for v in views}
            if mode in ('thematic', 'semantic'):
                if mode == 'semantic' and self.semantic_ranker is not None:
                    from .semantic import search_snapshot
                    hits = search_snapshot(db, workspace, allowed, query, self.semantic_ranker,
                                           limit=limit, timeout=budget.remaining(), cancelled=budget.stopped,
                                           on_status=on_status, cache=self.semantic_cache)
                else:
                    if mode == 'semantic' and on_status is not None:
                        on_status('Local model unavailable; keyword search used.')
                    hits = thematic.search_active(db, workspace, allowed, query, limit=limit,
                                                  timeout=budget.remaining(), cancelled=budget.stopped)
            else:
                hits = literal.search(db, workspace, query, edition_ids=tuple(allowed),
                                      mode=mode, limit=limit, offset=offset,
                                      timeout=budget.remaining(), cancelled=budget.stopped)
            # Catalog selection, active source selection, and metadata materialization
            # all share this read transaction. No cached edition filters cross snapshots.
            loader, metadata, attestor = self._loaders(db, workspace, cancelled=budget.stopped)
            results = []
            selections = {}
            shown = set()
            checked_assets = {}
            for hit in hits:
                budget.check()
                row = db.execute('SELECT ordinal FROM page WHERE id=? AND revision=?',
                    (hit['page_id'], hit['revision_id'])).fetchone()
                if row is None:
                    raise ValueError('The search returned an invalid source page.')
                ref = uuid4().hex
                selections[ref] = _Selection(workspace, hit['edition_id'], hit['revision_id'],
                    row[0], hit['start'], hit['end'], collection)
                selected, identity, locator, raw, meta = self._resolve_selection(
                    db, workspace, selections[ref], hit['revision_id'],
                    checked_assets=checked_assets, cancelled=budget.stopped)
                if raw[selected.start:selected.end] != hit['raw_quote']:
                    raise ValueError('Search quotation differs from the original source.')
                if result_mode is not None:
                    from .search.presentation import presentation_span
                    start, end = presentation_span(raw, hit, query, result_mode, check=budget.check)
                    selected = _Selection(workspace, selected.edition, selected.revision,
                                          selected.ordinal, start, end, collection)
                    selections[ref] = selected
                    identity_span = (identity, locator, start, end)
                    if identity_span in shown:
                        del selections[ref]
                        continue
                    shown.add(identity_span)
                verified = attestor(identity, locator, sha256(raw.encode('utf-8')).hexdigest()) is True
                budget.check()
                if verified:
                    anchor = make_anchor(identity, locator, raw, selected.start, selected.end, meta, text_verified=True)
                    citation = format_citation(anchor)
                else:
                    citation = ', '.join(filter(None, [meta.author or 'Unknown author', meta.title,
                        meta.edition, locator.display()])) + ' [extracted text requires independent review]'
                reason = hit.get('reason', 'literal source match')
                if mode == 'semantic' and self.semantic_ranker is None:
                    reason = 'Semantic model unavailable; using lexical thematic search.'
                results.append(SearchResult(workspace, ref, identity.revision, identity.edition_id,
                    meta.title, meta.author or 'Unknown author', meta.edition or '', locator.display(),
                    raw[selected.start:selected.end], citation, verified, reason))
        # A stopped or failed operation must neither publish partial selections
        # nor evict selections belonging to the previous completed search.
        self._selections.update(selections)
        while len(self._selections) > 2000:
            del self._selections[next(iter(self._selections))]
        return tuple(results)

    def _anchor(self, db, workspace, ref, revision, *, cancelled=lambda: False):
        selected, identity, locator, raw, metadata = self._selected(
            db, workspace, ref, revision, cancelled=cancelled)
        load, attest_metadata, _ = self._loaders(db, workspace, cancelled=cancelled)
        _, _, checked, verified = load(identity, locator)
        if checked != raw or not attest_metadata(identity, metadata):
            raise ValueError('Source verification failed.')
        return make_anchor(identity, locator, raw, selected.start, selected.end, metadata,
                           text_verified=verified), load, attest_metadata

    def context(self, workspace, ref, revision, *, cancelled=lambda: False):
        with self.read(workspace) as db:
            anchor, load, _ = self._anchor(db, workspace, ref, revision, cancelled=cancelled)
            return open_context(workspace, (anchor,), load)

    def preview(self, workspace, ref, revision, *, authorized_topic=None,
                cancelled=lambda: False):
        """Unverified extraction is clearly labelled and cannot be copied as evidence."""
        with self.read(workspace) as db:
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            selected, identity, locator, raw, meta = self._selected(
                db, workspace, ref, revision, cancelled=cancelled)
            section = ''
            if locator.kind == 'logical_segment':
                from .reader.ebook import section_description
                section = section_description(db, identity, selected.ordinal)
            return (f'{meta.title}\n{locator.display()}\n' + (section + '\n' if section else '') +
                    'Extracted text; verification status is shown in the search result.\n\n' + raw)

    def copy(self, workspace, ref, revision, field='both', *, cancelled=lambda: False):
        with self.read(workspace) as db:
            anchor, load, _ = self._anchor(db, workspace, ref, revision, cancelled=cancelled)
            return open_context(workspace, (anchor,), load).clipboard(workspace, 0, field, load)

    @contextmanager
    def evidence_transaction(self, workspace, *, cancelled=lambda: False):
        with self.write(workspace) as db:
            load, metadata, _ = self._loaders(db, workspace, cancelled=cancelled)
            yield SavedEvidenceStore(db, workspace, load, metadata)
            if cancelled():
                raise InterruptedError('Evidence operation cancelled.')

    def save_hit(self, workspace, topic, ref, revision, note='', *, authorized_topic=None,
                 cancelled=lambda: False):
        evidence = uuid4().hex
        with self.write(workspace) as db:
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            anchor, load, metadata = self._anchor(db, workspace, ref, revision, cancelled=cancelled)
            SavedEvidenceStore(db, workspace, load, metadata).save(topic, evidence, anchor, note)
            if cancelled():
                raise InterruptedError('Saving the quotation was cancelled.')
        return evidence

    def evidence(self, workspace, topic, *, cancelled=lambda: False):
        with self.read(workspace) as db:
            load, metadata, _ = self._loaders(db, workspace, cancelled=cancelled)
            return SavedEvidenceStore(db, workspace, load, metadata).list_saved(topic, cancelled=cancelled)

    def search_notebook(self, workspace, query, *, field='both', tag=None,
                        include_archived=False, limit=50, timeout=60.0,
                        cancelled=lambda: False):
        from .research.notebook_search import search_notebook
        with self._search_read(workspace, timeout, cancelled) as (db, budget):
            load, metadata, _ = self._loaders(db, workspace, cancelled=budget.stopped)
            return search_notebook(SavedEvidenceStore(db, workspace, load, metadata), workspace, query,
                field=field, tag=tag, include_archived=include_archived, limit=limit,
                timeout=budget.remaining(), cancelled=budget.stopped)

    def get_notebook_hit(self, workspace, topic, evidence, *, include_archived=False,
                         timeout=60.0, cancelled=lambda: False):
        from .research.notebook_search import get_notebook_hit
        with self._search_read(workspace, timeout, cancelled) as (db, budget):
            load, metadata, _ = self._loaders(db, workspace, cancelled=budget.stopped)
            return get_notebook_hit(SavedEvidenceStore(db, workspace, load, metadata), workspace,
                topic, evidence, include_archived=include_archived,
                timeout=budget.remaining(), cancelled=budget.stopped)

    def set_evidence_tags(self, workspace, topic, evidence, names):
        names = tuple(dict.fromkeys(_name(name) for name in names))
        if len(names) > 100:
            raise ValueError('An evidence item can have at most 100 tags.')
        with self.evidence_transaction(workspace) as store:
            store.tree._visible(topic)
            if not any(row.evidence_id == evidence for row in store.list_saved(topic)):
                raise ValueError('Unknown evidence in this topic.')
            db = store.db
            db.execute('DELETE FROM research_evidence_tag WHERE workspace=? AND topic=? AND evidence=?',
                       (workspace, topic, evidence))
            for name in names:
                row = db.execute('SELECT id FROM research_tag WHERE workspace=? AND name=?', (workspace, name)).fetchone()
                identity = row[0] if row else uuid4().hex
                if row is None:
                    db.execute('INSERT INTO research_tag(workspace,id,name) VALUES(?,?,?)', (workspace, identity, name))
                db.execute('INSERT INTO research_evidence_tag(workspace,topic,evidence,tag) VALUES(?,?,?,?)',
                           (workspace, topic, evidence, identity))

    def evidence_tags(self, workspace, topic, evidence):
        with self.read(workspace) as db:
            if not db.execute('SELECT 1 FROM research_evidence WHERE workspace=? AND topic=? AND id=?',
                              (workspace, topic, evidence)).fetchone():
                raise ValueError('Unknown evidence in this topic.')
            return tuple(row[0] for row in db.execute('SELECT t.name FROM research_tag t '
                'JOIN research_evidence_tag e ON e.workspace=t.workspace AND e.tag=t.id '
                'WHERE e.workspace=? AND e.topic=? AND e.evidence=? ORDER BY t.name',
                (workspace, topic, evidence)))

    def evidence_with_tag(self, workspace, topic, name):
        with self.read(workspace) as db:
            load, metadata, _ = self._loaders(db, workspace)
            ids = {row[0] for row in db.execute('SELECT e.evidence FROM research_evidence_tag e '
                'JOIN research_tag t ON t.workspace=e.workspace AND t.id=e.tag '
                'WHERE e.workspace=? AND e.topic=? AND t.name=?', (workspace, topic, _name(name)))}
            return tuple(row for row in SavedEvidenceStore(db, workspace, load, metadata).list_saved(topic)
                         if row.evidence_id in ids)

    def review_pdf_page(self, workspace, ref, revision, reviewer, *, confirmed=False,
                        authorized_topic=None, cancelled=lambda: False):
        """Record an explicit independent human comparison to the original page.

        This does not approve printed pagination. No parser/model agreement or
        successful import calls this method; the desktop asks for a fresh review.
        """
        if confirmed is not True:
            raise ValueError('Independent comparison with the original page must be confirmed.')
        if not callable(cancelled):
            raise ValueError('A review cancellation callback is required.')

        def check():
            if cancelled():
                raise InterruptedError('Review cancelled; no change was saved.')

        check()
        reviewer = _name(reviewer)
        from .ingest.worker import isolated_render_fingerprint
        with self.read(workspace) as db:
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            _, identity, locator, raw, _ = self._selected(db, workspace, ref, revision)
            if locator.kind != 'physical_page':
                raise ValueError('This review workflow is for physical PDF pages only.')
            data = self._asset_bytes(db, workspace, identity)
            renderer, fingerprint = isolated_render_fingerprint(data, locator.index, cancelled=cancelled)
            raw_hash = sha256(raw.encode('utf-8')).hexdigest()
        check()
        with self.write(workspace) as db:
            check()
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            if cancelled():
                raise InterruptedError('PDF page review cancelled.')
            _, current, current_loc, current_raw, _ = self._selected(db, workspace, ref, revision)
            if (current, current_loc, current_raw) != (identity, locator, raw):
                raise ValueError('The source changed during review.')
            previous = db.execute('SELECT raw_sha256,render_sha256,renderer,reviewer,active '
                'FROM source_page_review WHERE workspace=? AND edition=? AND revision=? AND physical=?',
                (workspace, identity.edition_id, revision, locator.index)).fetchone()
            if previous is not None:
                if tuple(previous) != (raw_hash, fingerprint, renderer, reviewer, 1):
                    raise ValueError('A different or revoked review already exists; do not overwrite review history.')
                return
            db.execute('INSERT INTO source_page_review VALUES(?,?,?,?,?,?,?,?,1)',
                (workspace, identity.edition_id, revision, locator.index, raw_hash, fingerprint, renderer, reviewer))
            check()

    def revoke_pdf_review(self, workspace, edition, revision, physical, *,
                          cancelled=lambda: False, authorized_topic=None):
        if not callable(cancelled):
            raise ValueError('A review cancellation callback is required.')
        if cancelled():
            raise InterruptedError('Revocation cancelled; no change was saved.')
        with self.write(workspace) as db:
            if cancelled():
                raise InterruptedError('Revocation cancelled; no change was saved.')
            if authorized_topic is not None:
                TopicTree(db, workspace)._visible(authorized_topic)
            if db.execute('UPDATE source_page_review SET active=0 WHERE workspace=? AND edition=? '
                'AND revision=? AND physical=? AND active=1',
                (workspace, edition, revision, physical)).rowcount != 1:
                raise ValueError('No active review found for this page.')
            if cancelled():
                raise InterruptedError('Revocation cancelled; no change was saved.')

    def export_topic(self, workspace, topic, *, format='markdown', cancelled=lambda: False):
        with self.read(workspace) as db:
            load, metadata, _ = self._loaders(db, workspace, cancelled=cancelled)
            rows = SavedEvidenceStore(db, workspace, load, metadata).list_saved(topic, cancelled=cancelled)
            if format in ('docx', 'html'):
                name, = db.execute('SELECT name FROM research_topic WHERE workspace=? AND id=?',
                                   (workspace, topic)).fetchone()
                return export_saved(workspace, topic, rows, load, format=format, title=name)
            return export_saved(workspace, topic, rows, load, format=format)

    def export_outline(self, workspace, topic, *, cancelled=lambda: False):
        """Export this topic and its visible descendants with freshly verified evidence."""
        from .research.outline_export import export_outline
        with self.read(workspace) as db:
            load, metadata, _ = self._loaders(db, workspace, cancelled=cancelled)
            return export_outline(db, workspace, topic, load_page=load,
                                  attest_metadata=metadata, cancelled=cancelled)

    def save_query(self, workspace, topic, name, query, *, field='quote'):
        identity = uuid4().hex
        with self.evidence_transaction(workspace) as store:
            SavedTopicQueries(store).create(topic, identity, name, query, field=field)
        return identity

    def run_query(self, workspace, topic, identity, *, timeout=60.0, cancelled=lambda: False):
        with self._search_read(workspace, timeout, cancelled) as (db, budget):
            load, metadata, _ = self._loaders(db, workspace, cancelled=budget.stopped)
            return SavedTopicQueries(SavedEvidenceStore(db, workspace, load, metadata)).run(
                topic, identity, timeout=budget.remaining(), cancelled=budget.stopped)

    def backup(self, destination, *, include_generated=False, cancelled=lambda: False):
        from .library_backup import create_backup
        return create_backup(self, destination, include_generated=include_generated, cancelled=cancelled)
