"""Content-addressed storage for complete, typed library backup snapshots.

Every snapshot independently names all of its files. Objects are immutable,
verified full-file copies; there is no delta chain and no automatic deletion.
The current library_backup engine remains the authority for capture, semantic
validation and fresh-directory restoration. This saves retained disk space,
not the cost of capturing and checking a new complete backup.

Use a trusted, stable local directory. The cooperative writer guard does not
provide a sandbox against a hostile process changing the filesystem.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unicodedata

from .audio.jobs import verified_reader
from .backup_files import local_path, optional_present, portable_relative, regular, sync_directory
from .ingest.writer_guard import exclusive_writer, LOCK_NAME
from . import library_backup

FORMAT = b'{"kind":"carr-library-snapshot-repository","version":1}\n'
MARKER = 'repository.json'
OBJECTS = 'objects'
SNAPSHOTS = 'snapshots'
STAGING = 'staging'
MAX_FILES = 200000
MAX_DIRECTORIES = 200000
MAX_SNAPSHOTS = 10000
MAX_MANIFEST_BYTES = 64 * 1024**2
DEFAULT_MAX_BYTES = 256 * 1024**3
MAX_TOTAL_BYTES = 1024**5
BLOCK_BYTES = 1024**2
_SHA = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_sha256: str
    created_utc: str
    backup_manifest_sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class SnapshotReceipt:
    snapshot: SnapshotInfo
    new_objects: int
    new_bytes: int
    reused_files: int
    reused_bytes: int
    cleanup_warning: str | None = None

    @property
    def status(self):
        return 'complete'


@dataclass(frozen=True)
class RestoreReceipt:
    path: Path
    snapshot: SnapshotInfo
    cleanup_warning: str | None = None

    @property
    def status(self):
        return 'complete'


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Incremental backup or restore cancelled.')


def _budget(max_files, max_bytes, cancelled):
    if (type(max_files) is not int or not 1 <= max_files <= MAX_FILES
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_TOTAL_BYTES
            or not callable(cancelled)):
        raise ValueError('Invalid incremental snapshot budget or cancellation callback.')
    _check(cancelled)


def _digest(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError('Use the separately retained lowercase SHA-256 snapshot checksum.')
    return value


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate snapshot JSON key.')
        value[key] = item
    return value


def _not_finite(value):
    raise ValueError('Non-finite snapshot JSON value.')


def _read(path, limit, cancelled):
    info = regular(path)
    if info.st_size > limit:
        raise ValueError('Snapshot metadata exceeds its byte budget.')
    blocks, seen = [], 0
    with verified_reader(path) as stream:
        while True:
            _check(cancelled)
            block = stream.read(min(BLOCK_BYTES, limit - seen + 1))
            if not block:
                break
            seen += len(block)
            if seen > limit:
                raise ValueError('Snapshot metadata exceeds its byte budget.')
            blocks.append(block)
    if seen != info.st_size:
        raise ValueError('Snapshot metadata changed during verification.')
    return b''.join(blocks)


def _stream(path, *, size, digest, cancelled, destination=None):
    """Hash all bytes through the existing stable native reader; optionally copy."""
    info = regular(path)
    if info.st_size != size:
        raise ValueError('A snapshot object has the wrong size.')
    result, remaining = sha256(), size
    with verified_reader(path) as incoming:
        if destination is None:
            output = None
        else:
            output = Path(destination).open('xb')
        try:
            while remaining:
                _check(cancelled)
                block = incoming.read(min(BLOCK_BYTES, remaining))
                if not block:
                    raise ValueError('A snapshot object was truncated.')
                result.update(block)
                if output is not None:
                    output.write(block)
                remaining -= len(block)
            _check(cancelled)
            if incoming.read(1) or result.hexdigest() != digest:
                raise ValueError('A snapshot object failed its full-byte checksum.')
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
        finally:
            if output is not None:
                output.close()


def _hash(path, cancelled):
    info = regular(path)
    result, remaining = sha256(), info.st_size
    with verified_reader(path) as stream:
        while remaining:
            _check(cancelled)
            block = stream.read(min(BLOCK_BYTES, remaining))
            if not block:
                raise ValueError('A captured backup file was truncated.')
            result.update(block)
            remaining -= len(block)
        _check(cancelled)
        if stream.read(1):
            raise ValueError('A captured backup file grew during verification.')
    return info.st_size, result.hexdigest()


def _write(path, raw):
    with Path(path).open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(temporary, destination):
    """Atomic create-only publication. Return only a post-commit cleanup notice."""
    if os.name == 'nt':
        os.rename(temporary, destination)
        return None
    os.link(temporary, destination, follow_symlinks=False)
    try:
        temporary.unlink()
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            return ('The snapshot was saved. Cleanup of its private staging file '
                    'is still pending; do not repeat the completed save.')
    return None


def _repository(value, *, create=False):
    root = local_path(value)
    regular(root.parent, directory=True)
    if not optional_present(root):
        if not create:
            raise FileNotFoundError('The selected snapshot repository does not exist.')
        root.mkdir(mode=0o700)
        # The exact marker is last. An interrupted initialization is never
        # mistaken for a usable repository or silently repaired over user files.
        for name in (OBJECTS, SNAPSHOTS, STAGING):
            (root / name).mkdir(mode=0o700)
        _write(root / MARKER, FORMAT)
        sync_directory(root)
    regular(root, directory=True)
    names = set()
    with os.scandir(root) as entries:
        for entry in entries:
            names.add(entry.name)
            if len(names) > 5:
                raise ValueError('Unexpected snapshot repository entry.')
    required = {MARKER, OBJECTS, SNAPSHOTS, STAGING}
    if not required <= names or names - required - {LOCK_NAME}:
        raise ValueError('Choose an initialized snapshot repository or a new folder.')
    for name in (OBJECTS, SNAPSHOTS, STAGING):
        regular(root / name, directory=True)
    if _read(root / MARKER, len(FORMAT), lambda: False) != FORMAT:
        raise ValueError('Unsupported or incomplete snapshot repository.')
    if LOCK_NAME in names:
        if regular(root / LOCK_NAME).st_size != 0:
            raise ValueError('Invalid snapshot repository writer lock.')
    return root


def _outside(path, source):
    if path.is_relative_to(source) or source.is_relative_to(path):
        raise ValueError('Choose a destination outside the source and snapshot repository.')


def _path(value):
    return portable_relative(value, max_parts=2048)


def _alias(value):
    return unicodedata.normalize('NFC', value).casefold()


def _validate_inventory(files, directories, max_files, max_bytes):
    if (not isinstance(files, list) or not 2 <= len(files) <= max_files
            or not isinstance(directories, list) or len(directories) > MAX_DIRECTORIES):
        raise ValueError('Invalid snapshot inventory or file budget.')
    if any(not isinstance(name, str) for name in directories):
        raise ValueError('Invalid snapshot directory.')
    if directories != sorted(directories):
        raise ValueError('Snapshot directories must have a canonical order.')
    kinds, all_directories = {}, set(directories)
    for name in directories:
        _path(name)
        key = _alias(name)
        if key in kinds:
            raise ValueError('Duplicate or conflicting snapshot path.')
        kinds[key] = 'directory'
    total, previous, seen_files = 0, None, {}
    for row in files:
        if not isinstance(row, dict) or set(row) != {'path', 'size_bytes', 'sha256'}:
            raise ValueError('Invalid snapshot file record.')
        name, size = row['path'], row['size_bytes']
        _path(name)
        if (type(size) is not int or size < 0 or size > max_bytes
                or previous is not None and name <= previous):
            raise ValueError('Invalid snapshot size or canonical file order.')
        previous = name
        _digest(row['sha256'])
        key = _alias(name)
        if key in kinds:
            raise ValueError('Duplicate or conflicting snapshot path.')
        kinds[key] = 'file'
        seen_files[name] = row
        total += size
        if total > max_bytes:
            raise ValueError('Snapshot bytes exceed the selected total budget.')
    for name in [*directories, *seen_files]:
        parts = _path(name).parts
        for count in range(1, len(parts)):
            parent = '/'.join(parts[:count])
            if parent not in all_directories or kinds.get(_alias(parent)) != 'directory':
                raise ValueError('A snapshot path has a missing or conflicting directory.')
    if not {'manifest.json', 'library.sqlite'} <= set(seen_files):
        raise ValueError('A snapshot must contain the complete library backup.')
    return total, seen_files


def _inventory(source, max_files, max_bytes, cancelled):
    regular(source, directory=True)
    files, directories, pending, byte_count, name_bytes = [], [], [(source, '')], 0, 0
    while pending:
        directory, prefix = pending.pop()
        _check(cancelled)
        regular(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancelled)
                name = prefix + entry.name
                _path(name)
                name_bytes += len(name.encode('utf-8')) + 160
                if name_bytes > MAX_MANIFEST_BYTES:
                    raise ValueError('Snapshot inventory exceeds its metadata budget.')
                path = directory / entry.name
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    directories.append(name)
                    if len(directories) > MAX_DIRECTORIES:
                        raise ValueError('Snapshot directory budget exceeded.')
                    pending.append((path, name + '/'))
                else:
                    info = regular(path)
                    if len(files) >= max_files or byte_count + info.st_size > max_bytes:
                        raise ValueError('Snapshot file or byte budget exceeded.')
                    size, digest = _hash(path, cancelled)
                    byte_count += size
                    files.append({'path': name, 'size_bytes': size, 'sha256': digest})
    files.sort(key=lambda row: row['path'])
    directories.sort()
    _validate_inventory(files, directories, max_files, max_bytes)
    return files, directories, byte_count


def _decode(raw, expected, max_files, max_bytes):
    if len(raw) > MAX_MANIFEST_BYTES or sha256(raw).hexdigest() != _digest(expected):
        raise ValueError('Snapshot checksum does not match the separately retained checksum.')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                           parse_constant=_not_finite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Invalid snapshot JSON.') from exc
    fields = {'kind', 'version', 'created_utc', 'backup_manifest_sha256',
              'files', 'directories', 'file_count', 'total_bytes'}
    if (not isinstance(value, dict) or set(value) != fields
            or value['kind'] != 'carr-library-incremental-snapshot'
            or type(value['version']) is not int or value['version'] != 1):
        raise ValueError('Unsupported snapshot schema.')
    if not isinstance(value['created_utc'], str):
        raise ValueError('Invalid snapshot time.')
    try:
        parsed = datetime.strptime(value['created_utc'], '%Y-%m-%dT%H:%M:%S.%fZ')
    except ValueError as exc:
        raise ValueError('Invalid snapshot time.') from exc
    if parsed.strftime('%Y-%m-%dT%H:%M:%S.%fZ') != value['created_utc']:
        raise ValueError('Invalid snapshot time.')
    total, rows = _validate_inventory(value['files'], value['directories'], max_files, max_bytes)
    if (type(value['file_count']) is not int or value['file_count'] != len(rows)
            or type(value['total_bytes']) is not int or value['total_bytes'] != total
            or rows['manifest.json']['sha256'] != _digest(value['backup_manifest_sha256'])):
        raise ValueError('Snapshot summary or full-backup manifest binding differs.')
    if _encode(value) != raw:
        raise ValueError('Snapshot metadata is not canonical.')
    return value


def _info(digest, value):
    return SnapshotInfo(digest, value['created_utc'], value['backup_manifest_sha256'],
                        value['file_count'], value['total_bytes'])


def _snapshot_names(root, cancelled):
    names = []
    with os.scandir(root / SNAPSHOTS) as entries:
        for entry in entries:
            _check(cancelled)
            if (not entry.name.endswith('.json')
                    or _SHA.fullmatch(entry.name[:-5]) is None):
                raise ValueError('Unexpected entry in the completed snapshot namespace.')
            regular(root / SNAPSHOTS / entry.name)
            names.append(entry.name)
            if len(names) > MAX_SNAPSHOTS:
                raise ValueError('Snapshot history exceeds its inspection budget.')
    return sorted(names)


def _load(root, digest, max_files, max_bytes, cancelled):
    name = _digest(digest) + '.json'
    raw = _read(root / SNAPSHOTS / name, MAX_MANIFEST_BYTES, cancelled)
    return _decode(raw, digest, max_files, max_bytes)


def list_snapshots(repository, *, max_files=MAX_FILES, max_bytes=DEFAULT_MAX_BYTES,
                   cancelled=lambda: False):
    """Validate metadata identities only. This does not verify the object contents."""
    _budget(max_files, max_bytes, cancelled)
    root = _repository(repository)
    with exclusive_writer(root):
        return tuple(_info(name[:-5], _load(root, name[:-5], max_files, max_bytes, cancelled))
                     for name in _snapshot_names(root, cancelled))


def create_snapshot(app, repository, *, max_files=MAX_FILES, max_bytes=DEFAULT_MAX_BYTES,
                    cancelled=lambda: False):
    """Capture and verify a complete backup, then retain only new content objects."""
    _budget(max_files, max_bytes, cancelled)
    candidate, source = local_path(repository), local_path(app.root, exists=True)
    _outside(candidate, source)
    root = _repository(candidate, create=True)
    receipt = None
    with exclusive_writer(root):
        if len(_snapshot_names(root, cancelled)) >= MAX_SNAPSHOTS:
            raise ValueError('Snapshot history is full; choose a new repository.')
        temporary = TemporaryDirectory(prefix='capture-', dir=root / STAGING)
        try:
            stage = Path(temporary.name)
            complete = stage / 'complete'
            manifest_digest = library_backup.create_backup(app, complete, cancelled=cancelled)
            library_backup.verify_backup(complete, manifest_digest, cancelled=cancelled)
            files, directories, total = _inventory(complete, max_files, max_bytes, cancelled)
            incoming, fresh_count, fresh_bytes, reused_count, reused_bytes = stage / 'object', 0, 0, 0, 0
            observed = {}
            for row in files:
                _check(cancelled)
                digest, size = row['sha256'], row['size_bytes']
                target = root / OBJECTS / digest
                if digest in observed:
                    if observed[digest] != size:
                        raise ValueError('Conflicting content object sizes.')
                    reused_count += 1
                    reused_bytes += size
                    continue
                if optional_present(target):
                    _stream(target, size=size, digest=digest, cancelled=cancelled)
                    reused_count += 1
                    reused_bytes += size
                else:
                    _stream(complete / row['path'], size=size, digest=digest,
                            cancelled=cancelled, destination=incoming)
                    _check(cancelled)
                    warning = _publish(incoming, target)
                    if warning is not None:
                        raise OSError('A new content object needs staging cleanup before it can be used.')
                    _stream(target, size=size, digest=digest, cancelled=cancelled)
                    fresh_count += 1
                    fresh_bytes += size
                observed[digest] = size
            sync_directory(root / OBJECTS)
            value = {'kind': 'carr-library-incremental-snapshot', 'version': 1,
                     'created_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                     'backup_manifest_sha256': manifest_digest, 'files': files,
                     'directories': directories, 'file_count': len(files), 'total_bytes': total}
            raw = _encode(value)
            digest = sha256(raw).hexdigest()
            _decode(raw, digest, max_files, max_bytes)
            _write(stage / 'snapshot.json', raw)
            _check(cancelled)
            warning = _publish(stage / 'snapshot.json', root / SNAPSHOTS / (digest + '.json'))
            receipt = SnapshotReceipt(_info(digest, value), fresh_count, fresh_bytes,
                                      reused_count, reused_bytes, warning)
            try:
                sync_directory(root / SNAPSHOTS)
            except OSError:
                receipt = replace(receipt, cleanup_warning=(
                    'The snapshot was published, but directory durability could not be confirmed. '
                    'Verify this saved checksum before relying on it.'))
        finally:
            try:
                temporary.cleanup()
            except OSError:
                if receipt is None:
                    raise
                receipt = replace(receipt, cleanup_warning=(
                    ((receipt.cleanup_warning + ' ') if receipt.cleanup_warning else '')
                    + 'The snapshot was saved. Its private staging folder still needs cleanup; '
                    'do not repeat the completed save.'))
    return receipt


@contextmanager
def _materialized(root, digest, max_files, max_bytes, cancelled, *, parent=None, publication=None):
    value = _load(root, digest, max_files, max_bytes, cancelled)
    temporary = TemporaryDirectory(prefix='.carr-snapshot-read-', dir=parent)
    try:
        stage = Path(temporary.name)
        for name in sorted(value['directories'], key=lambda name: (len(_path(name).parts), name)):
            _check(cancelled)
            (stage / name).mkdir(mode=0o700)
        for row in value['files']:
            _stream(root / OBJECTS / row['sha256'], size=row['size_bytes'],
                    digest=row['sha256'], cancelled=cancelled, destination=stage / row['path'])
        library_backup.verify_backup(stage, value['backup_manifest_sha256'], cancelled=cancelled)
        yield stage, value
    finally:
        try:
            temporary.cleanup()
        except OSError:
            if publication is None or not publication['complete']:
                raise
            publication['warning'] = (
                'The library was restored. Its private reconstruction folder still needs '
                'cleanup; do not repeat the completed restore.')


def verify_snapshot(repository, snapshot_sha256, *, max_files=MAX_FILES,
                    max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Reconstruct privately and run the current complete, typed backup validator."""
    _budget(max_files, max_bytes, cancelled)
    root = _repository(repository)
    with exclusive_writer(root):
        with _materialized(root, _digest(snapshot_sha256), max_files, max_bytes, cancelled) as (_, value):
            return _info(snapshot_sha256, value)


def restore_snapshot(repository, snapshot_sha256, destination, *, max_files=MAX_FILES,
                     max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Restore into a new directory; the existing backup engine owns publication."""
    _budget(max_files, max_bytes, cancelled)
    root = _repository(repository)
    target = local_path(destination)
    _outside(target, root)
    regular(target.parent, directory=True)
    if optional_present(target):
        raise FileExistsError('A snapshot restore requires a new destination folder.')
    publication = {'complete': False, 'warning': None}
    with exclusive_writer(root):
        with _materialized(root, _digest(snapshot_sha256), max_files, max_bytes, cancelled,
                           parent=target.parent, publication=publication) as (stage, value):
            restored = library_backup.restore_backup(stage, target, value['backup_manifest_sha256'],
                                                      cancelled=cancelled)
            publication['complete'] = True
            info = _info(snapshot_sha256, value)
    return RestoreReceipt(restored, info, publication['warning'])
