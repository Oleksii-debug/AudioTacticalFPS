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
from tempfile import TemporaryDirectory, TemporaryFile, gettempdir
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
    if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] not in (1, 2)
            or record.get('request_id') != request_id
            or record.get('data_dir') != str(Path(root).resolve())):
        raise ValueError('Import request identity changed.')
    if record['version'] == 2:
        from .streamed_text import profile_from_value
        profile_from_value(record.get('source_profile'))
        if 'pdf_max_bytes' in record:
            raise ValueError('Streamed text cannot acquire a PDF byte allowance.')
    elif 'source_profile' in record:
        raise ValueError('Legacy import cannot acquire a streamed source profile.')
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
    result = {key: record.get(key) for key in (
        'version', 'request_id', 'data_dir', 'workspace', 'source',
        'metadata', 'config', 'expected_manifest')}
    if 'pdf_max_bytes' in record:
        result['pdf_max_bytes'] = record['pdf_max_bytes']
    if record['version'] == 2:
        result['source_profile'] = record['source_profile']
    return result


def _publication_state(db, record, revision, *, allow_superseded=False):
    """Inspect this exact sealed revision and registration in the caller's snapshot.

    The digest includes original source identity, requested catalog metadata and
    every exact stored page. Read rows incrementally, without copying a large
    extracted document into the request file or a second in-memory collection.
    """
    from ..application import LibraryApp
    from ..catalog.membership import Catalog

    if record.get('job') is not None and record['job'] != revision:
        raise ValueError('Import publication journal identity changed.')
    workspace, metadata = record['workspace'], record['metadata']
    LibraryApp._authorize(db, workspace)
    row = db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                     (revision,)).fetchone()
    if row is None:
        return None
    if record['version'] == 2:
        from .streamed_text import profile_from_value
        if row[3] != canonical(profile_from_value(record['source_profile']).configuration()):
            raise ValueError('Streamed publication configuration changed.')
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
    if manifest['mime'] == 'application/pdf' and record.get('job') != revision:
        raise ValueError('Import publication journal identity changed.')
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
    from .publication_identity import PublicationAttempt, validate_configuration
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
            or record.get('pdf_max_bytes') != expected.get('pdf_max_bytes')
            or record.get('source_profile') != expected.get('source_profile')
            or config is None
            or asdict(config) != asdict(validate_configuration(expected['config']))
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


