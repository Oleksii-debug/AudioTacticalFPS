"""Manifest-bound private application sidecars for complete library backups.

Snapshot committed JSON/project files and optionally generated audio. Atomic
writers may continue: a changing file causes a clear retryable failure, never
a successful partial snapshot. Rebuildable audio cache is optional; a restored
render checks every receipt and regenerates missing/corrupt chunks.
"""
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .durable_state import local_path, regular, sync_directory

MAX_STATE_FILES = 100_000
MAX_STATE_BYTES = 32 * 1024**3


def relative_name(value):
    if (not isinstance(value, str) or not value
            or any(c in '<>:"\\|?*' or ord(c) < 32 for c in value)):
        raise ValueError('Invalid state manifest path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(x in ('.', '..') or x.endswith((' ', '.')) for x in path.parts)):
        raise ValueError('Invalid state manifest path.')
    # Win32 also reserves superscript 1, 2 and 3 for COM/LPT devices.
    # A name valid on a POSIX source must not create an unrestorable backup.
    digits = '123456789\u00b9\u00b2\u00b3'
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
                *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in digits)}
    if any(x.split('.')[0].rstrip(' ').casefold() in reserved for x in path.parts):
        raise ValueError('Invalid portable state filename.')
    return path


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _include(path, generated):
    return (not any(part.startswith('.pending-') or part.endswith(('.lock', '.tmp')) for part in path.parts)
            and (generated or 'cache' not in path.parts))


def _walk_error(error):
    # os.walk silently suppresses directory enumeration errors by default.
    # An unreadable subtree is not evidence that it contains no user state.
    raise error


def snapshot_state(root, target, *, include_generated=False, max_bytes=MAX_STATE_BYTES,
                   cancelled=lambda: False, missing_ok=True):
    root = Path(root)
    if cancelled():
        raise InterruptedError('Backup cancelled.')
    try:
        root.lstat()
    except FileNotFoundError:
        if not missing_ok:
            raise
        return []
    root = local_path(root, exists=True)
    regular(root, directory=True)
    def inventory():
        found = {}
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
            if cancelled():
                raise InterruptedError('Backup cancelled.')
            for name in dirs:
                regular(Path(directory) / name, directory=True)
            dirs[:] = [name for name in dirs if _include((Path(directory) / name).relative_to(root), include_generated)]
            for name in sorted(files):
                source = Path(directory) / name
                relative = source.relative_to(root)
                if _include(relative, include_generated):
                    found[relative] = _stamp(regular(source))
                    if len(found) > MAX_STATE_FILES:
                        raise ValueError('Application state exceeds the file budget.')
        return found
    before_inventory = inventory()
    entries, total = [], 0
    for relative in sorted(before_inventory):
            source = root / relative
            relative_name(relative.as_posix())
            if source.suffix.casefold() in ('.sqlite', '.db') or source.name.endswith(('-wal', '-shm', '-journal')):
                raise ValueError('An active state database needs its own consistent snapshot adapter.')
            before = regular(source)
            if _stamp(before) != before_inventory[relative]:
                raise ValueError('Application state changed during backup; retry after saving.')
            total += before.st_size
            if len(entries) >= MAX_STATE_FILES or total > max_bytes:
                raise ValueError('Application state exceeds the backup budget.')
            destination = Path(target) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, count = sha256(), 0
            with source.open('rb') as read, destination.open('xb') as write:
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Backup cancelled.')
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Application state grew during backup; retry after saving.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
            if count != before.st_size or _stamp(regular(source)) != _stamp(before):
                raise ValueError('Application state changed during backup; retry after saving.')
            entries.append({'filename': relative.as_posix(), 'size_bytes': count, 'sha256': digest.hexdigest()})
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # File metadata is not a content identity: on Windows an equal-size write
    # can retain the observed timestamps. Recheck every copied source against
    # its manifest hash before the caller can publish this snapshot.
    for entry in entries:
        if cancelled():
            raise InterruptedError('Backup cancelled.')
        relative = Path(entry['filename'])
        source = root / relative
        expected_stamp = before_inventory[relative]
        if _stamp(regular(source)) != expected_stamp:
            raise ValueError('Application state changed during backup; retry after saving.')
        digest, count = sha256(), 0
        with source.open('rb') as read:
            if _stamp(os.fstat(read.fileno())) != expected_stamp:
                raise ValueError('Application state changed during backup; retry after saving.')
            while block := read.read(min(1024 * 1024, entry['size_bytes'] + 1 - count)):
                if cancelled():
                    raise InterruptedError('Backup cancelled.')
                count += len(block)
                if count > entry['size_bytes']:
                    raise ValueError('Application state grew during backup; retry after saving.')
                digest.update(block)
            if _stamp(os.fstat(read.fileno())) != expected_stamp:
                raise ValueError('Application state changed during backup; retry after saving.')
        if (count != entry['size_bytes'] or digest.hexdigest() != entry['sha256']
                or _stamp(regular(source)) != expected_stamp):
            raise ValueError('Application state changed during backup; retry after saving.')
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # A snapshot must pass the same portable manifest checks as a restore.
    # Keep this after content revalidation and before publication.
    validate_state(entries)
    return sorted(entries, key=lambda entry: entry['filename'])


