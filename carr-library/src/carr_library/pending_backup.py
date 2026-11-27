"""Bounded, identity-preserving snapshots of the legacy import journals.

The caller holds ``exclusive_writer(app.root)`` during ``snapshot_pending``.
The directory is a component of the enclosing manifest-bound library backup,
not a standalone source of trust. Restore validates against the already checked
``stage/library.sqlite``. It never opens an external path recorded by a backup.

Only source locations and the data-directory binding change during restore.
Jobs, checkpoints, reviews, workspaces, editions and extraction identities stay
byte-for-byte intact. Missing original bytes fail the snapshot explicitly.
"""
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import time

from .catalog.intake import MAX_BYTES, classify
from .durable_state import local_path, regular, sync_directory
from .extract.page_ocr import Config
from .ingest.batch_scan import planned_snapshot
from .ingest.batches import MAX_BATCH, MAX_BATCHES, MAX_ROWS, STATES, _BATCH_KEYS, _ROW_KEYS
from .ingest.pipeline import canonical, digest
from .ingest.retained_assets import SUFFIXES
from .ingest.worker import FIELDS, MAX_REQUEST, MAX_REVIEWS
from .storage.schema_identity import expected_schema_fingerprint, schema_fingerprint

MAX_JOURNAL = 512 * 1024**2
MAX_FILES = 25_000
MAX_TOTAL = 8 * 1024**3
MAX_JOBS = 10_000
MAX_CHECKPOINTS = 100_000
MAX_PAYLOAD = 16 * 1024**2
_ID = re.compile(r'[0-9a-f]{32}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_REQUEST_REQUIRED = {'version', 'request_id', 'data_dir', 'workspace', 'source',
                     'metadata', 'config', 'state'}
_REQUEST_OPTIONAL = {'asset_sha256', 'job', 'expected_manifest', 'result',
                     'attempt', 'publication_candidate', 'publication_watermark'}
_JOB_STATES = {'pending', 'cancelled', 'failed', 'quarantined', 'complete', 'superseded', 'conflict'}
_REQUEST_STATES = _JOB_STATES | {'running', 'timed_out'}
_SCHEMA = '''
CREATE TABLE job(id TEXT PRIMARY KEY, workspace TEXT NOT NULL,
  edition TEXT NOT NULL, asset TEXT NOT NULL, config TEXT NOT NULL,
  count INTEGER NOT NULL, expected_active TEXT, state TEXT NOT NULL,
  error TEXT);
CREATE TABLE checkpoint(job TEXT NOT NULL REFERENCES job(id),
  ordinal INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
  PRIMARY KEY(job,ordinal));
'''


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Pending import backup cancelled.')


def _text(value, limit=32768, *, empty=False):
    return isinstance(value, str) and len(value) <= limit and '\x00' not in value and (empty or bool(value))


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _source(value):
    """Validate a saved location without accessing it, including Windows paths."""
    if not _text(value) or value.startswith(('//', '\\\\')):
        raise ValueError('Invalid local pending source path.')
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid local pending source path.')
    return value


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate pending journal JSON key.')
        value[key] = item
    return value


