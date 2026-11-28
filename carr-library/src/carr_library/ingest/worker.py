"""Bounded native-parser subprocess and durable private import requests.

This is process isolation with time/resource limits, not an OS security sandbox.
The existing ingest journal remains the only extraction/checkpoint pipeline.
"""
from contextlib import ExitStack, closing, contextmanager, nullcontext, redirect_stdout
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile, gettempdir
import time
from uuid import uuid4

from .pipeline import Ingest, canonical
from ..extract.document_formats import MIME_SUFFIXES, PHYSICAL_MIMES, raster_format
from .resource_budget import ImportBudget, checked_budget, effective_budget, source_limit
from ..audio.models import AudioCleanupPending

MAX_REQUEST = 64_000
MAX_REPLY = 64_000
MAX_LOG = 1_000_000
MAX_REVIEWS = 1_000_000
FIELDS = {'title', 'authors', 'language', 'edition', 'volume', 'collections', 'work_id', 'edition_id'}


def _directory(root):
    path = Path(root).resolve() / 'import-requests'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Import requests require a local private directory.')
    return path


def _path(root, request_id):
    if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{32}', request_id):
        raise ValueError('Invalid import request identity.')
    return _directory(root) / (request_id + '.json')


def load_request(root, request_id):
    path = _path(root, request_id)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_REQUEST
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Invalid import request file.')
    from ..durable_state import read_json
    record = read_json(path, maximum=MAX_REQUEST)
    if (not isinstance(record, dict) or type(record.get('version')) is not int
            or record['version'] not in (1, 2)
            or record.get('request_id') != request_id
            or record.get('data_dir') != str(Path(root).resolve())):
        raise ValueError('Import request identity changed.')
    _budget_from_record(record)
    return record



def _budget_from_record(record):
    if record.get('version') == 1:
        if 'import_budget' in record:
            raise ValueError('A legacy import cannot acquire an unreviewed resource budget.')
        return None
    if record.get('version') == 2:
        return ImportBudget.from_dict(record.get('import_budget'))
    raise ValueError('Unsupported import request version.')

def _store(root, request_id, record, *, create=False):
    path = _path(root, request_id)
    raw = (canonical(record) + '\n').encode('utf-8')
    if len(raw) > MAX_REQUEST:
        raise ValueError('Import request metadata is too large.')
    if create and os.path.lexists(path):
        raise FileExistsError('Import request identity already exists.')
    temporary = path.with_name(request_id + '-' + uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def record_progress(root, request_id, **changes):
    if request_id is None:
        return
    record = load_request(root, request_id)
    record.update(changes)
    _store(root, request_id, record)


def _request_identity(record):
    """Stable request identity; progress and publication candidates are not authority."""
    result = {key: record.get(key) for key in (
        'version', 'request_id', 'data_dir', 'workspace', 'source',
        'metadata', 'config', 'expected_manifest')}
    budget = _budget_from_record(record)
    if budget is not None:
        result['import_budget'] = budget.to_dict()
    return result


def _publication_state(db, record, revision, *, allow_superseded=False):
    """Inspect this exact sealed revision and registration in the caller's snapshot.

    The digest includes original source identity, requested catalog metadata and
    every exact stored page. Read rows incrementally, without copying a large
    extracted document into the request file or a second in-memory collection.
    """
    from ..application import LibraryApp
    from ..catalog.membership import Catalog

    workspace, metadata = record['workspace'], record['metadata']
    LibraryApp._authorize(db, workspace)
    row = db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                     (revision,)).fetchone()
    if row is None:
        return None
    active = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                        (workspace, metadata['edition_id'])).fetchone()
    historical = active is not None and active[0] != revision and allow_superseded
    if (tuple(row[:3]) != (workspace, metadata['edition_id'], record.get('asset_sha256'))
            or not db.execute('SELECT 1 FROM sealed WHERE revision=?', (revision,)).fetchone()
            or active is None or (active[0] != revision and not historical)):
        raise ValueError('Import publication identity changed.')
    catalog = db.execute(
        'SELECT e.work,e.asset,a.mime,a.size,e.language,e.edition,e.volume,w.title '
        'FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset '
        'JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work '
        'WHERE e.workspace=? AND e.id=?', (workspace, metadata['edition_id'])).fetchone()
    if catalog is None:
        raise ValueError('Import publication registration is missing.')
    manifest = dict(sha256=catalog[1], mime=catalog[2], size=catalog[3])
    if manifest['mime'] in PHYSICAL_MIMES and record.get('job') != revision:
        kind = 'PDF' if manifest['mime'] == 'application/pdf' else 'image'
        raise ValueError(f'Import publication differs from its saved {kind} job.')
    if (tuple(catalog[:2]) != (metadata['work_id'], record.get('asset_sha256'))
            or (record.get('expected_manifest') is not None
                and record['expected_manifest'] != manifest)):
        raise ValueError('Import publication metadata changed.')
    expected_catalog = (metadata['language'], metadata['edition'],
                        metadata['volume'], metadata['title'])
    if historical:
        # Rebuild only the original registration header from this request.
        # Equality to the immutable canonical SHA below must still prove it;
        # current metadata is never presented as the historical source's own.
        catalog = (*catalog[:4], *expected_catalog)
        authors = tuple(metadata['authors'])
    else:
        if tuple(catalog[4:]) != expected_catalog:
            raise ValueError('Import publication metadata changed.')
        authors = tuple(r[0] for r in db.execute(
            'SELECT name FROM catalog_author WHERE workspace=? AND work=? ORDER BY position',
            (workspace, metadata['work_id'])))
        if authors != tuple(metadata['authors']):
            raise ValueError('Import publication authors changed.')
        scoped = Catalog(db, workspace)
        for collection in metadata['collections']:
            scoped._active_collection(collection)
            if not db.execute('SELECT 1 FROM catalog_membership '
                              'WHERE workspace=? AND collection=? AND edition=?',
                              (workspace, collection, metadata['edition_id'])).fetchone():
                raise ValueError('Import publication membership is missing.')
    source = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                        'WHERE workspace=? AND edition=? AND revision=?',
                        (workspace, metadata['edition_id'], revision)).fetchone()
    if source is None or tuple(source) != (
            '; '.join(authors) or None, metadata['title'] or 'Unknown title',
            metadata['volume'], metadata['edition']):
        raise ValueError('Import publication source metadata changed.')
    digest = sha256(canonical([_request_identity(record), list(row), list(catalog),
                               list(source), authors]).encode('utf-8'))
    completed = 0
    for page in db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                           'FROM page WHERE revision=? ORDER BY ordinal', (revision,)):
        completed += 1
        # Extraction already enforces its format-specific capacity. This
        # streaming verifier must not impose the PDF page limit on DOCX records.
        if (page[0] != completed
                or sha256(page[4].encode('utf-8')).hexdigest() != page[5]):
            raise ValueError('Import publication page integrity changed.')
        digest.update(b'\n')
        digest.update(canonical(list(page)).encode('utf-8'))
    if completed == 0:
        raise ValueError('Import publication contains no source pages.')
    return dict(sha256=digest.hexdigest(), completed=completed, manifest=manifest)


