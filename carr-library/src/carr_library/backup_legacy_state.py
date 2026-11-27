"""Read compatibility for the canonical state-folder backup formats 3 and 4.

Only the exact historical manifest shape selects this adapter. These files are
preserved as saved data; they do not grant current assistant approvals or weaken
the typed job, project, provider, or source validators used by live services.
New backups use the registered current producers, never this generic walker.
"""
from contextlib import nullcontext
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re

from .backup_files import local_path, regular, sync_directory

MAX_STATE_FILES = 100_000
MAX_STATE_BYTES = 32 * 1024**3


def relative_name(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid legacy state manifest path.')
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value
            or any(part in ('.', '..') or part.endswith((' ', '.')) for part in path.parts)):
        raise ValueError('Invalid legacy state manifest path.')
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10))}
    if any(part.split('.')[0].casefold() in reserved for part in path.parts):
        raise ValueError('Invalid portable legacy state filename.')
    return path


def validate_state(entries, *, max_files=MAX_STATE_FILES, max_bytes=MAX_STATE_BYTES):
    if not isinstance(entries, list) or len(entries) > min(max_files, MAX_STATE_FILES):
        raise ValueError('Invalid legacy application state manifest.')
    names, directories, total = set(), set(), 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid legacy application state manifest.')
        name = relative_name(item['filename'])
        folded = name.as_posix().casefold()
        parents = {parent.as_posix().casefold() for parent in name.parents if str(parent) != '.'}
        if folded in names or folded in directories or parents & names:
            raise ValueError('Conflicting legacy application state filename.')
        names.add(folded)
        directories.update(parents)
        total += item['size_bytes']
    if total > min(max_bytes, MAX_STATE_BYTES):
        raise ValueError('Legacy application state exceeds its byte budget.')


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _inventory(source, entries, cancelled):
    expected = {item['filename'] for item in entries}
    # The historical producer omitted empty directories. They are harmless,
    # but their traversal still needs a finite bound.
    limit = min(MAX_STATE_FILES * 8, 1 + sum(len(PurePosixPath(name).parts) for name in expected)
                + MAX_STATE_FILES)
    count, found, aliases = 0, {}, {}
    pending = [source]
    while pending:
        directory = pending.pop()
        if cancelled():
            raise InterruptedError('Library backup or restore cancelled.')
        with os.scandir(directory) as children:
            for entry in children:
                count += 1
                if count > limit:
                    raise ValueError('Legacy state directory budget exceeded.')
                path = Path(entry.path)
                relative = relative_name(path.relative_to(source).as_posix()).as_posix()
                folded = relative.casefold()
                if folded in aliases:
                    raise ValueError('Conflicting legacy state path aliases.')
                aliases[folded] = relative
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                else:
                    found[relative] = _stamp(regular(path))
                    if len(found) > len(expected):
                        raise ValueError('Backup has missing or unlisted legacy state.')
    if set(found) != expected:
        raise ValueError('Backup has missing or unlisted legacy state.')
    return found


def verify_state(source, target, entries, *, cancelled=lambda: False, destination_name=None):
    """Stream verified bytes, optionally into a fresh private staging tree."""
    validate_state(entries)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    if target is not None:
        target = local_path(target)
        target.mkdir(mode=0o700)
    before = _inventory(source, entries, cancelled)
    directories = set()
    for item in entries:
        if cancelled():
            raise InterruptedError('Library backup or restore cancelled.')
        name = item['filename']
        incoming = local_path(source / name, root=source, exists=True)
        if regular(incoming).st_size != item['size_bytes'] or _stamp(regular(incoming)) != before[name]:
            raise ValueError('Legacy application state size or identity mismatch.')
        saved_name = relative_name(destination_name(name) if destination_name else name)
        destination = target / saved_name if target is not None else None
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directories.update((destination.parent, *destination.parent.parents))
        digest, size = sha256(), 0
        with incoming.open('rb') as read, (destination.open('xb') if destination is not None else nullcontext()) as write:
            if _stamp(os.fstat(read.fileno())) != before[name]:
                raise ValueError('Legacy application state changed during read.')
            while block := read.read(min(1024 * 1024, item['size_bytes'] + 1 - size)):
                if cancelled():
                    raise InterruptedError('Library backup or restore cancelled.')
                size += len(block)
                if size > item['size_bytes']:
                    raise ValueError('Legacy application state grew during read.')
                digest.update(block)
                if write is not None:
                    write.write(block)
            if write is not None:
                write.flush()
                os.fsync(write.fileno())
            if _stamp(os.fstat(read.fileno())) != before[name]:
                raise ValueError('Legacy application state changed during read.')
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Legacy application state checksum mismatch.')
    if _inventory(source, entries, cancelled) != before:
        raise ValueError('Legacy application state changed during verification.')
    if target is not None:
        for directory in sorted((path for path in directories if path.is_relative_to(target)), key=lambda p: len(p.parts), reverse=True):
            sync_directory(directory)
        sync_directory(target)
