"""File-backed PDF planning/checkpoints using the existing journal and publisher."""
from contextlib import contextmanager
from hashlib import sha256
import importlib.metadata
import json
from pathlib import Path

from ..audio.jobs import checked_directory, filename_component
from ..extract.page_ocr import Config, _extract_page_document
from ..extract.runtime_identity import runtime_identity
from ..storage.revisions import Page
from .file_pdf import inspect_pdf, open_pdf
from .pipeline import IngestError, canonical, digest
from .source_profiles import PdfProfile, profile_from_value
from .streamed_text import check_cancelled
from .worker import RuntimeBoundIngest

MAX_RECORD_BYTES = 16 * 1024**2
_RECORD_KEYS = {'physical_index', 'printed_label', 'printed_label_verified',
                'raw_text', 'status', 'blocks', 'warnings', 'provenance'}


class FilePdfIngest(RuntimeBoundIngest):
    """No new job store. The original's profile is part of its revision identity."""
    def __init__(self, journal, storage, *, profile, reviews=None, review_asset=None):
        self.profile = profile_from_value(profile)
        if not isinstance(self.profile, PdfProfile):
            raise ValueError('File-backed PDF ingest requires the explicit PDF profile.')
        self._opened = None
        self._cancelled = lambda: False
        super().__init__(journal, storage, reviews=reviews, review_asset=review_asset)

    def _settings(self, config):
        return canonical(self.profile.configuration(config, runtime_identity(config),
            pypdf=importlib.metadata.version('pypdf'),
            pdfium=importlib.metadata.version('pypdfium2')))

    @contextmanager
    def _open(self, path, *, asset=None, cancelled=lambda: False):
        with open_pdf(path, self.profile, expected_digest=asset, cancelled=cancelled) as opened:
            before = self._opened
            self._opened = opened
            try:
                yield opened
            finally:
                self._opened = before

    def _path(self, root, relative):
        filename_component(relative)
        return checked_directory(root) / relative

    def start(self, workspace, edition, root, relative, config=Config(), *, cancelled=lambda: False):
        if not all(isinstance(value, str) and value for value in (workspace, edition)):
            raise IngestError('missing scope or edition')
        path = self._path(root, relative)
        with self._open(path, cancelled=cancelled) as (reader, document, snapshot):
            count, settings = len(document), self._settings(config)
        job = digest(canonical([workspace, edition, snapshot.digest, settings]))
        with self.transaction():
            self.db.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (job, workspace, edition, snapshot.digest, settings, count,
                 self._active(workspace, edition), 'pending', None))
            row = self._job(workspace, job)
            if (row['edition'], row['asset'], row['config'], row['count']) != (
                    edition, snapshot.digest, settings, count):
                raise IngestError('file-backed PDF journal identity changed')
        return job

    def _rows(self, job):
        return self.db.execute('SELECT ordinal,CASE WHEN length(CAST(payload AS BLOB))<=? '
            'THEN payload ELSE NULL END AS payload,digest FROM checkpoint '
            'WHERE job=? ORDER BY ordinal', (MAX_RECORD_BYTES, job))

    def _record(self, saved, row, ordinal):
        raw = saved['payload']
        if (saved['ordinal'] != ordinal or ordinal > row['count'] or not isinstance(raw, str)
                or len(raw.encode('utf-8')) > MAX_RECORD_BYTES or digest(raw) != saved['digest']):
            raise IngestError('file-backed PDF checkpoint integrity failure')
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != _RECORD_KEYS or canonical(value) != raw
                or type(value['physical_index']) is not int or value['physical_index'] != ordinal
                or value['printed_label'] is not None
                or value['printed_label_verified'] is not False
                or type(value['raw_text']) is not str or '\x00' in value['raw_text']
                or type(value['provenance']) is not dict
                or value['provenance'].get('asset_sha256') != row['asset']
                or type(value['blocks']) is not list or type(value['warnings']) is not list
                or value['status'] not in ('candidate', 'blank', 'review_required')):
            raise IngestError('invalid file-backed PDF checkpoint')
        return value

    def _records(self, row, *, eligible=False):
        total, count = 0, 0
        for count, saved in enumerate(self._rows(row['id']), 1):
            check_cancelled(self._cancelled)
            value = self._record(saved, row, count)
            total += len(saved['payload'].encode('utf-8'))
            if total > self.profile.maximum_checkpoint_bytes:
                raise IngestError('PDF checkpoint metadata exceeds its selected aggregate budget')
            if eligible and not self._eligible(value):
                raise IngestError('unverified file-backed PDF checkpoint')
            yield value

    def _pages(self, row):
        count = 0
        for count, value in enumerate(self._records(row), 1):
            yield Page(value['raw_text'], value['physical_index'])
        if count != row['count']:
            raise IngestError('file-backed PDF checkpoint count mismatch')

    def _published(self, row):
        found = self.storage.db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                                        (row['id'],)).fetchone()
        if found is None:
            return False
        if tuple(found) != (row['workspace'], row['edition'], row['asset'], row['config']):
            raise IngestError('published file-backed PDF identity mismatch')
        if self.storage.db.execute('SELECT 1 FROM sealed WHERE revision=?', (row['id'],)).fetchone() is None:
            raise IngestError('file-backed PDF publication is incomplete')
        actual = iter(self.storage.db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                         'FROM page WHERE revision=? ORDER BY ordinal', (row['id'],)))
        count = 0
        for count, value in enumerate(self._records(row, eligible=True), 1):
            saved = next(actual, None)
            if saved is None or tuple(saved) != (count, count, None, 0, value['raw_text'], digest(value['raw_text'])):
                raise IngestError('published file-backed PDF differs from its checkpoints')
        if count != row['count'] or next(actual, None) is not None:
            raise IngestError('published file-backed PDF count mismatch')
        self.storage.check()
        return True

    def _review_digest(self):
        if self._opened is not None:
            return self._opened[2].digest
        if not isinstance(self.review_asset, (str, Path)):
            return None
        return inspect_pdf(self.review_asset, self.profile, cancelled=self._cancelled).digest

    def _render_review(self, physical):
        from ..citations.pdf_review_attestation import render_document_fingerprint
        if self._opened is not None:
            return render_document_fingerprint(self._opened[1], physical)
        with self._open(self.review_asset, cancelled=self._cancelled) as (_, document, _):
            return render_document_fingerprint(document, physical)

    def retry(self, workspace, job):
        with self.transaction():
            row = self._job(workspace, job)
            if row['state'] not in ('quarantined', 'failed', 'cancelled'):
                raise IngestError('file-backed PDF job is not retryable')
            last, count = None, 0
            for count, last in enumerate(self._records(row), 1):
                pass
            if row['state'] == 'quarantined' and last is not None and not self._eligible(last):
                self.db.execute('DELETE FROM checkpoint WHERE job=? AND ordinal=?', (job, count))
            self._set(job, 'pending')

    def run(self, workspace, job, root, relative, config=Config(), *,
            max_steps=1001, cancelled=lambda: False):
        if type(max_steps) is not int or not 1 <= max_steps <= 1001:
            raise IngestError('invalid file-backed PDF step limit')
        row = self._job(workspace, job)
        self._cancelled = cancelled
        try:
            path = self._path(root, relative)
            with self._open(path, asset=row['asset'], cancelled=cancelled) as (reader, document, snapshot):
                settings = self._settings(config)
                if (len(document), settings) != (row['count'], row['config']):
                    raise IngestError('file-backed PDF source or runtime changed')
                recovered = self._published(row)
                if not recovered:
                    if row['state'] != 'pending':
                        return self.inspect(workspace, job)
                    existing, size, visible = 0, 0, False
                    for existing, value in enumerate(self._records(row, eligible=True), 1):
                        size += len(canonical(value).encode('utf-8'))
                        visible = visible or bool(value['raw_text'].strip())
                    performed = 0
                    for ordinal in range(existing + 1, row['count'] + 1):
                        check_cancelled(cancelled)
                        if performed >= max_steps:
                            return self.inspect(workspace, job)
                        value = _extract_page_document(reader, document, ordinal, config, snapshot.digest)
                        payload = canonical(value)
                        length = len(payload.encode('utf-8'))
                        if length > MAX_RECORD_BYTES or size + length > self.profile.maximum_checkpoint_bytes:
                            raise IngestError('PDF checkpoint resource limit')
                        if self._settings(config) != settings:
                            raise IngestError('PDF runtime changed during extraction')
                        with self.transaction():
                            self.db.execute('INSERT INTO checkpoint VALUES(?,?,?,?)',
                                            (job, ordinal, payload, digest(payload)))
                            if not self._eligible(value):
                                self._set(job, 'quarantined', 'extraction_review_required')
                        performed += 1
                        size += length
                        visible = visible or bool(value['raw_text'].strip())
                        if self._job(workspace, job)['state'] == 'quarantined':
                            return self.inspect(workspace, job)
                    if performed >= max_steps:
                        return self.inspect(workspace, job)
                    if not visible:
                        raise IngestError('empty file-backed PDF source')
                    if self._settings(config) != settings:
                        raise IngestError('PDF runtime changed before publication')
            # Both parsers and both original readers settle and validate before
            # publication. No cancellation check after the catalog commits.
            if recovered:
                with self.transaction():
                    self._set(job, 'complete' if self._active(workspace, row['edition']) == job else 'superseded')
                return self.inspect(workspace, job)
            check_cancelled(cancelled)
            if self._active(workspace, row['edition']) != row['expected_active']:
                with self.transaction():
                    self._set(job, 'conflict', 'active_revision_changed')
                return self.inspect(workspace, job)
            self.storage.publish_revision(workspace, row['edition'], job, row['asset'], row['config'],
                                          self._pages(row))
            with self.transaction():
                self._set(job, 'complete')
            return self.inspect(workspace, job)
        except InterruptedError:
            with self.transaction():
                self._set(job, 'cancelled')
            return self.inspect(workspace, job)
        except Exception:
            with self.transaction():
                self._set(job, 'failed', 'file_pdf_validation_failed')
            raise