def record_publication_candidate(root, request_id, db, workspace, revision, *,
                                 publication_attempt=None, config=None):
    """Bind this attempt inside the existing publication transaction.

    The sidecar is only a hint. The immutable database receipt commits with the
    exact source; it distinguishes another request publishing the same
    deterministic revision after this transaction rolls back.
    """
    if request_id is None:
        return
    record = load_request(root, request_id)
    from .publication_identity import PublicationAttempt
    if publication_attempt is None and record.get('attempt') is None:
        return  # An unsupervised direct import creates no attempt receipt.
    if not isinstance(publication_attempt, PublicationAttempt) or not db.in_transaction:
        raise ValueError('A frozen supervising publication attempt is required.')
    expected = publication_attempt.request()
    attempt = publication_attempt.attempt_id
    if (record.get('attempt') != attempt or expected['request_id'] != request_id
            or expected['workspace'] != workspace
            or any(record.get(key) != expected[key] for key in
                   ('version', 'request_id', 'data_dir', 'source', 'workspace',
                    'metadata', 'config', 'expected_manifest'))
            or record.get('import_budget') != expected.get('import_budget')
            or config is None or asdict(config) != expected['config']
            or expected['pinned_asset'] not in (None, record.get('asset_sha256'))
            or expected['resume_job'] not in (None, revision)):
        raise ValueError('Import publication attempt changed.')
    state = _publication_state(db, record, revision)
    if state is None:
        raise ValueError('Import publication candidate is missing.')
    db.execute('INSERT INTO import_publication(revision,request_id,attempt,sha256) '
               'VALUES(?,?,?,?)', (revision, request_id, attempt, state['sha256']))
    record_progress(root, request_id, publication_candidate=dict(
        attempt=attempt, revision=revision, **state))


def _reconcile_publication(root, request_id, attempt, *, initial=None, _context=None):
    """Recover only a fresh publication from this stopped child, without replay."""
    record = load_request(root, request_id)
    if initial is not None and _request_identity(record) != _request_identity(initial):
        raise ValueError('Import request changed after this worker launch.')
    candidate = record.get('publication_candidate')
    if candidate is None:
        return None
    if (record.get('attempt') != attempt or not isinstance(candidate, dict)
            or set(candidate) != {'attempt', 'revision', 'sha256', 'completed', 'manifest'}
            or candidate.get('attempt') != attempt
            or not isinstance(candidate.get('revision'), str)
            or not re.fullmatch('[0-9a-f]{64}', candidate['revision'])
            or not isinstance(candidate.get('sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', candidate['sha256'])
            or type(candidate.get('completed')) is not int or candidate['completed'] < 1
            or not isinstance(candidate.get('manifest'), dict)):
        raise ValueError('Import publication candidate changed.')
    from ..application import LibraryApp
    from ..citations.anchors import Identity

    if _context is None:
        app = LibraryApp(root)
        snapshot = app.read(record['workspace'])
    else:
        from .reconciliation import ReconciliationContext
        if type(_context) is not ReconciliationContext:
            raise TypeError('Use an exact post-worker reconciliation context.')
        app, db = _context.check(root, record['workspace'])
        snapshot = nullcontext(db)
    with snapshot as db:
        receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication '
                             'WHERE revision=?', (candidate['revision'],)).fetchone()
        if receipt is None or tuple(receipt[:2]) != (request_id, attempt):
            # An intact equal revision may have been committed by a competitor
            # after this child rolled back. Its receipt cannot complete us.
            return None
        if receipt['sha256'] != candidate['sha256']:
            raise ValueError('Import publication receipt differs from its candidate.')
        state = _publication_state(db, record, candidate['revision'], allow_superseded=True)
        if state is None:  # The child died before its SQLite transaction committed.
            return None
        if state != {key: candidate[key] for key in ('sha256', 'completed', 'manifest')}:
            raise ValueError('Committed import differs from its publication candidate.')
        identity = Identity(record['workspace'], record['metadata']['edition_id'],
                            state['manifest']['sha256'], candidate['revision'])
        app._retained_source(db, record['workspace'], identity)
        active = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                            (record['workspace'], record['metadata']['edition_id'])).fetchone()
        current = active is not None and active[0] == candidate['revision']
        if _context is not None:
            _context.check(root, record['workspace'])
    return dict(state='complete' if current else 'superseded',
                id=candidate['revision'], workspace=record['workspace'],
                edition=record['metadata']['edition_id'], asset=state['manifest']['sha256'],
                completed=state['completed'], total=state['completed'], error=None,
                verified=current and state['manifest']['mime'] == 'text/plain',
                publication_recovered=True, recovered_publication=True,
                message='Import completed before the worker stopped; the committed source was verified.')


