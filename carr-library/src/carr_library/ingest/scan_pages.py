"""Deterministic bounded-memory folder preflight with restartable continuations.

A cursor is a position, not permission to read a path. The caller supplies the
same explicit roots again. Normal intake checks every file. A changed traversal
directory refuses continuation instead of silently skipping directory entries.
"""
from base64 import b64decode, urlsafe_b64encode
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import heapq
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from time import monotonic

from . import batch_scan as base

SCHEMA = 'carr-import-scan-cursor/1'
MAX_CURSOR_BYTES = 256 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ScanPage:
    rows: tuple
    next_cursor: str | None
    scanned_entries: int
    examined_files: int
    rejected_entries: int

    @property
    def complete(self):
        return self.next_cursor is None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def selected_paths(paths, recursive, check=lambda: None):
    if type(recursive) is not bool or isinstance(paths, (str, bytes, os.PathLike)):
        raise ValueError('Choose source files/folders and an explicit recursion setting.')
    selected = {}
    for count, value in enumerate(paths, 1):
        check()
        if count > base.MAX_ROOTS:
            raise ValueError('Too many explicitly selected source roots.')
        path = Path(value).absolute()
        if '..' in path.parts or '\x00' in str(path) or len(str(path)) > 32768:
            raise ValueError('Choose a direct source path within the metadata budget.')
        selected.setdefault(os.path.normcase(str(path)), path)
    if not selected:
        raise ValueError('Choose at least one source file or folder.')
    return tuple(sorted(selected.values(), key=base._key))


def _child_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024 and value not in ('.', '..')
            and not any(c in value for c in ('/', '\\', '\x00', ':'))
            and not any(ord(c) < 32 for c in value))


def _position_name(value):
    # A rejected basename may contain a colon, backslash or control character
    # on POSIX. This value is compared lexically, never joined to a path.
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and '/' not in value and '\x00' not in value)


def validate_cursor(value, *, root_count=None, binding=None):
    """Metadata only: backup validation never dereferences archived paths."""
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 0 < len(value) <= MAX_CURSOR_BYTES * 2:
            raise ValueError('size')
        raw = b64decode(value.encode('ascii'), altchars=b'-_', validate=True)
        if len(raw) > MAX_CURSOR_BYTES:
            raise ValueError('size')
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError('duplicate')
                result[key] = item
            return result
        row = json.loads(raw, object_pairs_hook=pairs)
        if (type(row) is not dict or set(row) != {'schema', 'binding', 'root', 'stack'}
                or row['schema'] != SCHEMA
                or not isinstance(row['binding'], str) or not base._DIGEST.fullmatch(row['binding'])
                or (binding is not None and row['binding'] != binding)
                or type(row['root']) is not int or row['root'] < 0
                or (root_count is not None and row['root'] >= root_count)
                or type(row['stack']) is not list or len(row['stack']) > base.MAX_DEPTH):
            raise ValueError('shape or scope')
        previous = None
        for frame in row['stack']:
            if (type(frame) is not dict or set(frame) != {'relative', 'after', 'identity'}
                    or not isinstance(frame['relative'], str) or len(frame['relative']) > 32768
                    or (frame['after'] is not None and not _position_name(frame['after']))
                    or type(frame['identity']) is not list or len(frame['identity']) != 7
                    or any(type(v) is not int for v in frame['identity'])):
                raise ValueError('frame')
            relative = frame['relative']
            parts = () if not relative else PurePosixPath(relative).parts
            if relative and ('/'.join(parts) != relative or not all(_child_name(p) for p in parts)):
                raise ValueError('path')
            if previous is None:
                if parts:
                    raise ValueError('root frame')
            elif len(parts) != len(previous) + 1 or parts[:-1] != previous:
                raise ValueError('noncontiguous traversal')
            previous = parts
        return row
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('Invalid import scan continuation or changed source selection.') from exc


def _encode(binding, index, stack):
    raw = _canonical(dict(schema=SCHEMA, binding=binding, root=index, stack=stack))
    if len(raw) > MAX_CURSOR_BYTES:
        raise ValueError('Import scan continuation exceeds its metadata budget.')
    return urlsafe_b64encode(raw).decode('ascii')


