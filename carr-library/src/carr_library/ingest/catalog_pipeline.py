"""Durable import intent and atomic publication through the existing pipeline.

Original bytes are retained before extraction. Catalog, revision, active index
and historical metadata become visible in one shared database transaction.
The existing per-page job journal remains the source of checkpoint/retry state.
"""
from dataclasses import asdict
import json

from ..catalog.membership import Catalog
from ..catalog.registration import Registration, register_batch
from ..citations.anchors import Identity, Metadata, digest
from ..storage.attestations import MetadataSnapshots
from ..storage.workspace import WorkspaceStore
from .pipeline import Ingest, IngestError, canonical


def _registration(payload):
    try:
        data = json.loads(payload)
        data['authors'] = tuple(data['authors'])
        data['collections'] = tuple(data['collections'])
        record = Registration(**data)
        if canonical(asdict(record)) != payload:
            raise ValueError('noncanonical import intent')
        return record
    except (TypeError, KeyError, RecursionError) as exc:
        raise IngestError('invalid import intent') from exc


class CatalogPublisher:
    """The Ingest storage interface, composed with exact persisted metadata."""
    def __init__(self, storage, workspace, record):
        self.storage, self.workspace, self.record = storage, workspace, record
        self.db = storage.db

    def check(self):
        self.storage.check()

    def publish_revision(self, workspace, edition, revision, asset, config, pages):
        record = self.record
        if (workspace != self.workspace or edition != record.edition_id
                or asset != record.manifest['sha256']):
            raise IngestError('import intent differs from publication')
        with self.storage.transaction():
            WorkspaceStore(self.db).require_active(workspace)
            register_batch(Catalog(self.db, workspace), (record,))
            self.storage.stage_revision(workspace, edition, revision, asset, config, pages)
            metadata = Metadata('; '.join(record.authors) or None,
                                record.title or 'Unknown title', record.volume, record.edition)
            MetadataSnapshots(self.db, workspace).stage(
                Identity(workspace, edition, asset, revision), metadata)
            self.storage._check()


class CatalogIngest:
    """Call only while holding the library's existing cooperative writer guard."""
    def __init__(self, journal, storage, retained):
        self.storage, self.retained = storage, retained
        # The job journal is private staging state, not a second source catalog.
        self.ingest = Ingest(journal, storage)
        self.ingest.db.executescript('''
            CREATE TABLE IF NOT EXISTS application_import_intent(
                job TEXT PRIMARY KEY REFERENCES job(id),
                payload TEXT NOT NULL, checksum TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS application_import_intent_immutable
            BEFORE UPDATE ON application_import_intent
            BEGIN SELECT RAISE(ABORT,'immutable import intent'); END;
        ''')

    def close(self):
        self.ingest.close()

    def start(self, workspace, record, retained_path):
        if not isinstance(record, Registration):
            raise IngestError('explicit registration required')
        with self.storage.transaction():
            WorkspaceStore(self.storage.db).require_active(workspace)
        job = self.ingest.start(workspace, record.edition_id,
                                self.retained.root, retained_path.name)
        payload = canonical(asdict(record))
        with self.ingest.transaction():
            row = self.ingest.db.execute(
                'SELECT payload,checksum FROM application_import_intent WHERE job=?', (job,)).fetchone()
            if row is None:
                self.ingest.db.execute('INSERT INTO application_import_intent VALUES(?,?,?)',
                                        (job, payload, digest(payload)))
            elif tuple(row) != (payload, digest(payload)):
                raise IngestError('import identity conflicts with original intent')
        return job

    def run(self, workspace, job, *, cancelled=lambda: False, max_steps=1001, retry=False):
        state = self.ingest.inspect(workspace, job)
        row = self.ingest.db.execute(
            'SELECT payload,checksum FROM application_import_intent WHERE job=?', (job,)).fetchone()
        if row is None or digest(row[0]) != row[1]:
            raise IngestError('missing or changed import intent')
        record = _registration(row[0])
        if (record.edition_id != state['edition']
                or record.manifest['sha256'] != state['asset']):
            raise IngestError('journal scope differs from import intent')
        with self.storage.transaction():
            WorkspaceStore(self.storage.db).require_active(workspace)
        self.ingest.storage = CatalogPublisher(self.storage, workspace, record)
        if retry:
            self.ingest.retry(workspace, job)
        return self.retained.run(self.ingest, workspace, job,
                                 cancelled=cancelled, max_steps=max_steps)
