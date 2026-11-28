"""Read-only folder/file preflight for the existing import pipeline.

Only manifests and private source paths leave this module. Every supported file
is screened by the existing bounded intake adapter; nothing is retained,
registered, extracted or marked as independently reviewed here.
"""
from pathlib import Path
import os
import re
import stat

from ..catalog.intake import MAX_BYTES, snapshot
from ..catalog.streaming_pdf import MAX_PDF_BYTES, pdf_budget, snapshot_original
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


def planned_snapshot(path, *, pdf_max_bytes=MAX_PDF_BYTES, cancelled=lambda: False):
    """Read one planned source under the preflight file policy.

    Import workers reuse this helper when consuming a saved plan. They compare
    its returned manifest with the saved manifest and retain these exact bytes,
    so a changed path is neither silently accepted nor reopened for retention.
    """
    path = Path(path).absolute()
    if '..' in path.parts or '\x00' in str(path) or path.suffix.casefold() not in SUPPORTED:
        raise ValueError('Choose a direct source path in a supported document format; see Help, Document formats.')
    _parents(path)
    before = path.lstat()
    if _linked(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError('The planned source must be a regular file without links or reparse points.')
    if before.st_nlink != 1:
        raise ValueError('Hard-linked source files are not imported.')
    pdf_budget(pdf_max_bytes)
    if path.suffix.casefold() == '.pdf' and before.st_size > pdf_max_bytes:
        raise ValueError('PDF original exceeds its selected byte budget.')
    asset = (snapshot_original(path.parent, path.name, pdf_max_bytes=pdf_max_bytes, cancelled=cancelled)
             if path.suffix.casefold() == '.pdf' and before.st_size > MAX_BYTES else
             snapshot(path.parent, path.name))
    after = path.lstat()
    _parents(path)
    if _linked(after) or after.st_nlink != 1 or _stamp(after) != _stamp(before):
        raise ValueError('Source changed during the scan; select it again.')
    return asset


def scan_paths(paths, *, recursive=True, existing_assets=(), cancelled=lambda: False,
               supported=None, file_manifest=None, max_files=None, skip_unsupported=False):
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

    Internal adapters may select another bounded file policy. ``file_manifest``
    returns a streaming-verified SHA256/size/MIME manifest; document callers keep
    the existing intake adapter. Explicitly skipped unsupported files remain
    visible as ``excluded`` rows, bounded by the same scan-entry allowance.
    """
    if type(recursive) is not bool or isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose a sequence of files or folders and a boolean recursion option.')
    if supported is None:
        supported = SUPPORTED
    if (not isinstance(supported, frozenset) or not 1 <= len(supported) <= 64
            or any(not isinstance(value, str) or not re.fullmatch(r'\.[a-z0-9]{1,16}', value)
                   for value in supported)):
        raise ValueError('A scan policy requires a bounded set of lowercase file extensions.')
    if file_manifest is not None and not callable(file_manifest):
        raise ValueError('A scan file-manifest policy must be callable.')
    if max_files is None:
        max_files = MAX_FILES
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError('A scan file allowance must remain within the existing bounded policy.')
    if type(skip_unsupported) is not bool or not callable(cancelled):
        raise ValueError('Invalid scan exclusion or cancellation policy.')

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
            if path.suffix.casefold() not in supported:
                if skip_unsupported:
                    rows.append(_row(path, root, 'excluded', 'Unsupported file excluded by the selected file policy.'))
                else:
                    reject(path, root, 'Unsupported file skipped; see Help, Document formats for supported sources.')
                return
            if attempts >= max_files:
                raise _ScanLimit(path, root,
                    'Supported-file limit reached; remaining files were not scanned. Select a smaller batch.')
            attempts += 1
            if info.st_nlink != 1:
                raise ValueError('Hard-linked source files are not imported.')
            check()
            if file_manifest is None:
                asset = planned_snapshot(path, cancelled=cancelled)
                manifest = asset.manifest()
                del asset
            else:
                manifest = file_manifest(path)
                if (not isinstance(manifest, dict) or set(manifest) != {'sha256', 'mime', 'size'}
                        or not isinstance(manifest['sha256'], str) or not _DIGEST.fullmatch(manifest['sha256'])
                        or not isinstance(manifest['mime'], str) or not manifest['mime']
                        or type(manifest['size']) is not int or manifest['size'] != info.st_size):
                    raise ValueError('The selected file policy returned an invalid source manifest.')
            check()
            after = path.lstat()
            _parents(path)
            if _linked(after) or after.st_nlink != 1 or _stamp(after) != _stamp(info):
                raise ValueError('Source changed during the scan; select it again.')
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


# Continued scans keep the original one-shot API above unchanged. A page uses
# bounded native directory iteration; its order is captured, not assumed to be
# portable across filesystems. On resume, the exact previously visited prefix
# must still match before another file can be admitted.
from dataclasses import dataclass
from hashlib import sha256
import json
import time

MAX_CURSOR_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ScanPage:
    rows: tuple
    roots: tuple
    cursor: dict | None


def _scan_scope(roots, recursive):
    return sha256(json.dumps([list(roots), recursive], ensure_ascii=True,
                             separators=(',', ':')).encode('ascii')).hexdigest()


def validate_scan_cursor(roots, recursive, cursor):
    """Validate saved metadata only; never probe external source paths."""
    from pathlib import PureWindowsPath, PurePosixPath
    if (not isinstance(roots, (list, tuple)) or len(roots) > MAX_ROOTS
            or type(recursive) is not bool):
        raise ValueError('Invalid continued scan scope.')
    for value in roots:
        if (not isinstance(value, str) or not 0 < len(value) <= 32768 or '\x00' in value
                or not (PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute())
                or '..' in PureWindowsPath(value).parts or '..' in PurePosixPath(value).parts):
            raise ValueError('Invalid continued scan source root.')
    if len(set(roots)) != len(roots):
        raise ValueError('Duplicate continued scan roots.')
    if cursor is None:
        return
    if (not isinstance(cursor, dict)
            or set(cursor) != {'version', 'scope_sha256', 'root_index', 'stack'}
            or type(cursor['version']) is not int or cursor['version'] != 1
            or cursor['scope_sha256'] != _scan_scope(roots, recursive)
            or type(cursor['root_index']) is not int
            or not 0 <= cursor['root_index'] <= len(roots)
            or not isinstance(cursor['stack'], list) or len(cursor['stack']) > MAX_DEPTH):
        raise ValueError('Invalid continued scan cursor.')
    previous = None
    for frame in cursor['stack']:
        if (not isinstance(frame, dict)
                or set(frame) != {'path', 'stamp', 'count', 'prefix_sha256'}
                or not isinstance(frame['path'], str) or not 0 < len(frame['path']) <= 32768
                or '\x00' in frame['path']
                or not isinstance(frame['stamp'], list) or len(frame['stamp']) != 7
                or any(type(value) is not int for value in frame['stamp'])
                or type(frame['count']) is not int or not 0 <= frame['count'] < 2 ** 63
                or not isinstance(frame['prefix_sha256'], str)
                or not _DIGEST.fullmatch(frame['prefix_sha256'])):
            raise ValueError('Invalid continued scan directory.')
        # Use the captured path's own platform for passive cross-OS backup
        # validation. Actual continuation separately requires native paths.
        kind = PureWindowsPath if PureWindowsPath(frame['path']).is_absolute() else PurePosixPath
        path = kind(frame['path'])
        if '..' in path.parts or not path.is_absolute():
            raise ValueError('Invalid continued scan directory path.')
        if previous is None:
            if not cursor['root_index'] or path != kind(roots[cursor['root_index'] - 1]):
                raise ValueError('Continued scan escaped its selected root.')
        elif path.parent != kind(previous):
            raise ValueError('Continued scan directory chain changed.')
        if frame['count'] == 0 and frame['prefix_sha256'] != sha256(b'').hexdigest():
            raise ValueError('Invalid empty scan prefix.')
        previous = frame['path']
    if len(json.dumps(cursor, ensure_ascii=True, separators=(',', ':')).encode('ascii')) > MAX_CURSOR_BYTES:
        raise ValueError('Continued scan cursor exceeds its byte budget.')


def _scan_roots(paths, recursive, check):
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose a sequence of source files or folders.')
    selected = {}
    for count, value in enumerate(paths, 1):
        check()
        if count > MAX_ROOTS:
            raise ValueError('Too many selected files or folders for one import scan.')
        raw = os.fspath(value)
        if not isinstance(raw, str) or not 0 < len(raw) <= 32768:
            raise ValueError('A selected source path exceeds its field budget.')
        path = Path(raw).absolute()
        if '..' in path.parts or '\x00' in str(path):
            raise ValueError('Choose a direct source path without parent traversal.')
        selected.setdefault(os.path.normcase(str(path)), path)
    return tuple(str(path) for path in sorted(selected.values(), key=_key))


def scan_paths_page(paths, *, recursive=True, existing_assets=(), cursor=None,
                    cancelled=lambda: False, max_files=None, max_entries=None,
                    timeout=120.0):
    """Continue a selected scan without reading an unbounded file list into RAM.

    File and newly visited directory-entry budgets apply to this page. Reopening
    a directory replays only names (never source bytes), checks its saved prefix,
    and is cancellation/time bounded. A changed active directory or ordering
    fails closed; completed pages stay durable in ImportBatchStore. Later source
    changes are independently rejected by the ordinary import worker.

    Duplicate bytes are screened within this page and the authorized catalog.
    Separate pending pages use the existing batch runner's cross-batch duplicate
    check before publication, including after a restart.
    """
    import math
    if (type(recursive) is not bool or not callable(cancelled)
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 600):
        raise ValueError('Invalid continued scan options.')
    max_files = MAX_FILES if max_files is None else max_files
    max_entries = MAX_SCAN_ENTRIES if max_entries is None else max_entries
    if (type(max_files) is not int or not 1 <= max_files <= MAX_FILES
            or type(max_entries) is not int or not 1 <= max_entries <= MAX_SCAN_ENTRIES):
        raise ValueError('Invalid continued scan page budget.')
    deadline = time.monotonic() + timeout

    def check(*, replay=False):
        if cancelled():
            raise InterruptedError('Import scan cancelled.')
        if replay and time.monotonic() >= deadline:
            raise TimeoutError('Reopening the continued scan exceeded its time budget; earlier pages remain saved.')

    check()
    roots = _scan_roots(paths, recursive, check)
    validate_scan_cursor(roots, recursive, cursor)
    existing = set()
    for digest in existing_assets:
        check(replay=True)
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError('Existing assets must be SHA256 digests from the selected library.')
        existing.add(digest)
        if len(existing) > MAX_EXISTING_ASSETS:
            raise ValueError('Too many existing assets for one import scan.')
    explicit = {os.path.normcase(path) for path in roots}
    state = (json.loads(json.dumps(cursor)) if cursor is not None else
             dict(version=1, scope_sha256=_scan_scope(roots, recursive), root_index=0, stack=[]))
    opened = []
    rows, seen_digests = [], {}
    attempts = entries_seen = rejections = omitted = 0
    omitted_path = None

    def reject(path, root, message):
        nonlocal rejections, omitted, omitted_path
        rejections += 1
        if rejections <= MAX_REJECTIONS:
            rows.append(_row(path, root, 'rejected', message))
        else:
            omitted += 1
            omitted_path = omitted_path or path

    def directory_stamp(path):
        _parents(path)
        info = path.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Continued scan directory is no longer a local directory.')
        return list(_stamp(info))

    def add_name(digest, name):
        data = name.encode('utf-8', 'surrogatepass')
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)

    def open_frame(frame):
        path = Path(frame['path'])
        if not path.is_absolute() or directory_stamp(path) != frame['stamp']:
            raise ValueError('A scan directory changed; keep saved pages and select the remaining sources again.')
        iterator = os.scandir(path)
        digest = sha256()
        try:
            for _ in range(frame['count']):
                check(replay=True)
                entry = next(iterator, None)
                if entry is None:
                    raise ValueError('The continued scan directory prefix changed.')
                add_name(digest, entry.name)
            if digest.hexdigest() != frame['prefix_sha256'] or directory_stamp(path) != frame['stamp']:
                raise ValueError('The continued scan directory order or contents changed.')
        except BaseException:
            iterator.close()
            raise
        opened.append((iterator, digest))

    def visit(path, root, depth):
        nonlocal attempts
        check()
        if depth and os.path.normcase(str(path)) in explicit:
            # Explicit nested selections keep their own root/depth budget and
            # are not admitted twice through an ancestor's traversal.
            return
        try:
            _parents(path)
            info = path.lstat()
            if _linked(info):
                raise ValueError('Links and reparse points are not imported.')
            if stat.S_ISDIR(info.st_mode):
                if depth and not recursive:
                    reject(path, root, 'Subfolder skipped because recursive scanning is off.')
                    return
                if depth >= MAX_DEPTH:
                    reject(path, root, 'Folder depth limit reached; this subfolder was skipped.')
                    return
                frame = dict(path=str(path), stamp=list(_stamp(info)), count=0,
                             prefix_sha256=sha256(b'').hexdigest())
                state['stack'].append(frame)
                try:
                    open_frame(frame)
                except BaseException:
                    state['stack'].pop()
                    raise
                return
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Only regular files are imported.')
            if path.suffix.casefold() not in SUPPORTED:
                reject(path, root, 'Unsupported file skipped; see Help, Document formats for supported sources.')
                return
            attempts += 1
            if info.st_nlink != 1:
                raise ValueError('Hard-linked source files are not imported.')
            asset = planned_snapshot(path, cancelled=lambda: cancelled() or time.monotonic() >= deadline)
            manifest = asset.manifest()
            del asset
            check()
            after = path.lstat()
            _parents(path)
            if _linked(after) or after.st_nlink != 1 or _stamp(after) != _stamp(info):
                raise ValueError('Source changed during the scan; select it again.')
            digest = manifest['sha256']
            duplicate = ('existing:' + digest if digest in existing else seen_digests.get(digest))
            if duplicate is not None:
                rows.append(_row(path, root, 'duplicate', 'Exact source bytes already exist in this library or page.',
                                 manifest, duplicate))
            else:
                seen_digests[digest] = str(path)
                rows.append(_row(path, root, 'queued', 'Ready to import.', manifest))
        except InterruptedError:
            raise
        except (ValueError, OSError) as exc:
            reject(path, root, str(exc))

    try:
        for frame in state['stack']:
            check(replay=True)
            open_frame(frame)
        while True:
            check()
            if (attempts >= max_files or entries_seen >= max_entries
                    or time.monotonic() >= deadline):
                break
            if not state['stack']:
                if state['root_index'] >= len(roots):
                    break
                path = Path(roots[state['root_index']])
                state['root_index'] += 1
                entries_seen += 1
                visit(path, path, 0)
                continue
            frame = state['stack'][-1]
            iterator, digest = opened[-1]
            entry = next(iterator, None)
            if entry is None:
                if directory_stamp(Path(frame['path'])) != frame['stamp']:
                    raise ValueError('A scan directory changed during its page.')
                iterator.close()
                opened.pop()
                state['stack'].pop()
                continue
            add_name(digest, entry.name)
            frame['count'] += 1
            frame['prefix_sha256'] = digest.hexdigest()
            entries_seen += 1
            visit(Path(frame['path']) / entry.name, Path(roots[state['root_index'] - 1]),
                  len(state['stack']))
        for frame in state['stack']:
            if directory_stamp(Path(frame['path'])) != frame['stamp']:
                raise ValueError('A scan directory changed before its continuation was saved.')
        check()
        if omitted:
            rows.append(_row(omitted_path, omitted_path, 'rejected',
                             f'{omitted} additional entries were rejected or skipped; '
                             f'{rejections} total rejected or skipped entries in this scan page.'))
        following = state if state['stack'] or state['root_index'] < len(roots) else None
        validate_scan_cursor(roots, recursive, following)
        # Keep each displayed page stable without retaining all directory names.
        rows.sort(key=lambda row: _key(row['source']))
        return ScanPage(tuple(rows), roots, following)
    finally:
        for iterator, _ in opened:
            iterator.close()
