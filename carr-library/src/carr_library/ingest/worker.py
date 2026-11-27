"""Bounded native-parser subprocess and durable private import requests.

This is process isolation with time/resource limits, not an OS security sandbox.
The existing ingest journal remains the only extraction/checkpoint pipeline.
"""
from contextlib import closing, contextmanager, redirect_stdout
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
from ..extract.document_formats import MIME_SUFFIXES

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


def request_identity(record):
    """Hash only immutable admission fields, never a claimed terminal result."""
    keys = {'version', 'request_id', 'data_dir', 'workspace', 'source', 'metadata', 'config'}
    if (not isinstance(record, dict) or not keys <= set(record)
            or type(record['version']) is not int or record['version'] != 1
            or not isinstance(record['metadata'], dict) or set(record['metadata']) != FIELDS
            or not isinstance(record['config'], dict)):
        raise ValueError('Invalid persisted import request identity.')
    identity = {key: record[key] for key in keys}
    identity['expected_manifest'] = record.get('expected_manifest')
    return sha256(canonical(identity).encode('utf-8')).hexdigest()


def validate_publication_document(record):
    """Validate optional request receipt fields without opening paths or a DB."""
    if not isinstance(record, dict):
        raise ValueError('Invalid persisted import request identity.')
    attempt, intent = record.get('attempt_id'), record.get('publication')
    if attempt is not None and (not isinstance(attempt, str)
            or not re.fullmatch('[0-9a-f]{32}', attempt)):
        raise ValueError('Invalid import worker attempt identity.')
    if intent is None:
        return None
    keys = {'version', 'attempt_id', 'request_sha256', 'revision', 'config_sha256',
            'page_count', 'pages_sha256', 'before', 'result', 'committed'}
    if (not isinstance(intent, dict) or set(intent) != keys
            or type(intent['version']) is not int or intent['version'] != 1
            or type(intent['committed']) is not bool or attempt is None
            or intent['attempt_id'] != attempt
            or any(not isinstance(intent[key], str) or not re.fullmatch('[0-9a-f]{64}', intent[key])
                   for key in ('request_sha256', 'revision', 'config_sha256', 'pages_sha256'))
            or intent['request_sha256'] != request_identity(record)
            or intent['revision'] != record.get('job')
            or type(intent['page_count']) is not int or not 1 <= intent['page_count'] <= 1000):
        raise ValueError('Import source or configuration changed; publication identity is invalid.')
    before, result = intent['before'], intent['result']
    metadata = record['metadata']
    collections = metadata.get('collections')
    if (not isinstance(collections, list) or len(collections) > 100
            or any(not isinstance(value, str) or not value for value in collections)
            or not isinstance(before, dict) or set(before) !=
                {'revision_existed', 'active_revision', 'missing_memberships'}
            or type(before['revision_existed']) is not bool
            or (before['active_revision'] is not None and
                (not isinstance(before['active_revision'], str) or not before['active_revision']
                 or len(before['active_revision']) > 200))
            or not isinstance(before['missing_memberships'], list)
            or len(before['missing_memberships']) > 100
            or any(not isinstance(value, str) for value in before['missing_memberships'])
            or len(set(before['missing_memberships'])) != len(before['missing_memberships'])
            or not set(before['missing_memberships']) <= set(collections)
            or not isinstance(result, dict)
            or not {'state', 'id', 'edition', 'completed', 'verified'} <= set(result)
            or not set(result) <= {'state', 'id', 'edition', 'completed', 'verified',
                'workspace', 'asset', 'total', 'error', 'format', 'warnings', 'message'}
            or result.get('state') not in ('complete', 'superseded')
            or result.get('id') != intent['revision']
            or result.get('edition') != metadata['edition_id']
            or type(result.get('completed')) is not int or result['completed'] != intent['page_count']
            or type(result.get('verified')) is not bool
            or ('total' in result and (type(result['total']) is not int or
                                       result['total'] != intent['page_count']))
            or result.get('error') is not None):
        raise ValueError('Invalid request-bound publication pre-state or result.')
    if len(canonical(intent).encode('utf-8')) > MAX_REQUEST:
        raise ValueError('Import publication receipt exceeds its limit.')
    return intent


