"""Read-only folder/file preflight for the existing import pipeline.

Only manifests and private source paths leave this module. Every supported file
is screened by the existing bounded intake adapter; nothing is retained,
registered, extracted or marked as independently reviewed here.
"""
from pathlib import Path
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import os
import re
import stat
import time

from ..catalog.intake import snapshot
from ..extract.document_formats import SUPPORTED_SUFFIXES


SUPPORTED = SUPPORTED_SUFFIXES
MAX_FILES = 5000
MAX_SCAN_ENTRIES = 20000
MAX_ROOTS = 256
MAX_DEPTH = 64
MAX_REJECTIONS = 100
MAX_EXISTING_ASSETS = 100000
MAX_PATH_BYTES = 8 * 1024 * 1024
MAX_PAGE_METADATA_BYTES = 8 * 1024 * 1024
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


@dataclass(frozen=True)
class ScanPage:
    """One bounded page; an inventory fingerprint is not source-byte authority."""
    rows: tuple
    roots: tuple[str, ...]
    inventory_sha256: str
    inventory_entries: int
    offset: int
    next_offset: int | None
    unscanned: tuple[str, ...]


@dataclass
class _Page:
    offset: int = 0
    inventory_only: bool = False
    count: int = 0
    sources_read: int = 0
    next_offset: int | None = None
    metadata_bytes: int = MAX_REJECTIONS * 4500
    path_bytes: int = 0
    entries: int = 0
    unscanned: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()
    digest: object = field(default_factory=sha256)

    def observe(self, value):
        self.digest.update((json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode())

    def admit(self, path, root, *, source=True):
        index = self.count
        self.count += 1
        if self.inventory_only or index < self.offset:
            return False
        # Reserve enough metadata for IDs, manifests, messages and duplicate paths
        # before opening a source. Journals retain their independent 16 MiB cap.
        cost = (len(json.dumps(_row(path, root, 'queued', ''), ensure_ascii=False).encode('utf-8'))
                + len(str(path).encode('utf-8')) + 512) if source else 0
        if (self.next_offset is not None or self.sources_read >= MAX_FILES
                or self.metadata_bytes + cost > MAX_PAGE_METADATA_BYTES):
            if self.next_offset is None:
                self.next_offset = index
            return False
        self.metadata_bytes += cost
        self.sources_read += int(source)
        return True


def _selection(paths, check):
    if isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose a sequence of files or folders.')
    selected = {}
    size = 0
    for count, value in enumerate(paths, 1):
        check()
        if count > MAX_ROOTS:
            raise ValueError('Too many selected files or folders for one import scan.')
        path = Path(value).absolute()
        if '..' in path.parts or '\x00' in str(path) or len(str(path)) > 32768:
            raise ValueError('Choose a direct local source path without parent traversal.')
        size += len(os.fsencode(path))
        if size > MAX_PATH_BYTES:
            raise ValueError('Selected source paths exceed the path-byte budget.')
        selected.setdefault(os.path.normcase(str(path)), path)
    return selected


def scan_page(paths, *, recursive=True, existing_assets=(), offset=0,
              expected_inventory=None, cancelled=lambda: False, timeout=120.0):
    """Inspect at most 5,000 sources using the same walker and intake as scan_paths.

    Metadata-only passes bind the selected roots, traversal policy and ordinary
    file/directory identities before and after the page. A cursor is local
    continuation metadata, not permission to read changed sources: import still
    compares each exact retained byte manifest. Unscanned structural scopes are
    reported explicitly; no prefix of an oversized directory is accepted.
    """
    if (type(offset) is not int or not 0 <= offset <= MAX_SCAN_ENTRIES + MAX_ROOTS
            or type(recursive) is not bool or not callable(cancelled)
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not .1 <= timeout <= 600
            or expected_inventory is not None and
               (not isinstance(expected_inventory, str) or not _DIGEST.fullmatch(expected_inventory))):
        raise ValueError('Invalid scan continuation or operation budget.')
    if offset and expected_inventory is None:
        raise ValueError('A continuation requires its saved inventory fingerprint.')
    deadline, stopped = time.monotonic() + timeout, False
    def stop():
        nonlocal stopped
        stopped = stopped or bool(cancelled())
        if stopped:
            raise InterruptedError('Import scan cancelled; the previous page was kept.')
        if time.monotonic() >= deadline:
            raise TimeoutError('Import scan deadline reached; the previous page was kept.')
        return False
    selected = _selection(paths, stop)
    roots = tuple(sorted(selected.values(), key=_key))
    # Materialize only the bounded caller-supplied digest inventory, never an
    # unbounded iterable or source data for more than one intake at a time.
    assets = []
    for value in existing_assets:
        stop()
        if len(assets) >= MAX_EXISTING_ASSETS:
            raise ValueError('Too many existing assets for one import scan.')
        assets.append(value)
    before = _Page(inventory_only=True)
    _scan_paths(roots, recursive=recursive, cancelled=stop, _page=before)
    fingerprint = before.digest.hexdigest()
    if expected_inventory is not None and fingerprint != expected_inventory:
        raise ValueError('Source inventory changed. Start a new scan; existing import pages were kept.')
    if offset > before.count:
        raise ValueError('The scan continuation is outside its source inventory.')
    current = _Page(offset=offset)
    rows = _scan_paths(roots, recursive=recursive, existing_assets=assets, cancelled=stop, _page=current)
    after = _Page(inventory_only=True)
    _scan_paths(roots, recursive=recursive, cancelled=stop, _page=after)
    if current.digest.hexdigest() != fingerprint or after.digest.hexdigest() != fingerprint:
        raise ValueError('Source inventory changed during scanning. No new page was saved.')
    stop()
    return ScanPage(rows, before.roots, fingerprint, before.entries, offset,
                    current.next_offset, before.unscanned)


def planned_snapshot(path):
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
    asset = snapshot(path.parent, path.name)
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
    return _scan_paths(paths, recursive=recursive, existing_assets=existing_assets, cancelled=cancelled)


def _scan_paths(paths, *, recursive=True, existing_assets=(), cancelled=lambda: False, _page=None):
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

    selected = _selection(paths, check)
    roots = tuple(sorted(selected.values(), key=_key))
    if _page is not None:
        _page.roots = tuple(map(str, roots))
        _page.observe(('scan-inventory/1', _page.roots, recursive, MAX_DEPTH,
                       MAX_SCAN_ENTRIES, MAX_FILES, MAX_PATH_BYTES, MAX_PAGE_METADATA_BYTES,
                       tuple(sorted(SUPPORTED))))

    rows, seen_paths, seen_digests = [], set(), {}
    attempts = entries_seen = rejections = omitted = 0
    summary_path = None

    def reject(path, root, message, *, force=False, included=False):
        nonlocal rejections, omitted, summary_path
        if _page is not None:
            if force:
                if _page.inventory_only:
                    return
            elif not included and not _page.admit(path, root, source=False):
                return
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
        observed = included = False
        try:
            _parents(path)
            info = path.lstat()
            if _page is not None:
                _page.path_bytes += len(os.fsencode(path))
                if _page.path_bytes > MAX_PATH_BYTES:
                    raise _ScanLimit(path, root,
                        'Scan path-byte limit reached; this folder and remaining selections were not scanned.')
                _page.observe((str(path), str(root), _stamp(info)))
                observed = True
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
                        if _page is not None:
                            _page.path_bytes += len(os.fsencode(entry.name))
                            if _page.path_bytes > MAX_PATH_BYTES:
                                raise _ScanLimit(path, root,
                                    'Scan path-byte limit reached; this folder and remaining selections were not scanned.')
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
            if _page is not None:
                if not _page.admit(path, root):
                    return
                included = True
            elif attempts >= MAX_FILES:
                raise _ScanLimit(path, root,
                    'Supported-file limit reached; remaining files were not scanned. Select a smaller batch.')
            attempts += 1
            if info.st_nlink != 1:
                raise ValueError('Hard-linked source files are not imported.')
            check()
            asset = planned_snapshot(path)
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
            if _page is not None and not observed:
                _page.observe(('rejection', str(path), type(exc).__name__, getattr(exc, 'errno', None), str(exc)))
            reject(path, root, str(exc), included=included)

    try:
        for root_index, path in enumerate(roots):
            visit(path, path, 0)
    except _ScanLimit as exc:
        # An incomplete scan must remain visible even when ordinary rejected
        # entry details have reached their display cap.
        reject(exc.path, exc.root, str(exc), force=True)
        if _page is not None:
            _page.unscanned = tuple(dict.fromkeys(map(str, (exc.path, *roots[root_index:]))))
            _page.observe(('unscanned', _page.unscanned, str(exc)))
    check()
    if _page is not None:
        _page.entries = entries_seen
    if omitted:
        rows.append(_row(summary_path, summary_path, 'rejected',
            f'{omitted} additional entries were rejected or skipped; '
            f'{rejections} total rejected or skipped entries in this scan.'))
    return tuple(rows)
