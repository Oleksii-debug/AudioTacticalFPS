"""Read-only folder/file preflight for the existing import pipeline.

Only manifests and private source paths leave this module. Every supported file
is screened by the existing bounded intake adapter; nothing is retained,
registered, extracted or marked as independently reviewed here.
"""
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import re
import stat

from ..catalog.intake import source_snapshot as snapshot
from .resource_budget import checked_budget, effective_budget
from ..extract.document_formats import SUPPORTED_SUFFIXES


SUPPORTED = SUPPORTED_SUFFIXES
MAX_FILES = 5000
MAX_SCAN_ENTRIES = 20000
MAX_ROOTS = 256
MAX_DEPTH = 64
MAX_REJECTIONS = 100
MAX_EXISTING_ASSETS = 100000
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')


class _ScanLimit(Exception):
    def __init__(self, path, root, message):
        super().__init__(message)
        self.path, self.root = path, root


def _key(path):
    text = os.fspath(path)
    return text.casefold(), text


def _linked(info):
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _parents(path):
    # Do not resolve a selected folder first: that would hide a root symlink or
    # junction. Check ancestors as well, since intake resolves its trusted root.
    for parent in reversed(path.parents):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('A source folder is a link, reparse point or non-directory.')


def _row(path, root, state, message, manifest=None, duplicate_of=None):
    relative = path.relative_to(root).as_posix() if root != path else path.name
    return dict(source=str(path), file=relative, title=path.stem, state=state,
                message=message, manifest=manifest, duplicate_of=duplicate_of)


def planned_snapshot(path, *, cancelled=lambda: False, import_budget=None):
    """Read one planned source under the preflight file policy.

    Import workers reuse this helper when consuming a saved plan. They compare
    its returned manifest with the saved manifest and retain these exact bytes,
    so a changed path is neither silently accepted nor reopened for retention.
    """
    checked_budget(import_budget)
    if not callable(cancelled):
        raise ValueError('Source cancellation must be callable.')
    if cancelled():
        raise InterruptedError('Import source review cancelled.')
    path = Path(path).absolute()
    if '..' in path.parts or '\x00' in str(path) or path.suffix.casefold() not in SUPPORTED:
        raise ValueError('Choose a direct source path in a supported document format; see Help, Document formats.')
    _parents(path)
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError('The planned source must be a regular file without links or reparse points.')
    if before.st_nlink != 1:
        raise ValueError('Hard-linked source files are not imported.')
    asset = snapshot(path.parent, path.name, cancelled=cancelled,
                     max_bytes=effective_budget(import_budget).source_bytes,
                     file_backed=import_budget is not None)
    if cancelled():
        raise InterruptedError('Import source review cancelled.')
    after = path.lstat()
    _parents(path)
    if _linked(after) or after.st_nlink != 1 or _stamp(after) != _stamp(before):
        raise ValueError('Source changed during the scan; select it again.')
    return asset


