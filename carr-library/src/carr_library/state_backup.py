"""Manifest-bound private application sidecars for complete library backups.

Snapshot committed JSON/project files and optionally generated audio. Atomic
writers may continue: a changing file causes a clear retryable failure, never
a successful partial snapshot. Rebuildable audio cache is optional; a restored
render checks every receipt and regenerates missing/corrupt chunks.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .durable_state import local_path, regular, state_lock, sync_directory

MAX_STATE_FILES = 100_000
MAX_STATE_BYTES = 32 * 1024**3
_CUE_SOURCE = re.compile(r'studio/cache/cue-[0-9a-f]{64}-[0-9a-f]{10}\.[a-z0-9]{1,8}')


def relative_name(value):
    if (type(value) is not str or not value or len(value) > 4096
            or any(c in value for c in '\\:<>"|?*')
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('Invalid state manifest path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or not path.parts or len(path.parts) > 64
            or any(x in ('.', '..') or x.endswith((' ', '.')) for x in path.parts)):
        raise ValueError('Invalid state manifest path.')
    if any(x.split('.')[0].casefold() in {'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),
                                        *(f'lpt{i}' for i in range(1,10))} for x in path.parts):
        raise ValueError('Invalid portable state filename.')
    return path


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('A backup cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Backup or restore cancelled before completion.')


@contextmanager
def _stable_reader(path):
    path = local_path(path, exists=True)
    before = regular(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        # Windows path and descriptor stat use different identity APIs. Compare
        # each identity only with a later result from that same API; size/type
        # still bind the opened descriptor before any bytes are accepted.
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_size != before.st_size
                or (os.name != 'nt' and _stamp(opened) != _stamp(before))):
            raise ValueError('Application state changed while opening a file.')
        yield stream, before
        if (_stamp(os.fstat(stream.fileno())) != _stamp(opened)
                or _stamp(regular(path)) != _stamp(before)):
            raise ValueError('Application state changed while reading a file.')


def read_state_bytes(path, maximum, *, cancelled=lambda: False):
    """Read one immutable bounded sidecar without platform-specific inode assumptions."""
    _check(cancelled)
    with _stable_reader(path) as (stream, info):
        if not 0 < info.st_size <= maximum:
            raise ValueError('Application sidecar is empty or exceeds its byte budget.')
        raw = stream.read(maximum + 1)
    if len(raw) != info.st_size:
        raise ValueError('Application sidecar changed during backup.')
    _check(cancelled)
    return raw


def _inventory(root, include_generated, cancelled, max_files, *, filtering=True):
    found, names, pending, count = {}, set(), [(root, False)], 0
    while pending:
        directory, omitted = pending.pop()
        regular(directory, directory=True)
        with os.scandir(directory) as children:
            for child in children:
                _check(cancelled)
                count += 1
                if count > max_files:
                    raise ValueError('Application state exceeds the file and directory entry budget.')
                path = Path(child.path)
                relative = path.relative_to(root)
                relative_name(relative.as_posix())
                key = relative.as_posix().casefold()
                if key in names:
                    raise ValueError('Application state has colliding portable paths.')
                names.add(key)
                info = path.lstat()
                is_directory = stat.S_ISDIR(info.st_mode)
                info = regular(path, directory=is_directory)
                # Traverse cache directories for bounded validation and retained
                # accepted cue inputs, even when generated audio is excluded.
                excluded = omitted or (filtering and not _include(relative, include_generated or is_directory))
                if is_directory:
                    pending.append((path, excluded))
                elif not excluded:
                    found[relative] = _stamp(info)
    return found


def _hash_state(path, cancelled):
    digest = sha256()
    with _stable_reader(path) as (stream, info):
        count = 0
        while block := stream.read(min(1024 * 1024, info.st_size + 1 - count)):
            _check(cancelled)
            count += len(block)
            if count > info.st_size:
                raise ValueError('Application state grew during verification.')
            digest.update(block)
    if count != info.st_size:
        raise ValueError('Application state was truncated during verification.')
    return digest.hexdigest()


@contextmanager
def _production_guard(root):
    production = root / 'studio'
    if os.path.lexists(production):
        regular(local_path(production, exists=True), directory=True)
        with state_lock(production, 'production'):
            yield
    else:
        yield


def _include(path, generated):
    return (not any(part.startswith('.pending-') or part.endswith(('.lock', '.tmp')) for part in path.parts)
            and (generated or 'cache' not in path.parts or _CUE_SOURCE.fullmatch(path.as_posix())))


def snapshot_state(root, target, *, include_generated=False, max_bytes=MAX_STATE_BYTES,
                   max_files=MAX_STATE_FILES, cancelled=lambda: False):
    _check(cancelled)
    if (type(include_generated) is not bool or type(max_bytes) is not int or not 0 <= max_bytes <= MAX_STATE_BYTES
            or type(max_files) is not int or not 1 <= max_files <= MAX_STATE_FILES):
        raise ValueError('Invalid application-state backup budgets.')
    root = Path(root)
    if not os.path.lexists(root):
        return []
    root = local_path(root, exists=True)
    regular(root, directory=True)
    target = local_path(target)
    if target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('Application-state snapshot destination must be separate from its source.')
    with _production_guard(root):
        before_inventory = _inventory(root, include_generated, cancelled, max_files)
        entries, total = [], 0
        for relative, identity in sorted(before_inventory.items()):
            _check(cancelled)
            source = root / relative
            if source.suffix.casefold() in ('.sqlite', '.db') or source.name.endswith(('-wal', '-shm', '-journal')):
                raise ValueError('An active state database needs its own consistent snapshot adapter.')
            before = regular(source)
            if _stamp(before) != identity:
                raise ValueError('Application state changed during backup; retry after saving.')
            total += before.st_size
            if total > max_bytes:
                raise ValueError('Application state exceeds the backup budget.')
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            local_path(destination.parent, exists=True)
            digest, count = sha256(), 0
            with _stable_reader(source) as (read, _), destination.open('xb') as write:
                while block := read.read(min(1024 * 1024, before.st_size + 1 - count)):
                    _check(cancelled)
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Application state grew during backup; retry after saving.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
            if count != before.st_size or _stamp(regular(source)) != identity:
                raise ValueError('Application state changed during backup; retry after saving.')
            entries.append({'filename': relative.as_posix(), 'size_bytes': count, 'sha256': digest.hexdigest()})
        if _inventory(root, include_generated, cancelled, max_files) != before_inventory:
            raise ValueError('Application state changed during backup; retry after saving.')
        for entry in entries:
            if _hash_state(root / entry['filename'], cancelled) != entry['sha256']:
                raise ValueError('Application state changed after capture; retry after saving.')
        if _inventory(root, include_generated, cancelled, max_files) != before_inventory:
            raise ValueError('Application state changed during verification; retry after saving.')
        return entries


def validate_state(entries):
    if not isinstance(entries, list) or len(entries) > MAX_STATE_FILES:
        raise ValueError('Invalid application state manifest.')
    names, total = set(), 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid application state manifest.')
        name = relative_name(item['filename']).as_posix()
        if name.casefold() in names:
            raise ValueError('Duplicate application state filename.')
        names.add(name.casefold())
        total += item['size_bytes']
    if total > MAX_STATE_BYTES:
        raise ValueError('Application state manifest exceeds its byte budget.')


def restore_state(source, target, entries, *, cancelled=lambda: False):
    _check(cancelled)
    validate_state(entries)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    found = _inventory(source, True, cancelled, MAX_STATE_FILES, filtering=False)
    if {name.as_posix() for name in found} != {item['filename'] for item in entries}:
        raise ValueError('Backup has missing or unlisted application state.')
    target = local_path(target)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError('Application-state restore must be separate from its source.')
    target.mkdir(parents=True, exist_ok=True)
    for item in entries:
        _check(cancelled)
        incoming = local_path(source / item['filename'], root=source, exists=True)
        if regular(incoming).st_size != item['size_bytes']:
            raise ValueError('Application state size mismatch.')
        destination = target / item['filename']
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = sha256(), 0
        local_path(destination.parent, exists=True)
        with _stable_reader(incoming) as (read, _), destination.open('xb') as write:
            while block := read.read(min(1024 * 1024, item['size_bytes'] + 1 - size)):
                _check(cancelled)
                size += len(block)
                if size > item['size_bytes']:
                    raise ValueError('Application state changed during restore.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Application state checksum mismatch.')
    if _inventory(source, True, cancelled, MAX_STATE_FILES, filtering=False) != found:
        raise ValueError('Application state changed during restore.')
    for item in entries:
        if _hash_state(source / item['filename'], cancelled) != item['sha256']:
            raise ValueError('Application state changed during restore verification.')
    if _inventory(source, True, cancelled, MAX_STATE_FILES, filtering=False) != found:
        raise ValueError('Application state changed during restore verification.')
    _check(cancelled)
    sync_directory(target)


def publish_state(source, target, *, cancelled=lambda: False):
    _check(cancelled)
    source = local_path(source, exists=True)
    inventory = _inventory(source, True, cancelled, MAX_STATE_FILES, filtering=False)
    target = Path(target)
    target.mkdir(mode=0o700)
    directories = {target}
    for relative in sorted(inventory):
        _check(cancelled)
        path = source / relative
        if _stamp(regular(path)) != inventory[relative]:
            raise ValueError('Application state changed before publication.')
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, destination)
        parent = destination.parent
        while parent != target:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        sync_directory(directory)
    sync_directory(target)
