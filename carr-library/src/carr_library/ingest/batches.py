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
_BATCH_V2_KEYS = _BATCH_KEYS | {'scan'}
_ROW_KEYS = {'row_id', 'work_id', 'edition_id', 'request_id', 'source', 'file', 'title',
             'state', 'message', 'manifest', 'duplicate_of', 'result'}


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

    def _directory(self):
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
        with self.app.read(workspace) as db:
            catalog = Catalog(db, workspace)
            for collection in collections:
                catalog._active_collection(collection)

    def _validate(self, record, batch_id):
        if (not isinstance(record, dict)
                or set(record) != (_BATCH_KEYS if record.get('version') == 1 else _BATCH_V2_KEYS)
                or type(record['version']) is not int or record['version'] not in (1, 2)
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
            from .scan_pages import validate_batch_scan
            validate_batch_scan(record['scan'], batch_id=batch_id, recursive=record['recursive'])
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
            if create:
                from ..audio.jobs import publish_noreplace
                publish_noreplace(temporary, path)
            else:
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
             config=None, cancelled=lambda: False):
        from .batch_scan import MAX_EXISTING_ASSETS, scan_paths
        if (not isinstance(collections, (list, tuple)) or len(collections) > 128
                or type(recursive) is not bool or not callable(cancelled)):
            raise ValueError('Invalid import batch options.')
        collections = tuple(dict.fromkeys(collections))
        config = config or Config()
        settings = asdict(config)
        Config(**settings)
        self._authorize(workspace, collections)
        with self.app.read(workspace) as db:
            existing = tuple(row[0] for row in db.execute(
                'SELECT DISTINCT asset FROM catalog_edition WHERE workspace=? LIMIT ?',
                (workspace, MAX_EXISTING_ASSETS + 1)))
        rows = []
        for candidate in scan_paths(paths, recursive=recursive, existing_assets=existing,
                                    cancelled=cancelled):
            rows.append(dict(candidate, row_id=uuid4().hex, work_id=uuid4().hex,
                             edition_id=uuid4().hex, request_id=uuid4().hex, result=None))
            if len(rows) > MAX_ROWS:
                raise ValueError('Too many files in one import batch.')
        self._authorize(workspace, collections)
        record = dict(version=1, batch_id=uuid4().hex, data_dir=str(self.root),
                      workspace=workspace, created=_now(), updated=_now(), state='queued',
                      collections=list(collections), recursive=recursive, config=settings,
                      rows=rows, counts={}, total=len(rows))
        with self._lock(record['batch_id']):
            self._store(record, create=True, paused=bool(cancelled()))
        return _copy(record)

    def _scan_slot(self):
        # This remains an explicit journal bound, not a corpus-size claim.
        for count, path in enumerate(self._directory().glob('*.json'), 1):
            if count >= MAX_BATCHES:
                raise ValueError('The saved import-batch count limit is reached; retain or export completed plans before creating more.')

    def _scan_candidate(self, workspace, roots, *, collections, recursive, config,
                        batch_id, previous, input_cursor, page_number,
                        file_budget, entry_budget, timeout, cancelled):
        from .batch_scan import scan_paths_page
        from .scan_pages import BATCH_SCAN_SCHEMA
        with self.app.read(workspace) as db:
            def existing(digest):
                return db.execute('SELECT 1 FROM catalog_edition WHERE workspace=? AND asset=? LIMIT 1',
                                  (workspace, digest)).fetchone() is not None
            page = scan_paths_page(roots, recursive=recursive, cursor=input_cursor,
                existing_contains=existing, file_budget=file_budget,
                entry_budget=entry_budget, timeout=timeout, cancelled=cancelled)
        self._authorize(workspace, collections)
        rows = [dict(candidate, row_id=uuid4().hex, work_id=uuid4().hex,
                     edition_id=uuid4().hex, request_id=uuid4().hex, result=None)
                for candidate in page.rows]
        if len(rows) > MAX_ROWS:
            raise ValueError('Too many files in one import scan page.')
        now = _now()
        return dict(version=2, batch_id=batch_id, data_dir=str(self.root),
            workspace=workspace, created=now, updated=now, state='queued',
            collections=list(collections), recursive=recursive, config=_copy(config),
            rows=rows, counts={}, total=len(rows),
            scan=dict(schema=BATCH_SCAN_SCHEMA, roots=list(map(str, roots)),
                input_cursor=input_cursor, next_cursor=page.next_cursor, page=page_number,
                previous_batch=previous, next_batch=None, resumable=True,
                file_budget=file_budget, entry_budget=entry_budget, timeout=timeout,
                examined_files=page.examined_files, scanned_entries=page.scanned_entries,
                rejected_entries=page.rejected_entries))

    def plan_page(self, workspace, paths, *, collections=(), recursive=True,
                  config=None, file_budget=5000, entry_budget=20000,
                  timeout=60.0, cancelled=lambda: False):
        """Save the first bounded scan page; imports remain an explicit action."""
        from .scan_pages import selected_paths
        from .writer_guard import exclusive_writer
        if (not isinstance(collections, (list, tuple)) or len(collections) > 128
                or type(recursive) is not bool or not callable(cancelled)):
            raise ValueError('Invalid import scan options.')
        def check():
            if cancelled():
                raise InterruptedError('Import scan cancelled before saving a new page.')
        check()
        roots = selected_paths(paths, recursive, check)
        collections = tuple(dict.fromkeys(collections))
        settings = asdict(config or Config())
        Config(**settings)
        with exclusive_writer(self.root):
            self._authorize(workspace, collections)
            self._scan_slot()
            record = self._scan_candidate(workspace, roots, collections=collections,
                recursive=recursive, config=settings, batch_id=uuid4().hex,
                previous=None, input_cursor=None, page_number=1,
                file_budget=file_budget, entry_budget=entry_budget,
                timeout=timeout, cancelled=cancelled)
            check()
            with self._lock(record['batch_id']):
                self._store(record, create=True)
            return _copy(record)

    def continue_scan(self, workspace, batch_id, *, file_budget=None, entry_budget=None,
                      timeout=None, cancelled=lambda: False):
        """Create or recover exactly one next page; never revisit archived roots.

        The child ID is bound to the parent and its exact cursor. A crash after
        child publication but before the parent link reuses that same child.
        Existing saved children can be opened after restore; new traversal
        requires a live, non-restored cursor and explicit user continuation.
        """
        from ..audio.jobs import _sync_directory
        from .scan_pages import successor_id, validate_scan_successor
        from .writer_guard import exclusive_writer
        if not callable(cancelled):
            raise ValueError('A scan cancellation callback is required.')
        for value, maximum in ((file_budget, 5000), (entry_budget, 20000)):
            if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                raise ValueError('Choose bounded file and entry budgets for the next scan page.')
        if timeout is not None and (type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 3600):
            raise ValueError('Choose a scan time budget between zero and 3600 seconds.')
        def check():
            if cancelled():
                raise InterruptedError('Import scan cancelled; the saved page remains unchanged.')
        check()
        with exclusive_writer(self.root), self._lock(batch_id):
            parent = self.get(workspace, batch_id)
            self._authorize(workspace, parent['collections'])
            if parent.get('version') != 2 or parent['scan']['next_cursor'] is None:
                raise ValueError('This saved plan has no scan continuation. Select roots for a new first page.')
            scan = parent['scan']
            following = successor_id(batch_id, scan['next_cursor'])
            # The deterministic child may exist even when the parent link was
            # not durable. Validate before acknowledgement, without source I/O.
            with self._lock(following):
                try:
                    child = self._load(following)
                except FileNotFoundError:
                    child = None
                if child is not None:
                    validate_scan_successor(parent, child)
                    check()
                    _sync_directory(self._directory())
                else:
                    if scan['next_batch'] is not None:
                        raise ValueError('A linked saved scan page is missing; restore it before continuing.')
                    if not scan['resumable']:
                        raise ValueError('Restored folder cursors are historical only. Select source roots for a new first page; saved queued files remain usable.')
                    if scan['page'] >= MAX_BATCHES:
                        raise ValueError('The bounded scan-page chain limit is reached.')
                    self._scan_slot()
                    check()
                    child = self._scan_candidate(workspace, scan['roots'],
                        collections=parent['collections'], recursive=parent['recursive'],
                        config=parent['config'], batch_id=following, previous=batch_id,
                        input_cursor=scan['next_cursor'], page_number=scan['page'] + 1,
                        file_budget=scan['file_budget'] if file_budget is None else file_budget,
                        entry_budget=scan['entry_budget'] if entry_budget is None else entry_budget,
                        timeout=scan['timeout'] if timeout is None else timeout,
                        cancelled=cancelled)
                    check()
                    validate_scan_successor(parent, child)
                    self._store(child, create=True)
                parent['scan']['next_batch'] = following
                # No post-publication cancellation reclassifies an accepted
                # child as unsaved. The parent link is completed or recoverable.
                self._store(parent, paused=parent['state'] == 'paused')
                return _copy(self._reconcile(child))

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
                rows.append({key: record[key] for key in
                             ('batch_id', 'created', 'updated', 'state', 'counts', 'total')})
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
