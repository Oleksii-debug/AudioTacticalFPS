"""Deterministic, streamed ZIP containers for already checked private backups.

This is deliberately a small data-only archive format. It never calls extractall,
follows links, executes content, or accepts a caller's arbitrary ZIP inventory.
The portable transfer receipt authenticates the outer bytes independently.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import struct
import unicodedata
from zipfile import ZIP_STORED, BadZipFile, ZipFile, ZipInfo


CHUNK = 1024 * 1024
MAX_FILES = 100_000
MAX_TOTAL = 1024 ** 4
MAX_DIRECTORY = 64 * 1024 * 1024
INVENTORY = 'carr-transfer.json'


def cancelled_check(cancelled):
    if cancelled():
        raise InterruptedError('Portable transfer cancelled; completed chunks can be reused.')


def local_path(value, *, exists=False, directory=False):
    raw = os.fspath(value)
    if (not isinstance(raw, str) or not raw or '\x00' in raw or '://' in raw
            or raw.startswith(('//', '\\\\')) or '..' in Path(raw).parts
            or any(unicodedata.category(ch) in ('Cc', 'Cs') for ch in raw)):
        raise ValueError('Choose a local filesystem path, not a URL or network share.')
    path = Path(raw).absolute()
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked or redirected paths cannot be used for portable snapshots.')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('A snapshot parent is not a directory.')
    if exists:
        info = path.lstat()
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError('Expected a local directory.')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Expected a single regular file.')
    return path


def _name(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or '\\' in value or ':' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('Unsafe snapshot archive path.')
    for part in value.split('/'):
        if (part.rstrip(' .') != part or PureWindowsPath(part).is_reserved()
                or any(unicodedata.category(ch) in ('Cc', 'Cs') or ch in '<>"|?*' for ch in part)):
            raise ValueError('Snapshot path has a Windows alias or invalid character.')
    return value


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_regular(path):
    path = local_path(path, exists=True)
    before = path.lstat()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    try:
        actual = os.fstat(fd)
        if (_identity(before) != _identity(actual) or not stat.S_ISREG(actual.st_mode)
                or actual.st_nlink != 1):
            raise ValueError('Snapshot file changed before reading.')
        return os.fdopen(fd, 'rb'), before
    except BaseException:
        os.close(fd)
        raise


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate snapshot metadata field.')
        result[key] = value
    return result


def read_json(path, *, max_bytes=1024 * 1024):
    stream, before = _open_regular(path)
    with stream:
        raw = stream.read(max_bytes + 1)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Snapshot metadata changed while reading.')
    if len(raw) > max_bytes:
        raise ValueError('Snapshot metadata exceeds the configured budget.')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid snapshot metadata.') from exc


def write_json(path, value):
    path = local_path(path)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    with path.open('xb') as out:
        os.chmod(path, 0o600)
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    return sha256(raw).hexdigest()


def hash_file(path, *, cancelled=lambda: False):
    cancelled_check(cancelled)
    digest = sha256()
    stream, before = _open_regular(path)
    with stream:
        while chunk := stream.read(CHUNK):
            cancelled_check(cancelled)
            digest.update(chunk)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError('Snapshot file changed while hashing.')
    return digest.hexdigest()


def pack_directory(source, destination, *, cancelled=lambda: False, on_status=None):
    source = local_path(source, exists=True, directory=True)
    destination = local_path(destination)
    if destination.is_relative_to(source):
        raise ValueError('Archive destination must be outside its source directory.')
    if os.path.lexists(destination):
        raise FileExistsError('Snapshot archive destination already exists.')
    entries, aliases, file_aliases, total = [], set(), set(), 0
    for directory, subdirs, names in os.walk(source, followlinks=False):
        cancelled_check(cancelled)
        local_path(directory, exists=True, directory=True)
        for name, is_directory in [(name, True) for name in subdirs] + [(name, False) for name in names]:
            path = local_path(Path(directory) / name, exists=True, directory=is_directory)
            relative = _name(path.relative_to(source).as_posix())
            alias = unicodedata.normalize('NFC', relative).casefold()
            if alias in aliases:
                raise ValueError('Snapshot contains duplicate portable path identities.')
            aliases.add(alias)
            if not is_directory:
                total += path.stat().st_size
                file_aliases.add(alias)
            entries.append((relative, path, is_directory))
            if len(entries) > MAX_FILES or total > MAX_TOTAL:
                raise ValueError('Snapshot exceeds the portable archive resource budget.')
    if INVENTORY not in file_aliases:
        raise ValueError('Portable snapshot metadata is missing.')
    _check_parent_aliases(file_aliases, aliases)
    output_owned = False
    try:
        with destination.open('xb') as outer:
            output_owned = True
            os.chmod(destination, 0o600)
            with ZipFile(outer, 'w', compression=ZIP_STORED, allowZip64=True) as archive:
                for index, (relative, path, is_directory) in enumerate(sorted(entries), 1):
                    cancelled_check(cancelled)
                    info = ZipInfo(relative + ('/' if is_directory else ''), date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = ((stat.S_IFDIR | 0o700) if is_directory else (stat.S_IFREG | 0o600)) << 16
                    if is_directory:
                        local_path(path, exists=True, directory=True)
                        archive.writestr(info, b'')
                        continue
                    incoming, before = _open_regular(path)
                    with incoming, archive.open(info, 'w', force_zip64=True) as outgoing:
                        while block := incoming.read(CHUNK):
                            cancelled_check(cancelled)
                            outgoing.write(block)
                        if _identity(os.fstat(incoming.fileno())) != _identity(before):
                            raise ValueError('Snapshot file changed while archiving.')
                    if on_status:
                        on_status(f'Preparing portable snapshot: {index} of {len(entries)} entries.')
            outer.flush()
            os.fsync(outer.fileno())
        return {'files': len(file_aliases), 'bytes': total, 'sha256': hash_file(destination, cancelled=cancelled)}
    except BaseException:
        if output_owned:
            destination.unlink(missing_ok=True)
        raise


def _check_parent_aliases(file_aliases, all_aliases):
    for alias in all_aliases:
        parts = alias.split('/')
        if any('/'.join(parts[:index]) in file_aliases for index in range(1, len(parts))):
            raise ValueError('Snapshot archive contains a portable file/directory collision.')


def _preflight(stream, size, cancelled):
    """Bound central-directory allocation before ZipFile parses it.

    This application writes single-disk, comment-free stored ZIP/ZIP64 archives.
    PKWARE APPNOTE 6.3.10 sections 4.3.14-16 specify the fixed end records.
    Other ZIP variants are deliberately outside this private transfer format.
    """
    if size < 22:
        raise ValueError('Truncated snapshot archive.')
    end_offset = size - 22
    stream.seek(end_offset)
    signature, disk, directory_disk, disk_count, count, directory_size, offset, comment = struct.unpack('<4s4H2LH', stream.read(22))
    if (signature != b'PK\x05\x06' or disk or directory_disk or comment
            or disk_count != count):
        raise ValueError('Unsupported snapshot archive end record.')
    legacy = (count, directory_size, offset)
    locator = b''
    if end_offset >= 20:
        stream.seek(end_offset - 20)
        locator = stream.read(20)
    if locator.startswith(b'PK\x06\x07') or count == 0xffff or directory_size == 0xffffffff or offset == 0xffffffff:
        if end_offset < 76:
            raise ValueError('Truncated ZIP64 snapshot end record.')
        signature, disk, end64_offset, disks = struct.unpack('<4sLQL', locator)
        if signature != b'PK\x06\x07' or disk or disks != 1 or end64_offset + 56 != end_offset - 20:
            raise ValueError('Unsupported ZIP64 snapshot locator.')
        stream.seek(end64_offset)
        values = struct.unpack('<4sQ2H2L4Q', stream.read(56))
        signature, record_size, _, _, disk, directory_disk, disk_count, count, directory_size, offset = values
        if (signature != b'PK\x06\x06' or record_size != 44 or disk
                or directory_disk or disk_count != count):
            raise ValueError('Unsupported ZIP64 snapshot end record.')
        if any(old != sentinel and old != actual for old, actual, sentinel in
                zip(legacy, (count, directory_size, offset), (0xffff, 0xffffffff, 0xffffffff))):
            raise ValueError('Conflicting snapshot directory lengths.')
        end_offset = end64_offset
    if (not 0 < count <= MAX_FILES or not 0 < directory_size <= MAX_DIRECTORY
            or offset < 30 or offset + directory_size != end_offset
            or directory_size < 46 * count):
        raise ValueError('Snapshot archive directory exceeds the resource budget or is inconsistent.')
    # ZipFile scans to the directory's end rather than enforcing EOCD's entry
    # count. Count its records ourselves before it allocates any ZipInfo objects.
    stream.seek(offset)
    for _ in range(count):
        cancelled_check(cancelled)
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b'PK\x01\x02':
            raise ValueError('Invalid snapshot central-directory record.')
        name_size, extra_size, comment_size = struct.unpack_from('<3H', header, 28)
        if not 0 < name_size <= 4096 or extra_size > 4096 or comment_size:
            raise ValueError('Unsupported snapshot central-directory metadata.')
        if stream.tell() + name_size + extra_size > offset + directory_size:
            raise ValueError('Truncated snapshot central directory.')
        stream.seek(name_size + extra_size, os.SEEK_CUR)
    if stream.tell() != offset + directory_size:
        raise ValueError('Snapshot directory count does not match its records.')
    stream.seek(0)
    return count


def unpack_archive(source, new_destination, *, cancelled=lambda: False, on_status=None):
    source = local_path(source, exists=True)
    target = local_path(new_destination)
    if os.path.lexists(target):
        raise FileExistsError('Snapshot unpack directory already exists.')
    # Caller supplies private staging. No incomplete unpack is a restored library.
    cancelled_check(cancelled)
    incoming_archive, before = _open_regular(source)
    try:
        with incoming_archive:
            expected_count = _preflight(incoming_archive, before.st_size, cancelled)
            return _unpack_open(incoming_archive, before, expected_count, target, cancelled, on_status)
    except (BadZipFile, RuntimeError, struct.error) as exc:
        raise ValueError('Invalid or damaged portable snapshot archive.') from exc


def _unpack_open(incoming_archive, before, expected_count, target, cancelled, on_status):
    with ZipFile(incoming_archive, 'r') as archive:
        entries = archive.infolist()
        if len(entries) != expected_count:
            raise ValueError('Invalid snapshot archive inventory.')
        names, aliases, file_aliases, total = set(), set(), set(), 0
        for item in entries:
            name = _name(item.filename[:-1] if item.is_dir() else item.filename)
            alias = unicodedata.normalize('NFC', name).casefold()
            mode = item.external_attr >> 16
            wanted_type = stat.S_IFDIR if item.is_dir() else stat.S_IFREG
            if (name in names or alias in aliases
                    or (stat.S_IFMT(mode) not in (0, wanted_type))
                    or item.flag_bits & 1 or item.compress_type != ZIP_STORED
                    or item.compress_size != item.file_size or (item.is_dir() and item.file_size)):
                raise ValueError('Unsupported or duplicated snapshot archive member.')
            names.add(name); aliases.add(alias)
            if not item.is_dir():
                file_aliases.add(alias)
            total += item.file_size
            if item.file_size < 0 or total > MAX_TOTAL:
                raise ValueError('Snapshot archive exceeds the resource budget.')
        if INVENTORY not in file_aliases:
            raise ValueError('Portable snapshot metadata is absent.')
        _check_parent_aliases(file_aliases, aliases)
        target.mkdir(mode=0o700)
        for index, item in enumerate(entries, 1):
            cancelled_check(cancelled)
            path = target.joinpath(*(item.filename.rstrip('/')).split('/'))
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                local_path(path, exists=True, directory=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            local_path(path.parent, exists=True, directory=True)
            size = 0
            with archive.open(item) as incoming, path.open('xb') as outgoing:
                os.chmod(path, 0o600)
                while block := incoming.read(CHUNK):
                    cancelled_check(cancelled)
                    size += len(block)
                    if size > item.file_size:
                        raise ValueError('Archive member exceeds its declared length.')
                    outgoing.write(block)
                outgoing.flush(); os.fsync(outgoing.fileno())
            if size != item.file_size:
                raise ValueError('Archive member was truncated.')
            if on_status:
                on_status(f'Checking portable snapshot: {index} of {len(entries)} files.')
    if _identity(os.fstat(incoming_archive.fileno())) != _identity(before):
        raise ValueError('Snapshot archive changed while reading.')
    return target