def publication_before(db, workspace, edition, revision, collections):
    """Capture pre-state inside the existing catalog publication transaction."""
    if not db.in_transaction:
        raise ValueError('Import publication requires its existing transaction.')
    active = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                        (workspace, edition)).fetchone()
    return dict(revision_existed=db.execute('SELECT 1 FROM revision WHERE id=?',
                                            (revision,)).fetchone() is not None,
                active_revision=active[0] if active else None,
                missing_memberships=[collection for collection in dict.fromkeys(collections)
                    if db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? '
                                  'AND collection=? AND edition=?',
                                  (workspace, collection, edition)).fetchone() is None])


def publication_pages(db, revision):
    """Bound immutable page contents without copying source text to the request."""
    digest, count = sha256(), 0
    for count, row in enumerate(db.execute(
            'SELECT ordinal,physical,printed,verified,raw,digest FROM page '
            'WHERE revision=? ORDER BY ordinal', (revision,)), 1):
        if (count > 1000 or row[0] != count or not isinstance(row[4], str)
                or sha256(row[4].encode('utf-8')).hexdigest() != row[5]):
            raise ValueError('Import publication page integrity changed.')
        digest.update((canonical([*row[:4], row[5]]) + '\n').encode('utf-8'))
    if not count:
        raise ValueError('Import publication has no source pages.')
    return count, digest.hexdigest()


def prepare_publication(root, request_id, db, revision, before, result):
    """Persist request-bound intent before COMMIT; intent is never proof alone.

    The parent verifies the sealed database, original bytes and registration
    after the child has exited. This uses the existing private request and
    journal; there is no second publication pipeline or catalog schema.
    """
    if request_id is None:
        return
    request = load_request(root, request_id)
    attempt = request.get('attempt_id')
    if not isinstance(attempt, str) or not re.fullmatch('[0-9a-f]{32}', attempt):
        raise ValueError('Import publication has no admitted worker attempt.')
    if not db.in_transaction:
        raise ValueError('Import publication requires its existing transaction.')
    published = db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                           (revision,)).fetchone()
    if (published is None or tuple(published[:3]) != (request['workspace'],
            request['metadata']['edition_id'], request.get('asset_sha256'))
            or request.get('job') != revision):
        raise ValueError('Import publication differs from its admitted request.')
    count, pages_digest = publication_pages(db, revision)
    intent = dict(version=1, attempt_id=attempt, request_sha256=request_identity(request),
                  revision=revision, config_sha256=sha256(published[3].encode('utf-8')).hexdigest(),
                  page_count=count, pages_sha256=pages_digest, before=before,
                  result=dict(result), committed=False)
    record_progress(root, request_id, publication=intent)


def confirm_publication(root, request_id):
    """Acknowledge an already committed transaction, still subject to DB checks."""
    if request_id is None:
        return
    request = load_request(root, request_id)
    intent = request.get('publication')
    if (not isinstance(intent, dict) or intent.get('attempt_id') != request.get('attempt_id')
            or intent.get('request_sha256') != request_identity(request)):
        raise ValueError('Import publication acknowledgment identity changed.')
    record_progress(root, request_id, publication=dict(intent, committed=True))


def _settle_request(root, request_id, identity, result=None):
    """Resolve a fully stopped child under the same writer admission guard."""
    from ..application import LibraryApp
    from .writer_guard import exclusive_writer
    app = LibraryApp(root)
    with exclusive_writer(root, timeout=5):
        request = load_request(root, request_id)
        if request_identity(request) != identity:
            raise ValueError('Import request identity changed while the worker ran.')
        try:
            committed = app._reconcile_import_publication(request)
        except (ValueError, OSError, sqlite3.Error) as exc:
            committed = None
            result = dict(state='failed', message=str(exc)[:2000],
                          worker_terminal_state=result.get('state') if result else None)
        if committed is not None:
            if result is None and isinstance(request.get('result'), dict):
                # Reopening the completed request must retain why its original
                # worker stopped, even though no new child was needed.
                for key in ('worker_terminal_state', 'worker_message'):
                    if key in request['result']:
                        committed[key] = request['result'][key]
            if result is not None and result.get('state') not in ('complete', 'superseded'):
                committed.update(worker_terminal_state=result.get('state'),
                                 worker_message=result.get('message', ''))
            result = dict(committed, reconciled=True)
        elif result is None:
            return None
        elif result.get('state') in ('complete', 'superseded'):
            result = dict(state='failed', message='Import completion has no validated durable publication.')
        result = dict(result, request_id=request_id, isolated=True)
        record_progress(root, request_id, state=result.get('state', 'failed'), result=result)
        return result