def validate_state(entries, *, max_bytes=None):
    # Historical readers keep their original cap. Only the already typed
    # restored projection supplies its separately checked aggregate budget.
    maximum = MAX_STATE_BYTES if max_bytes is None else max_bytes
    if type(maximum) is not int or maximum < 1:
        raise ValueError('Application state validation needs a positive byte budget.')
    if not isinstance(entries, list) or len(entries) > MAX_STATE_FILES:
        raise ValueError('Invalid application state manifest.')
    names, directories, portable_paths, total = set(), set(), {}, 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid application state manifest.')
        name = relative_name(item['filename']).as_posix()
        key = unicodedata.normalize('NFC', name).casefold()
        if key in names:
            raise ValueError('Duplicate application state filename.')
        names.add(key)
        # Reuse the typed sidecar portable-path policy, including ancestors.
        # A/one and a/two may coexist on POSIX but alias on Windows.
        path = PurePosixPath(name)
        for component in (path, *path.parents):
            current = str(component)
            if current == '.':
                continue
            portable = unicodedata.normalize('NFC', current).casefold()
            previous = portable_paths.setdefault(portable, current)
            if previous != current:
                raise ValueError('Application state paths collide on a portable filesystem.')
            if component != path:
                directories.add(portable)
        total += item['size_bytes']
    if names & directories:
        raise ValueError('Application state file and directory paths collide.')
    if total > maximum:
        raise ValueError('Application state manifest exceeds its byte budget.')


def restore_state(source, target, entries, *, cancelled=lambda: False):
    def check():
        if not callable(cancelled):
            raise ValueError('A cancellation callback is required.')
        if cancelled():
            raise InterruptedError('State restoration cancelled before publication.')
    check()
    validate_state(entries)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    missing = {item['filename'] for item in entries}
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=_walk_error):
        check()
        for name in dirs:
            regular(Path(directory) / name, directory=True)
        for name in files:
            path = Path(directory) / name
            regular(path)
            # Reject the first unexpected file rather than accumulating an
            # attacker-controlled inventory beyond the manifest's file budget.
            relative = path.relative_to(source).as_posix()
            if relative not in missing:
                raise ValueError('Backup has missing or unlisted application state.')
            missing.remove(relative)
    if missing:
        raise ValueError('Backup has missing or unlisted application state.')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in entries:
        check()
        incoming = local_path(source / item['filename'], root=source, exists=True)
        if regular(incoming).st_size != item['size_bytes']:
            raise ValueError('Application state size mismatch.')
        destination = target / item['filename']
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = sha256(), 0
        with incoming.open('rb') as read, destination.open('xb') as write:
            while block := read.read(1024 * 1024):
                check()
                size += len(block)
                if size > item['size_bytes']:
                    raise ValueError('Application state changed during restore.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Application state checksum mismatch.')
    check()
    sync_directory(target)


def publish_state(source, target):
    target = Path(target)
    target.mkdir(mode=0o700)
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_dir():
            (target / relative).mkdir(exist_ok=True)
        else:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target / relative)
    sync_directory(target)
