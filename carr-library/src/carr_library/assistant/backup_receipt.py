"""Compact verification receipts for every byte of a completed local backup.

The producer's trusted manifest hash identifies the backup. A second streamed
tree digest binds all files and directories, so resumed jobs cannot mistake an
unchanged manifest for an unchanged database or retained original. No backup is
executed, restored, or modified while a receipt is captured or checked.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat


_SHA = re.compile(r'[0-9a-f]{64}')
_SCHEMA = 'carr-backup-receipt/1'


def _digest(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError('A saved SHA256 is required for this backup.')
    return value


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'{name} must be a positive integer.')
    return value


def _plain(path, *, directory=False):
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))):
        raise ValueError('Backup receipts require ordinary local files and directories.')
    return value


def _root(value):
    path = Path(value).expanduser().absolute()
    if str(value).startswith(('\\\\', '//')) or '://' in str(value):
        raise ValueError('Choose a local backup directory.')
    for part in (path, *path.parents):
        _plain(part, directory=True)
    return path


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _same_handle(value, expected):
    # Win32 path stat derives executable permission bits from .cmd/.exe names;
    # fstat cannot. Keep exact type, identity, size and timestamps for handles,
    # while every path-to-path comparison still checks the full mode as well.
    actual = _stamp(value)
    return (actual[:2] == expected[:2]
            and stat.S_IFMT(actual[2]) == stat.S_IFMT(expected[2])
            and actual[3:] == expected[3:])


@dataclass(frozen=True)
class BackupReceipt:
    manifest_sha256: str
    tree_sha256: str
    total_bytes: int
    files: int
    directories: int
    schema: str = _SCHEMA

    def __post_init__(self):
        if self.schema != _SCHEMA:
            raise ValueError('Unsupported backup receipt.')
        _digest(self.manifest_sha256)
        _digest(self.tree_sha256)
        _positive(self.total_bytes, 'Backup bytes')
        _positive(self.files, 'Backup files')
        if type(self.directories) is not int or self.directories < 0:
            raise ValueError('Invalid backup directory count.')

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {
                'schema', 'manifest_sha256', 'tree_sha256', 'total_bytes', 'files', 'directories'}:
            raise ValueError('Invalid backup receipt fields.')
        return cls(**value)


def capture_backup_receipt(directory, manifest_sha256, *, max_bytes, max_files,
                           cancelled=lambda: False):
    """Hash a complete backup within the approved byte/file budget.

    The creating service must also pass its byte limit to ``app.backup`` so
    oversized output is rejected before publication. This scan independently
    validates the resulting artifact and keeps job checkpoint size constant.
    """
    _digest(manifest_sha256)
    _positive(max_bytes, 'Backup byte budget')
    _positive(max_files, 'Backup file budget')
    if not callable(cancelled):
        raise ValueError('A cancellation handler is required.')
    root = _root(directory)
    aggregate = sha256()
    total = files = directories = visited = 0
    found_manifest = False
    observed = {}

    def check():
        if cancelled():
            raise InterruptedError('Backup verification was cancelled.')

    def add(kind, relative, size=0, digest=''):
        raw = json.dumps([kind, relative, size, digest], ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
        aggregate.update(len(raw).to_bytes(8, 'big'))
        aggregate.update(raw)

    def walk(path, depth):
        nonlocal total, files, directories, visited, found_manifest
        check()
        if depth > 64:
            raise ValueError('Backup directory nesting exceeds the verification budget.')
        before = _plain(path, directory=True)
        names = []
        with os.scandir(path) as entries:
            for entry in entries:
                check()
                visited += 1
                # Bound empty-directory trees too, before allocating a list.
                if visited > max_files * 4 + 1024:
                    raise ValueError('Backup directory entries exceed the verification budget.')
                if '\\' in entry.name or ':' in entry.name:
                    raise ValueError('Backup paths must be portable local paths.')
                names.append(entry.name)
        for name in sorted(names):
            check()
            child = path / name
            relative = child.relative_to(root).as_posix()
            before_file = child.lstat()
            if stat.S_ISDIR(before_file.st_mode):
                _plain(child, directory=True)
                directories += 1
                add('directory', relative)
                walk(child, depth + 1)
                continue
            before_file = _plain(child)
            files += 1
            if files > max_files or total + before_file.st_size > max_bytes:
                raise ValueError('The complete backup exceeds the approved file or byte budget.')
            digest = sha256()
            count = 0
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(child, flags)
            with os.fdopen(fd, 'rb') as stream:
                if not _same_handle(os.fstat(stream.fileno()), _stamp(before_file)):
                    raise ValueError('A backup file changed during verification.')
                while block := stream.read(1024 * 1024):
                    check()
                    count += len(block)
                    if total + count > max_bytes or count > before_file.st_size:
                        raise ValueError('The complete backup exceeds its approved budget or changed while reading.')
                    digest.update(block)
                if not _same_handle(os.fstat(stream.fileno()), _stamp(before_file)):
                    raise ValueError('A backup file changed during verification.')
            if count != before_file.st_size or _stamp(_plain(child)) != _stamp(before_file):
                raise ValueError('A backup file changed during verification.')
            total += count
            checksum = digest.hexdigest()
            if relative == 'manifest.json':
                found_manifest = True
                if checksum != manifest_sha256:
                    raise ValueError('The backup manifest differs from the producer receipt.')
            add('file', relative, count, checksum)
            observed[child] = (False, _stamp(before_file), checksum)
        if _stamp(_plain(path, directory=True)) != _stamp(before):
            raise ValueError('The backup directory changed during verification.')
        observed[path] = (True, _stamp(before), tuple(sorted(names)))

    walk(root, 0)
    # Re-attest earlier entries after later files have been scanned. Directory
    # timestamps alone do not reveal in-place edits to an already hashed file.
    for path, (is_directory, stamp, expected) in observed.items():
        check()
        try:
            if _stamp(_plain(path, directory=is_directory)) != stamp:
                raise ValueError('The backup tree changed during verification.')
            if is_directory:
                names = []
                with os.scandir(path) as entries:
                    for entry in entries:
                        check()
                        if len(names) >= len(expected):
                            raise ValueError('The backup tree changed during verification.')
                        names.append(entry.name)
                if tuple(sorted(names)) != expected:
                    raise ValueError('The backup tree changed during verification.')
                continue
            # Windows can retain a timestamp across rapid same-size writes.
            # A second bounded content check must therefore attest bytes too.
            digest, count = sha256(), 0
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, 'rb') as stream:
                if not _same_handle(os.fstat(stream.fileno()), stamp):
                    raise ValueError('The backup tree changed during verification.')
                while block := stream.read(1024 * 1024):
                    check()
                    count += len(block)
                    if count > stamp[3]:
                        raise ValueError('The backup tree changed during verification.')
                    digest.update(block)
                if not _same_handle(os.fstat(stream.fileno()), stamp):
                    raise ValueError('The backup tree changed during verification.')
            if (count != stamp[3] or digest.hexdigest() != expected
                    or _stamp(_plain(path)) != stamp):
                raise ValueError('The backup tree changed during verification.')
        except (InterruptedError, TimeoutError):
            raise
        except OSError:
            raise ValueError('The backup tree changed during verification.') from None
    check()
    if not found_manifest:
        raise ValueError('A completed backup manifest is required.')
    return BackupReceipt(manifest_sha256, aggregate.hexdigest(), total, files, directories)


def verify_backup_receipt(directory, receipt, *, max_bytes, max_files, cancelled=lambda: False):
    expected = receipt if isinstance(receipt, BackupReceipt) else BackupReceipt.from_dict(receipt)
    actual = capture_backup_receipt(directory, expected.manifest_sha256,
        max_bytes=max_bytes, max_files=max_files, cancelled=cancelled)
    if actual != expected:
        raise ValueError('A completed backup changed; its database and sources must be reviewed before resuming.')
    return actual