class RuntimeBoundIngest(Ingest):
    """Bind PDF journal checkpoints to the existing extraction fingerprint."""
    def __init__(self, journal, storage, *, reviews=None, review_asset=None):
        super().__init__(journal, storage)
        self.reviews = reviews or {}
        self.review_asset = review_asset
        self.validated_reviews = {}

    def _plan(self, asset, config):
        count, settings, segments = super()._plan(asset, config)
        if asset.mime in PHYSICAL_MIMES:
            from ..extract.runtime_identity import runtime_identity
            data = json.loads(settings)
            data['runtime_identity'] = runtime_identity(config)
            settings = canonical(data)
        return count, settings, segments

    def _validated(self, row):
        settings = json.loads(row['config'])
        if settings.get('kind') == 'physical_page':
            from ..extract.page_ocr import config_from_dict
            from ..extract.runtime_identity import runtime_identity
            if settings.get('runtime_identity') != runtime_identity(config_from_dict(settings['config'])):
                raise ValueError('Extraction runtime changed; this import cannot resume with mixed results.')
        # Existing run calls this after extraction inside its checkpoint write
        # transaction. A changed runtime rolls that checkpoint back atomically.
        return super()._validated(row)

    def _eligible(self, record):
        if super()._eligible(record):
            return True
        physical = record.get('physical_index')
        review = self.reviews.get(str(physical))
        if review is None:
            return False
        from ..catalog.intake import FileSnapshot
        if isinstance(self.review_asset, FileSnapshot):
            if self.review_asset.mime != 'application/pdf':
                raise ValueError('Reviewed checkpoint requires its exact PDF original.')
            self.review_asset.verify(cancelled=self._cancelled)
            reviewed_digest = self.review_asset.digest
        elif isinstance(self.review_asset, bytes):
            reviewed_digest = sha256(self.review_asset).hexdigest()
        else:
            reviewed_digest = None
        if (record.get('status') != 'review_required' or not record.get('raw_text', '').strip()
                or type(physical) is not int or reviewed_digest is None
                or not isinstance(review, dict)
                or review.get('physical') != physical
                or review.get('asset_sha256') != record['provenance'].get('asset_sha256')
                or review.get('asset_sha256') != reviewed_digest
                or review.get('raw_sha256') != sha256(record['raw_text'].encode('utf-8')).hexdigest()
                or review.get('checkpoint_sha256') != sha256(canonical(record).encode('utf-8')).hexdigest()
                or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
            raise ValueError('Reviewed checkpoint identity differs from the extracted source.')
        if physical not in self.validated_reviews:
            from ..citations.pdf_review_attestation import render_fingerprint
            renderer, fingerprint = (render_fingerprint(self.review_asset, physical, cancelled=self._cancelled)
                                     if isinstance(self.review_asset, FileSnapshot)
                                     else render_fingerprint(self.review_asset, physical))
            if review.get('renderer') != renderer or review.get('render_sha256') != fingerprint:
                raise ValueError('Reviewed PDF page rendering changed.')
            self.validated_reviews[physical] = dict(review)
        return True

    def retry(self, workspace, job, *, import_budget=None, cancelled=lambda: False):
        self._configure(import_budget, cancelled)
        # Approved quarantine keeps the exact original checkpoint; ordinary
        # retry continues to discard/re-extract the last unapproved candidate.
        with self.transaction():
            row = self._job(workspace, job)
            records = self._validated(row)
            if row['state'] == 'quarantined' and records and self._eligible(records[-1]):
                if not self._recover(row, records):
                    self._set(job, 'pending')
                return
        return super().retry(workspace, job, import_budget=import_budget, cancelled=cancelled)


@contextmanager
def request_lock(root, request_id):
    from filelock import FileLock, Timeout
    lock_path = _path(root, request_id).with_suffix('.lock')
    if os.path.lexists(lock_path):
        info = lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Invalid import request lock.')
    lock = FileLock(lock_path, mode=0o600, thread_local=False)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise ValueError('This import request is already running.') from exc
    retained = False
    try:
        yield
    except AudioCleanupPending as exc:
        retained = True
        exc.cleanup_owner.add_quiescent_callback(lock.release)
        raise
    finally:
        if not retained:
            lock.release()


def _review_path(root, request_id):
    directory = _path(root, request_id).parent / 'reviews'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Invalid import review directory.')
    return directory / (request_id + '.json')


def load_reviews(root, request_id):
    if request_id is None:
        return {}
    path = _review_path(root, request_id)
    if not os.path.lexists(path):
        return {}
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_REVIEWS
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Invalid import review file.')
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict) or len(result) > 1000:
        raise ValueError('Invalid import review records.')
    return result


