"""Bounded native-parser subprocess and durable private import requests.

This is process isolation with time/resource limits, not an OS security sandbox.
The existing ingest journal remains the only extraction/checkpoint pipeline.
"""
from contextlib import ExitStack, closing, contextmanager, redirect_stdout
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
from tempfile import TemporaryDirectory, TemporaryFile
import time
from uuid import uuid4

from .pipeline import Ingest, canonical
from ..extract.document_formats import (MIME_SUFFIXES, PHYSICAL_MIMES, RASTER_MIMES,
                                        RASTER_BY_MIME, raster_format)
from ..catalog.intake import MAX_BYTES, FileSnapshot, source_snapshot, source_limit

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
    record = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(record, dict) or record.get('version') != 1
            or record.get('request_id') != request_id
            or record.get('data_dir') != str(Path(root).resolve())):
        raise ValueError('Import request identity changed.')
    return record


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
    return {key: record.get(key) for key in (
        'version', 'request_id', 'data_dir', 'workspace', 'source',
        'metadata', 'config', 'expected_manifest')}


def _publication_state(db, record, revision, *, check=lambda: None, max_payload=None):
    """Inspect this exact sealed revision and registration in the caller's snapshot.

    The digest includes original source identity, requested catalog metadata and
    every exact stored page. Read rows incrementally, without copying a large
    extracted document into the request file or a second in-memory collection.
    """
    from ..application import LibraryApp
    from ..catalog.membership import Catalog

    check()
    workspace, metadata = record['workspace'], record['metadata']
    LibraryApp._authorize(db, workspace)
    row = db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                     (revision,)).fetchone()
    if row is None:
        return None
    active = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                        (workspace, metadata['edition_id'])).fetchone()
    if (tuple(row[:3]) != (workspace, metadata['edition_id'], record.get('asset_sha256'))
            or not db.execute('SELECT 1 FROM sealed WHERE revision=?', (revision,)).fetchone()
            or active is None or active[0] != revision):
        raise ValueError('Import publication identity changed.')
    catalog = db.execute(
        'SELECT e.work,e.asset,a.mime,a.size,e.language,e.edition,e.volume,w.title '
        'FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset '
        'JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work '
        'WHERE e.workspace=? AND e.id=?', (workspace, metadata['edition_id'])).fetchone()
    if catalog is None:
        raise ValueError('Import publication registration is missing.')
    manifest = dict(sha256=catalog[1], mime=catalog[2], size=catalog[3])
    if (tuple(catalog[:2]) != (metadata['work_id'], record.get('asset_sha256'))
            or tuple(catalog[4:]) != (metadata['language'], metadata['edition'],
                                      metadata['volume'], metadata['title'])
            or (record.get('expected_manifest') is not None
                and record['expected_manifest'] != manifest)):
        raise ValueError('Import publication metadata changed.')
    authors = []
    for author in db.execute(
            'SELECT name FROM catalog_author WHERE workspace=? AND work=? ORDER BY position',
            (workspace, metadata['work_id'])):
        check()
        authors.append(author[0])
    authors = tuple(authors)
    if authors != tuple(metadata['authors']):
        raise ValueError('Import publication authors changed.')
    scoped = Catalog(db, workspace)
    for collection in metadata['collections']:
        check()
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
    check()
    digest = sha256(canonical([_request_identity(record), list(row), list(catalog),
                               list(source), authors]).encode('utf-8'))
    completed = 0
    # The normal publishing path retains the canonical digest byte-for-byte.
    # Recovery additionally caps each fetched payload and checks its deadline.
    if max_payload is None:
        pages = db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                           'FROM page WHERE revision=? ORDER BY ordinal', (revision,))
    else:
        pages = db.execute('SELECT ordinal,physical,printed,verified,'
                           'CASE WHEN length(CAST(raw AS BLOB))<=? THEN raw END,digest '
                           'FROM page WHERE revision=? ORDER BY ordinal', (max_payload, revision))
    for page in pages:
        check()
        completed += 1
        # Extraction already enforces its format-specific capacity. This
        # streaming verifier must not impose the PDF page limit on DOCX records.
        if (page[0] != completed or not isinstance(page[4], str)
                or sha256(page[4].encode('utf-8')).hexdigest() != page[5]):
            raise ValueError('Import publication page integrity changed.')
        digest.update(b'\n')
        digest.update(canonical(list(page)).encode('utf-8'))
    check()
    if completed == 0:
        raise ValueError('Import publication contains no source pages.')
    return dict(sha256=digest.hexdigest(), completed=completed, manifest=manifest)