def scan_paths(paths, *, recursive=True, existing_assets=(), cancelled=lambda: False):
    """Return a bounded, deterministic import plan without changing any files.

    ``existing_assets`` contains SHA256 digests from the caller's authorized
    library only. An existing duplicate points to ``existing:<digest>``; a
    duplicate within this plan points to the first queued absolute source path.
    Equal bytes do not merge bibliographic identities. Repeated selected paths
    are scanned once. Rejections beyond the first 100 are counted in one row.

    Cancellation raises InterruptedError, discarding this unfinished plan. The
    caller must revalidate bytes when importing: a preflight is not authority to
    read a subsequently changed file. Memory retains one <=64 MiB intake
    snapshot at a time, bounded path lists, manifests and digest identities.
    """
    if type(recursive) is not bool or isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose a sequence of files or folders and a boolean recursion option.')

    def check():
        if cancelled():
            raise InterruptedError('Import scan cancelled.')

    check()
    existing = set()
    for digest in existing_assets:
        check()
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError('Existing assets must be SHA256 digests from the selected library.')
        existing.add(digest)
        if len(existing) > MAX_EXISTING_ASSETS:
            raise ValueError('Too many existing assets for one import scan.')

    selected = {}
    for count, value in enumerate(paths, 1):
        check()
        if count > MAX_ROOTS:
            raise ValueError('Too many selected files or folders for one import scan.')
        path = Path(value).absolute()
        if '..' in path.parts or '\x00' in str(path):
            raise ValueError('Choose a direct local source path without parent traversal.')
        selected.setdefault(os.path.normcase(str(path)), path)

    rows, seen_paths, seen_digests = [], set(), {}
    attempts = entries_seen = rejections = omitted = 0
    summary_path = None

    def reject(path, root, message, *, force=False):
        nonlocal rejections, omitted, summary_path
        rejections += 1
        if force or rejections <= MAX_REJECTIONS:
            rows.append(_row(path, root, 'rejected', message))
        else:
            omitted += 1
            summary_path = summary_path or path

    def visit(path, root, depth):
        nonlocal attempts, entries_seen
        check()
        identity = os.path.normcase(str(path))
        if identity in seen_paths:
            return
        seen_paths.add(identity)
        try:
            _parents(path)
            info = path.lstat()
            if _linked(info):
                raise ValueError('Links and reparse points are not imported.')
            if stat.S_ISDIR(info.st_mode):
                # A separately selected folder has its own recursion/depth budget.
                # Defer it to the root loop, without marking it visited.
                if depth and identity in selected:
                    seen_paths.remove(identity)
                    return
                if depth and not recursive:
                    reject(path, root, 'Subfolder skipped because recursive scanning is off.')
                    return
                if depth >= MAX_DEPTH:
                    reject(path, root, 'Folder depth limit reached; this subfolder was skipped.')
                    return
                names = []
                # Reject an oversized directory as a whole. Selecting an
                # arbitrary scandir prefix would make plans platform-dependent.
                with os.scandir(path) as directory:
                    for entry in directory:
                        check()
                        entries_seen += 1
                        if entries_seen > MAX_SCAN_ENTRIES:
                            raise _ScanLimit(path, root,
                                'Scan entry limit reached; this folder and remaining selections '
                                'were not scanned. Select fewer folders.')
                        names.append(entry.name)
                if _stamp(path.lstat()) != _stamp(info):
                    raise ValueError('Folder changed during the scan; select it again.')
                for name in sorted(names, key=_key):
                    visit(path / name, root, depth + 1)
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Only regular files are imported.')
            if path.suffix.casefold() not in SUPPORTED:
                reject(path, root, 'Unsupported file skipped; see Help, Document formats for supported sources.')
                return
            if attempts >= MAX_FILES:
                raise _ScanLimit(path, root,
                    'Supported-file limit reached; remaining files were not scanned. Select a smaller batch.')
            attempts += 1
            if info.st_nlink != 1:
                raise ValueError('Hard-linked source files are not imported.')
            check()
            asset = planned_snapshot(path, cancelled=cancelled)
            check()
            after = path.lstat()
            _parents(path)
            if _linked(after) or after.st_nlink != 1 or _stamp(after) != _stamp(info):
                raise ValueError('Source changed during the scan; select it again.')
            manifest = asset.manifest()
            del asset
            digest = manifest['sha256']
            if digest in existing:
                rows.append(_row(path, root, 'duplicate',
                    'Exact source bytes already exist in this library.', manifest,
                    'existing:' + digest))
            elif digest in seen_digests:
                rows.append(_row(path, root, 'duplicate',
                    'Exact source bytes match an earlier file in this batch.', manifest,
                    seen_digests[digest]))
            else:
                seen_digests[digest] = str(path)
                rows.append(_row(path, root, 'queued', 'Ready to import.', manifest))
        except InterruptedError:
            raise
        except (OSError, ValueError) as exc:
            reject(path, root, str(exc))

    try:
        for path in sorted(selected.values(), key=_key):
            visit(path, path, 0)
    except _ScanLimit as exc:
        # An incomplete scan must remain visible even when ordinary rejected
        # entry details have reached their display cap.
        reject(exc.path, exc.root, str(exc), force=True)
    check()
    if omitted:
        rows.append(_row(summary_path, summary_path, 'rejected',
            f'{omitted} additional entries were rejected or skipped; '
            f'{rejections} total rejected or skipped entries in this scan.'))
    return tuple(rows)