def _pending_review(root, workspace, request_id):
    request = load_request(root, request_id)
    if request.get('workspace') != workspace:
        raise ValueError('Import request belongs to another library.')
    if not request.get('job'):
        return None
    journal = (Path(root).resolve() / 'imports.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(journal, uri=True, isolation_level=None)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        job = db.execute('SELECT * FROM job WHERE id=? AND workspace=?',
                         (request['job'], workspace)).fetchone()
        if job is None or job['edition'] != request['metadata']['edition_id']:
            raise ValueError('Import review job identity changed.')
        if job['state'] != 'quarantined':
            return None
        row = db.execute('SELECT ordinal,payload,digest FROM checkpoint WHERE job=? '
                         'ORDER BY ordinal DESC LIMIT 1', (job['id'],)).fetchone()
        if row is None or sha256(row['payload'].encode('utf-8')).hexdigest() != row['digest']:
            raise ValueError('Import review checkpoint integrity failed.')
        record = json.loads(row['payload'])
        if (canonical(record) != row['payload'] or record.get('status') != 'review_required'
                or record.get('physical_index') != row['ordinal']
                or record.get('provenance', {}).get('asset_sha256') != job['asset']
                or request.get('asset_sha256') != job['asset']
                or not isinstance(record.get('raw_text'), str) or not record['raw_text'].strip()):
            raise ValueError('No intact extracted PDF text is available for review.')
        from .retained_assets import RetainedAssets
        from ..catalog.intake import source_snapshot
        budget = effective_budget(_budget_from_record(request))
        originals = RetainedAssets(Path(root) / 'originals')
        retained = originals.resolve(job['asset'], max_bytes=budget.source_bytes)
        source = source_snapshot(originals.root, retained.name, max_bytes=budget.source_bytes,
                                 file_backed=request['version'] == 2)
        data = source if request['version'] == 2 else source.data
        renderer, fingerprint = isolated_render_fingerprint(data, row['ordinal'], resource_root=root)
        binding = dict(request_id=request_id, workspace=workspace, job=job['id'], edition=job['edition'],
                       asset_sha256=job['asset'], physical=row['ordinal'],
                       raw_sha256=sha256(record['raw_text'].encode('utf-8')).hexdigest(),
                       checkpoint_sha256=row['digest'], renderer=renderer, render_sha256=fingerprint)
        return dict(binding, raw_text=record['raw_text'], warnings=record.get('warnings', []),
                    method=record['provenance'].get('method'),
                    checksum=sha256(canonical(binding).encode('utf-8')).hexdigest())


def pending_review(root, workspace, request_id):
    with request_lock(root, request_id):
        return _pending_review(root, workspace, request_id)


def confirm_review(root, workspace, request_id, reviewer, *, confirmed, expected_checksum):
    if confirmed is not True:
        raise ValueError('Independent comparison with the original PDF page must be confirmed.')
    if (not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 512
            or '\x00' in reviewer):
        raise ValueError('Enter the reviewer identity.')
    with request_lock(root, request_id):
        current = _pending_review(root, workspace, request_id)
        if current is None or current['checksum'] != expected_checksum:
            raise ValueError('The displayed review changed; reopen the pending page.')
        approval = {key: value for key, value in current.items()
                    if key not in ('raw_text', 'warnings', 'method', 'checksum')}
        approval['reviewer'] = reviewer.strip()
        reviews = load_reviews(root, request_id)
        existing = reviews.get(str(current['physical']))
        if existing is not None and existing != approval:
            raise ValueError('A different import review already exists; do not overwrite it.')
        reviews[str(current['physical'])] = approval
        raw = (canonical(reviews) + '\n').encode('utf-8')
        if len(raw) > MAX_REVIEWS:
            raise ValueError('Import review records exceed the supported limit.')
        path = _review_path(root, request_id)
        temporary = path.with_name(uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return dict(request_id=request_id, physical=current['physical'], state='reviewed',
                    checksum=current['checksum'])


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=2)