def _reconcile_publication(root, request_id, attempt, *, initial=None):
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

    app = LibraryApp(root)
    with app.read(record['workspace']) as db:
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

    def _plan(self, asset, config, *, cancelled=lambda: False):
        count, settings, segments = super()._plan(asset, config, cancelled=cancelled)
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
        from ..catalog.streaming_pdf import FileAsset
        review_digest = (self.review_asset.digest if isinstance(self.review_asset, FileAsset) else
                         sha256(self.review_asset).hexdigest() if isinstance(self.review_asset, bytes) else None)
        if (record.get('status') != 'review_required' or not record.get('raw_text', '').strip()
                or type(physical) is not int or review_digest is None
                or not isinstance(review, dict)
                or review.get('physical') != physical
                or review.get('asset_sha256') != record['provenance'].get('asset_sha256')
                or review.get('asset_sha256') != review_digest
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
        from ..catalog.streaming_pdf import FileAsset, snapshot_original
        path = RetainedAssets(Path(root) / 'originals').resolve(job['asset'])
        original = snapshot_original(path.parent, path.name)
        data = original if isinstance(original, FileAsset) else original.data
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


def _posix_group_quiescent(group):
    """Confirm no live member remains; Linux zombies cannot retain writers.

    A container's PID 1 may leave an orphan zombie unreaped indefinitely. It
    owns no descriptors and cannot execute. Only an actual bounded /proc scan
    of this exact killed group can distinguish that from a live descendant.
    Other POSIX platforms keep the stronger group-disappearance check.
    """
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    import sys
    if not sys.platform.startswith('linux'):
        return False
    seen = False
    try:
        with os.scandir('/proc') as entries:
            for count, entry in enumerate(entries, 1):
                if count > 100000:
                    return False
                if not entry.name.isdecimal():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != group:
                        continue
                    with open('/proc/' + entry.name + '/stat', 'rb') as stream:
                        raw = stream.read(4097)
                    if len(raw) > 4096:
                        return False
                    fields = raw.rsplit(b')', 1)[1].split()
                    if len(fields) < 3 or int(fields[2]) != group:
                        return False
                    seen = True
                    if fields[0] not in (b'Z', b'X'):
                        return False
                except ProcessLookupError:
                    continue
                except FileNotFoundError:
                    continue
    except (OSError, IndexError, ValueError):
        return False
    if seen:
        return True
    # No visible member is not proof under hidepid or a changing process table.
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def _stop(process):
    """Keep parser streams and admission until the exact native family stops."""
    from ..audio.recording_process import _stop_owned
    from ..audio.cleanup import pending_cleanup, wait_for_cleanup

    def settle():
        if getattr(process, '_carr_ingest_quiescent', False):
            return True
        _stop_owned(process)
        if os.name != 'nt':
            deadline = time.monotonic() + 2
            while not _posix_group_quiescent(process.pid):
                if time.monotonic() >= deadline:
                    raise RuntimeError('The import process family has not confirmed shutdown.')
                time.sleep(.02)
        process._carr_ingest_quiescent = True
        return True

    try:
        return settle()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        error = pending_cleanup(settle,
            message='Import cleanup is pending; streams and resource admission remain reserved.')
        wait_for_cleanup(error)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return True


def _request_record(root, workspace, source, metadata, config, request_id, expected_manifest=None,
                    pdf_max_bytes=None, source_profile=None):
    metadata = dict(metadata)
    if metadata.get('title') in (None, ''):
        metadata['title'] = Path(source).stem
    metadata['work_id'] = metadata.get('work_id') or uuid4().hex
    metadata['edition_id'] = metadata.get('edition_id') or uuid4().hex
    record = dict(version=1, request_id=request_id, data_dir=str(Path(root).resolve()),
                  workspace=workspace, source=str(Path(source).absolute()),
                  metadata=metadata, config=asdict(config), state='pending')
    profile = None
    if source_profile is not None:
        from .streamed_text import profile_from_value
        profile = profile_from_value(source_profile)
        if Path(source).suffix.casefold() != '.txt' or pdf_max_bytes is not None:
            raise ValueError('Streamed text requests require .txt and an independent byte allowance.')
        record.update(version=2, source_profile=profile.to_dict())
    from ..catalog.streaming_pdf import MAX_PDF_BYTES, pdf_budget, valid_original_size
    if pdf_max_bytes is not None:
        record['pdf_max_bytes'] = pdf_budget(pdf_max_bytes)
    if expected_manifest is not None:
        if (not isinstance(expected_manifest, dict)
                or set(expected_manifest) != {'sha256', 'mime', 'size'}
                or not isinstance(expected_manifest['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', expected_manifest['sha256'])
                or expected_manifest['mime'] not in MIME_SUFFIXES
                or type(expected_manifest['size']) is not int
                or (profile is None and not valid_original_size(
                    expected_manifest['mime'], expected_manifest['size'],
                    pdf_max_bytes=record.get('pdf_max_bytes', MAX_PDF_BYTES)))
                or (profile is not None and (expected_manifest['mime'] != 'text/plain'
                    or not 0 < expected_manifest['size'] <= profile.maximum_bytes))):
            raise ValueError('Invalid planned source manifest.')
        record['expected_manifest'] = dict(expected_manifest)
    # Compare the exact representation persisted by JSON, including sequences.
    return json.loads(canonical(record))


def prepare_request(root, workspace, source, metadata, config, *, request_id=None,
                    expected_manifest=None, pdf_max_bytes=None, source_profile=None):
    """Persist stable import identities without opening or processing the source.

    A batch can save its request ID first and safely repeat this preparation
    after a crash. An existing request must retain the entire planned identity.
    """
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        planned = _request_record(root, workspace, source, metadata, config,
                                  request_id, expected_manifest, pdf_max_bytes, source_profile)
        try:
            existing = load_request(root, request_id)
        except FileNotFoundError:
            _store(root, request_id, planned, create=True)
            return planned
        identity = set(planned) - {'state'}
        if (any(existing.get(key) != planned[key] for key in identity)
                or existing.get('expected_manifest') != planned.get('expected_manifest')
                or existing.get('pdf_max_bytes') != planned.get('pdf_max_bytes')
                or existing.get('source_profile') != planned.get('source_profile')):
            raise ValueError('Persisted import request differs from its planned identity.')
        return existing


def run_request(root, workspace, source, metadata, config, *, cancelled=lambda: False,
                timeout=120.0, request_id=None, broker=None, pdf_max_bytes=None, source_profile=None):
    """Execute a new or resumed document request without parsing in the caller."""
    new = request_id is None
    request_id = request_id or uuid4().hex
    with request_lock(root, request_id):
        return _run_request(root, workspace, source, metadata, config,
                            cancelled=cancelled, timeout=timeout,
                            request_id=request_id, new=new, broker=broker, pdf_max_bytes=pdf_max_bytes,
                            source_profile=source_profile)


def _published_after_stop(app, initial, attempt_id):
    """Compatibility read through the one canonical receipt reconciler."""
    try:
        return _reconcile_publication(app.root, initial['request_id'], attempt_id,
                                      initial=initial)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def _run_request(root, workspace, source, metadata, config, *, cancelled,
                 timeout, request_id, new, broker=None, pdf_max_bytes=None, source_profile=None):
    if (not callable(cancelled) or type(timeout) not in (int, float)
            or not math.isfinite(timeout) or not 0.1 <= timeout <= 600):
        raise ValueError('Import timeout must be between 0.1 and 600 seconds.')
    if new:
        record = _request_record(root, workspace, source, metadata, config, request_id,
                                 pdf_max_bytes=pdf_max_bytes, source_profile=source_profile)
        _store(root, request_id, record, create=True)
    else:
        record = load_request(root, request_id)
        if record.get('workspace') != workspace:
            raise ValueError('Import request belongs to another library.')
        if pdf_max_bytes is not None and record.get('pdf_max_bytes') != pdf_max_bytes:
            raise ValueError('Import PDF byte budget changed; prepare a new request.')
        if source_profile is not None:
            from .streamed_text import profile_from_value
            if record.get('source_profile') != profile_from_value(source_profile).to_dict():
                raise ValueError('Import source profile changed; prepare a new request.')
        if record.get('state') == 'complete' and isinstance(record.get('result'), dict):
            return dict(record['result'], request_id=request_id, isolated=True)
    from ..audio.jobs import checked_directory
    from ..operations import ResourceBroker, ResourceCancelled, ResourceStateError
    from ..operations.admission import document_request
    from .publication_identity import request_identity
    library = checked_directory(root)
    request_identity(record)  # Validate before waiting without changing saved identity.
    # Old launch hints never become authority for a request waiting to restart.
    # Its stable metadata remains visible in the existing import queue.
    pending = dict(record, state='pending', publication_candidate=None)
    # A first queued request has not launched a native attempt. A replay must
    # invalidate its earlier attempt even if cancellation happens in admission.
    if 'attempt' in pending:
        pending['attempt'] = uuid4().hex
    pending.pop('publication_watermark', None)
    _store(root, request_id, pending)
    try:
        selected = broker or ResourceBroker.for_app(library, cancelled=cancelled)
        if not isinstance(selected, ResourceBroker):
            raise TypeError('Use the shared typed local resource coordinator.')
        if (selected.root != library / 'runtime' / 'resources'
                or selected.policy_path != library / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The resource coordinator belongs to another library directory.')
        from ..catalog.streaming_pdf import MAX_PDF_BYTES, valid_original_size
        from ..audio.jobs import regular_file
        profile = None
        if record['version'] == 2:
            from .streamed_text import profile_from_value
            profile = profile_from_value(record['source_profile'])
        source_path = Path(record['source'])
        if record.get('asset_sha256'):
            from .retained_assets import RetainedAssets
            source_path = RetainedAssets(library / 'originals').resolve(
                record['asset_sha256'], profile=profile, cancelled=cancelled)
        source_info = regular_file(source_path)
        source_mime = 'application/pdf' if source_path.suffix.casefold() == '.pdf' else ''
        if (source_info.st_nlink != 1
                or (profile is None and not valid_original_size(source_mime, source_info.st_size,
                    pdf_max_bytes=record.get('pdf_max_bytes', MAX_PDF_BYTES)))
                or (profile is not None and (source_path.suffix.casefold() != '.txt'
                    or not 0 < source_info.st_size <= profile.maximum_bytes))):
            raise ValueError('Document original exceeds its declared format byte budget.')
        from ..catalog.intake import MAX_BYTES
        pdf_limit = (min(record.get('pdf_max_bytes', MAX_PDF_BYTES),
                         max(MAX_BYTES, source_info.st_size)) if source_mime == 'application/pdf' else None)
        request = document_request(library, Path(gettempdir()), selected.limits,
                                   original_bytes=pdf_limit or source_info.st_size)
        if profile is not None:
            from dataclasses import replace
            # Original, escaped checkpoints, authoritative pages and index scratch.
            request = replace(request, disk_bytes=max(request.disk_bytes,
                                profile.maximum_bytes * 10 + 128 * 1024**2))
        admission = selected.reserve(request, operation_id='document:' + request_id,
                                     cancelled=cancelled, timeout=60)
        return _execute_request(root, workspace, record, request_id,
                                admission=admission, timeout=timeout, pdf_limit=pdf_limit)
    except ResourceCancelled:
        result = dict(state='cancelled', message='Import stopped while waiting for shared resources.',
                      request_id=request_id, isolated=True)
        record_progress(root, request_id, state='cancelled', result=result)
        return result


def _execute_request(root, workspace, record, request_id, *, admission, timeout, pdf_limit=None):
    # The resource reservation spans the actual child and its owned cleanup.
    # Receipt reconciliation can perform another accepted operation and must
    # not retain the completed child's full-pool compute reservation.
    from .publication_identity import request_identity
    expected = request_identity(record)
    with admission as lease:
        current = load_request(root, request_id)
        if request_identity(current) != expected:
            raise ValueError('The import request changed while waiting for shared resources.')
        record = current
        from ..application import LibraryApp
        from .publication_identity import request_identity
        app = LibraryApp(root)
        # This value is fresh for every native launch, including resumed requests.
        # Only the worker that commits a new revision can persist its receipt.
        attempt_id = uuid4().hex
        fingerprint = sha256(request_identity(record).encode('utf-8')).hexdigest()
        # A new parent attempt clears old hints even when cancelled before launch.
        record_progress(root, request_id, attempt=attempt_id, publication_candidate=None)
        started = False

        def worker_started():
            nonlocal started
            started = True

        # The launch identity is frozen before the native child reads the sidecar.
        # Release this authorization snapshot before parsing or OCR begins.
        with app.read(workspace):
            pass
        record_progress(root, request_id, state='running')
        message = {'data_dir': str(Path(root).resolve()), 'request_id': request_id,
                   'attempt_id': attempt_id, 'request_sha256': fingerprint,
                   'source': record['source'], 'timeout': timeout}
        if pdf_limit is not None:
            message['pdf_max_bytes'] = pdf_limit
        result = _invoke(message,
                         timeout=timeout, cancelled=lease.cancelled,
                         on_started=worker_started)
    if started and result.get('state') in ('cancelled', 'timed_out', 'failed'):
        try:
            committed = _reconcile_publication(root, request_id, attempt_id, initial=record)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            result = dict(state='failed', message=
                          'The stopped import could not verify its publication. Review or resume this request.')
        else:
            if committed is not None:
                result = dict(committed, worker_state=result['state'])
    result = dict(result, request_id=request_id, isolated=True)
    record_progress(root, request_id, state=result.get('state', 'failed'), result=result)
    return result


def _invoke(message, *, timeout, cancelled=lambda: False, on_started=None):
    """One process supervisor for import and render operations."""
    from ..audio.engines import _start_process
    payload = canonical(message).encode('utf-8')
    if len(payload) > MAX_REQUEST:
        raise ValueError('Import request is too large.')
    if cancelled():
        return {'state': 'cancelled', 'message': 'Import cancelled before native parsing.'}
    result = None
    with ExitStack() as owned:
        # Reopen the bounded request with actual read-only native rights.
        # Every stream outlives the owned family, including constructor failure.
        temporary = owned.enter_context(TemporaryDirectory(prefix='carr-worker-request-'))
        request_path = Path(temporary) / 'request.json'
        with request_path.open('xb') as destination:
            destination.write(payload)
        input_stream = owned.enter_context(request_path.open('rb'))
        output = owned.enter_context(TemporaryFile())
        errors = owned.enter_context(TemporaryFile())
        # Isolated Python ignores CWD, user-site and inherited PYTHONPATH.
        # Only paths derived from this trusted application file are added.
        package_root = Path(__file__).resolve().parents[2]
        roots = [str(package_root)]
        if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
            roots.append(str(package_root.parent))
        bootstrap = ("import sys,runpy; sys.path[:0]=sys.argv[1:]; "
                     "runpy.run_module('carr_library.ingest.worker',run_name='__main__')")
        process = _start_process(
            [sys.executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap, *roots],
            stdin=input_stream, stdout=output, stderr=errors, shell=False,
            env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'))
        owned.callback(_stop, process)
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
        # A completed leader is not proof that its descendants stopped. Settle
        # the complete native owner before accepting bytes or releasing streams.
        _stop(process)
        if result is None and (os.fstat(output.fileno()).st_size > MAX_REPLY
                               or os.fstat(errors.fileno()).st_size > MAX_LOG):
            result = {'state': 'failed', 'message': 'Import worker output exceeded its limit.'}
        if result is None:
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
                failure = reply.get('error') if isinstance(reply, dict) else None
                result = {'state': 'failed', 'message': failure or
                          f'Import worker exited unexpectedly ({process.returncode}).'}
            else:
                result = reply['result']
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
    from ..catalog.streaming_pdf import FileAsset
    if not isinstance(data, FileAsset) and (type(data) is not bytes or not 0 < len(data) <= 64 * 1024 * 1024):
        raise ValueError('Invalid PDF review bytes.')
    if type(physical) is not int or not 1 <= physical <= 1000:
        raise ValueError('Invalid PDF review page.')
    if isinstance(data, FileAsset):
        # The parent retains the verified read handle across the owned child.
        # Windows denies mutation; POSIX revalidates its digest after rendering.
        with data.open(cancelled=cancelled):
            result = _invoke(dict(action='render', source=str(data.path), sha256=data.digest,
                                  source_size=data.size, source_identity=list(data.identity),
                                  physical=physical, timeout=30), timeout=30, cancelled=cancelled)
    else:
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
        import_keys = {'data_dir', 'request_id', 'attempt_id', 'request_sha256', 'source', 'timeout'}
        file_import_keys = import_keys | {'pdf_max_bytes'}
        render_keys = {'action', 'source', 'sha256', 'physical', 'timeout'}
        file_render_keys = render_keys | {'source_size', 'source_identity'}
        conversion_keys = {'action', 'source', 'output', 'report', 'sha256', 'source_size',
                           'budget', 'ocr_config', 'timeout'}
        conversion_adapter_keys = conversion_keys | {'adapter'}
        if not isinstance(message, dict) or set(message) not in (import_keys, file_import_keys, render_keys, file_render_keys, conversion_keys, conversion_adapter_keys):
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
        if set(message) in (conversion_keys, conversion_adapter_keys):
            if message['action'] != 'convert':
                raise ValueError('Invalid document conversion action.')
            from ..conversion.worker import convert_request
            with redirect_stdout(sys.stderr):
                result = convert_request(message)
            print(canonical({'result': result}))
            return 0
        if set(message) in (render_keys, file_render_keys):
            if message['action'] != 'render' or not isinstance(message['source'], str):
                raise ValueError('Invalid render request.')
            source = Path(message['source'])
            if set(message) == file_render_keys:
                from ..catalog.streaming_pdf import FileAsset
                data = FileAsset.capture(source,
                    expected_manifest=dict(sha256=message['sha256'], mime='application/pdf',
                                           size=message['source_size']),
                    expected_identity=message['source_identity'])
            else:
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
        from .publication_identity import PublicationAttempt, request_identity
        identity = request_identity(record)
        attempt = PublicationAttempt(message['attempt_id'], identity)
        attempt.request()
        if (record['source'] != message['source']
                or record.get('attempt') != message['attempt_id'] or
                sha256(identity.encode('utf-8')).hexdigest() != message['request_sha256']):
            raise ValueError('Import request changed before this worker attempt.')
        from ..application import LibraryApp
        from ..extract.page_ocr import Config
        from ..catalog.streaming_pdf import MAX_PDF_BYTES, pdf_budget
        pdf_limit = record.get('pdf_max_bytes', MAX_PDF_BYTES)
        if record['version'] == 2 and 'pdf_max_bytes' in message:
            raise ValueError('Streamed text cannot use a PDF launch allowance.')
        if 'pdf_max_bytes' in message:
            admitted = pdf_budget(message['pdf_max_bytes'])
            if admitted > pdf_limit:
                raise ValueError('PDF launch allowance exceeds the immutable request budget.')
            pdf_limit = admitted
        metadata = dict(record['metadata'])
        if not isinstance(metadata['authors'], list) or not isinstance(metadata['collections'], list):
            raise ValueError('Invalid import metadata sequences.')
        metadata['authors'] = tuple(metadata['authors'])
        metadata['collections'] = tuple(metadata['collections'])
        with redirect_stdout(sys.stderr):
            app = LibraryApp(message['data_dir'])
            source = record['source']
            if record.get('asset_sha256'):
                source = app.assets.resolve(record['asset_sha256'], profile=record.get('source_profile'))
            result = app._import_file(record['workspace'], source, **metadata,
                                      config=Config(**record['config']),
                                      _request_id=record['request_id'], _resume_job=record.get('job'),
                                      _expected_manifest=record.get('expected_manifest'),
                                      _publication_attempt=attempt,
                                      pdf_max_bytes=pdf_limit if record['version'] == 1 else None,
                                      _source_profile=record.get('source_profile'))
        print(canonical({'result': result}))
        return 0
    except Exception as exc:
        print(canonical({'error': f'{type(exc).__name__}: {str(exc)[:2000]}'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
