"""Resumable local ingestion candidate; one writer per private job journal.

Requires the pinned catalog, extraction and storage candidates. Originals are
never modified. Only native/blank PDF pages and strict UTF-8 TXT are eligible
for publication here. OCR candidates require a separate review workflow.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
import importlib.metadata
import json
import sqlite3

from carr_library.catalog.intake import (source_snapshot as snapshot, IntakeError,
    FileSnapshot, TextSegments, source_stream, copy_snapshot, _selected, _stamp)
from .resource_budget import effective_budget, MAX_CHECKPOINT_BYTES
from carr_library.extract.page_ocr import Config, extract_page
from carr_library.extract.document_formats import MIME_SUFFIXES, RASTER_MIMES, RASTER_BY_MIME
from carr_library.storage.revisions import Page
from ..audio.cleanup import retained_temporary_directory
from ..audio.models import AudioCleanupPending


class IngestError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


class _CheckpointRecords:
    """A checked journal view; extracted text is read one record at a time."""
    def __init__(self, ingest, row, count, nonempty, token):
        self.ingest, self.row = ingest, dict(row)
        self.count, self.nonempty, self.token = count, nonempty, token

    def _same(self):
        if self.ingest._journal_token(self.row) != self.token:
            raise IngestError('checkpoint journal changed during validation')

    def __len__(self):
        self._same()
        return self.count

    def __iter__(self):
        self._same()
        count = 0
        for count, record in enumerate(self.ingest._iter_records(self.row), 1):
            yield record
        self._same()
        if count != self.count:
            raise IngestError('checkpoint count changed during validation')

    def __getitem__(self, index):
        self._same()
        if type(index) is not int:
            raise TypeError('Use one exact checkpoint ordinal.')
        index = index + self.count if index < 0 else index
        if not 0 <= index < self.count:
            raise IndexError(index)
        row = self.ingest.db.execute('SELECT * FROM checkpoint WHERE job=? AND ordinal=?',
                                      (self.row['id'], index + 1)).fetchone()
        if row is None:
            raise IngestError('checkpoint count changed during validation')
        result = self.ingest._record(self.row, index + 1, row)
        self._same()
        return result


class Ingest:
    def __init__(self, journal, storage):
        self.storage = storage
        self._budget = effective_budget()
        self._cancelled = lambda: False
        self._checkpoint_cache = None
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

    def _configure(self, import_budget, cancelled):
        if not callable(cancelled):
            raise TypeError('A current cancellation callback is required.')
        self._budget = effective_budget(import_budget)
        self._cancelled = cancelled
        self._checkpoint_cache = None

    def _plan(self, asset, config):
        if asset.mime == 'text/plain':
            # Original form-feed boundaries and exact UTF-8/CRLF bytes remain
            # the strict-utf8-v1 interpretation; no new artificial pages.
            try:
                segments = TextSegments(asset, cancelled=self._cancelled,
                    max_segments=self._budget.segments, max_segment_bytes=self._budget.segment_bytes)
            except IntakeError as exc:
                raise IngestError(str(exc)) from exc
            count = len(segments)
            settings = dict(adapter='strict-utf8-v1', kind='logical_segment')
        elif asset.mime == 'application/pdf':
            from pypdf import PdfReader
            with source_stream(asset, cancelled=self._cancelled) as stream:
                reader = PdfReader(stream)
                if reader.is_encrypted:
                    raise IngestError('encrypted PDF')
                count = len(reader.pages)
            settings = dict(adapter='page_ocr-v1', kind='physical_page', config=asdict(config),
                            pypdf=importlib.metadata.version('pypdf'),
                            pdfium=importlib.metadata.version('pypdfium2'))
            segments = None
        elif asset.mime in RASTER_MIMES:
            from carr_library.extract.raster_pages import frame_inventory, inventory_sha256, NORMALIZATION
            frames = frame_inventory(asset.data, config, cancelled=self._cancelled)
            count = len(frames)
            settings = dict(adapter='raster_ocr-v1', kind='physical_page',
                config=asdict(config), format=RASTER_BY_MIME[asset.mime].name,
                pillow=importlib.metadata.version('Pillow'), normalization=NORMALIZATION,
                frame_inventory_sha256=inventory_sha256(frames))
            segments = None
        else:
            raise IngestError('unsupported source format')
        if not 1 <= count <= self._budget.segments:
            raise IngestError('invalid segment count for the selected budget')
        return count, canonical(settings), segments

    def dry_run(self, root, relative, config=Config(), *, import_budget=None,
                cancelled=lambda: False):
        self._configure(import_budget, cancelled)
        asset = snapshot(root, relative, max_bytes=self._budget.source_bytes,
                         cancelled=cancelled, file_backed=import_budget is not None)
        count, settings, _ = self._plan(asset, config)
        return dict(asset.manifest(), segments=count, extraction=json.loads(settings))

    def start(self, workspace, edition, root, relative, config=Config(), *,
              import_budget=None, cancelled=lambda: False):
        if not all(isinstance(v, str) and v for v in (workspace, edition)):
            raise IngestError('missing scope or edition')
        self._configure(import_budget, cancelled)
        asset = snapshot(root, relative, max_bytes=self._budget.source_bytes,
                         cancelled=cancelled, file_backed=import_budget is not None)
        count, settings, _ = self._plan(asset, config)
        # Resource admission does not rewrite the historical extraction config
        # or source revision identity. The request freezes its separate budget.
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

    def _journal_token(self, row):
        return (id(self.db), self.db.total_changes,
                self.db.execute('PRAGMA data_version').fetchone()[0],
                self.db.execute('PRAGMA schema_version').fetchone()[0],
                tuple(row[name] for name in ('id', 'workspace', 'edition', 'asset',
                      'config', 'count', 'expected_active', 'state', 'error')))

    def _record(self, row, ordinal, rec):
        if (rec['ordinal'] != ordinal or len(rec['payload'].encode('utf-8')) > MAX_CHECKPOINT_BYTES
                or digest(rec['payload']) != rec['digest']):
            raise IngestError('checkpoint integrity failure')
        try:
            p = json.loads(rec['payload'])
            kind = json.loads(row['config'])['kind']
            if p['physical_index'] != (ordinal if kind == 'physical_page' else None):
                raise IngestError('locator mismatch')
            if (p['provenance']['asset_sha256'] != row['asset']
                    or p['printed_label'] is not None or p['printed_label_verified']):
                raise IngestError('checkpoint identity mismatch')
            if not isinstance(p['raw_text'], str) or '\x00' in p['raw_text']:
                raise IngestError('invalid raw text')
            settings = json.loads(row['config'])
            if settings.get('adapter') == 'raster_ocr-v1':
                from carr_library.extract.raster_pages import validate_checkpoint
                validate_checkpoint(p, settings, ordinal, row['count'])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise IngestError('invalid checkpoint shape') from exc
        return p

    def _iter_records(self, row):
        count = 0
        for count, rec in enumerate(self.db.execute(
                'SELECT * FROM checkpoint WHERE job=? ORDER BY ordinal', (row['id'],)), 1):
            if self._cancelled():
                raise InterruptedError('Checkpoint verification cancelled.')
            if count > row['count']:
                raise IngestError('checkpoint count mismatch')
            yield self._record(row, count, rec)

    def _validated(self, row):
        if type(row['count']) is not int or not 0 < row['count'] <= self._budget.segments:
            raise IngestError('invalid journal segment count')
        token = self._journal_token(row)
        if self._checkpoint_cache is not None and self._checkpoint_cache[0] == token:
            _, count, nonempty = self._checkpoint_cache
        else:
            count, nonempty = 0, False
            for count, p in enumerate(self._iter_records(row), 1):
                nonempty = nonempty or bool(p['raw_text'].strip())
            if self._journal_token(row) != token:
                raise IngestError('checkpoint journal changed during validation')
            self._checkpoint_cache = (token, count, nonempty)
        return _CheckpointRecords(self, row, count, nonempty, token)

    def _accept_checkpoint(self, row, previous, payload):
        """Reuse validation only across this exact single INSERT, never unknown writes."""
        before = previous.token
        after = self._journal_token(row)
        expected = (before[0], before[1] + 1, *before[2:])
        if after == expected:
            self._checkpoint_cache = (after, previous.count + 1,
                                      previous.nonempty or bool(payload['raw_text'].strip()))
        else:
            self._checkpoint_cache = None

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
        stored = self.storage.db.execute('SELECT * FROM page WHERE revision=? ORDER BY ordinal', (row['id'],))
        for i, (p, r) in enumerate(zip_longest(records, stored), 1):
            if p is None or r is None:
                raise IngestError('published count mismatch')
            if (r['ordinal'], r['physical'], r['printed'], r['verified'], r['raw'], r['digest']) != (
                    i, p['physical_index'], None, 0, p['raw_text'], digest(p['raw_text'])):
                raise IngestError('published page mismatch')
        self.storage.check()
        self._set(row['id'], 'complete' if self._active(row['workspace'], row['edition']) == row['id'] else 'superseded')
        return True

    def retry(self, workspace, job, *, import_budget=None, cancelled=lambda: False):
        """Explicitly discard only the last unverified page, not prior checkpoints."""
        self._configure(import_budget, cancelled)
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

    def run(self, workspace, job, root, relative, config=Config(), *, max_steps=None,
            cancelled=lambda: False, import_budget=None, check_resources=lambda: None):
        """Run one bounded slice; complete publication remains independently checked."""
        try:
            return self._run(workspace, job, root, relative, config, max_steps=max_steps,
                cancelled=cancelled, import_budget=import_budget, check_resources=check_resources)
        except InterruptedError:
            if not cancelled():
                raise
            with self.transaction():
                row = self._job(workspace, job)
                if self.storage.db.execute('SELECT 1 FROM revision WHERE id=?', (job,)).fetchone():
                    # The outer captured PublicationAttempt reconciles a late
                    # interruption; a completed revision is never relabeled here.
                    raise
                if row['state'] in ('pending', 'failed'):
                    self._set(job, 'cancelled')
            return self.inspect(workspace, job)

    def _run(self, workspace, job, root, relative, config=Config(), *, max_steps=None,
             cancelled=lambda: False, import_budget=None, check_resources=lambda: None):
        self._configure(import_budget, cancelled)
        steps = self._budget.steps if max_steps is None else max_steps
        if type(steps) is not int or not 1 <= steps <= self._budget.steps:
            raise IngestError('invalid step limit')
        if not callable(check_resources):
            raise TypeError('A current resource check is required.')
        check_resources()
        row = self._job(workspace, job)
        asset = snapshot(root, relative, max_bytes=self._budget.source_bytes,
                         cancelled=cancelled, file_backed=import_budget is not None)
        count, settings, segments = self._plan(asset, config)
        if (asset.digest, count, settings) != (row['asset'], row['count'], row['config']):
            raise IngestError('source or configuration changed')
        with retained_temporary_directory(prefix='carr-ingest-', dir=None) as tmp:
            staged = Path(tmp) / ('source' + MIME_SUFFIXES[asset.mime])
            copy_snapshot(asset, staged, cancelled=cancelled)
            path, info = _selected(staged.parent, staged.name)
            staged_asset = FileSnapshot(asset.digest, asset.mime, asset.size, path, _stamp(info))
            staged_asset.verify(cancelled=cancelled)
            if isinstance(segments, TextSegments):
                segments.rebind(staged_asset)
            with (segments.reader() if segments is not None else nullcontext(None)) as read_segment:
                for _ in range(steps):
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
                            check_resources()
                            if len(records) == count:
                                # Force an independent complete journal pass at the
                                # publication boundary, even after our own inserts.
                                self._checkpoint_cache = None
                                records = self._validated(row)
                                if not records.nonempty:
                                    self._set(job, 'failed', 'empty_source')
                                    break
                                for p in records:
                                    if not self._eligible(p):
                                        raise IngestError('unverified checkpoint')
                                staged_asset.verify(cancelled=cancelled)
                                if isinstance(asset, FileSnapshot):
                                    asset.verify(cancelled=cancelled)
                                check_resources()
                                if self._active(workspace, row['edition']) != row['expected_active']:
                                    self._set(job, 'conflict', 'active_revision_changed')
                                    break
                                def checked_pages():
                                    for p in records:
                                        check_resources()
                                        yield Page(p['raw_text'], p['physical_index'])
                                        check_resources()
                                self.storage.publish_revision(workspace, row['edition'], job, asset.digest, settings,
                                    checked_pages())
                                self.storage.check()
                                self._set(job, 'complete')
                                break
                            i = len(records) + 1
                            if asset.mime in RASTER_MIMES:
                                from carr_library.extract.raster_pages import extract_raster_page
                                p = extract_raster_page(staged, i, config, cancelled=cancelled)
                            elif segments is None:
                                if import_budget is None:
                                    p = extract_page(staged, i, config)
                                else:
                                    p = extract_page(staged, i, config, original=staged_asset,
                                        max_pages=self._budget.segments, cancelled=cancelled)
                            else:
                                p = dict(physical_index=None, printed_label=None, printed_label_verified=False,
                                         raw_text=read_segment(i-1), status='candidate', blocks=[], warnings=[],
                                         provenance=dict(asset_sha256=asset.digest, method='strict_utf8'))
                            # Extraction/custom hooks cannot silently invalidate the
                            # checked predecessor view on the same connection.
                            records._same()
                            payload = canonical(p)
                            if len(payload.encode('utf-8')) > MAX_CHECKPOINT_BYTES:
                                raise IngestError('checkpoint resource limit')
                            self._record(row, i, dict(ordinal=i, payload=payload, digest=digest(payload)))
                            self.db.execute('INSERT INTO checkpoint VALUES(?,?,?,?)',
                                            (job, i, payload, digest(payload)))
                            self._accept_checkpoint(row, records, p)
                            # RuntimeBoundIngest still checks its captured native
                            # runtime after every extraction inside this transaction.
                            self._validated(row)
                            check_resources()
                            if not self._eligible(p):
                                self._set(job, 'quarantined', 'extraction_review_required')
                                break
                        check_resources()
                    except AudioCleanupPending:
                        # Retain the same temporary directory with its existing
                        # native cleanup owner; never convert uncertainty to failure.
                        raise
                    except Exception:
                        with self.transaction():
                            self._set(job, 'failed', 'step_failed')
                        raise
        return self.inspect(workspace, job)
