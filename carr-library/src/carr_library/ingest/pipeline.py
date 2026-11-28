"""Resumable local ingestion candidate; one writer per private job journal.

Requires the pinned catalog, extraction and storage candidates. Originals are
never modified. Only native/blank PDF pages and strict UTF-8 TXT are eligible
for publication here. OCR candidates require a separate review workflow.
"""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import importlib.metadata
import json
import sqlite3
import tempfile

from carr_library.catalog.intake import IntakeError
from carr_library.catalog.streaming_pdf import (FileAsset, MAX_PDF_BYTES, original_stream,
                                               snapshot_original)
from carr_library.extract.page_ocr import Config, extract_page
from carr_library.storage.revisions import Page
from bootstrap.vendor.cairn_extractors.hasher import hash_file


class IngestError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


class Ingest:
    def __init__(self, journal, storage):
        self.storage = storage
        self.db = sqlite3.connect(journal, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS job(id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
          edition TEXT NOT NULL, asset TEXT NOT NULL, config TEXT NOT NULL,
          count INTEGER NOT NULL, expected_active TEXT, state TEXT NOT NULL,
          error TEXT);
        CREATE TABLE IF NOT EXISTS checkpoint(job TEXT NOT NULL REFERENCES job(id),
          ordinal INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
          PRIMARY KEY(job,ordinal));
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _active(self, workspace, edition):
        row = self.storage.db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                                      (workspace, edition)).fetchone()
        return row[0] if row else None

    def _job(self, workspace, job):
        row = self.db.execute('SELECT * FROM job WHERE workspace=? AND id=?', (workspace, job)).fetchone()
        if row is None:
            raise IngestError('unknown job in workspace')
        return row

    def inspect(self, workspace, job):
        row = self._job(workspace, job)
        count = self.db.execute('SELECT count(*) FROM checkpoint WHERE job=?', (job,)).fetchone()[0]
        return dict(id=job, workspace=workspace, edition=row['edition'], asset=row['asset'],
                    state=row['state'], completed=count, total=row['count'], error=row['error'])

    def _plan(self, asset, config, *, cancelled=lambda: False):
        if asset.mime == 'text/plain':
            # Upstream TXT extractor replaces invalid bytes and normalizes CRLF;
            # preserve exact decoded source instead, and do not invent pagination.
            segments = asset.data.decode('utf-8-sig', errors='strict').split('\f')
            if not any(t.strip() for t in segments):
                raise IngestError('empty text')
            count = len(segments)
            settings = dict(adapter='strict-utf8-v1', kind='logical_segment')
        elif asset.mime == 'application/pdf':
            from pypdf import PdfReader
            with original_stream(asset, cancelled=cancelled) as stream:
                reader = PdfReader(stream)
                if reader.is_encrypted:
                    raise IngestError('encrypted PDF')
                count = len(reader.pages)
            settings = dict(adapter='page_ocr-v1', kind='physical_page', config=asdict(config),
                            pypdf=importlib.metadata.version('pypdf'),
                            pdfium=importlib.metadata.version('pypdfium2'))
            segments = None
        else:
            raise IngestError('unsupported source format')
        if not 1 <= count <= 1000:
            raise IngestError('invalid segment count')
        return count, canonical(settings), segments

    def dry_run(self, root, relative, config=Config(), *, pdf_max_bytes=MAX_PDF_BYTES):
        asset = snapshot_original(root, relative, pdf_max_bytes=pdf_max_bytes)
        count, settings, _ = self._plan(asset, config)
        return dict(asset.manifest(), segments=count, extraction=json.loads(settings))

    def start(self, workspace, edition, root, relative, config=Config(), *,
              pdf_max_bytes=MAX_PDF_BYTES, cancelled=lambda: False):
        if not all(isinstance(v, str) and v for v in (workspace, edition)):
            raise IngestError('missing scope or edition')
        asset = snapshot_original(root, relative, pdf_max_bytes=pdf_max_bytes, cancelled=cancelled)
        count, settings, _ = (self._plan(asset, config, cancelled=cancelled)
                              if isinstance(asset, FileAsset) else self._plan(asset, config))
        job = digest(canonical([workspace, edition, asset.digest, settings]))
        with self.transaction():
            self.db.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                            (job, workspace, edition, asset.digest, settings, count,
                             self._active(workspace, edition), 'pending', None))
        return job

    def _set(self, job, state, error=None):
        self.db.execute('UPDATE job SET state=?, error=? WHERE id=?', (state, error, job))

    def _eligible(self, record):
        """Base publication policy; an application may supply authenticated review."""
        return (record['status'] in ('candidate', 'blank') and
                record['provenance'].get('method') in ('native', 'render_blank', 'strict_utf8'))

    def _validated(self, row):
        records = []
        for i, rec in enumerate(self.db.execute('SELECT * FROM checkpoint WHERE job=? ORDER BY ordinal',
                                                (row['id'],)), 1):
            if rec['ordinal'] != i or digest(rec['payload']) != rec['digest']:
                raise IngestError('checkpoint integrity failure')
            p = json.loads(rec['payload'])
            kind = json.loads(row['config'])['kind']
            if p['physical_index'] != (i if kind == 'physical_page' else None):
                raise IngestError('locator mismatch')
            if p['provenance']['asset_sha256'] != row['asset'] or p['printed_label'] is not None or p['printed_label_verified']:
                raise IngestError('checkpoint identity mismatch')
            if not isinstance(p['raw_text'], str) or '\x00' in p['raw_text']:
                raise IngestError('invalid raw text')
            records.append(p)
        if len(records) > row['count']:
            raise IngestError('checkpoint count mismatch')
        return records

    def _recover(self, row, records):
        # Cross-database crash window: storage committed, journal not yet committed.
        rev = self.storage.db.execute('SELECT * FROM revision WHERE id=?', (row['id'],)).fetchone()
        if rev is None:
            return False
        if (rev['workspace'], rev['edition'], rev['asset'], rev['config']) != (
                row['workspace'], row['edition'], row['asset'], row['config']):
            raise IngestError('published identity mismatch')
        if len(records) != row['count'] or self.storage.db.execute('SELECT 1 FROM sealed WHERE revision=?', (row['id'],)).fetchone() is None:
            raise IngestError('incomplete published revision')
        stored = self.storage.db.execute('SELECT * FROM page WHERE revision=? ORDER BY ordinal', (row['id'],)).fetchall()
        if len(stored) != len(records):
            raise IngestError('published count mismatch')
        for i, (p, r) in enumerate(zip(records, stored), 1):
            if (r['ordinal'], r['physical'], r['printed'], r['verified'], r['raw'], r['digest']) != (
                    i, p['physical_index'], None, 0, p['raw_text'], digest(p['raw_text'])):
                raise IngestError('published page mismatch')
        self.storage.check()
        self._set(row['id'], 'complete' if self._active(row['workspace'], row['edition']) == row['id'] else 'superseded')
        return True

    def retry(self, workspace, job):
        """Explicitly discard only the last unverified page, not prior checkpoints."""
        with self.transaction():
            row = self._job(workspace, job)
            records = self._validated(row)
            if row['state'] not in ('quarantined', 'failed', 'cancelled'):
                raise IngestError('job is not retryable')
            if self._recover(row, records):
                return
            if row['state'] == 'quarantined':
                self.db.execute('DELETE FROM checkpoint WHERE job=? AND ordinal=?', (job, len(records)))
            self._set(job, 'pending')

    def run(self, workspace, job, root, relative, config=Config(), *, max_steps=1001,
            cancelled=lambda: False, pdf_max_bytes=MAX_PDF_BYTES):
        """Run bounded sequential steps. Resume checks same input and configuration.

        max_steps includes the final publish step; checkpoint writes are durable
        before the next extraction. A single journal must mediate all writers.
        """
        if not 1 <= max_steps <= 1001:
            raise IngestError('invalid step limit')
        row = self._job(workspace, job)
        asset = snapshot_original(root, relative, pdf_max_bytes=pdf_max_bytes, cancelled=cancelled)
        count, settings, segments = (self._plan(asset, config, cancelled=cancelled)
                                     if isinstance(asset, FileAsset) else self._plan(asset, config))
        if (asset.digest, count, settings) != (row['asset'], row['count'], row['config']):
            raise IngestError('source or configuration changed')
        with tempfile.TemporaryDirectory(prefix='carr-ingest-') as tmp:
            staged = Path(tmp) / ('source.pdf' if segments is None else 'source.txt')
            if isinstance(asset, FileAsset):
                asset.copy_to(staged, cancelled=cancelled)
            else:
                staged.write_bytes(asset.data)
            staged_original = None
            if isinstance(asset, FileAsset):
                staged_original = FileAsset.capture(staged, cancelled=cancelled,
                                                    expected_manifest=asset.manifest())
            elif hash_file(staged) != asset.digest:
                raise IngestError('staging digest mismatch')
            for _ in range(max_steps):
                try:
                    with self.transaction():
                        row = self._job(workspace, job)
                        records = self._validated(row)
                        if self._recover(row, records):
                            break
                        if row['state'] != 'pending':
                            break
                        if cancelled():
                            self._set(job, 'cancelled')
                            break
                        if len(records) == count:
                            if not any(p['raw_text'].strip() for p in records):
                                self._set(job, 'failed', 'empty_source')
                                break
                            if any(not self._eligible(p) for p in records):
                                raise IngestError('unverified checkpoint')
                            if staged_original is not None:
                                with staged_original.open(cancelled=cancelled):
                                    pass
                            elif hash_file(staged) != asset.digest:
                                raise IngestError('staging digest mismatch')
                            if self._active(workspace, row['edition']) != row['expected_active']:
                                self._set(job, 'conflict', 'active_revision_changed')
                                break
                            self.storage.publish_revision(workspace, row['edition'], job, asset.digest, settings,
                                                          [Page(p['raw_text'], p['physical_index']) for p in records])
                            self.storage.check()
                            self._set(job, 'complete')
                            break
                        i = len(records) + 1
                        if segments is None:
                            p = (extract_page(staged_original, i, config, cancelled=cancelled)
                                 if staged_original is not None else extract_page(staged, i, config))
                        else:
                            p = dict(physical_index=None, printed_label=None, printed_label_verified=False,
                                     raw_text=segments[i-1], status='candidate', blocks=[], warnings=[],
                                     provenance=dict(asset_sha256=asset.digest, method='strict_utf8'))
                        payload = canonical(p)
                        if len(payload.encode('utf-8')) > 16 * 1024 * 1024:
                            raise IngestError('checkpoint resource limit')
                        self.db.execute('INSERT INTO checkpoint VALUES(?,?,?,?)', (job, i, payload, digest(payload)))
                        self._validated(row)
                        if not self._eligible(p):
                            self._set(job, 'quarantined', 'extraction_review_required')
                            break
                except Exception:
                    # Do not publish an exception string containing source paths.
                    # If the journal itself is full, fail visibly and leave its last
                    # durable checkpoint intact; the caller may retry after repair.
                    with self.transaction():
                        self._set(job, 'failed', 'step_failed')
                    raise
        return self.inspect(workspace, job)
