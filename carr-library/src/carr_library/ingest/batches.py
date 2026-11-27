"""Durable private queues over the existing isolated import requests.

The queue stores bounded metadata, never document bytes. Atomic replacements
and a per-batch process lock coordinate the application's cooperating writers;
as with retained originals, the private directory must not be replaced by an
untrusted process while an operation is running.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

from filelock import FileLock, Timeout

from ..catalog.intake import MAX_BYTES
from ..catalog.membership import Catalog
from ..extract.page_ocr import Config
from ..extract.document_formats import MIME_SUFFIXES
from .pipeline import canonical
from .worker import load_request, prepare_request


MAX_BATCH = 16 * 1024 * 1024
MAX_ROWS = 10_000
MAX_BATCHES = 1_000
STATES = ('queued', 'running', 'complete', 'duplicate', 'rejected', 'failed', 'quarantined', 'excluded')
_ID = re.compile('[0-9a-f]{32}')
_MIMES = frozenset(MIME_SUFFIXES)
_BATCH_KEYS = {'version', 'batch_id', 'data_dir', 'workspace', 'created', 'updated',
               'state', 'collections', 'recursive', 'config', 'rows', 'counts', 'total'}
_ROW_KEYS = {'row_id', 'work_id', 'edition_id', 'request_id', 'source', 'file', 'title',
             'state', 'message', 'manifest', 'duplicate_of', 'result'}
_SCAN_KEYS = {'version', 'roots', 'inventory_sha256', 'inventory_entries', 'offset',
              'next_offset', 'unscanned', 'chain_id', 'page', 'previous_batch_id',
              'next_batch_id', 'options_sha256'}


def _scan_timeout(value=120.0):
    if (type(value) not in (int, float) or not 30 <= value <= 600
            or not math.isfinite(value)):
        raise ValueError('Choose a scan time limit between 30 and 600 seconds.')
    return float(value)


def _options_digest(record):
    fields = {key: record[key] for key in ('workspace', 'collections', 'recursive', 'config')}
    fields['roots'] = record['scan']['roots']
    return sha256(canonical(fields).encode('utf-8')).hexdigest()


def _page_id(chain, page, offset):
    return chain if page == 1 else sha256(f'import-scan-page/1:{chain}:{page}:{offset}'.encode()).hexdigest()[:32]


def _text(value, limit, *, empty=True):
    return (isinstance(value, str) and len(value) <= limit and '\x00' not in value
            and (empty or bool(value)))


def _regular(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError('Import batch files must be regular private files.')
    return info


def _now():
    return datetime.now(timezone.utc).isoformat()


def _copy(value):
    return json.loads(canonical(value))


class ImportBatchStore:
    def __init__(self, app):
        self.app = app
        self.root = app.root
        self._binding = self._identity()

    def _identity(self):
        paths = (self.app.root, self.app.database, self.app.assets_dir, self.app.assets.root)
        identities = []
        for path in paths:
            info = path.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError('Import batches require the original private library folder.')
            identities.append((str(path), info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
        return tuple(identities)

    def _bound(self):
        if self._identity() != self._binding:
            raise ValueError('The library folder changed. Reopen Import Center.')

    def _cancellation(self, cancelled):
        if not callable(cancelled):
            raise ValueError('An import cancellation callback is required.')
        stopped = False
        def check():
            nonlocal stopped
            self._bound()
            stopped = stopped or bool(cancelled())
            self._bound()
            if stopped:
                raise InterruptedError('Import scan cancelled; saved pages were kept.')
            return False
        return check

    def _directory(self):
        self._bound()
        path = self.root / 'import-batches'
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Import batches require a local private directory.')
        return path

    def _path(self, batch_id):
        if not isinstance(batch_id, str) or not _ID.fullmatch(batch_id):
            raise ValueError('Invalid import batch identity.')
        return self._directory() / (batch_id + '.json')

    @contextmanager
    def _lock(self, batch_id):
        path = self._path(batch_id).with_suffix('.lock')
        _regular(path, missing=True)
        try:
            with FileLock(path, mode=0o600).acquire(timeout=0):
                yield
        except Timeout as exc:
            raise ValueError('This import batch is already running.') from exc

    def _authorize(self, workspace, collections=()):
        self._bound()
        with self.app.read(workspace) as db:
            catalog = Catalog(db, workspace)
            for collection in collections:
                catalog._active_collection(collection)
        self._bound()

    def _validate(self, record, batch_id):
        if (not isinstance(record, dict)
                or type(record.get('version')) is not int or record['version'] not in (1, 2)
                or set(record) != (_BATCH_KEYS | ({'scan'} if record['version'] == 2 else set()))
                or record['batch_id'] != batch_id or record['data_dir'] != str(self.root)
                or not _text(record['workspace'], 200, empty=False)
                or not _text(record['created'], 64, empty=False)
                or not _text(record['updated'], 64, empty=False)
                or record['state'] not in ('queued', 'running', 'paused', 'attention', 'complete')
                or type(record['recursive']) is not bool
                or not isinstance(record['collections'], list) or len(record['collections']) > 128
                or any(not _text(c, 200, empty=False) for c in record['collections'])
                or len(set(record['collections'])) != len(record['collections'])
                or not isinstance(record['config'], dict)
                or not isinstance(record['rows'], list) or len(record['rows']) > MAX_ROWS):
            raise ValueError('Invalid import batch schema or data-directory binding.')
        Config(**record['config'])
        if record['version'] == 2:
            self._validate_scan(record)
        seen = {name: set() for name in ('row_id', 'work_id', 'edition_id', 'request_id')}
        for row in record['rows']:
            if (not isinstance(row, dict) or set(row) != _ROW_KEYS
                    or row['state'] not in STATES
                    or not _text(row['source'], 32768, empty=False)
                    or not Path(row['source']).is_absolute()
                    or not _text(row['file'], 32768, empty=False)
                    or not _text(row['title'], 4000)
                    or not _text(row['message'], 4000)
                    or (row['duplicate_of'] is not None and not _text(row['duplicate_of'], 32768))
                    or (row['result'] is not None and (not isinstance(row['result'], dict)
                        or len(canonical(row['result']).encode('utf-8')) > 64_000))):
                raise ValueError('Invalid import batch row.')
            for key, identities in seen.items():
                identity = row[key]
                if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in identities:
                    raise ValueError('Duplicate or invalid import batch row identity.')
                identities.add(identity)
            manifest = row['manifest']
            if manifest is None:
                if row['state'] != 'rejected':
                    raise ValueError('Import batch row is missing its planned manifest.')
            elif (not isinstance(manifest, dict) or set(manifest) != {'sha256', 'mime', 'size'}
                    or not isinstance(manifest['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', manifest['sha256'])
                    or manifest['mime'] not in _MIMES
                    or type(manifest['size']) is not int or not 0 < manifest['size'] <= MAX_BYTES):
                raise ValueError('Invalid import batch source manifest.')
        counts = dict.fromkeys(STATES, 0)
        counts.update(Counter(row['state'] for row in record['rows']))
        if (type(record['total']) is not int or record['total'] != len(record['rows'])
                or not isinstance(record['counts'], dict)
                or any(type(value) is not int for value in record['counts'].values())
                or record['counts'] != counts):
            raise ValueError('Import batch summary differs from its rows.')
        return record

    @staticmethod
    def _validate_scan(record):
        from .batch_scan import MAX_ROOTS, MAX_SCAN_ENTRIES, MAX_PATH_BYTES
        scan = record['scan']
        if (type(scan) is not dict or not _SCAN_KEYS <= set(scan) <= _SCAN_KEYS | {'timeout_seconds'}
                or type(scan['version']) is not int
                or scan['version'] != 1 or type(scan['roots']) is not list or len(scan['roots']) > MAX_ROOTS
                or any(not _text(path, 32768, empty=False) or not Path(path).is_absolute()
                       or '..' in Path(path).parts for path in scan['roots'])
                or len(set(scan['roots'])) != len(scan['roots'])
                or sum(len(path.encode('utf-8')) for path in scan['roots']) > MAX_PATH_BYTES
                or not isinstance(scan['inventory_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', scan['inventory_sha256'])
                or type(scan['inventory_entries']) is not int or not 0 <= scan['inventory_entries'] <= MAX_SCAN_ENTRIES + 1
                or type(scan['offset']) is not int or not 0 <= scan['offset'] <= MAX_SCAN_ENTRIES + MAX_ROOTS
                or type(scan['page']) is not int or not 1 <= scan['page'] <= MAX_SCAN_ENTRIES + MAX_ROOTS
                or not isinstance(scan['chain_id'], str) or not _ID.fullmatch(scan['chain_id'])
                or scan['next_offset'] is not None and (type(scan['next_offset']) is not int
                    or not scan['offset'] < scan['next_offset'] <= MAX_SCAN_ENTRIES + MAX_ROOTS)
                or type(scan['unscanned']) is not list or len(scan['unscanned']) > MAX_ROOTS + 1
                or any(not _text(path, 32768, empty=False) or not Path(path).is_absolute()
                       for path in scan['unscanned'])):
            raise ValueError('Invalid import scan continuation metadata.')
        _scan_timeout(scan.get('timeout_seconds', 120.0))
        if ((scan['page'] == 1) != (scan['offset'] == 0)
                or (scan['page'] == 1) != (scan['previous_batch_id'] is None)
                or scan['previous_batch_id'] is not None and
                   (not isinstance(scan['previous_batch_id'], str) or not _ID.fullmatch(scan['previous_batch_id']))
                or record['batch_id'] != _page_id(scan['chain_id'], scan['page'], scan['offset'])
                or scan['next_batch_id'] != (None if scan['next_offset'] is None else
                    _page_id(scan['chain_id'], scan['page'] + 1, scan['next_offset']))
                or scan['options_sha256'] != _options_digest(record)):
            raise ValueError('The scan cursor differs from its saved selection and options.')

    def _load(self, batch_id):
        path = self._path(batch_id)
        info = _regular(path)
        if info.st_size > MAX_BATCH:
            raise ValueError('Import batch journal exceeds its size limit.')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Import batch journal changed during read.')
            raw = stream.read(MAX_BATCH + 1)
        if len(raw) > MAX_BATCH:
            raise ValueError('Import batch journal exceeds its size limit.')
        try:
            record = json.loads(raw)
            return self._validate(record, batch_id)
        except (KeyError, TypeError, UnicodeError, RecursionError) as exc:
            raise ValueError('Invalid import batch journal.') from exc

    @staticmethod
    def _refresh(record, *, paused=False):
        counts = dict.fromkeys(STATES, 0)
        counts.update(Counter(row['state'] for row in record['rows']))
        record.update(counts=counts, total=len(record['rows']), updated=_now())
        if counts['running']:
            state = 'running'
        elif counts['queued']:
            state = 'paused' if paused else 'queued'
        elif counts['failed'] or counts['quarantined'] or counts['rejected']:
            state = 'attention'
        else:
            state = 'complete'
        record['state'] = state

    def _store(self, record, *, create=False, paused=False):
        self._refresh(record, paused=paused)
        self._validate(record, record['batch_id'])
        raw = (canonical(record) + '\n').encode('utf-8')
        if len(raw) > MAX_BATCH:
            raise ValueError('Import batch journal exceeds its size limit.')
        path = self._path(record['batch_id'])
        existing = _regular(path, missing=True)
        if create and existing is not None:
            raise ValueError('Import batch identity already exists.')
        temporary = path.with_name(path.stem + '-' + uuid4().hex + '.tmp')
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

    def plan(self, workspace, paths, *, collections=(), recursive=True,
             config=None, cancelled=lambda: False, scan_timeout=120.0):
        from .batch_scan import scan_page
        scan_timeout = _scan_timeout(scan_timeout)
        if (not isinstance(collections, (list, tuple)) or len(collections) > 128
                or type(recursive) is not bool or not callable(cancelled)):
            raise ValueError('Invalid import batch options.')
        collections = tuple(dict.fromkeys(collections))
        config = config or Config()
        settings = asdict(config)
        Config(**settings)
        check = self._cancellation(cancelled)
        check()
        self._authorize(workspace, collections)
        page = scan_page(paths, recursive=recursive, existing_assets=self._existing(workspace),
                         cancelled=check, timeout=scan_timeout)
        record = self._planned_record(workspace, collections, recursive, settings, page,
                                      scan_timeout=scan_timeout)
        self._authorize(workspace, collections)
        with self._lock(record['batch_id']):
            check()
            self._authorize(workspace, collections)
            self._store(record, create=True)
        return _copy(record)

    def _existing(self, workspace):
        from .batch_scan import MAX_EXISTING_ASSETS
        with self.app.read(workspace) as db:
            return tuple(row[0] for row in db.execute(
                'SELECT DISTINCT asset FROM catalog_edition WHERE workspace=? LIMIT ?',
                (workspace, MAX_EXISTING_ASSETS + 1)))

    def _planned_record(self, workspace, collections, recursive, settings, page, *,
                        previous=None, scan_timeout=120.0):
        rows = []
        for candidate in page.rows:
            rows.append(dict(candidate, row_id=uuid4().hex, work_id=uuid4().hex,
                             edition_id=uuid4().hex, request_id=uuid4().hex, result=None))
            if len(rows) > MAX_ROWS:
                raise ValueError('Too many files in one import batch.')
        chain = uuid4().hex if previous is None else previous['scan']['chain_id']
        number = 1 if previous is None else previous['scan']['page'] + 1
        record = dict(version=2, batch_id=_page_id(chain, number, page.offset), data_dir=str(self.root),
                      workspace=workspace, created=_now(), updated=_now(), state='queued',
                      collections=list(collections), recursive=recursive, config=settings,
                      rows=rows, counts={}, total=len(rows),
                      scan=dict(version=1, timeout_seconds=scan_timeout,
                        roots=list(page.roots), inventory_sha256=page.inventory_sha256,
                        inventory_entries=page.inventory_entries, offset=page.offset,
                        next_offset=page.next_offset, unscanned=list(page.unscanned),
                        chain_id=chain, page=number, previous_batch_id=None if previous is None else previous['batch_id'],
                        next_batch_id=None if page.next_offset is None else _page_id(chain, number + 1, page.next_offset),
                        options_sha256=''))
        record['scan']['options_sha256'] = _options_digest(record)
        return record

    def next_scan_page(self, workspace, batch_id, *, cancelled=lambda: False, scan_timeout=None):
        """Save one next page, using the original choices; repeat calls reuse its ID.

        Every page remains an ordinary import batch. Neither a failed scan nor a
        cancellation rewrites previous rows or advances their saved continuation.
        A missing time limit reuses the saved budget (120 seconds for legacy
        pages). An explicit retry budget changes no source or content choices.
        """
        from .batch_scan import scan_page
        if scan_timeout is not None:
            scan_timeout = _scan_timeout(scan_timeout)
        check = self._cancellation(cancelled)
        check()
        self._authorize(workspace)
        with self._lock(batch_id):
            previous = self.get(workspace, batch_id)
            from .recovery import require_approval
            require_approval(self.root, 'batch', batch_id, previous)
            scan = previous.get('scan')
            if scan is None or scan['next_offset'] is None:
                raise ValueError('This saved page has no continuation. Select new source folders to scan.')
            selected_timeout = (_scan_timeout(scan.get('timeout_seconds', 120.0))
                                if scan_timeout is None else scan_timeout)
            self._authorize(workspace, previous['collections'])
            page = scan_page(scan['roots'], recursive=previous['recursive'],
                existing_assets=self._existing(workspace), offset=scan['next_offset'],
                expected_inventory=scan['inventory_sha256'], cancelled=check, timeout=selected_timeout)
            record = self._planned_record(workspace, previous['collections'], previous['recursive'],
                previous['config'], page, previous=previous, scan_timeout=selected_timeout)
            self._authorize(workspace, previous['collections'])
            with self._lock(record['batch_id']):
                check()
                self._authorize(workspace, previous['collections'])
                if self._path(record['batch_id']).exists():
                    saved = self.get(workspace, record['batch_id'])
                    # A completed next page is immutable. A retry can spend a
                    # different bounded time checking its identity, but cannot
                    # rewrite the budget under which that page was first saved.
                    saved_scan = {k: v for k, v in saved['scan'].items() if k != 'timeout_seconds'}
                    current_scan = {k: v for k, v in record['scan'].items() if k != 'timeout_seconds'}
                    if (saved_scan != current_scan or saved['collections'] != record['collections']
                            or saved['config'] != record['config'] or saved['recursive'] != record['recursive']
                            or [(row['source'], row['manifest']) for row in saved['rows']]
                               != [(row['source'], row['manifest']) for row in record['rows']]):
                        raise ValueError('The saved next page differs from its source continuation.')
                    return _copy(saved)
                self._store(record, create=True)
                return _copy(record)

    def get(self, workspace, batch_id):
        self._authorize(workspace)
        record = self._load(batch_id)
        if record['workspace'] != workspace:
            raise ValueError('Import batch belongs to another library.')
        return self._reconcile(record)

    def _reconcile(self, record):
        """Refresh completed external resumes without guessing about live jobs.

        Reads observe atomic request replacements; they never rewrite the batch
        held by another running process. The next queue run persists the view.
        """
        changed = False
        size = len(canonical(record).encode('utf-8'))
        for row in record['rows']:
            if row['state'] in ('complete', 'duplicate', 'rejected', 'excluded'):
                continue
            try:
                request = load_request(self.root, row['request_id'])
            except FileNotFoundError:
                continue
            if request.get('state') != 'complete':
                continue
            expected = dict(workspace=record['workspace'], source=row['source'],
                            metadata=_copy(self._metadata(row, record)),
                            config=record['config'], expected_manifest=row['manifest'])
            result = request.get('result')
            if (any(request.get(key) != value for key, value in expected.items())
                    or not isinstance(result, dict) or result.get('state') != 'complete'
                    or result.get('edition') != row['edition_id']):
                raise ValueError('Completed import request differs from its batch identity.')
            replacement = dict(row)
            self._result(replacement, result)
            size += (len(canonical(replacement).encode('utf-8'))
                     - len(canonical(row).encode('utf-8')))
            if size > MAX_BATCH - 1024:
                raise ValueError('Import batch journal exceeds its size limit.')
            row.update(replacement)
            changed = True
        if changed:
            self._refresh(record)
            self._validate(record, record['batch_id'])
        return record

    def list(self, workspace):
        self._authorize(workspace)
        rows = []
        count = 0
        for path in self._directory().glob('*.json'):
            count += 1
            if count > MAX_BATCHES:
                raise ValueError('Too many saved import batches to list safely.')
            record = self._load(path.stem)
            if record['workspace'] == workspace:
                record = self._reconcile(record)
                summary = {key: record[key] for key in
                           ('batch_id', 'created', 'updated', 'state', 'counts', 'total')}
                if 'scan' in record:
                    summary['scan'] = {key: record['scan'][key] for key in ('page', 'next_offset')}
                    summary['scan']['unscanned_count'] = len(record['scan']['unscanned'])
                rows.append(summary)
        return tuple(sorted(rows, key=lambda row: (row['created'], row['batch_id']), reverse=True))

    def set_row(self, workspace, batch_id, row_id, *, included):
        if type(included) is not bool:
            raise ValueError('Import inclusion must be boolean.')
        self._authorize(workspace)
        with self._lock(batch_id):
            record = self.get(workspace, batch_id)
            self._authorize(workspace, record['collections'])
            row = next((row for row in record['rows'] if row['row_id'] == row_id), None)
            if row is None:
                raise ValueError('Unknown import batch row.')
            alternate = (included and row['state'] == 'duplicate'
                and row['duplicate_of'] is not None and not row['duplicate_of'].startswith('existing:')
                and any(other['source'] == row['duplicate_of'] and other['manifest'] == row['manifest']
                        for other in record['rows']))
            if ((row['state'] not in ('queued', 'excluded') and not alternate)
                    or row['result'] is not None):
                raise ValueError('Only imports that have never started can be selected or excluded.')
            try:
                load_request(self.root, row['request_id'])
            except FileNotFoundError:
                pass
            else:
                raise ValueError('This import request has already been prepared.')
            row['state'] = 'queued' if included else 'excluded'
            if included:
                row.update(duplicate_of=None, message='')
            self._store(record)
            return _copy(record)

    @staticmethod
    def _metadata(row, record):
        return dict(title=row['title'], authors=(), language=None, edition=None, volume=None,
                    collections=tuple(record['collections']), work_id=row['work_id'],
                    edition_id=row['edition_id'])

    @staticmethod
    def _result(row, result):
        row['result'] = result
        state = result.get('state')
        row['state'] = (state if state in ('complete', 'quarantined') else
                        'queued' if state == 'cancelled' else 'failed')
        row['message'] = str(result.get('message') or result.get('error') or '')[:4000]

    def run(self, workspace, batch_id, *, cancelled=lambda: False, timeout=120.0,
            retry_failed=False, progress=None):
        if (not callable(cancelled) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0.1 <= timeout <= 600
                or type(retry_failed) is not bool or (progress is not None and not callable(progress))):
            raise ValueError('Invalid import batch execution options.')
        self._authorize(workspace)
        stopped = False
        def stop():
            nonlocal stopped
            stopped = stopped or bool(cancelled())
            return stopped
        def save(record):
            self._store(record, paused=stopped)
            if progress is not None:
                progress(_copy(record))
        with self._lock(batch_id):
            record = self.get(workspace, batch_id)
            self._authorize(workspace, record['collections'])
            from .recovery import require_approval
            require_approval(self.root, 'batch', batch_id, record)
            config = Config(**record['config'])
            for row in record['rows']:
                if stop():
                    break
                if row['state'] in ('complete', 'duplicate', 'rejected', 'excluded'):
                    continue
                self._authorize(workspace, record['collections'])
                # A separate batch may have completed since this plan was made.
                # Our own stable edition must resume/reconcile instead of being
                # mistaken for a duplicate after a lost worker reply.
                with self.app.read(workspace) as db:
                    existing = db.execute('SELECT id FROM catalog_edition WHERE workspace=? AND asset=? '
                        'ORDER BY (id=?) DESC LIMIT 1',
                        (workspace, row['manifest']['sha256'], row['edition_id'])).fetchone()
                if existing is not None and existing[0] != row['edition_id']:
                    row.update(state='duplicate', duplicate_of='existing:' + row['manifest']['sha256'],
                               message='These source bytes are already in this library.')
                    save(record)
                    continue
                try:
                    request = prepare_request(self.root, workspace, row['source'],
                        self._metadata(row, record), config, request_id=row['request_id'],
                        expected_manifest=row['manifest'])
                except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                    self._result(row, {'state': 'failed', 'message': str(exc)[:4000]})
                    save(record)
                    continue
                if request.get('state') == 'complete' and isinstance(request.get('result'), dict):
                    self._result(row, request['result'])
                    save(record)
                    continue
                if row['state'] in ('failed', 'quarantined') and not retry_failed:
                    continue
                if stop():
                    break
                row['state'], row['message'] = 'running', ''
                save(record)
                try:
                    result = self.app.resume_import(workspace, row['request_id'],
                                                     cancelled=stop, timeout=timeout)
                    self._result(row, result)
                    if result.get('state') == 'cancelled':
                        stopped = True
                except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
                    row['state'] = 'failed'
                    row['message'] = str(exc)[:4000]
                    row['result'] = {'state': 'failed', 'message': row['message']}
                save(record)
            save(record)
            return _copy(record)