class RuntimeBoundIngest(Ingest):
    """Bind PDF journal checkpoints to the existing extraction fingerprint."""
    def __init__(self, journal, storage, *, reviews=None, review_asset=None):
        super().__init__(journal, storage)
        self.reviews = reviews or {}
        self.review_asset = review_asset
        self.validated_reviews = {}

    def _plan(self, asset, config):
        count, settings, segments = super()._plan(asset, config)
        if asset.mime == 'application/pdf':
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
                or type(physical) is not int or not isinstance(self.review_asset, bytes)
                or not isinstance(review, dict)
                or review.get('physical') != physical
                or review.get('asset_sha256') != record['provenance'].get('asset_sha256')
                or review.get('asset_sha256') != sha256(self.review_asset).hexdigest()
                or review.get('raw_sha256') != sha256(record['raw_text'].encode('utf-8')).hexdigest()
                or review.get('checkpoint_sha256') != sha256(canonical(record).encode('utf-8')).hexdigest()
                or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
            raise ValueError('Reviewed checkpoint identity differs from the extracted source.')
        if physical not in self.validated_reviews:
            from ..citations.pdf_review_attestation import render_fingerprint
            renderer, fingerprint = render_fingerprint(self.review_asset, physical)
            if review.get('renderer') != renderer or review.get('render_sha256') != fingerprint:
                raise ValueError('Reviewed PDF page rendering changed.')
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
            raise ValueError('No intact extracted PDF text is available for review.')
        from .retained_assets import RetainedAssets
        data = RetainedAssets(Path(root) / 'originals').resolve(job['asset']).read_bytes()
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
        from ..catalog.intake import MAX_BYTES
        if (not isinstance(expected_manifest, dict)
                or set(expected_manifest) != {'sha256', 'mime', 'size'}
                or not isinstance(expected_manifest['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest['sha256'])
                or expected_manifest['mime'] not in MIME_SUFFIXES
                or type(expected_manifest['size']) is not int
                or not 0 < expected_manifest['size'] <= MAX_BYTES):
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
    identity = request_identity(record)
    if record.get('publication') is not None:
        recovered = _settle_request(root, request_id, identity)
        if recovered is not None:
            return recovered
    record_progress(root, request_id, state='running', attempt_id=uuid4().hex,
                    publication=None)
    result = _invoke({'data_dir': str(Path(root).resolve()), 'request_id': request_id,
                      'timeout': timeout}, timeout=timeout, cancelled=cancelled)
    # _invoke returns only after its process has exited or has been stopped and
    # reaped. A timeout/cancel/lost stdout receipt cannot undo a durable commit.
    return _settle_request(root, request_id, identity, result)


def _invoke(message, *, timeout, cancelled=lambda: False):
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
                process = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap, *roots],
                                           stdin=input_stream, stdout=output, stderr=errors,
                                           env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
                                           **options)
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


def _resource_limits(timeout):
    try:
        import resource
    except ImportError:
        return
    # Runtime libraries and a bounded 64 MiB source fit well below this address
    # limit. Windows uses process/time isolation; POSIX limits are not claimed there.
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
    if type(data) is not bytes or not 0 < len(data) <= 64 * 1024 * 1024:
        raise ValueError('Invalid PDF review bytes.')
    if type(physical) is not int or not 1 <= physical <= 1000:
        raise ValueError('Invalid PDF review page.')
    with TemporaryDirectory(prefix='carr-review-') as temporary:
        source = Path(temporary) / 'page-source.pdf'
        source.write_bytes(data)
        result = _invoke(dict(action='render', source=str(source), sha256=sha256(data).hexdigest(),
                              physical=physical, timeout=30), timeout=30, cancelled=cancelled)
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
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 64 * 1024 * 1024:
                raise ValueError('Invalid PDF review source.')
            data = source.read_bytes()
            if sha256(data).hexdigest() != message['sha256']:
                raise ValueError('PDF review source digest changed.')
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