def _decode(raw, *, newline=True):
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON.')))
        if (canonical(value) + ('\n' if newline else '')).encode('utf-8') != raw:
            raise ValueError('Pending journal JSON is not canonical.')
    except (UnicodeError, TypeError, RecursionError) as exc:
        raise ValueError('Invalid pending journal JSON.') from exc
    return value


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read(path, maximum):
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('Pending import file exceeds its size budget.')
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Pending import file changed during read.')
        raw = stream.read(maximum + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Pending import file changed during read.')
    if len(raw) != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('Pending import file changed during read.')
    return raw


def _write(path, raw):
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {'filename': path.name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def _copy(source, target, entry, cancelled):
    """Bounded stable copy, with the expected size and hash checked before use."""
    source = local_path(source, exists=True)
    before = regular(source)
    if before.st_size != entry['size_bytes']:
        raise ValueError('Pending import backup size mismatch.')
    target = local_path(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    hashed, size = sha256(), 0
    with source.open('rb') as read, target.open('xb') as write:
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise ValueError('Pending import backup changed during read.')
        while block := read.read(1024 * 1024):
            _check(cancelled)
            size += len(block)
            if size > entry['size_bytes']:
                raise ValueError('Pending import backup grew during read.')
            hashed.update(block)
            write.write(block)
        write.flush()
        os.fsync(write.fileno())
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise ValueError('Pending import backup changed during read.')
    if (_stamp(regular(source)) != _stamp(before) or size != entry['size_bytes']
            or hashed.hexdigest() != entry['sha256']):
        raise ValueError('Pending import backup checksum mismatch.')


def _entry(path, name, cancelled):
    before = regular(path)
    hashed, size = sha256(), 0
    with Path(path).open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('Pending import file changed during hashing.')
        while block := stream.read(1024 * 1024):
            _check(cancelled)
            size += len(block)
            if size > before.st_size:
                raise ValueError('Pending import file grew during hashing.')
            hashed.update(block)
    if _stamp(regular(path)) != _stamp(before) or size != before.st_size:
        raise ValueError('Pending import file changed during hashing.')
    return {'filename': name, 'size_bytes': size, 'sha256': hashed.hexdigest()}


def _manifest(value):
    if (not isinstance(value, dict) or set(value) != {'sha256', 'mime', 'size'}
            or not _hash(value['sha256']) or value['mime'] not in SUFFIXES
            or type(value['size']) is not int or not 0 < value['size'] <= MAX_BYTES):
        raise ValueError('Invalid pending source manifest.')
    return value


def _config(value):
    if not isinstance(value, dict) or set(value) != set(asdict(Config())):
        raise ValueError('Invalid pending extraction configuration.')
    if any(type(value[key]) is not int for key in ('dpi', 'timeout', 'max_pixels')):
        raise ValueError('Invalid pending extraction resource limits.')
    Config(**value)


def _catalog(path):
    path = local_path(path, exists=True)
    if not 0 < regular(path).st_size <= MAX_JOURNAL:
        raise ValueError('Invalid catalog for pending import recovery.')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (3, 4, 5) or schema_fingerprint(db) != expected_schema_fingerprint(version):
            raise ValueError('Pending imports require the canonical catalog schema.')
    except BaseException:
        db.close()
        raise
    return db


def _workspace(db, workspace):
    if not _text(workspace, 200) or db.execute('SELECT 1 FROM workspace_meta WHERE workspace=?', (workspace,)).fetchone() is None:
        raise ValueError('Pending import belongs to a foreign workspace.')


def _metadata(value, db, workspace):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('Invalid pending request metadata.')
    for key in ('work_id', 'edition_id'):
        if not _text(value[key], 200):
            raise ValueError('Invalid pending bibliographic identity.')
    if not _text(value['title'], 4000, empty=True):
        raise ValueError('Invalid pending title.')
    for key in ('language', 'edition', 'volume'):
        if value[key] is not None and not _text(value[key], 4000, empty=True):
            raise ValueError('Invalid pending metadata text.')
    for key in ('authors', 'collections'):
        items = value[key]
        if (not isinstance(items, list) or len(items) > 128
                or any(not _text(item, 4000 if key == 'authors' else 200) for item in items)):
            raise ValueError('Invalid pending metadata sequence.')
    if len(set(value['collections'])) != len(value['collections']):
        raise ValueError('Duplicate pending collection.')
    for collection in value['collections']:
        if db.execute('SELECT 1 FROM catalog_collection WHERE workspace=? AND id=?', (workspace, collection)).fetchone() is None:
            raise ValueError('Pending import collection belongs to another workspace.')
    edition = db.execute('SELECT work,asset FROM catalog_edition WHERE workspace=? AND id=?',
                         (workspace, value['edition_id'])).fetchone()
    if edition is not None and edition['work'] != value['work_id']:
        raise ValueError('Pending edition/work identity differs from the catalog.')


def _result(value, workspace, edition, db, *, complete=False):
    if value is None and not complete:
        return
    if not isinstance(value, dict) or len(canonical(value).encode('utf-8')) > MAX_REQUEST:
        raise ValueError('Invalid pending import result.')
    if value.get('workspace', workspace) != workspace or value.get('edition', edition) != edition:
        raise ValueError('Pending result has a foreign workspace or edition.')
    if complete:
        revision = value.get('id')
        if value.get('state') != 'complete' or not _hash(revision):
            raise ValueError('Invalid complete import result.')
        if db.execute('SELECT 1 FROM revision JOIN sealed ON sealed.revision=revision.id '
                      'WHERE revision.id=? AND workspace=? AND edition=?',
                      (revision, workspace, edition)).fetchone() is None:
            raise ValueError('Import completion has no matching sealed publication.')


def _request(record, request_id, data_dir, db):
    if (not isinstance(record, dict) or not _REQUEST_REQUIRED <= set(record)
            or set(record) - _REQUEST_REQUIRED - _REQUEST_OPTIONAL
            or type(record['version']) is not int or record['version'] != 1
            or record['request_id'] != request_id or not _id(request_id)
            or record['data_dir'] != data_dir or record['state'] not in _REQUEST_STATES):
        raise ValueError('Invalid pending request schema or data-directory binding.')
    _source(record['source'])
    _workspace(db, record['workspace'])
    _metadata(record['metadata'], db, record['workspace'])
    _config(record['config'])
    if 'expected_manifest' in record:
        _manifest(record['expected_manifest'])
    # Earlier requests may retain this scalar. It is accepted only for backup
    # compatibility; import completion is authorized solely by the DB receipt.
    boundary = record.get('publication_watermark')
    if boundary is not None and (type(boundary) is not int or not 0 <= boundary < 2 ** 63):
        raise ValueError('Invalid pending request publication boundary.')
    for key in ('asset_sha256', 'job'):
        if key in record and not _hash(record[key]):
            raise ValueError('Invalid pending request journal identity.')
    if ('asset_sha256' in record and 'expected_manifest' in record
            and record['asset_sha256'] != record['expected_manifest']['sha256']):
        raise ValueError('Pending request source identity changed.')
    if 'attempt' in record and not _id(record['attempt']):
        raise ValueError('Invalid pending import attempt identity.')
    candidate = record.get('publication_candidate')
    if candidate is not None:
        if (not isinstance(candidate, dict)
                or set(candidate) != {'attempt', 'revision', 'sha256', 'completed', 'manifest'}
                or not _id(candidate['attempt']) or candidate['attempt'] != record.get('attempt')
                or not _hash(candidate['revision']) or not _hash(candidate['sha256'])
                or type(candidate['completed']) is not int or candidate['completed'] < 1):
            raise ValueError('Invalid pending import publication candidate.')
        manifest = _manifest(candidate['manifest'])
        if (manifest['sha256'] != record.get('asset_sha256')
                or (record.get('expected_manifest') is not None
                    and manifest != record['expected_manifest'])
                or (record.get('job') is not None and candidate['revision'] != record['job'])):
            raise ValueError('Pending publication candidate has a foreign source or journal identity.')
    _result(record.get('result'), record['workspace'], record['metadata']['edition_id'], db,
            complete=record['state'] == 'complete')


def _batch(record, batch_id, data_dir, db):
    if (not isinstance(record, dict) or set(record) != _BATCH_KEYS
            or type(record['version']) is not int or record['version'] != 1
            or record['batch_id'] != batch_id or not _id(batch_id) or record['data_dir'] != data_dir
            or record['state'] not in ('queued', 'running', 'paused', 'attention', 'complete')
            or type(record['recursive']) is not bool
            or not _text(record['created'], 64) or not _text(record['updated'], 64)
            or not isinstance(record['rows'], list) or len(record['rows']) > MAX_ROWS
            or not isinstance(record['collections'], list) or len(record['collections']) > 128
            or any(not _text(item, 200) for item in record['collections'])
            or len(set(record['collections'])) != len(record['collections'])):
        raise ValueError('Invalid pending batch schema or data-directory binding.')
    _workspace(db, record['workspace'])
    _config(record['config'])
    seen = {key: set() for key in ('row_id', 'work_id', 'edition_id', 'request_id')}
    sources = {}
    for row in record['rows']:
        if (not isinstance(row, dict) or set(row) != _ROW_KEYS or row['state'] not in STATES
                or not _text(row['file']) or not _text(row['title'], 4000, empty=True)
                or not _text(row['message'], 4000, empty=True)):
            raise ValueError('Invalid pending batch row.')
        for key, known in seen.items():
            if not _id(row[key]) or row[key] in known:
                raise ValueError('Duplicate pending batch identity.')
            known.add(row[key])
        # Rejected selections are inert audit records and can describe a path
        # that the scanner explicitly refused. They are never read or rebound.
        if row['manifest'] is None:
            if row['state'] != 'rejected' or not _text(row['source']):
                raise ValueError('A pending source is missing its planned identity.')
        else:
            _source(row['source'])
            _manifest(row['manifest'])
            if row['source'] in sources and sources[row['source']] != row['manifest']:
                raise ValueError('Conflicting pending source location in one batch.')
            sources[row['source']] = row['manifest']
        _metadata(dict(title=row['title'], authors=[], language=None, edition=None, volume=None,
                       collections=record['collections'], work_id=row['work_id'], edition_id=row['edition_id']),
                  db, record['workspace'])
        _result(row['result'], record['workspace'], row['edition_id'], db, complete=row['state'] == 'complete')
    for row in record['rows']:
        duplicate = row['duplicate_of']
        if duplicate is None:
            continue
        if not _text(duplicate) or row['manifest'] is None:
            raise ValueError('Invalid pending duplicate binding.')
        if duplicate.startswith('existing:'):
            if duplicate != 'existing:' + row['manifest']['sha256'] or db.execute(
                    'SELECT 1 FROM catalog_edition WHERE workspace=? AND asset=?',
                    (record['workspace'], row['manifest']['sha256'])).fetchone() is None:
                raise ValueError('Pending duplicate belongs to another workspace.')
        elif sources.get(duplicate) != row['manifest']:
            raise ValueError('Pending duplicate no longer identifies its planned source.')
    counts = dict.fromkeys(STATES, 0)
    counts.update(Counter(row['state'] for row in record['rows']))
    if (type(record['total']) is not int or record['total'] != len(record['rows'])
            or record['counts'] != counts or any(type(v) is not int for v in record['counts'].values())):
        raise ValueError('Pending batch summary differs from its rows.')


def _journal_schema(db):
    def signature(connection):
        return [(row[0], row[1], row[2], ' '.join(row[3].split()) if row[3] else None)
                for row in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.executescript(_SCHEMA)
        if signature(db) != signature(expected) or db.execute('PRAGMA user_version').fetchone()[0] != 0:
            raise ValueError('Pending import journal has an unsupported schema.')
    if db.execute('PRAGMA quick_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Pending import journal integrity failed.')


def _journal(path, catalog, cancelled):
    """Read-only validation; never CREATE tables in an imported database."""
    path = local_path(path, exists=True)
    if not 0 < regular(path).st_size <= MAX_JOURNAL:
        raise ValueError('Pending import journal exceeds its byte budget.')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    deadline = time.monotonic() + 60
    db.set_progress_handler(lambda: int(cancelled() or time.monotonic() > deadline), 10_000)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        _journal_schema(db)
        if (db.execute('SELECT count(*) FROM job').fetchone()[0] > MAX_JOBS
                or db.execute('SELECT count(*) FROM checkpoint').fetchone()[0] > MAX_CHECKPOINTS):
            raise ValueError('Pending import journal exceeds its row budget.')
        db.row_factory = sqlite3.Row
        jobs = {}
        for item in db.execute('SELECT * FROM job ORDER BY id'):
            _check(cancelled)
            row = dict(item)
            if (not _hash(row['id']) or not _hash(row['asset']) or not _text(row['edition'], 200)
                    or not _text(row['config'], 128_000) or type(row['count']) is not int
                    or not 1 <= row['count'] <= 1000 or row['state'] not in _JOB_STATES
                    or (row['error'] is not None and not _text(row['error'], 4000, empty=True))):
                raise ValueError('Invalid pending journal job.')
            _workspace(catalog, row['workspace'])
            settings = _decode(row['config'].encode('utf-8'), newline=False)
            if not isinstance(settings, dict) or settings.get('kind') not in ('logical_segment', 'physical_page'):
                raise ValueError('Invalid pending journal extraction identity.')
            if settings['kind'] == 'logical_segment':
                if settings != {'adapter': 'strict-utf8-v1', 'kind': 'logical_segment'}:
                    raise ValueError('Unsupported pending text extraction identity.')
            else:
                required = {'adapter', 'kind', 'config', 'pypdf', 'pdfium'}
                if (set(settings) not in (required, required | {'runtime_identity'})
                        or settings['adapter'] != 'page_ocr-v1'
                        or not _text(settings['pypdf'], 128) or not _text(settings['pdfium'], 128)):
                    raise ValueError('Unsupported pending PDF extraction identity.')
                _config(settings['config'])
                if 'runtime_identity' in settings and not isinstance(settings['runtime_identity'], dict):
                    raise ValueError('Invalid pending PDF runtime identity.')
            if row['id'] != digest(canonical([row['workspace'], row['edition'], row['asset'], row['config']])):
                raise ValueError('Pending journal job identity mismatch.')
            if row['expected_active'] is not None and (not _hash(row['expected_active']) or catalog.execute(
                    'SELECT 1 FROM revision WHERE id=? AND workspace=? AND edition=?',
                    (row['expected_active'], row['workspace'], row['edition'])).fetchone() is None):
                raise ValueError('Pending job expected revision has a foreign identity.')
            publication = catalog.execute('SELECT * FROM revision WHERE id=?', (row['id'],)).fetchone()
            if publication is not None and (publication['workspace'], publication['edition'], publication['asset'], publication['config']) != (
                    row['workspace'], row['edition'], row['asset'], row['config']):
                raise ValueError('Pending job differs from its published identity.')
            completed = 0
            for completed, checkpoint in enumerate(db.execute('SELECT * FROM checkpoint WHERE job=? ORDER BY ordinal', (row['id'],)), 1):
                _checkpoint(checkpoint, row, completed, settings['kind'])
                if publication is not None:
                    payload = _decode(checkpoint['payload'].encode('utf-8'), newline=False)
                    stored = catalog.execute('SELECT * FROM page WHERE revision=? AND ordinal=?', (row['id'], completed)).fetchone()
                    if stored is None or (stored['raw'], stored['digest'], stored['physical'], stored['printed'], stored['verified']) != (
                            payload['raw_text'], digest(payload['raw_text']), payload['physical_index'], None, 0):
                        raise ValueError('Pending checkpoint differs from the immutable published page.')
            if completed > row['count']:
                raise ValueError('Pending checkpoint count exceeds the planned source.')
            if row['state'] in ('complete', 'superseded') and publication is None:
                raise ValueError('Completed import journal has no publication.')
            if publication is not None and (completed != row['count'] or catalog.execute(
                    'SELECT 1 FROM sealed WHERE revision=?', (row['id'],)).fetchone() is None
                    or catalog.execute('SELECT count(*) FROM page WHERE revision=?', (row['id'],)).fetchone()[0] != completed):
                raise ValueError('Published pending import has incomplete checkpoints.')
            jobs[row['id']] = row
        return db, jobs
    except BaseException:
        db.close()
        raise


def _checkpoint(checkpoint, job, ordinal, kind):
    raw = checkpoint['payload']
    if (type(checkpoint['ordinal']) is not int or checkpoint['ordinal'] != ordinal
            or not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_PAYLOAD
            or not _hash(checkpoint['digest']) or digest(raw) != checkpoint['digest']):
        raise ValueError('Pending checkpoint integrity failure.')
    value = _decode(raw.encode('utf-8'), newline=False)
    fields = {'physical_index', 'printed_label', 'printed_label_verified', 'raw_text', 'status', 'blocks', 'warnings', 'provenance'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['physical_index'] != (ordinal if kind == 'physical_page' else None)
            or (kind == 'physical_page' and type(value['physical_index']) is not int)
            or value['printed_label'] is not None or value['printed_label_verified'] is not False
            or not _text(value['raw_text'], MAX_PAYLOAD, empty=True)
            or value['status'] not in ('candidate', 'blank', 'review_required')
            or not isinstance(value['blocks'], list) or not isinstance(value['warnings'], list)
            or not isinstance(value['provenance'], dict) or value['provenance'].get('asset_sha256') != job['asset']):
        raise ValueError('Pending checkpoint source or locator identity mismatch.')


def _inventory(root):
    """Recognize canonical committed sidecars; ignore only known transient names."""
    found = {}
    for name in ('import-requests', 'import-batches'):
        directory = root / name
        if not os.path.lexists(directory):
            continue
        local_path(directory, exists=True)
        regular(directory, directory=True)
        for path in directory.iterdir():
            if path.name == 'reviews' and name == 'import-requests':
                regular(path, directory=True)
                for review in path.iterdir():
                    info = regular(review)
                    if re.fullmatch('[0-9a-f]{32}\\.tmp', review.name):
                        continue
                    if not re.fullmatch('[0-9a-f]{32}\\.json', review.name):
                        raise ValueError('Unrecognized pending review journal file.')
                    found[review.relative_to(root).as_posix()] = _stamp(info)
                    if len(found) > MAX_FILES:
                        raise ValueError('Too many pending import sidecars.')
                continue
            info = regular(path)
            if re.fullmatch('[0-9a-f]{32}(?:-[0-9a-f]{32})?\\.tmp|[0-9a-f]{32}\\.lock', path.name):
                continue
            if not re.fullmatch('[0-9a-f]{32}\\.json', path.name):
                raise ValueError('Unrecognized pending import journal file.')
            found[path.relative_to(root).as_posix()] = _stamp(info)
            if len(found) > MAX_FILES:
                raise ValueError('Too many pending import sidecars.')
    return found


def _records(root, data_dir, db, names):
    requests, batches, reviews = {}, {}, {}
    for name in sorted(names):
        if name == 'imports.sqlite' or name.startswith('originals/'):
            continue
        path = root / name
        if name.startswith('import-batches/'):
            value = _decode(_read(path, MAX_BATCH))
            _batch(value, path.stem, data_dir, db)
            batches[path.stem] = value
        elif name.startswith('import-requests/reviews/'):
            reviews[path.stem] = _decode(_read(path, MAX_REVIEWS))
        else:
            value = _decode(_read(path, MAX_REQUEST))
            _request(value, path.stem, data_dir, db)
            requests[path.stem] = value
    if len(batches) > MAX_BATCHES:
        raise ValueError('Too many pending import batches.')
    return requests, batches, reviews


def _relations(requests, batches, reviews, jobs, journal):
    for request in requests.values():
        if 'job' in request:
            job = jobs.get(request['job'])
            if job is None or (job['workspace'], job['edition'], job['asset']) != (
                    request['workspace'], request['metadata']['edition_id'], request.get('asset_sha256')):
                raise ValueError('Pending request has a foreign journal job.')
    for batch in batches.values():
        for row in batch['rows']:
            request = requests.get(row['request_id'])
            if request is None:
                if row['state'] in ('running', 'complete', 'quarantined'):
                    raise ValueError('Pending batch is missing its prepared import request.')
                continue
            expected = dict(workspace=batch['workspace'], source=row['source'], config=batch['config'],
                            expected_manifest=row['manifest'], metadata=dict(title=row['title'], authors=[], language=None,
                            edition=None, volume=None, collections=batch['collections'], work_id=row['work_id'], edition_id=row['edition_id']))
            if any(request.get(key) != value for key, value in expected.items()):
                raise ValueError('Pending request differs from its batch identity.')
    review_fields = {'request_id', 'workspace', 'job', 'edition', 'asset_sha256', 'physical',
                     'raw_sha256', 'checkpoint_sha256', 'renderer', 'render_sha256', 'reviewer'}
    for request_id, records in reviews.items():
        request = requests.get(request_id)
        if not isinstance(records, dict) or len(records) > 1000 or request is None or journal is None:
            raise ValueError('Pending reviews have no matching import request.')
        job = jobs.get(request.get('job'))
        if job is None:
            raise ValueError('Pending review has no matching journal job.')
        for key, review in records.items():
            if (not isinstance(review, dict) or set(review) != review_fields
                    or type(review['physical']) is not int or not 1 <= review['physical'] <= job['count']
                    or key != str(review['physical'])
                    or (review['request_id'], review['workspace'], review['job'], review['edition'], review['asset_sha256']) != (
                        request_id, job['workspace'], job['id'], job['edition'], job['asset'])
                    or not all(_hash(review[field]) for field in ('raw_sha256', 'checkpoint_sha256', 'render_sha256'))
                    or not _text(review['renderer'], 4000) or not _text(review['reviewer'], 512)):
                raise ValueError('Invalid pending review attestation identity.')
            checkpoint = journal.execute('SELECT payload,digest FROM checkpoint WHERE job=? AND ordinal=?',
                                         (job['id'], review['physical'])).fetchone()
            if checkpoint is None:
                raise ValueError('Pending review is missing its extracted checkpoint.')
            payload = _decode(checkpoint['payload'].encode('utf-8'), newline=False)
            if (checkpoint['digest'] != review['checkpoint_sha256']
                    or digest(payload['raw_text']) != review['raw_sha256']
                    or payload['physical_index'] != review['physical'] or payload['status'] != 'review_required'):
                raise ValueError('Pending review differs from its extracted checkpoint.')


def _references(requests, batches):
    for identity, request in sorted(requests.items()):
        yield 'import-requests/' + identity + '.json', None, request['source'], request.get('expected_manifest'), request.get('asset_sha256')
    for identity, batch in sorted(batches.items()):
        for row in batch['rows']:
            if row['manifest'] is not None:
                yield 'import-batches/' + identity + '.json', row['row_id'], row['source'], row['manifest'], None


def _source_checkpoints(asset, raw, jobs, journal):
    """A TXT checkpoint is verifiable against original bytes without parsing."""
    for job in jobs.values():
        if job['asset'] != asset['sha256']:
            continue
        kind = json.loads(job['config'])['kind']
        if (asset['mime'], kind) not in (('text/plain', 'logical_segment'), ('application/pdf', 'physical_page')):
            raise ValueError('Pending journal MIME differs from the source format.')
        if kind == 'logical_segment':
            segments = raw.decode('utf-8-sig').split('\f')
            if len(segments) != job['count'] or not any(item.strip() for item in segments):
                raise ValueError('Pending text source count differs from its journal.')
            for checkpoint in journal.execute('SELECT ordinal,payload FROM checkpoint WHERE job=? ORDER BY ordinal', (job['id'],)):
                payload = json.loads(checkpoint['payload'])
                if (payload['raw_text'] != segments[checkpoint['ordinal'] - 1]
                        or payload['provenance'] != {'asset_sha256': job['asset'], 'method': 'strict_utf8'}
                        or payload['status'] != 'candidate' or payload['blocks'] != [] or payload['warnings'] != []):
                    raise ValueError('Pending text checkpoint differs from the exact original bytes.')


def snapshot_pending(app, target, *, cancelled=lambda: False):
    """Create a self-contained component under the caller's root writer guard."""
    _check(cancelled)
    root = local_path(app.root, exists=True)
    target = local_path(target)
    target.mkdir(mode=0o700)
    before = _inventory(root)
    catalog_path = target.parent / 'library.sqlite'
    if not catalog_path.exists():
        catalog_path = app.database
    journal = None
    files, assets, bindings = [], {}, []
    try:
        with closing(_catalog(catalog_path)) as catalog:
            jobs = {}
            journal_path = root / 'imports.sqlite'
            if os.path.lexists(journal_path):
                for suffix in ('-wal', '-shm', '-journal'):
                    if os.path.lexists(Path(str(journal_path) + suffix)):
                        local_path(Path(str(journal_path) + suffix), exists=True)
                source_db, _ = _journal(journal_path, catalog, cancelled)
                page_size = source_db.execute('PRAGMA page_size').fetchone()[0]
                with closing(source_db), closing(sqlite3.connect(target / 'imports.sqlite')) as backup:
                    def progress(status, remaining, total):
                        _check(cancelled)
                        if total * page_size > MAX_JOURNAL:
                            raise ValueError('Pending journal exceeds its backup budget.')
                    source_db.backup(backup, pages=256, progress=progress)
                    # Online backup copies the source header's WAL flag. Make
                    # the independent snapshot self-contained before read-only
                    # validation, so it cannot create unlisted WAL/SHM files.
                    if backup.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                        raise ValueError('Pending journal snapshot is not self-contained.')
                journal, jobs = _journal(target / 'imports.sqlite', catalog, cancelled)
                files.append(_entry(target / 'imports.sqlite', 'imports.sqlite', cancelled))
            for name, stamp in sorted(before.items()):
                _check(cancelled)
                maximum = MAX_BATCH if name.startswith('import-batches/') else MAX_REVIEWS if '/reviews/' in name else MAX_REQUEST
                raw = _read(root / name, maximum)
                if _stamp(regular(root / name)) != stamp:
                    raise ValueError('Pending import state changed during backup; retry after pausing imports.')
                entry = _write(target / name, raw)
                entry['filename'] = name
                files.append(entry)
            requests, batches, reviews = _records(target, str(root), catalog, before)
            _relations(requests, batches, reviews, jobs, journal)

            def retain(expected=None, known=None, source=None):
                wanted = known or (expected['sha256'] if expected else None)
                if wanted in assets:
                    entry = assets[wanted]
                    if expected is not None and (entry['sha256'], entry['mime'], entry['size_bytes']) != (
                            expected['sha256'], expected['mime'], expected['size']):
                        raise ValueError('Pending source manifests disagree.')
                    return wanted
                retained = None
                if wanted is not None:
                    candidates = [root / 'originals' / (wanted + suffix) for suffix in SUFFIXES.values()]
                    if any(os.path.lexists(path) for path in candidates):
                        retained = app.assets.resolve(wanted)
                try:
                    incoming = planned_snapshot(local_path(retained or source, exists=True)) if retained or source else None
                except FileNotFoundError as exc:
                    raise ValueError('Pending import source bytes are missing; restore the selected original before a complete backup.') from exc
                if incoming is None:
                    raise ValueError('Pending import retained source bytes are missing; a complete backup cannot be created.')
                if wanted is not None and incoming.digest != wanted or expected is not None and incoming.manifest() != expected:
                    raise ValueError('Pending import source changed after planning; original pinned bytes are required.')
                name = incoming.digest + SUFFIXES[incoming.mime]
                entry = _write(target / 'originals' / name, incoming.data)
                entry['filename'] = name
                entry['mime'] = incoming.mime
                assets[incoming.digest] = entry
                files.append({key: value for key, value in dict(entry, filename='originals/' + name).items() if key != 'mime'})
                if sum(item['size_bytes'] for item in files) > MAX_TOTAL:
                    raise ValueError('Pending import backup exceeds its total byte budget.')
                return incoming.digest

            for job in jobs.values():
                retain(known=job['asset'])
            for name, row_id, source, expected, known in _references(requests, batches):
                _check(cancelled)
                asset_hash = retain(expected, known, source)
                bindings.append(dict(filename=name, row_id=row_id, source=source, asset_sha256=asset_hash))
            for asset in assets.values():
                _check(cancelled)
                _source_checkpoints(asset, _read(target / 'originals' / asset['filename'], MAX_BYTES), jobs, journal)
        if _inventory(root) != before:
            raise ValueError('Pending import state changed during backup; retry after pausing imports.')
    finally:
        if journal is not None:
            journal.close()
    result = dict(version=1, data_dir=str(root), files=sorted(files, key=lambda item: item['filename']),
                  assets=sorted(assets.values(), key=lambda item: item['filename']), sources=bindings)
    validate_pending(result)
    sync_directory(target)
    return result


def _component_name(name):
    return isinstance(name, str) and bool(re.fullmatch(
        r'imports\.sqlite|import-requests/(?:reviews/)?[0-9a-f]{32}\.json|'
        r'import-batches/[0-9a-f]{32}\.json|originals/[0-9a-f]{64}\.(?:txt|pdf|docx|epub)', name))


def validate_pending(metadata):
    """Validate enclosing manifest fields before touching component paths."""
    if (not isinstance(metadata, dict) or set(metadata) != {'version', 'data_dir', 'files', 'assets', 'sources'}
            or type(metadata['version']) is not int or metadata['version'] != 1
            or any(not isinstance(metadata[key], list) for key in ('files', 'assets', 'sources'))
            or any(len(metadata[key]) > MAX_FILES for key in ('files', 'assets', 'sources'))):
        raise ValueError('Invalid pending import backup manifest.')
    _source(metadata['data_dir'])
    files, total = {}, 0
    for item in metadata['files']:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or not _component_name(item['filename']) or item['filename'] in files
                or not _hash(item['sha256']) or type(item['size_bytes']) is not int or item['size_bytes'] <= 0):
            raise ValueError('Invalid pending import file manifest.')
        name = item['filename']
        limit = MAX_JOURNAL if name == 'imports.sqlite' else MAX_BYTES if name.startswith('originals/') else (
            MAX_BATCH if name.startswith('import-batches/') else MAX_REVIEWS if '/reviews/' in name else MAX_REQUEST)
        if item['size_bytes'] > limit:
            raise ValueError('Pending import file exceeds its byte budget.')
        files[name] = item
        total += item['size_bytes']
    if total > MAX_TOTAL:
        raise ValueError('Pending import backup exceeds its total byte budget.')
    assets = {}
    for item in metadata['assets']:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256', 'mime'}
                or not _hash(item['sha256']) or item['mime'] not in SUFFIXES
                or item['filename'] != item['sha256'] + SUFFIXES[item['mime']]
                or item['sha256'] in assets or type(item['size_bytes']) is not int or not 0 < item['size_bytes'] <= MAX_BYTES):
            raise ValueError('Invalid pending original manifest.')
        expected = dict(filename='originals/' + item['filename'], size_bytes=item['size_bytes'], sha256=item['sha256'])
        if files.get(expected['filename']) != expected:
            raise ValueError('Pending original is missing from its component manifest.')
        assets[item['sha256']] = item
    if {name for name in files if name.startswith('originals/')} != {'originals/' + item['filename'] for item in assets.values()}:
        raise ValueError('Unlisted pending original bytes.')
    bindings = set()
    for item in metadata['sources']:
        if (not isinstance(item, dict) or set(item) != {'filename', 'row_id', 'source', 'asset_sha256'}
                or item['filename'] not in files or item['asset_sha256'] not in assets
                or not (re.fullmatch(r'import-requests/[0-9a-f]{32}\.json', item['filename']) and item['row_id'] is None
                        or re.fullmatch(r'import-batches/[0-9a-f]{32}\.json', item['filename']) and _id(item['row_id']))):
            raise ValueError('Invalid pending source binding.')
        identity = item['filename'], item['row_id']
        if identity in bindings:
            raise ValueError('Duplicate pending source binding.')
        bindings.add(identity)
        _source(item['source'])


def restore_pending(source, stage, new_root, metadata, *, cancelled=lambda: False):
    """Restore into a private staging root; return extra assets and root paths.

    ``stage/library.sqlite`` must already be the enclosing verified catalog.
    The caller publishes only after this and the other backup components pass.
    ``new_root`` is the final destination, not the temporary staging directory.
    """
    _check(cancelled)
    validate_pending(metadata)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    stage = local_path(stage, exists=True)
    regular(stage, directory=True)
    new_root = local_path(new_root)
    names = set()
    for directory, dirs, filenames in os.walk(source, followlinks=False):
        for name in dirs:
            regular(Path(directory) / name, directory=True)
            if (Path(directory) / name).relative_to(source).as_posix() not in ('originals', 'import-requests', 'import-requests/reviews', 'import-batches'):
                raise ValueError('Unexpected directory in pending import backup.')
        for name in filenames:
            path = Path(directory) / name
            regular(path)
            relative = path.relative_to(source).as_posix()
            if not _component_name(relative):
                raise ValueError('Unexpected file in pending import backup.')
            names.add(relative)
    if names != {entry['filename'] for entry in metadata['files']}:
        raise ValueError('Pending import backup has missing or unlisted files.')
    # Verify into a fresh private component first. No database is queried and no
    # original is accepted until all component hashes have been checked.
    verified = stage / '.pending-import-restore'
    verified.mkdir(mode=0o700)
    for entry in metadata['files']:
        _copy(source / entry['filename'], verified / entry['filename'], entry, cancelled)
    journal = None
    try:
        with closing(_catalog(stage / 'library.sqlite')) as catalog:
            jobs = {}
            if 'imports.sqlite' in names:
                journal, jobs = _journal(verified / 'imports.sqlite', catalog, cancelled)
            requests, batches, reviews = _records(verified, metadata['data_dir'], catalog, names)
            _relations(requests, batches, reviews, jobs, journal)
            assets = {entry['sha256']: entry for entry in metadata['assets']}
            bindings = {(entry['filename'], entry['row_id']): entry for entry in metadata['sources']}
            expected_bindings, referenced = set(), {job['asset'] for job in jobs.values()}
            for name, row_id, old_source, expected, known in _references(requests, batches):
                identity = name, row_id
                expected_bindings.add(identity)
                binding = bindings.get(identity)
                if binding is None or binding['source'] != old_source:
                    raise ValueError('Pending source binding differs from its original request.')
                asset = assets[binding['asset_sha256']]
                if (known is not None and asset['sha256'] != known or expected is not None and
                        (asset['sha256'], asset['mime'], asset['size_bytes']) != (expected['sha256'], expected['mime'], expected['size'])):
                    raise ValueError('Pending source binding would retarget an import.')
                referenced.add(asset['sha256'])
            if set(bindings) != expected_bindings or referenced != set(assets):
                raise ValueError('Pending backup contains missing or unreferenced original bytes.')
            for asset in assets.values():
                raw = _read(verified / 'originals' / asset['filename'], MAX_BYTES)
                if classify(raw, Path(asset['filename']).suffix) != asset['mime'] or sha256(raw).hexdigest() != asset['sha256']:
                    raise ValueError('Pending original MIME or checksum mismatch.')
                _source_checkpoints(asset, raw, jobs, journal)

            # Change only the explicitly listed location fields. Existing
            # status, job, expected manifest, result and identity fields survive.
            for request_id, request in requests.items():
                binding = bindings['import-requests/' + request_id + '.json', None]
                request['data_dir'] = str(new_root)
                request['source'] = str(new_root / 'originals' / assets[binding['asset_sha256']]['filename'])
                # A candidate is bound to one original process attempt and its
                # root/source paths. Restoring is not evidence that it committed;
                # explicit resume verifies the existing source/journal again.
                request.pop('attempt', None)
                request.pop('publication_candidate', None)
                request.pop('publication_watermark', None)
                _write(stage / 'import-requests' / (request_id + '.json'), (canonical(request) + '\n').encode('utf-8'))
            for batch_id, batch in batches.items():
                batch['data_dir'] = str(new_root)
                rebased = {}
                for row in batch['rows']:
                    if row['manifest'] is None:
                        continue
                    binding = bindings['import-batches/' + batch_id + '.json', row['row_id']]
                    replacement = str(new_root / 'originals' / assets[binding['asset_sha256']]['filename'])
                    rebased[row['source']] = replacement
                    row['source'] = replacement
                for row in batch['rows']:
                    if row['duplicate_of'] in rebased:
                        row['duplicate_of'] = rebased[row['duplicate_of']]
                _write(stage / 'import-batches' / (batch_id + '.json'), (canonical(batch) + '\n').encode('utf-8'))
    finally:
        if journal is not None:
            journal.close()
    for name in names:
        if name == 'imports.sqlite' or name.startswith('import-requests/reviews/'):
            entry = next(entry for entry in metadata['files'] if entry['filename'] == name)
            _copy(verified / name, stage / name, entry, cancelled)
    (stage / 'originals').mkdir(mode=0o700, exist_ok=True)
    added = []
    for asset in metadata['assets']:
        target = stage / 'originals' / asset['filename']
        if os.path.lexists(target):
            raw = _read(target, MAX_BYTES)
            if len(raw) != asset['size_bytes'] or sha256(raw).hexdigest() != asset['sha256']:
                raise ValueError('Pending original conflicts with a catalog original.')
        else:
            _copy(verified / 'originals' / asset['filename'], target, asset, cancelled)
            added.append(dict(asset))
    # The temporary verified copy is never a published part of the data root.
    import shutil
    shutil.rmtree(verified)
    sync_directory(stage)
    return {'assets': added, 'paths': [name for name in ('imports.sqlite', 'import-requests', 'import-batches')
                                      if (stage / name).exists()]}