# The legacy one-shot scanner above keeps its exact bounded contract. Explicit
# continuation uses the same source preflight and saves only this lexical cursor,
# never an OS directory handle, offset, document payload or execution request.
MAX_CURSOR_BYTES = 2 * 1024**2
MAX_PAGE_BYTES = 8 * 1024**2
_SCAN_SCHEMA = 'carr-import-scan/1'
_CURSOR_KEYS = {'schema', 'roots', 'recursive', 'root_index', 'stack',
                'visited', 'planned', 'complete'}
_FRAME_KEYS = {'path', 'root', 'depth', 'identity', 'after'}


def _scan_json(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def scan_cursor_digest(value):
    from hashlib import sha256
    return sha256(_scan_json(validate_scan_cursor(value))).hexdigest()


def _absolute_source(value):
    if type(value) is not str or not value or len(value) > 32768 or '\x00' in value:
        raise ValueError('Invalid saved scan source path.')
    path = PureWindowsPath(value) if PureWindowsPath(value).is_absolute() else PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid saved scan source path.')
    return path


def validate_scan_cursor(value):
    """Pure bounded schema validation; never inspect a saved external path."""
    if (type(value) is not dict or set(value) != _CURSOR_KEYS
            or value['schema'] != _SCAN_SCHEMA or type(value['recursive']) is not bool
            or type(value['roots']) is not list or not 1 <= len(value['roots']) <= MAX_ROOTS
            or type(value['stack']) is not list or len(value['stack']) > MAX_DEPTH
            or type(value['root_index']) is not int
            or not 0 <= value['root_index'] <= len(value['roots'])
            or type(value['complete']) is not bool
            or any(type(value[name]) is not int or not 0 <= value[name] < 2**63
                   for name in ('visited', 'planned'))
            or value['planned'] > value['visited']):
        raise ValueError('Invalid bounded import scan cursor.')
    roots = [_absolute_source(path) for path in value['roots']]
    if (len({os.path.normcase(str(path)) for path in roots}) != len(roots)
            or roots != sorted(roots, key=_key)):
        raise ValueError('Saved scan roots are duplicated or not in their reviewed order.')
    previous = None
    for frame in value['stack']:
        if type(frame) is not dict or set(frame) != _FRAME_KEYS:
            raise ValueError('Invalid saved scan directory frame.')
        path, root = _absolute_source(frame['path']), _absolute_source(frame['root'])
        if (type(frame['depth']) is not int or not 0 <= frame['depth'] < MAX_DEPTH
                or type(frame['identity']) is not list or len(frame['identity']) != 7
                or any(type(number) is not int or abs(number) > 2**64 for number in frame['identity'])
                or frame['after'] is not None and (type(frame['after']) is not str
                    or not frame['after'] or len(frame['after']) > 32768
                    or frame['after'] in ('.', '..') or '/' in frame['after'] or '\x00' in frame['after']
                    or type(path)(frame['after']).name != frame['after'])
                or value['root_index'] == 0 or root != roots[value['root_index'] - 1]
                or not path.is_relative_to(root) or frame['depth'] != len(path.relative_to(root).parts)):
            raise ValueError('Saved scan frame lost its exact selected-root binding.')
        if previous is None:
            if path != root or frame['depth'] != 0:
                raise ValueError('Saved scan stack lost its selected root.')
        elif path.parent != _absolute_source(previous['path']) or frame['depth'] != previous['depth'] + 1:
            raise ValueError('Saved scan stack is not a direct directory ancestry.')
        previous = frame
    if value['complete'] != (value['root_index'] == len(roots) and not value['stack']):
        raise ValueError('Saved scan completion differs from its remaining traversal.')
    if len(_scan_json(value)) > MAX_CURSOR_BYTES:
        raise ValueError('Saved scan cursor exceeds its metadata byte allowance.')
    return value


def new_scan_cursor(paths, *, recursive=True):
    if type(recursive) is not bool or isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose explicit files/folders and a boolean recursion option.')
    selected = {}
    for count, value in enumerate(paths, 1):
        if count > MAX_ROOTS:
            raise ValueError('Choose at most 256 roots; each folder can be continued in bounded pages.')
        path = Path(value).absolute()
        _absolute_source(str(path))
        selected.setdefault(os.path.normcase(str(path)), path)
    if not selected:
        raise ValueError('Choose at least one source file or folder.')
    result = {'schema': _SCAN_SCHEMA,
        'roots': [str(path) for path in sorted(selected.values(), key=_key)],
        'recursive': recursive, 'root_index': 0, 'stack': [], 'visited': 0,
        'planned': 0, 'complete': False}
    return validate_scan_cursor(result)


def scan_page_limits(*, max_files=1000, max_entries=MAX_SCAN_ENTRIES,
                     timeout=60, max_bytes=4 * 1024**2):
    import math
    if (type(max_files) is not int or not 1 <= max_files <= MAX_FILES
            or type(max_entries) is not int or not 1 <= max_entries <= MAX_SCAN_ENTRIES
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not .05 <= timeout <= 600
            or type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_PAGE_BYTES):
        raise ValueError('Choose bounded scan-page file, entry, time and metadata allowances.')
    return {'max_files': max_files, 'max_entries': max_entries,
            'timeout': float(timeout), 'max_bytes': max_bytes}


def _live_scan_frame(frame):
    path = Path(frame['path'])
    if not path.is_absolute():
        raise ValueError('This scan root needs a fresh selection on the current platform.')
    _parents(path)
    info = path.lstat()
    if (_linked(info) or not stat.S_ISDIR(info.st_mode)
            or list(_stamp(info)) != frame['identity']):
        raise ValueError('A saved scan directory changed. Review the source selection as a new scan.')
    return path


class _ScanPageYield(Exception):
    pass


def scan_page(paths=(), *, cursor=None, recursive=True, cancelled=lambda: False,
              max_files=1000, max_entries=MAX_SCAN_ENTRIES, timeout=60, max_bytes=4 * 1024**2,
              import_budget=None):
    """Return one explicit resumable page with bounded memory and no writes.

    Lexical names are selected with a bounded heap; a wide directory is streamed
    under the time/cancellation allowance, not materialized as an unbounded list.
    A saved directory identity is checked before/after listing and on continuation.
    Already planned files keep their own source manifests and are checked again
    by the existing import request. A scan is not a filesystem-wide snapshot.

    Entry allowance counts traversal advances. Enumerating a wide directory to
    select its next lexical names is separately bounded by the time allowance.
    Cancellation/time exhaustion returns the exact unadvanced cursor, allowing
    the caller to persist completed review work without skipping the next file.
    """
    import heapq
    import json
    import time
    checked_budget(import_budget)
    limits = scan_page_limits(max_files=max_files, max_entries=max_entries,
                             timeout=timeout, max_bytes=max_bytes)
    if not callable(cancelled):
        raise ValueError('Scan cancellation must be callable.')
    if cursor is None:
        value = new_scan_cursor(paths, recursive=recursive)
    else:
        validate_scan_cursor(cursor)
        if paths or type(recursive) is not bool or recursive != cursor['recursive']:
            raise ValueError('Continue the exact saved roots and recursion option.')
        value = json.loads(_scan_json(validate_scan_cursor(cursor)))
    if any(not Path(path).is_absolute() for path in value['roots']):
        raise ValueError('These scan roots need a fresh selection on the current platform.')
    rows, seen_digests, metadata_bytes, advanced = [], {}, 0, 0
    deadline = time.monotonic() + limits['timeout']
    selected = {os.path.normcase(path) for path in value['roots']}

    def check():
        if time.monotonic() >= deadline:
            raise _ScanPageYield('time')
        if cancelled():
            raise _ScanPageYield('cancelled')
        if advanced >= limits['max_entries']:
            raise _ScanPageYield('entries')

    def append(row):
        nonlocal metadata_bytes
        check()
        if len(rows) >= limits['max_files']:
            raise _ScanPageYield('files')
        size = len(_scan_json(row))
        if metadata_bytes + size > limits['max_bytes']:
            if not rows:
                raise ValueError('One source row exceeds the selected page metadata allowance.')
            raise _ScanPageYield('bytes')
        rows.append(row)
        metadata_bytes += size
        value['planned'] += 1

    def visit(path, root, depth):
        check()
        if depth and os.path.normcase(str(path)) in selected:
            return  # A separately selected root is visited exactly once later.
        try:
            _parents(path)
            info = path.lstat()
            if _linked(info):
                raise ValueError('Links and reparse points are not imported.')
            if stat.S_ISDIR(info.st_mode):
                if depth and not value['recursive']:
                    raise ValueError('Subfolder skipped because recursive scanning is off.')
                if depth >= MAX_DEPTH:
                    raise ValueError('Folder depth allowance reached; select this folder as another root.')
                value['stack'].append({'path': str(path), 'root': str(root), 'depth': depth,
                                       'identity': list(_stamp(info)), 'after': None})
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Only regular source files without hard links are imported.')
            if path.suffix.casefold() not in SUPPORTED:
                raise ValueError('Unsupported file skipped; see Help, Document formats.')
            check()
            asset = planned_snapshot(path, cancelled=lambda: cancelled() or time.monotonic() >= deadline,
                                     import_budget=import_budget)
            manifest = asset.manifest()
            del asset
            check()
            _parents(path)
            if _stamp(path.lstat()) != _stamp(info):
                raise ValueError('Source changed during review; select it again.')
            digest = manifest['sha256']
            manifest_key = (digest, manifest['mime'], manifest['size'])
            duplicate = seen_digests.get(manifest_key)
            append(_row(path, root, 'duplicate' if duplicate else 'queued',
                        'Exact bytes match an earlier source in this page.' if duplicate else 'Ready to import.',
                        manifest, duplicate))
            seen_digests.setdefault(manifest_key, str(path))
        except InterruptedError:
            raise _ScanPageYield('time' if time.monotonic() >= deadline else 'cancelled') from None
        except (OSError, ValueError) as exc:
            append(_row(path, root, 'rejected', str(exc)[:4000]))

    reason = 'complete'
    try:
        for frame in value['stack']:
            check()
            _live_scan_frame(frame)
        while not value['complete']:
            check()
            if not value['stack']:
                if value['root_index'] == len(value['roots']):
                    value['complete'] = True
                    break
                path = Path(value['roots'][value['root_index']])
                visit(path, path, 0)
                value['root_index'] += 1
                value['visited'] += 1
                advanced += 1
                if not value['stack'] and value['root_index'] == len(value['roots']):
                    value['complete'] = True
                continue
            frame = value['stack'][-1]
            directory = _live_scan_frame(frame)
            remaining = max(1, min(limits['max_entries'] - advanced,
                                   limits['max_files'] - len(rows) + 1))
            def names():
                with os.scandir(directory) as entries:
                    for entry in entries:
                        check()
                        if frame['after'] is None or _key(entry.name) > _key(frame['after']):
                            yield entry.name
            chosen = heapq.nsmallest(remaining + 1, names(), key=_key)
            _live_scan_frame(frame)
            if not chosen:
                value['stack'].pop()
                if not value['stack'] and value['root_index'] == len(value['roots']):
                    value['complete'] = True
                continue
            exhausted = len(chosen) <= remaining
            for name in chosen[:remaining]:
                check()
                if len(rows) >= limits['max_files']:
                    raise _ScanPageYield('files')
                visit(directory / name, Path(frame['root']), frame['depth'] + 1)
                frame['after'] = name
                value['visited'] += 1
                advanced += 1
                if value['stack'][-1] is not frame:
                    break
            else:
                if exhausted:
                    value['stack'].pop()
                    if not value['stack'] and value['root_index'] == len(value['roots']):
                        value['complete'] = True
    except _ScanPageYield as stop:
        reason = str(stop)
    # A completed review cannot advance past a directory that was concurrently
    # replaced. This metadata-only check is also retained after cancellation.
    for frame in value['stack']:
        _live_scan_frame(frame)
    validate_scan_cursor(value)
    return {'rows': tuple(rows), 'cursor': value,
            'reason': 'complete' if value['complete'] else reason}