def record_publication_candidate(root, request_id, db, workspace, revision):
    """Bind this attempt inside the existing publication transaction.

    The sidecar is only a hint. The immutable database receipt commits with the
    exact source; it distinguishes another request publishing the same
    deterministic revision after this transaction rolls back.
    """
    if request_id is None:
        return
    record = load_request(root, request_id)
    attempt = record.get('attempt')
    if attempt is None:  # Legacy direct invocations have no supervising attempt.
        return
    if (not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt)
            or not db.in_transaction or record['workspace'] != workspace):
        raise ValueError('Import publication attempt changed.')
    state = _publication_state(db, record, revision)
    if state is None:
        raise ValueError('Import publication candidate is missing.')
    db.execute('INSERT INTO import_publication(revision,request_id,attempt,sha256) '
               'VALUES(?,?,?,?)', (revision, request_id, attempt, state['sha256']))
    record_progress(root, request_id, publication_candidate=dict(
        attempt=attempt, revision=revision, **state))


def _reconcile_publication(root, request_id, attempt, *, workspace, timeout=1.0, initial=None):
    """Recover this child's transaction receipt without replay or writable open."""
    from .publication import completed_request

    return completed_request(root, workspace, request_id, attempt=attempt, timeout=timeout,
                             initial=initial)


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
            from ..extract.page_ocr import Config
            from ..extract.runtime_identity import runtime_identity
            if settings.get('runtime_identity') != runtime_identity(Config(**settings['config'])):
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
        if (record.get('status') != 'review_required' or not record.get('raw_text', '').strip()
                or type(physical) is not int or not isinstance(self.review_asset, (bytes, FileSnapshot))
                or not isinstance(review, dict)
                or review.get('physical') != physical
                or review.get('asset_sha256') != record['provenance'].get('asset_sha256')
                or review.get('asset_sha256') != (self.review_asset.verify(
                    cancelled=getattr(self, '_cancelled', lambda: False)).digest
                    if isinstance(self.review_asset, FileSnapshot) else sha256(self.review_asset).hexdigest())
                or review.get('raw_sha256') != sha256(record['raw_text'].encode('utf-8')).hexdigest()
                or review.get('checkpoint_sha256') != sha256(canonical(record).encode('utf-8')).hexdigest()
                or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
            raise ValueError('Reviewed checkpoint identity differs from the extracted source.')
        if physical not in self.validated_reviews:
            from ..citations.pdf_review_attestation import render_fingerprint
            renderer, fingerprint = render_fingerprint(self.review_asset, physical,
                cancelled=getattr(self, '_cancelled', lambda: False))
            if review.get('renderer') != renderer or review.get('render_sha256') != fingerprint:
                raise ValueError('Reviewed physical-page rendering changed.')
            self.validated_reviews[physical] = dict(review)
        return True

    def retry(self, workspace, job):
        # Approved quarantine keeps the exact original checkpoint; ordinary
        # retry continues to discard/re-extract the last unapproved candidate.
        with self.transaction():
            row = self._job(workspace, job)
            records = self._validated(row)
            if row['state'] == 'quarantined' and records and self._eligible(records[-1]):
                if not self._recover(row, records):
                    self._set(job, 'pending')
                return
        return super().retry(workspace, job)


@contextmanager
def request_lock(root, request_id):
    from filelock import FileLock, Timeout
    lock_path = _path(root, request_id).with_suffix('.lock')
    if os.path.lexists(lock_path):
        info = lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Invalid import request lock.')
    try:
        with FileLock(lock_path, mode=0o600).acquire(timeout=0):
            yield
    except Timeout as exc:
        raise ValueError('This import request is already running.') from exc


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
            raise ValueError('No intact extracted physical-page text is available for review.')
        from .retained_assets import RetainedAssets
        retained = RetainedAssets(Path(root) / 'originals').resolve(job['asset'])
        asset = source_snapshot(retained.parent, retained.name)
        data = asset if isinstance(asset, FileSnapshot) else asset.data
        renderer, fingerprint = isolated_render_fingerprint(data, row['ordinal'])
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
        raise ValueError('Independent comparison with the original physical page or image frame must be confirmed.')
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


def _request_record(root, workspace, source, metadata, config, request_id, expected_manifest=None):
    metadata = dict(metadata)
    if metadata.get('title') in (None, ''):
        metadata['title'] = Path(source).stem
    metadata['work_id'] = metadata.get('work_id') or uuid4().hex
    metadata['edition_id'] = metadata.get('edition_id') or uuid4().hex
    record = dict(version=1, request_id=request_id, data_dir=str(Path(root).resolve()),
                  workspace=workspace, source=str(Path(source).absolute()),
                  metadata=metadata, config=asdict(config), state='pending')
    if expected_manifest is not None:
        if (not isinstance(expected_manifest, dict)
                or set(expected_manifest) != {'sha256', 'mime', 'size'}
                or not isinstance(expected_manifest['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest['sha256'])
                or expected_manifest['mime'] not in MIME_SUFFIXES
                or type(expected_manifest['size']) is not int
                or not 0 < expected_manifest['size'] <= source_limit(expected_manifest['mime'])):
            raise ValueError('Invalid planned source manifest.')
        record['expected_manifest'] = dict(expected_manifest)
    # Compare the exact representation persisted by JSON, including sequences.
    return json.loads(canonical(record))


def prepare_request(root, workspace, source, metadata, config, *, request_id=None,
                    expected_manifest=None):
    """Persist stable import identities without opening or processing the source.

    A batch can save its request ID first and safely repeat this preparation
    after a crash. An existing request must retain the entire planned identity.
    """
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        planned = _request_record(root, workspace, source, metadata, config,
                                  request_id, expected_manifest)
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
                timeout=120.0, request_id=None):
    """Execute a new or resumed document request without parsing in the caller."""
    new = request_id is None
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        return _run_request(root, workspace, source, metadata, config,
                            cancelled=cancelled, timeout=timeout,
                            request_id=request_id, new=new)


def _publication_error_details(error):
    """Keep useful recovery diagnostics without exposing exception payloads/paths."""
    kind = type(error).__name__
    details = [kind if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,79}', kind) else 'Error']
    location = None
    trace = error.__traceback__
    while trace is not None:
        module = trace.tb_frame.f_globals.get('__name__', '')
        if isinstance(module, str) and re.fullmatch(r'carr_library\.[A-Za-z_0-9.]{1,160}', module):
            location = f'{module}:{trace.tb_lineno}'
        trace = trace.tb_next
    if location is not None:
        details.append(location)
    for name in ('errno', 'winerror', 'sqlite_errorcode'):
        value = getattr(error, name, None)
        if type(value) is int and 0 <= value <= 0xFFFFFFFF:
            details.append(f'{name}={value}')
    value = getattr(error, 'sqlite_errorname', None)
    if isinstance(value, str) and re.fullmatch(r'SQLITE_[A-Z_0-9]{1,64}', value):
        details.append(value)
    return ', '.join(details)


class _UnreportedWorkerExit(dict):
    """Host-only marker: the child supplied no explicit error reply."""


def _run_request(root, workspace, source, metadata, config, *, cancelled,
                 timeout, request_id, new):
    if (not callable(cancelled) or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or not 0.1 <= timeout <= 600):
        raise ValueError('Import timeout must be between 0.1 and 600 seconds.')
    if new:
        record = _request_record(root, workspace, source, metadata, config, request_id)
        _store(root, request_id, record, create=True)
    else:
        record = load_request(root, request_id)
        if record.get('workspace') != workspace:
            raise ValueError('Import request belongs to another library.')
        if record.get('state') == 'complete' and isinstance(record.get('result'), dict):
            return dict(record['result'], request_id=request_id, isolated=True)
    if record.get('attempt') is not None or record.get('publication_candidate') is not None:
        # Explicit resume invalidates only a prior process hint before waiting.
        # A fresh queued request gains no execution attempt until admission.
        record = dict(record, state='pending', attempt=uuid4().hex,
                      publication_candidate=None)
        record.pop('result', None)
        _store(root, request_id, record)
    from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled, ResourceError, ResourceTimeout
    with ExitStack() as reservations:
        try:
            broker = ResourceBroker.for_app(root, cancelled=cancelled)
            request = ResourceRequest(cpu_slots=1, memory_bytes=3 * 1024**3,
                disk_bytes=512 * 1024**2, disk_path=Path(root))
            lease = reservations.enter_context(broker.reserve(request,
                operation_id='import:' + request_id, cancelled=cancelled,
                timeout=min(60.0, timeout)))
        except ResourceError as exc:
            state = ('cancelled' if isinstance(exc, ResourceCancelled)
                     else 'timed_out' if isinstance(exc, ResourceTimeout) else 'failed')
            result = dict(state=state, message=str(exc), request_id=request_id, isolated=True)
            record_progress(root, request_id, state=state, result=result)
            return result
        # Recovery and journal publication stay inside the same reservation.
        # A later lease cleanup error cannot rewrite an already committed job.
        return _run_admitted_request(root, workspace, record, request_id=request_id,
                                     timeout=timeout, cancelled=lease.cancelled)


def _run_admitted_request(root, workspace, record, *, request_id, timeout, cancelled):
    attempt = uuid4().hex
    record_progress(root, request_id, state='running', attempt=attempt, publication_candidate=None)
    result = _invoke({'data_dir': str(Path(root).resolve()), 'request_id': request_id,
                      'timeout': timeout}, timeout=timeout, cancelled=cancelled)
    # A real error reply remains an error, even if its source was published.
    # Only an absent/invalid reply can represent a lost post-commit response.
    unreported_exit = isinstance(result, _UnreportedWorkerExit)
    result = dict(result)  # Never persist or expose the host-only marker.
    if result.get('state') in ('cancelled', 'timed_out') or unreported_exit:
        try:
            committed = _reconcile_publication(root, request_id, attempt,
                                               workspace=workspace, timeout=min(timeout, 1.0), initial=record)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            from .writer_guard import WriterBusy
            details = _publication_error_details(error)
            if isinstance(error, (TimeoutError, InterruptedError, WriterBusy)):
                result = dict(result, message=f"{result.get('message', 'Import worker stopped.')} "
                              f'Publication verification is pending ({details}); resume to check it.')
            else:
                result = dict(state='failed', message=
                              f'The stopped import could not verify its publication ({details}); '
                              'review or resume to check it.')
        else:
            if committed is not None:
                result = dict(committed, recovered_publication=True,
                              worker_state=result['state'])
    result = dict(result, request_id=request_id, isolated=True)
    record_progress(root, request_id, state=result.get('state', 'failed'), result=result)
    return result


def _invoke(message, *, timeout, cancelled=lambda: False, on_started=None):
    """One process supervisor for import and render operations."""
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
                        if isinstance(message, str) and message:
                            result = {'state': 'failed', 'message': message}
                        else:
                            result = _UnreportedWorkerExit(state='failed', message=
                                f'Import worker exited unexpectedly ({process.returncode}).')
                    else:
                        result = reply['result']
    finally:
        if process is not None and process.poll() is None:
            _stop(process)
    return result


def _resource_limits(timeout):
    try:
        import resource
    except ImportError:
        return
    # Keep the existing 3 GiB parser address ceiling for the 128 MiB TXT/PDF
    # streaming profile and the unchanged 64 MiB memory formats. Windows uses
    # process/time isolation; POSIX address limits are not claimed there.
    for key, value in ((resource.RLIMIT_AS, 3 * 1024**3),
                       (resource.RLIMIT_CPU, max(1, math.ceil(timeout)))):
        _, hard = resource.getrlimit(key)
        limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(key, (limit, limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def isolated_render_fingerprint(data, physical, *, cancelled=lambda: False):
    """Reuse the citation renderer in the same bounded isolated worker."""
    if not callable(cancelled):
        raise ValueError('A review cancellation callback is required.')
    if isinstance(data, FileSnapshot):
        if data.mime != 'application/pdf' or not 0 < data.size <= source_limit(data.mime):
            raise ValueError('Invalid file-backed PDF review source.')
    elif type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('Invalid physical-page review bytes.')
    if type(physical) is not int or not 1 <= physical <= 1000:
        raise ValueError('Invalid physical review page.')
    with TemporaryDirectory(prefix='carr-review-') as temporary:
        selected = raster_format(data) if type(data) is bytes else None
        suffix = MIME_SUFFIXES[selected.mime] if selected is not None else '.pdf'
        source = Path(temporary) / ('page-source' + suffix)
        if isinstance(data, FileSnapshot):
            data.copy_to(source, cancelled=cancelled)
            digest = data.digest
        else:
            source.write_bytes(data)
            digest = sha256(data).hexdigest()
        result = _invoke(dict(action='render', source=str(source), sha256=digest,
                              physical=physical, timeout=30), timeout=30, cancelled=cancelled)
    if (isinstance(result, dict) and result.get('state') == 'cancelled') or cancelled():
        raise InterruptedError('Physical-page source verification cancelled.')
    if (not isinstance(result, dict) or set(result) != {'renderer', 'fingerprint'}
            or not isinstance(result['renderer'], str) or not result['renderer']
            or not isinstance(result['fingerprint'], str)
            or not re.fullmatch('[0-9a-f]{64}', result['fingerprint'])):
        raise ValueError('The isolated review renderer could not verify this physical page.')
    return result['renderer'], result['fingerprint']


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('Import worker request exceeded its limit.')
        message = json.loads(raw)
        import_keys = {'data_dir', 'request_id', 'timeout'}
        render_keys = {'action', 'source', 'sha256', 'physical', 'timeout'}
        if not isinstance(message, dict) or set(message) not in (import_keys, render_keys):
            raise ValueError('Invalid import worker request schema.')
        timeout = message['timeout']
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 600:
            raise ValueError('Invalid import worker timeout.')
        _resource_limits(timeout)
        import socket
        def denied(*args, **kwargs):
            raise PermissionError('Network access is disabled in the import worker.')
        socket.create_connection = socket.getaddrinfo = denied
        socket.socket.connect = socket.socket.connect_ex = denied
        if set(message) == render_keys:
            if message['action'] != 'render' or not isinstance(message['source'], str):
                raise ValueError('Invalid render request.')
            source = Path(message['source'])
            original = source_snapshot(source.parent, source.name)
            if original.mime not in PHYSICAL_MIMES or original.digest != message['sha256']:
                raise ValueError('Physical-page review source digest changed.')
            data = original if isinstance(original, FileSnapshot) else original.data
            from ..citations.pdf_review_attestation import render_fingerprint
            with redirect_stdout(sys.stderr):
                renderer, fingerprint = render_fingerprint(data, message['physical'])
            print(canonical({'result': dict(renderer=renderer, fingerprint=fingerprint)}))
            return 0
        record = load_request(message['data_dir'], message['request_id'])
        if (not isinstance(record.get('metadata'), dict) or set(record['metadata']) != FIELDS
                or not isinstance(record.get('config'), dict)
                or not isinstance(record.get('workspace'), str)
                or not isinstance(record.get('source'), str)):
            raise ValueError('Invalid persisted import request schema.')
        from ..application import LibraryApp
        from ..extract.page_ocr import Config
        metadata = dict(record['metadata'])
        if not isinstance(metadata['authors'], list) or not isinstance(metadata['collections'], list):
            raise ValueError('Invalid import metadata sequences.')
        metadata['authors'] = tuple(metadata['authors'])
        metadata['collections'] = tuple(metadata['collections'])
        with redirect_stdout(sys.stderr):
            app = LibraryApp(message['data_dir'])
            source = record['source']
            if record.get('asset_sha256'):
                source = app.assets.resolve(record['asset_sha256'])
            result = app._import_file(record['workspace'], source, **metadata,
                                      config=Config(**record['config']),
                                      _request_id=record['request_id'], _resume_job=record.get('job'),
                                      _expected_manifest=record.get('expected_manifest'))
        print(canonical({'result': result}))
        return 0
    except Exception as exc:
        print(canonical({'error': f'{type(exc).__name__}: {str(exc)[:2000]}'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