def _request_record(root, workspace, source, metadata, config, request_id, expected_manifest=None,
                    import_budget=None):
    checked_budget(import_budget)
    metadata = dict(metadata)
    if metadata.get('title') in (None, ''):
        metadata['title'] = Path(source).stem
    metadata['work_id'] = metadata.get('work_id') or uuid4().hex
    metadata['edition_id'] = metadata.get('edition_id') or uuid4().hex
    record = dict(version=1, request_id=request_id, data_dir=str(Path(root).resolve()),
                  workspace=workspace, source=str(Path(source).absolute()),
                  metadata=metadata, config=asdict(config), state='pending')
    if import_budget is not None:
        record.update(version=2, import_budget=import_budget.to_dict())
    if expected_manifest is not None:
        if (not isinstance(expected_manifest, dict)
                or set(expected_manifest) != {'sha256', 'mime', 'size'}
                or not isinstance(expected_manifest['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest['sha256'])
                or expected_manifest['mime'] not in MIME_SUFFIXES
                or type(expected_manifest['size']) is not int
                or not 0 < expected_manifest['size'] <= source_limit(expected_manifest['mime'], import_budget)):
            raise ValueError('Invalid planned source manifest.')
        record['expected_manifest'] = dict(expected_manifest)
    # Compare the exact representation persisted by JSON, including sequences.
    return json.loads(canonical(record))


def prepare_request(root, workspace, source, metadata, config, *, request_id=None,
                    expected_manifest=None, import_budget=None):
    """Persist stable import identities without opening or processing the source.

    A batch can save its request ID first and safely repeat this preparation
    after a crash. An existing request must retain the entire planned identity.
    """
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        planned = _request_record(root, workspace, source, metadata, config,
                                  request_id, expected_manifest, import_budget)
        try:
            existing = load_request(root, request_id)
        except FileNotFoundError:
            _store(root, request_id, planned, create=True)
            return planned
        identity = set(planned) - {'state'}
        if (any(existing.get(key) != planned[key] for key in identity)
                or existing.get('expected_manifest') != planned.get('expected_manifest')):
            raise ValueError('Persisted import request differs from its planned identity.')
        return existing


def run_request(root, workspace, source, metadata, config, *, cancelled=lambda: False,
                timeout=None, request_id=None, import_budget=None):
    """Execute a new or resumed document request without parsing in the caller."""
    new = request_id is None
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        return _run_request(root, workspace, source, metadata, config,
                            cancelled=cancelled, timeout=timeout,
                            request_id=request_id, new=new, import_budget=import_budget)


def _published_after_stop(app, initial, attempt_id):
    """Compatibility read through the one canonical receipt reconciler."""
    try:
        return _reconcile_publication(app.root, initial['request_id'], attempt_id,
                                      initial=initial)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def _run_request(root, workspace, source, metadata, config, *, cancelled,
                 timeout, request_id, new, import_budget=None):
    checked_budget(import_budget)
    if (not callable(cancelled) or timeout is not None and
            (type(timeout) not in (int, float) or not math.isfinite(timeout)
             or not 0.1 <= timeout <= 86400)):
        raise ValueError('Choose a finite import work-slice allowance.')
    if new:
        record = _request_record(root, workspace, source, metadata, config, request_id,
                                 import_budget=import_budget)
        _store(root, request_id, record, create=True)
    else:
        record = load_request(root, request_id)
        if record.get('workspace') != workspace:
            raise ValueError('Import request belongs to another library.')
        if import_budget is not None and record.get('import_budget') != import_budget.to_dict():
            raise ValueError('Resume must retain the exact reviewed import budget.')
    budget = _budget_from_record(record)
    timeout = (budget.seconds if budget is not None else 120.0) if timeout is None else timeout
    if timeout > (budget.seconds if budget is not None else 600):
        raise ValueError('The work slice exceeds its reviewed import allowance.')
    if record.get('state') == 'complete' and isinstance(record.get('result'), dict):
        return dict(record['result'], request_id=request_id, isolated=True)
    from ..application import LibraryApp
    from .publication_identity import request_identity
    app = LibraryApp(root)
    # Admission must precede any new attempt or progress write. A previous
    # pending native owner retains that same reservation; waiting for it must
    # not rebind its request or overwrite its saved progress.
    attempt_id = None
    captured_identity = request_identity(record)
    fingerprint = sha256(captured_identity.encode('utf-8')).hexdigest()
    started = False

    def worker_started():
        nonlocal started
        started = True

    # Reserve around the existing isolated parser/OCR supervisor, without
    # holding a database transaction or changing its request/journal protocol.
    # Native engine thread count and peak RAM are not measured, so this caller
    # takes the whole declared CPU/RAM pool rather than claiming one thread.
    from ..operations import (ResourceBroker, ResourceRequest, ResourceCancelled,
                              ResourceTimeout, ResourceError, ResourceUnavailable)
    from ..extract.page_ocr import config_from_dict
    try:
        broker = ResourceBroker.for_app(app.root, cancelled=cancelled)
        limits = broker.limits
        if not limits.memory_bytes:
            raise ResourceUnavailable('Configure a positive declared memory allowance before isolated import/OCR.')
        selected = config_from_dict(record['config'])
        if budget is None:
            temporary_bytes = 4 * selected.max_pixels + 64 * 1024**2 + MAX_REPLY + MAX_LOG
            memory_bytes, library_bytes = limits.memory_bytes, 256 * 1024**2
        else:
            minimum_temporary = budget.source_bytes + 4 * selected.max_pixels + MAX_REPLY + MAX_LOG
            if budget.temporary_bytes < minimum_temporary:
                raise ResourceUnavailable('The selected temporary allowance must cover the source, pixels and bounded worker logs.')
            temporary_bytes = budget.temporary_bytes
            from .session import WRITE_HEADROOM
            memory_bytes, library_bytes = budget.memory_bytes, budget.library_bytes + WRITE_HEADROOM
        request = ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=memory_bytes,
            disk_path=app.root, disk_bytes=library_bytes,
            additional_disks=((Path(gettempdir()), temporary_bytes),))
        with import_admission(broker, request, operation_id='import:' + request_id,
                              cancelled=cancelled, timeout=min(60, timeout)) as lease:
            # Reauthorize after waiting. This read snapshot is released before
            # native parsing; revocation while queued cannot authorize a launch.
            with app.read(workspace):
                pass
            current = load_request(root, request_id)
            if (request_identity(current) != captured_identity
                    or current.get('attempt') != record.get('attempt')):
                raise ValueError('The saved import changed while waiting for admission.')
            attempt_id = uuid4().hex
            record_progress(root, request_id, attempt=attempt_id,
                            publication_candidate=None, state='running')
            result = _invoke({'data_dir': str(Path(root).resolve()), 'request_id': request_id,
                              'attempt_id': attempt_id, 'request_sha256': fingerprint,
                              'source': record['source'], 'timeout': timeout},
                             timeout=timeout, cancelled=lease.cancelled,
                             on_started=worker_started, **({'owned_tree': True, 'staging_bytes': temporary_bytes}
                                                         if budget is not None else {}))
    except ResourceCancelled:
        result = {'state': 'cancelled', 'message': 'Import cancelled while waiting for shared local resources.'}
        if record.get('attempt') is not None:
            # A cancelled caller may retire an old, already quiescent attempt,
            # but must never rewrite an attempt still owned by pending cleanup.
            # The outer request lock serializes this identity. Acquire the same
            # operation through the ordinary broker for this metadata-only step;
            # do not wait for occupied resources and do not launch native work.
            deadline = time.monotonic() + 0.1
            stopped = lambda: time.monotonic() >= deadline
            try:
                idle = ResourceBroker.for_app(app.root, cancelled=stopped)
                with idle.reserve(ResourceRequest(cpu_slots=1),
                                  operation_id='import:' + request_id,
                                  cancelled=stopped, timeout=0):
                    with app.read(workspace):
                        current = load_request(root, request_id)
                        if (request_identity(current) != captured_identity
                                or current.get('attempt') != record.get('attempt')):
                            raise ValueError('The saved import changed before cancellation.')
                    record_progress(root, request_id, attempt=uuid4().hex,
                        publication_candidate=None, state='cancelled',
                        result=dict(result, request_id=request_id, isolated=True))
            except (ResourceError, OSError, ValueError, sqlite3.Error):
                # Occupied, changed or unverifiable ownership stays untouched.
                # A later admitted resume creates its own fresh attempt.
                pass
    except ResourceTimeout:
        result = {'state': 'timed_out', 'message': 'Shared local resources remained busy; the saved import can be resumed.'}
    except ResourceError as exc:
        result = {'state': 'failed', 'message': str(exc)[:2000]}
    if attempt_id is not None and (started or budget is not None) and result.get('state') in ('cancelled', 'timed_out', 'failed'):
        try:
            committed = _reconcile_publication(root, request_id, attempt_id, initial=record)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            result = dict(state='failed', message=
                          'The stopped import could not verify its publication. Review or resume this request.')
        else:
            if committed is not None:
                result = dict(committed, worker_state=result['state'])
    result = dict(result, request_id=request_id, isolated=True)
    if attempt_id is not None:
        record_progress(root, request_id, state=result.get('state', 'failed'), result=result)
    elif new:
        # This invocation created this exact request while holding request_lock.
        # No native attempt has been admitted, so retain its waiting outcome
        # without manufacturing publication authority or rebinding an old owner.
        with app.read(workspace):
            current = load_request(root, request_id)
            if current != record:
                raise ValueError('The fresh import request changed before admission settled.')
            _store(root, request_id, dict(current, state=result.get('state', 'failed'),
                                         result=result))
    return result


def _invoke(message, *, timeout, cancelled=lambda: False, on_started=None, owned_tree=False,
            staging_bytes=256 * 1024**2):
    """One worker protocol; explicit streaming requests use the current native tree owner."""
    if owned_tree:
        return _invoke_owned(message, timeout=timeout, cancelled=cancelled,
                             on_started=on_started, staging_bytes=staging_bytes)
    payload = canonical(message).encode('utf-8')
    if len(payload) > MAX_REQUEST:
        raise ValueError('Import request is too large.')
    result = None
    process = None
    try:
        if cancelled():
            result = {'state': 'cancelled', 'message': 'Import cancelled before native parsing.'}
        else:
            with TemporaryFile() as input_stream, TemporaryFile() as output, TemporaryFile() as errors:
                input_stream.write(payload)
                input_stream.seek(0)
                options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                            | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
                           if os.name == 'nt' else {'start_new_session': True})
                # Isolated Python ignores CWD, user-site and inherited PYTHONPATH.
                # Only paths derived from this trusted application file are added.
                package_root = Path(__file__).resolve().parents[2]
                roots = [str(package_root)]
                if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
                    roots.append(str(package_root.parent))
                bootstrap = ("import sys,runpy; sys.path[:0]=sys.argv[1:]; "
                             "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")
                executable = sys.executable
                environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
                if (os.name == 'nt' and sys.prefix != sys.base_prefix
                        and getattr(sys, '_base_executable', None)
                        and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
                    # CPython multiprocessing uses this pair on Windows: own
                    # the interpreter, not the venv redirector, while retaining
                    # the selected executable/prefix and its site-packages.
                    # Waiting for a killed redirector alone can race the real
                    # worker's release of its native writer-lock handle.
                    environment.pop('PYTHONEXECUTABLE', None)
                    environment['__PYVENV_LAUNCHER__'] = executable
                    executable = sys._base_executable
                process = subprocess.Popen([executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap, *roots],
                                           stdin=input_stream, stdout=output, stderr=errors,
                                           env=environment,
                                           **options)
                if on_started is not None:
                    on_started()
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if cancelled():
                        result = {'state': 'cancelled', 'message':
                                  'Import worker stopped. Resume checks saved checkpoints and publication.'}
                        break
                    if time.monotonic() >= deadline:
                        result = {'state': 'timed_out', 'message':
                                  'Import time limit reached. Saved checkpoints can be resumed.'}
                        break
                    if os.fstat(output.fileno()).st_size > MAX_REPLY or os.fstat(errors.fileno()).st_size > MAX_LOG:
                        result = {'state': 'failed', 'message': 'Import worker output exceeded its limit.'}
                        break
                    time.sleep(0.05)
                if result is None and (
                        os.fstat(output.fileno()).st_size > MAX_REPLY
                        or os.fstat(errors.fileno()).st_size > MAX_LOG):
                    result = {'state': 'failed', 'message': 'Import worker output exceeded its limit.'}
                if result is not None:
                    _stop(process)
                else:
                    output.seek(0)
                    raw = output.read(MAX_REPLY + 1)
                    if len(raw) > MAX_REPLY:
                        raise ValueError('Import worker reply exceeded its limit.')
                    try:
                        reply = json.loads(raw)
                    except (UnicodeError, ValueError):
                        reply = None
                    if (process.returncode != 0 or not isinstance(reply, dict)
                            or not isinstance(reply.get('result'), dict)):
                        message = reply.get('error') if isinstance(reply, dict) else None
                        result = {'state': 'failed', 'message': message or
                                  f'Import worker exited unexpectedly ({process.returncode}).'}
                    else:
                        result = reply['result']
    finally:
        if process is not None and process.poll() is None:
            _stop(process)
    return result


@contextmanager
def import_admission(broker, request, *, operation_id, cancelled, timeout):
    """Keep this exact resource reservation with a pending native cleanup owner."""
    context = broker.reserve(request, operation_id=operation_id, cancelled=cancelled, timeout=timeout)
    lease = context.__enter__()
    retained = False
    try:
        yield lease
    except AudioCleanupPending as exc:
        retained = True
        exc.cleanup_owner.add_quiescent_callback(lambda: context.__exit__(None, None, None))
        raise
    finally:
        if not retained:
            context.__exit__(None, None, None)


def _load_worker_input(path, expected):
    """Open only the parent's finite private protocol file in the native child."""
    from ..durable_state import _open_json_file, local_path, regular
    from ..catalog.intake import _stamp
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= MAX_REQUEST:
        raise ValueError('Import worker input exceeds its private protocol allowance.')
    with _open_json_file(path) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Import worker input was replaced before open.')
        raw = stream.read(before.st_size + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Import worker input changed while reading.')
    if (_stamp(regular(path)) != _stamp(before) or len(raw) != before.st_size
            or sha256(raw).hexdigest() != expected):
        raise ValueError('Import worker input differs from its captured protocol bytes.')
    return raw


def _invoke_owned(message, *, timeout, cancelled, on_started=None, staging_bytes=256 * 1024**2):
    """Reuse audio.process's exact owned Job/group and unchanged worker JSON."""
    from ..audio.cleanup import retained_temporary_directory
    from ..audio.models import AudioBudget, AudioCancelled, AudioError
    from ..audio.process import run_process
    payload = canonical(message).encode('utf-8')
    if not 0 < len(payload) <= MAX_REQUEST:
        raise ValueError('Import request is too large.')
    if type(staging_bytes) is not int or not 4096 <= staging_bytes <= 2**63-1:
        raise ValueError('Invalid import worker staging allowance.')
    if cancelled():
        return {'state': 'cancelled', 'message': 'Import cancelled before native parsing.'}
    with retained_temporary_directory(prefix='carr-import-worker-', dir=None) as temporary:
        directory = Path(temporary)
        path = directory / 'request.json'
        with path.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        package_root = Path(__file__).resolve().parents[2]
        roots = [str(package_root)]
        if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
            roots.append(str(package_root.parent))
        bootstrap = ("import sys,runpy,io; sys.path[:0]=sys.argv[3:]; "
                     "from carr_library.ingest.worker import _load_worker_input; "
                     "raw=_load_worker_input(sys.argv[1],sys.argv[2]); "
                     "sys.stdin=io.TextIOWrapper(io.BytesIO(raw),encoding='utf-8'); "
                     "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")
        executable = sys.executable
        environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                           TMPDIR=str(directory), TEMP=str(directory), TMP=str(directory))
        if (os.name == 'nt' and sys.prefix != sys.base_prefix
                and getattr(sys, '_base_executable', None)
                and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
            environment.pop('PYTHONEXECUTABLE', None)
            environment['__PYVENV_LAUNCHER__'] = executable
            executable = sys._base_executable
        try:
            native = run_process([str(Path(executable).absolute()), '-I', '-B', '-X', 'utf8', '-c',
                    bootstrap, str(path), sha256(payload).hexdigest(), *roots],
                budget=AudioBudget(timeout=timeout, max_log_bytes=MAX_LOG,
                                   max_output_bytes=staging_bytes, reserve_disk_bytes=0),
                cancelled=cancelled, env=environment, watch=(directory,),
                owned_tree=True, allow_failure=True)
            if on_started is not None:
                on_started()  # A returned native result proves a real launch.
        except AudioCleanupPending:
            raise
        except AudioCancelled:
            return {'state': 'cancelled', 'message': 'The owned import worker was cancelled; resume verifies saved checkpoints.'}
        except AudioError as exc:
            return {'state': 'failed', 'message': str(exc)[:2000]}
        if len(native.stdout) > MAX_REPLY:
            return {'state': 'failed', 'message': 'Import worker reply exceeded its limit.'}
        try:
            reply = json.loads(native.stdout)
        except (UnicodeError, ValueError):
            reply = None
        if (native.returncode != 0 or not isinstance(reply, dict)
                or not isinstance(reply.get('result'), dict)):
            return {'state': 'failed', 'message': str(reply.get('error') if isinstance(reply, dict)
                else f'Import worker exited unexpectedly ({native.returncode}).')[:2000]}
        return reply['result']


def _resource_limits(timeout, memory_bytes=None):
    try:
        import resource
    except ImportError:
        return
    # Keep the historical address limit unless a reviewed request selected its
    # own declared RAM budget. Windows does not claim POSIX address enforcement.
    memory_bytes = 3 * 1024**3 if memory_bytes is None else memory_bytes
    for key, value in ((resource.RLIMIT_AS, memory_bytes),
                       (resource.RLIMIT_CPU, max(1, math.ceil(timeout)))):
        _, hard = resource.getrlimit(key)
        limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(key, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def isolated_render_fingerprint(data, physical, *, cancelled=lambda: False, resource_root=None):
    """Use the same isolated renderer; large originals stay file-backed."""
    from ..catalog.intake import FileSnapshot
    if not callable(cancelled):
        raise ValueError('A review cancellation callback is required.')
    file_backed = isinstance(data, FileSnapshot)
    if file_backed:
        if (data.mime != 'application/pdf' or not 0 < data.size <= 2 ** 63 - 1
                or resource_root is None):
            raise ValueError('A large PDF review requires its scoped resource owner.')
        if type(physical) is not int or not 1 <= physical <= 2 ** 31 - 1:
            raise ValueError('Invalid PDF review page.')
    else:
        if type(data) is not bytes or not 0 < len(data) <= 64 * 1024 * 1024:
            raise ValueError('Invalid PDF review bytes.')
        if type(physical) is not int or not 1 <= physical <= 1000:
            raise ValueError('Invalid PDF review page.')
    with ExitStack() as lifetime:
        stopped = cancelled
        if file_backed:
            from ..operations import ResourceBroker, ResourceRequest
            broker = ResourceBroker.for_app(resource_root, cancelled=cancelled)
            if not broker.limits.memory_bytes:
                raise ValueError('Configure a positive declared memory allowance before PDF review.')
            lease = lifetime.enter_context(import_admission(broker,
                ResourceRequest(cpu_slots=broker.limits.cpu_slots,
                    memory_bytes=broker.limits.memory_bytes,
                    disk_path=Path(gettempdir()), disk_bytes=data.size + 100 * 1024**2),
                operation_id='import-review:' + uuid4().hex, cancelled=cancelled, timeout=30))
            stopped = lease.cancelled
        if file_backed:
            from ..audio.cleanup import retained_temporary_directory
            temporary = lifetime.enter_context(retained_temporary_directory(prefix='carr-review-', dir=None))
        else:
            temporary = lifetime.enter_context(TemporaryDirectory(prefix='carr-review-'))
        raster = raster_format(data) if type(data) is bytes else None
        suffix = MIME_SUFFIXES[raster.mime] if raster is not None else '.pdf'
        source = Path(temporary) / ('page-source' + suffix)
        if file_backed:
            data.copy_to(source, cancelled=stopped)
            message = dict(action='render-file', source=str(source), sha256=data.digest,
                           size=data.size, physical=physical, timeout=30)
        else:
            source.write_bytes(data)
            message = dict(action='render', source=str(source), sha256=sha256(data).hexdigest(),
                           physical=physical, timeout=30)
        result = _invoke(message, timeout=30, cancelled=stopped,
                         **({'owned_tree': True} if file_backed else {}))
        if file_backed:
            data.verify(cancelled=stopped)
    if (isinstance(result, dict) and result.get('state') == 'cancelled') or cancelled():
        raise InterruptedError('PDF source verification cancelled.')
    if (not isinstance(result, dict) or set(result) != {'renderer', 'fingerprint'}
            or not isinstance(result['renderer'], str) or not result['renderer']
            or not isinstance(result['fingerprint'], str)
            or not re.fullmatch('[0-9a-f]{64}', result['fingerprint'])):
        raise ValueError('The isolated PDF review renderer could not verify this page.')
    return result['renderer'], result['fingerprint']


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('Import worker request exceeded its limit.')
        message = json.loads(raw)
        import_keys = {'data_dir', 'request_id', 'attempt_id', 'request_sha256', 'source', 'timeout'}
        render_keys = {'action', 'source', 'sha256', 'physical', 'timeout'}
        render_file_keys = render_keys | {'size'}
        from .slices import MESSAGE_KEYS as slice_keys, SCHEMA as slice_schema
        if not isinstance(message, dict) or set(message) not in (import_keys, render_keys, render_file_keys, slice_keys):
            raise ValueError('Invalid import worker request schema.')
        timeout = message['timeout']
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 86400:
            raise ValueError('Invalid import worker timeout.')
        if set(message) == slice_keys:
            if (message.get('action') != slice_schema or type(message.get('request_ids')) is not list
                    or not 1 <= len(message['request_ids']) <= 1000):
                raise ValueError('Invalid import slice header.')
            record = load_request(message['data_dir'], message['request_ids'][0])
        else:
            record = load_request(message['data_dir'], message['request_id']) if set(message) == import_keys else None
        budget = _budget_from_record(record) if record is not None else None
        if timeout > (budget.seconds if budget is not None else 600):
            raise ValueError('Import worker exceeds its reviewed time allowance.')
        if budget is None:
            _resource_limits(timeout)
        else:
            _resource_limits(timeout, budget.memory_bytes)
        import socket
        def denied(*args, **kwargs):
            raise PermissionError('Network access is disabled in the import worker.')
        socket.create_connection = socket.getaddrinfo = denied
        socket.socket.connect = socket.socket.connect_ex = denied
        if set(message) == slice_keys:
            from .slices import run_child
            with redirect_stdout(sys.stderr):
                result = run_child(message)
            print(canonical({'result': result}))
            return 0
        if set(message) in (render_keys, render_file_keys):
            action = 'render-file' if set(message) == render_file_keys else 'render'
            if message['action'] != action or not isinstance(message['source'], str):
                raise ValueError('Invalid render request.')
            from ..catalog.intake import file_snapshot, snapshot
            source = Path(message['source'])
            if action == 'render-file':
                size = message['size']
                if type(size) is not int or not 0 < size <= 2 ** 63 - 1:
                    raise ValueError('Invalid captured PDF review size.')
                data = file_snapshot(source.parent, source.name, max_bytes=size)
                if data.size != size or data.digest != message['sha256'] or data.mime != 'application/pdf':
                    raise ValueError('PDF review source identity changed.')
            else:
                selected = snapshot(source.parent, source.name)
                if selected.mime not in PHYSICAL_MIMES or selected.digest != message['sha256']:
                    raise ValueError('PDF review source digest changed.')
                data = selected.data
            from ..citations.pdf_review_attestation import render_fingerprint
            with redirect_stdout(sys.stderr):
                renderer, fingerprint = render_fingerprint(data, message['physical'])
            print(canonical({'result': dict(renderer=renderer, fingerprint=fingerprint)}))
            return 0
        if (not isinstance(record.get('metadata'), dict) or set(record['metadata']) != FIELDS
                or not isinstance(record.get('config'), dict)
                or not isinstance(record.get('workspace'), str)
                or not isinstance(record.get('source'), str)):
            raise ValueError('Invalid persisted import request schema.')
        from .publication_identity import PublicationAttempt, request_identity
        identity = request_identity(record)
        attempt = PublicationAttempt(message['attempt_id'], identity)
        attempt.request()
        if (record['source'] != message['source']
                or record.get('attempt') != message['attempt_id'] or
                sha256(identity.encode('utf-8')).hexdigest() != message['request_sha256']):
            raise ValueError('Import request changed before this worker attempt.')
        from ..application import LibraryApp
        from ..extract.page_ocr import config_from_dict
        metadata = dict(record['metadata'])
        if not isinstance(metadata['authors'], list) or not isinstance(metadata['collections'], list):
            raise ValueError('Invalid import metadata sequences.')
        metadata['authors'] = tuple(metadata['authors'])
        metadata['collections'] = tuple(metadata['collections'])
        with redirect_stdout(sys.stderr):
            app = LibraryApp(message['data_dir'])
            source = record['source']
            if record.get('asset_sha256'):
                source = app.assets.resolve(record['asset_sha256'],
                                            max_bytes=effective_budget(budget).source_bytes)
            result = app._import_file(record['workspace'], source, **metadata,
                                      config=config_from_dict(record['config']),
                                      _request_id=record['request_id'], _resume_job=record.get('job'),
                                      _expected_manifest=record.get('expected_manifest'),
                                      _publication_attempt=attempt,
                                      **({'import_budget': budget} if budget is not None else {}))
        print(canonical({'result': result}))
        return 0
    except Exception as exc:
        print(canonical({'error': f'{type(exc).__name__}: {str(exc)[:2000]}'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
