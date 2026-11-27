"""Bounded byte retention for exact canonical historical backup envelopes.

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
import unicodedata

from .backup_files import local_path, regular, sync_directory

MAX_STATE_FILES = 100_000
# The common canonical v4 format separates 32 GiB generated media from a
# 512 MiB metadata budget. Both remain bounded when retained as inert history.
MAX_STATE_BYTES = 32 * 1024**3 + 512 * 1024**2


def relative_name(value):
    if (not isinstance(value, str) or not value
            or any(c in '<>:"\\|?*' or ord(c) < 32 for c in value)):
        raise ValueError('Invalid legacy state manifest path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(x in ('.', '..') or x.endswith((' ', '.')) for x in path.parts)):
        raise ValueError('Invalid legacy state manifest path.')
    # Win32 also reserves superscript 1, 2 and 3 for COM/LPT devices.
    # A name valid on a POSIX source must not create an unrestorable backup.
    digits = '123456789\u00b9\u00b2\u00b3'
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
                *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in digits)}
    if any(x.split('.')[0].rstrip(' ').casefold() in reserved for x in path.parts):
        raise ValueError('Invalid portable legacy state filename.')
    return path


def validate_state(entries, *, max_files=MAX_STATE_FILES, max_bytes=MAX_STATE_BYTES):
    if not isinstance(entries, list) or len(entries) > min(max_files, MAX_STATE_FILES):
        raise ValueError('Invalid legacy application state manifest.')
    names, directories, portable_paths, total = set(), set(), {}, 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid legacy application state manifest.')
        name = relative_name(item['filename'])
        folded = unicodedata.normalize('NFC', name.as_posix()).casefold()
        if folded in names:
            raise ValueError('Conflicting legacy application state filename.')
        names.add(folded)
        # Preserve each actual spelling, including ancestors: A/one and a/two
        # are different source directories on POSIX and aliases on Windows.
        for component in (name, *name.parents):
            current = component.as_posix()
            if current == '.':
                continue
            portable = unicodedata.normalize('NFC', current).casefold()
            previous = portable_paths.setdefault(portable, current)
            if previous != current:
                raise ValueError('Conflicting portable legacy application state paths.')
            if component != name:
                directories.add(portable)
        total += item['size_bytes']
    if names & directories:
        raise ValueError('Conflicting legacy application state file and directory paths.')
    if total > min(max_bytes, MAX_STATE_BYTES):
        raise ValueError('Legacy application state exceeds its byte budget.')


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _inventory(source, entries, cancelled, *, ignored=()):
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
                # Only the root-level catalog components verified by the
                # enclosing backup reader may be excluded from this inventory.
                if relative in ignored:
                    continue
                folded = unicodedata.normalize('NFC', relative).casefold()
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


def verify_state(source, target, entries, *, cancelled=lambda: False, destination_name=None,
                 ignored=()):
    """Stream verified bytes, optionally into a fresh private staging tree."""
    validate_state(entries)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    if target is not None:
        target = local_path(target)
        target.mkdir(mode=0o700)
    before = _inventory(source, entries, cancelled, ignored=ignored)
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
    if _inventory(source, entries, cancelled, ignored=ignored) != before:
        raise ValueError('Legacy application state changed during verification.')
    # Windows creation time and coarse/restored mtimes cannot prove content
    # stability after copying. Reauthenticate the actual declared bytes too.
    for item in entries:
        path = local_path(source / item['filename'], root=source, exists=True)
        digest, size = sha256(), 0
        with path.open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != before[item['filename']]:
                raise ValueError('Legacy application state changed during verification.')
            while block := stream.read(min(1024 * 1024, item['size_bytes'] + 1 - size)):
                if cancelled():
                    raise InterruptedError('Library backup or restore cancelled.')
                size += len(block)
                digest.update(block)
                if size > item['size_bytes']:
                    raise ValueError('Legacy application state grew during verification.')
            if (_stamp(os.fstat(stream.fileno())) != before[item['filename']]
                    or _stamp(regular(path)) != before[item['filename']]):
                raise ValueError('Legacy application state changed during verification.')
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Legacy application state checksum changed after copying.')
    if target is not None:
        for directory in sorted((path for path in directories if path.is_relative_to(target)), key=lambda p: len(p.parts), reverse=True):
            sync_directory(directory)
        sync_directory(target)