def _directory(path, expected=None):
    base._parents(path)
    info = path.lstat()
    if base._linked(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError('A source folder is a link, reparse point or non-directory.')
    if expected is not None and tuple(expected) != base._stamp(info):
        raise ValueError('A source folder changed. Start a new scan of the selected roots.')
    return info


def _names(path, after, maximum, expected, check):
    """Stream an unchanged directory; keep only its next bounded lexical page."""
    _directory(path, expected)
    def candidates():
        with os.scandir(path) as entries:
            for entry in entries:
                check()
                if after is None or base._key(entry.name) > base._key(after):
                    yield entry.name
    names = heapq.nsmallest(maximum, candidates(), key=base._key)
    _directory(path, expected)
    check()
    return deque(names)


def scan_page(paths, *, recursive=True, cursor=None, existing_assets=(),
              existing_contains=None, file_budget=5000, entry_budget=20000,
              timeout=60.0, cancelled=lambda: False):
    """Reuse original-byte intake with per-page, rather than whole-scan, limits.

    Directory order is not a portable offset: enumeration streams into a bounded
    lexical heap under the same cooperative deadline. Duplicate checks cover
    this page and the caller's authorized existing-asset lookup. Saved imports
    still revalidate originals before retaining or publishing them.
    """
    if (type(file_budget) is not int or not 1 <= file_budget <= base.MAX_FILES
            or type(entry_budget) is not int or not 1 <= entry_budget <= base.MAX_SCAN_ENTRIES
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600
            or not callable(cancelled) or (existing_contains is not None and not callable(existing_contains))):
        raise ValueError('Choose bounded file, entry and scan-time budgets.')
    deadline = monotonic() + timeout
    def check():
        if cancelled():
            raise InterruptedError('Import scan cancelled; the previous saved page remains usable.')
        if monotonic() >= deadline:
            raise TimeoutError('Import scan page timed out; retry its unchanged continuation with a larger time budget.')
    roots = selected_paths(paths, recursive, check)
    selected = {os.path.normcase(str(p)) for p in roots}
    binding = sha256(_canonical([list(map(str, roots)), recursive])).hexdigest()
    state = validate_cursor(cursor, root_count=len(roots), binding=binding)
    index, stack = (state['root'], state['stack']) if state else (0, [])
    existing = set()
    for digest in existing_assets:
        check()
        if not isinstance(digest, str) or not base._DIGEST.fullmatch(digest):
            raise ValueError('Existing assets must be authorized SHA256 digests.')
        existing.add(digest)
        if len(existing) > base.MAX_EXISTING_ASSETS:
            raise ValueError('Use a scoped asset lookup for a larger existing library.')
    rows, digests = [], {}
    metadata_bytes = 0
    entries = examined = rejections = omitted = 0
    omitted_path = None

    def append(row):
        nonlocal metadata_bytes
        metadata_bytes += len(_canonical(row))
        if metadata_bytes > MAX_METADATA_BYTES:
            raise OverflowError('Scan-page metadata budget exceeded. Retry with fewer supported files or directory entries per page.')
        rows.append(row)

    def reject(path, root, message):
        nonlocal rejections, omitted, omitted_path
        rejections += 1
        if rejections <= base.MAX_REJECTIONS:
            append(base._row(path, root, 'rejected', message))
        else:
            omitted += 1
            omitted_path = omitted_path or path

    def file(path, root, info):
        nonlocal examined
        if not stat.S_ISREG(info.st_mode):
            reject(path, root, 'Only regular files are imported.')
            return
        if path.suffix.casefold() not in base.SUPPORTED:
            reject(path, root, 'Unsupported file skipped; see Help, Document formats.')
            return
        examined += 1
        try:
            asset = base.planned_snapshot(path)
            check()
            after = path.lstat()
            base._parents(path)
            if base._linked(after) or after.st_nlink != 1 or base._stamp(after) != base._stamp(info):
                raise ValueError('Source changed during the scan; select it again.')
            manifest = asset.manifest()
            del asset
            digest = manifest['sha256']
            found = digest in existing
            if not found and existing_contains is not None:
                found = existing_contains(digest)
                if type(found) is not bool:
                    raise ValueError('The authorized existing-asset lookup must return a boolean.')
            check()
            if found:
                row = base._row(path, root, 'duplicate',
                    'Exact source bytes already exist in this library.', manifest, 'existing:' + digest)
            elif digest in digests:
                row = base._row(path, root, 'duplicate',
                    'Exact source bytes match an earlier file in this scan page.', manifest, digests[digest])
            else:
                digests[digest] = str(path)
                row = base._row(path, root, 'queued', 'Ready to import.', manifest)
            append(row)
        except (InterruptedError, TimeoutError):
            raise
        except (ValueError, OSError) as exc:
            reject(path, root, str(exc))

    # At most one names page is live. Descending drops the parent's temporary
    # names list; the persistent stack contains identities/positions only.
    names, cached_relative = deque(), None
    while index < len(roots) and entries < entry_budget and examined < file_budget:
        check()
        root = roots[index]
        if not stack:
            try:
                base._parents(root)
                info = root.lstat()
                if base._linked(info):
                    raise ValueError('Links and reparse points are not imported.')
                if stat.S_ISDIR(info.st_mode):
                    stack.append(dict(relative='', after=None, identity=list(base._stamp(info))))
                else:
                    entries += 1
                    file(root, root, info)
                    index += 1
                    continue
            except (InterruptedError, TimeoutError):
                raise
            except (OSError, ValueError) as exc:
                entries += 1
                reject(root, root, str(exc))
                index += 1
                continue
        frame = stack[-1]
        directory = root.joinpath(*PurePosixPath(frame['relative']).parts) if frame['relative'] else root
        _directory(directory, frame['identity'])
        if cached_relative != (index, frame['relative']) or not names:
            names = _names(directory, frame['after'], min(entry_budget - entries, file_budget - examined),
                           frame['identity'], check)
            cached_relative = index, frame['relative']
        if not names:
            stack.pop()
            cached_relative = None
            if not stack:
                index += 1
            continue
        name = names.popleft()
        path = directory / name
        entries += 1
        try:
            if not _child_name(name):
                raise ValueError('Source name cannot be represented safely in an import plan.')
            if os.path.normcase(str(path)) in selected:
                frame['after'] = name
                continue
            base._parents(path)
            info = path.lstat()
            if base._linked(info):
                raise ValueError('Links and reparse points are not imported.')
            if stat.S_ISDIR(info.st_mode):
                if not recursive:
                    reject(path, root, 'Subfolder skipped because recursive scanning is off.')
                elif len(stack) >= base.MAX_DEPTH:
                    reject(path, root, 'Folder depth limit reached; this subfolder was skipped.')
                else:
                    frame['after'] = name
                    stack.append(dict(relative=path.relative_to(root).as_posix(), after=None,
                                      identity=list(base._stamp(info))))
                    names, cached_relative = deque(), None
                    continue
            else:
                file(path, root, info)
        except (InterruptedError, TimeoutError):
            raise
        except (OSError, ValueError) as exc:
            reject(path, root, str(exc))
        frame['after'] = name
    check()
    if index < len(roots):
        for frame in stack:
            path = roots[index].joinpath(*PurePosixPath(frame['relative']).parts) if frame['relative'] else roots[index]
            _directory(path, frame['identity'])
    if omitted:
        append(base._row(omitted_path, omitted_path, 'rejected',
            f'{omitted} additional entries were rejected or skipped; {rejections} total in this scan page.'))
    continuation = _encode(binding, index, stack) if index < len(roots) else None
    return ScanPage(tuple(rows), continuation, entries, examined, rejections)


# Durable batch metadata is a pure contract shared by live queues and backups.
BATCH_SCAN_SCHEMA = 'carr-import-batch-scan/1'
_SCAN_KEYS = {'schema', 'roots', 'input_cursor', 'next_cursor', 'page',
              'previous_batch', 'next_batch', 'resumable', 'file_budget',
              'entry_budget', 'timeout', 'examined_files', 'scanned_entries',
              'rejected_entries'}


def selection_binding(roots, recursive):
    return sha256(_canonical([list(roots), recursive])).hexdigest()


def successor_id(parent_id, cursor):
    if (not isinstance(parent_id, str) or len(parent_id) != 32
            or any(c not in '0123456789abcdef' for c in parent_id)
            or cursor is None):
        raise ValueError('A saved scan page and continuation are required.')
    validate_cursor(cursor)
    return sha256(_canonical(['carr-import-scan-successor/1', parent_id, cursor])).hexdigest()[:32]


def validate_batch_scan(value, *, batch_id, recursive):
    """Validate exact archived metadata without opening any selected root."""
    if type(value) is not dict or set(value) != _SCAN_KEYS or value['schema'] != BATCH_SCAN_SCHEMA:
        raise ValueError('Invalid saved import scan metadata.')
    roots = value['roots']
    if (type(recursive) is not bool or type(roots) is not list or not 1 <= len(roots) <= base.MAX_ROOTS
            or type(value['page']) is not int or not 1 <= value['page'] <= 1000
            or type(value['resumable']) is not bool
            or type(value['file_budget']) is not int or not 1 <= value['file_budget'] <= base.MAX_FILES
            or type(value['entry_budget']) is not int or not 1 <= value['entry_budget'] <= base.MAX_SCAN_ENTRIES
            or type(value['timeout']) not in (int, float) or not math.isfinite(value['timeout'])
            or not 0 < value['timeout'] <= 3600):
        raise ValueError('Invalid saved import scan bounds.')
    for root in roots:
        if (not isinstance(root, str) or not 0 < len(root) <= 32768
                or root.startswith(('//', '\\\\')) or any(ord(c) < 32 for c in root)):
            raise ValueError('Invalid saved import scan root.')
        path = PureWindowsPath(root) if PureWindowsPath(root).drive else PurePosixPath(root)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('Invalid saved import scan root.')
    if len(set(roots)) != len(roots) or len(_canonical(roots)) > 512 * 1024:
        raise ValueError('Saved import scan selection exceeds its metadata budget.')
    for name, maximum in (('examined_files', value['file_budget']),
                          ('scanned_entries', value['entry_budget']),
                          ('rejected_entries', value['entry_budget'])):
        if type(value[name]) is not int or not 0 <= value[name] <= maximum:
            raise ValueError('Invalid saved import scan counters.')
    if value['examined_files'] > value['scanned_entries']:
        raise ValueError('Saved import scan counters disagree.')
    binding = selection_binding(roots, recursive)
    for name in ('input_cursor', 'next_cursor'):
        validate_cursor(value[name], root_count=len(roots), binding=binding)
    previous = value['previous_batch']
    if previous is None:
        if value['page'] != 1 or value['input_cursor'] is not None:
            raise ValueError('The first scan page has an invalid predecessor.')
    elif (not isinstance(previous, str) or len(previous) != 32
          or any(c not in '0123456789abcdef' for c in previous)
          or value['page'] < 2 or value['input_cursor'] is None
          or successor_id(previous, value['input_cursor']) != batch_id):
        raise ValueError('Saved scan successor identity changed.')
    if (value['next_batch'] is not None and
            (value['next_cursor'] is None or value['next_batch'] != successor_id(batch_id, value['next_cursor']))):
        raise ValueError('Saved scan continuation refers to a different successor.')
    if value['input_cursor'] is not None and value['input_cursor'] == value['next_cursor']:
        raise ValueError('Saved import scan did not make forward progress.')
    return value


def validate_scan_successor(parent, child):
    """Correlate saved pages, including the crash-before-parent-link interval."""
    if parent.get('version') != 2 or child.get('version') != 2:
        raise ValueError('Saved scan succession requires version-two batches.')
    first, second = parent['scan'], child['scan']
    validate_batch_scan(first, batch_id=parent['batch_id'], recursive=parent['recursive'])
    validate_batch_scan(second, batch_id=child['batch_id'], recursive=child['recursive'])
    if (first['next_cursor'] is None
            or child['batch_id'] != successor_id(parent['batch_id'], first['next_cursor'])
            or first['next_batch'] not in (None, child['batch_id'])
            or second['previous_batch'] != parent['batch_id']
            or second['input_cursor'] != first['next_cursor']
            or second['page'] != first['page'] + 1
            or second['roots'] != first['roots']
            or second['resumable'] != first['resumable']
            or any(child[key] != parent[key] for key in
                   ('data_dir', 'workspace', 'recursive', 'collections', 'config'))):
        raise ValueError('Saved scan page differs from its predecessor.')
    return child


def validate_scan_relations(batches):
    """A complete backup carries every linked page; archived cursors stay data."""
    for batch in batches.values():
        if batch.get('version') != 2:
            continue
        scan = validate_batch_scan(batch['scan'], batch_id=batch['batch_id'], recursive=batch['recursive'])
        previous = scan['previous_batch']
        if previous is not None:
            parent = batches.get(previous)
            if parent is None:
                raise ValueError('Saved import scan predecessor is missing.')
            validate_scan_successor(parent, batch)
        following = scan['next_batch']
        if following is not None:
            child = batches.get(following)
            if child is None:
                raise ValueError('Saved import scan successor is missing.')
            validate_scan_successor(batch, child)
