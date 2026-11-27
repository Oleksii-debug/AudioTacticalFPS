"""Incremental storage over the existing verified full-backup and restore APIs.

Each layer contains changed bytes and a complete canonical snapshot manifest.
Its parent is identified only by SHA256: callers select every base explicitly.
Materialization is private, bounded and reverified by library_backup before any
restored library is published. There is no alternate database or restore model.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from tempfile import TemporaryDirectory

from . import backup_sidecars, library_backup as full
from .durable_state import local_path
from .ingest.bundle import _plain, _unique_object
from .ingest.restore import RestoreError

SCHEMA = 'carr.library-incremental.v1'
MAX_CHAIN = 8
MAX_FILES = full.MAX_ASSETS + 2 * backup_sidecars.MAX_FILES + 2
DEFAULT_MAX_BYTES = 64 * 1024**3
_HASH = re.compile(r'[0-9a-f]{64}\Z')


@dataclass(frozen=True)
class BackupReference:
    path: Path
    sha256: str

    def __post_init__(self):
        if type(self.sha256) is not str or _HASH.fullmatch(self.sha256) is None:
            raise RestoreError('An independently retained backup manifest SHA256 is required.')
        object.__setattr__(self, 'path', local_path(self.path, exists=True))
        _plain(self.path, directory=True)


@dataclass(frozen=True)
class IncrementalReceipt:
    path: Path
    manifest_sha256: str
    base_sha256: str
    snapshot_sha256: str
    total_files: int
    stored_files: int
    logical_bytes: int
    stored_bytes: int

    def to_dict(self):
        return {**asdict(self), 'path': str(self.path),
                'base_required': True, 'kind': 'carr-library-incremental'}


@dataclass(frozen=True)
class _Snapshot:
    reference: BackupReference
    root: Path
    manifest_raw: bytes
    manifest: dict
    files: dict
    directories: tuple
    chain: tuple


def _check(cancelled):
    backup_sidecars._check_cancelled(cancelled)


def _budget(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise RestoreError('Choose a positive incremental logical-byte budget below 2^63 bytes.')
    return value


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _json(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(RestoreError('Non-finite backup field.')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RestoreError('Invalid incremental backup JSON.') from exc


def _read(path, checksum, *, maximum=full.MAX_MANIFEST):
    local_path(path, exists=True)
    raw = backup_sidecars.read_regular(path, maximum)
    if sha256(raw).hexdigest() != checksum:
        raise RestoreError('Incremental backup manifest digest mismatch.')
    return raw


def _inventory(manifest, max_bytes):
    """Project only filenames/checksums; canonical verification remains authoritative."""
    try:
        inventory = backup_sidecars.normalize_manifest(manifest)
        rows = [manifest['database']]
        rows += [dict(row, filename='originals/' + row['filename']) for row in manifest['assets']]
        rows += list(inventory.entries)
        if inventory.pending is not None:
            rows += [dict(row, filename='pending/' + row['filename'])
                     for row in inventory.pending['files']]
    except (KeyError, TypeError, AttributeError) as exc:
        raise RestoreError('Invalid full-snapshot inventory in incremental backup.') from exc
    if len(rows) > MAX_FILES:
        raise RestoreError('Incremental file inventory exceeds the existing backup allowance.')
    files, keys, total = {}, set(), 0
    directories = {'originals', *inventory.empty_roots}
    if inventory.pending is not None:
        directories.add('pending')
    for row in rows:
        if type(row) is not dict:
            raise RestoreError('Invalid incremental file identity.')
        name, size, checksum = row.get('filename'), row.get('size_bytes'), row.get('sha256')
        portable = backup_sidecars.portable_name(name)
        key = backup_sidecars._portable_key(name)
        if (key in keys or type(size) is not int or size < 0 or size > max_bytes
                or type(checksum) is not str or _HASH.fullmatch(checksum) is None):
            raise RestoreError('Invalid or duplicate incremental file identity.')
        keys.add(key)
        files[name] = {'filename': name, 'size_bytes': size, 'sha256': checksum}
        total += size
        if total > max_bytes:
            raise RestoreError('Incremental logical backup exceeds its byte budget.')
        directories.update(str(parent) for parent in portable.parents if str(parent) != '.')
    for name in directories:
        backup_sidecars.portable_name(name)
        if backup_sidecars._portable_key(name) in keys:
            raise RestoreError('Incremental file and directory identities conflict.')
    return files, tuple(sorted(directories, key=lambda name: (name.count('/'), name)))


def _parents(references):
    if type(references) not in (tuple, list) or len(references) > MAX_CHAIN + 1:
        raise RestoreError('Select at most eight incremental ancestors and one full backup.')
    result = {}
    for reference in references:
        if not isinstance(reference, BackupReference) or reference.sha256 in result:
            raise RestoreError('Choose each exact base backup only once.')
        result[reference.sha256] = reference
    return result


def _layer_header(raw):
    descriptor = _json(raw)
    if type(descriptor) is not dict:
        raise RestoreError('Backup manifest must be an object.')
    if descriptor.get('schema') != SCHEMA:
        backup_sidecars.normalize_manifest(descriptor)
        return None
    if set(descriptor) != {'schema', 'base_sha256', 'snapshot_sha256', 'stored'}:
        raise RestoreError('Unsupported incremental backup fields.')
    base_digest, snapshot_digest = descriptor['base_sha256'], descriptor['snapshot_sha256']
    if any(type(value) is not str or _HASH.fullmatch(value) is None
           for value in (base_digest, snapshot_digest)):
        raise RestoreError('Invalid incremental parent or snapshot checksum.')
    stored = descriptor['stored']
    if (type(stored) is not list or len(stored) > MAX_FILES
            or any(type(name) is not str for name in stored)
            or stored != sorted(set(stored))):
        raise RestoreError('Invalid incremental changed-file inventory.')
    return descriptor


def required_base_sha256(reference):
    """Read an authenticated header only; complete verification is a separate action."""
    if not isinstance(reference, BackupReference):
        raise RestoreError('Choose an exact backup reference.')
    descriptor = _layer_header(_read(reference.path / 'manifest.json', reference.sha256))
    return None if descriptor is None else descriptor['base_sha256']


def _root_guard(reference, identity):
    value = _plain(local_path(reference.path, exists=True), directory=True)
    if (value.st_dev, value.st_ino) != identity:
        raise RestoreError('The selected backup directory changed.')
    _read(reference.path / 'manifest.json', reference.sha256)


def _space(parent, amount):
    if shutil.disk_usage(parent).free < amount + 1024 * 1024:
        raise RestoreError('There is not enough staging space for this verified backup copy.')


def _copy(source, target, row, cancelled):
    _check(cancelled)
    local_path(source, exists=True)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    local_path(target.parent, exists=True)
    _space(target.parent, row['size_bytes'])
    backup_sidecars._stream_file(source, destination=target, expected=row,
        maximum=max(1, row['size_bytes']), minimum=0, cancelled=cancelled)
    local_path(source, exists=True)


def _namespace(root, stored, cancelled=lambda: False):
    expected_files = {'snapshot.json', 'manifest.json'} | {'files/' + name for name in stored}
    expected_dirs = {'files'}
    for name in expected_files:
        expected_dirs.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != '.')
    files, directories, stack = set(), set(), [root]
    while stack:
        _check(cancelled)
        directory = stack.pop()
        _plain(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                _check(cancelled)
                relative = Path(entry.path).relative_to(root).as_posix()
                if len(files) + len(directories) >= MAX_FILES * 2 + 3:
                    raise RestoreError('Incremental backup namespace exceeds its bound.')
                if entry.is_dir(follow_symlinks=False):
                    _plain(Path(entry.path), directory=True)
                    directories.add(relative)
                    stack.append(Path(entry.path))
                else:
                    backup_sidecars._regular(Path(entry.path))
                    files.add(relative)
    if files != expected_files or directories != expected_dirs:
        raise RestoreError('Incremental backup is incomplete or contains unexpected files.')


@contextmanager
def _snapshot(reference, parents, temporary_parent, cancelled, max_bytes, *, depth=0, seen=()):
    _check(cancelled)
    if not isinstance(reference, BackupReference):
        raise RestoreError('Choose an exact backup reference.')
    if depth > MAX_CHAIN or reference.sha256 in seen:
        raise RestoreError('Incremental backup chain is cyclic or exceeds eight layers.')
    before = _plain(reference.path, directory=True)
    identity = before.st_dev, before.st_ino
    raw = _read(reference.path / 'manifest.json', reference.sha256)
    descriptor = _layer_header(raw)
    if descriptor is None:
        manifest = full.verify_backup(reference.path, reference.sha256, cancelled=cancelled)
        files, directories = _inventory(manifest, max_bytes)
        _root_guard(reference, identity)
        yield _Snapshot(reference, reference.path, raw, manifest, files, directories, (reference,))
        return
    base_digest, snapshot_digest = descriptor['base_sha256'], descriptor['snapshot_sha256']
    stored = descriptor['stored']
    snapshot_raw = _read(reference.path / 'snapshot.json', snapshot_digest)
    manifest = _json(snapshot_raw)
    files, directories = _inventory(manifest, max_bytes)
    if not set(stored) <= set(files):
        raise RestoreError('Incremental changed files are not in the canonical snapshot.')
    _namespace(reference.path, stored, cancelled)
    base = parents.get(base_digest)
    if base is None:
        raise RestoreError('Select the required base backup with manifest SHA256 ' + base_digest + '.')
    with _snapshot(base, parents, temporary_parent, cancelled, max_bytes,
                   depth=depth + 1, seen=(*seen, reference.sha256)) as previous:
        for name, row in files.items():
            if name not in stored and previous.files.get(name) != row:
                raise RestoreError('An inherited file does not match the selected base backup.')
        with TemporaryDirectory(prefix='.carr-incremental-materialize-', dir=temporary_parent) as temporary:
            stage = Path(temporary)
            for name in directories:
                (stage / name).mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, row in files.items():
                source = reference.path / 'files' / name if name in stored else previous.root / name
                _copy(source, stage / name, row, cancelled)
            full._sync_file(stage / 'manifest.json', snapshot_raw)
            verified = full.verify_backup(stage, snapshot_digest, cancelled=cancelled)
            _root_guard(reference, identity)
            yield _Snapshot(reference, stage, snapshot_raw, verified, files, directories,
                            (reference, *previous.chain))


def _recheck_chain(chain, cancelled, max_bytes):
    """Recheck actual base bytes after current snapshot work and before publication."""
    for reference in chain:
        _check(cancelled)
        value = _plain(reference.path, directory=True)
        identity = value.st_dev, value.st_ino
        descriptor = _layer_header(_read(reference.path / 'manifest.json', reference.sha256))
        if descriptor is None:
            full.verify_backup(reference.path, reference.sha256, cancelled=cancelled)
        else:
            manifest = _json(_read(reference.path / 'snapshot.json', descriptor['snapshot_sha256']))
            files, _directories = _inventory(manifest, max_bytes)
            _namespace(reference.path, descriptor['stored'], cancelled)
            for name in descriptor['stored']:
                if name not in files:
                    raise RestoreError('A changed base file is outside its snapshot.')
                row = files[name]
                backup_sidecars._stream_file(reference.path / 'files' / name, expected=row,
                    maximum=max(1, row['size_bytes']), minimum=0, cancelled=cancelled)
        _root_guard(reference, identity)


def _selected_destination(value, references, *, library=None):
    references = tuple(references)
    if not references:
        raise RestoreError('Choose a base backup first.')
    destination = full._destination(value, library if library is not None else references[0].path)
    for reference in references:
        source = reference.path.resolve(strict=True)
        if destination.is_relative_to(source) or source.is_relative_to(destination):
            raise RestoreError('Keep backup and restored destinations outside every selected backup.')
    return destination


def _publish(stage, destination, stored):
    """Existing no-replace directory ownership; the manifest is published last."""
    destination.mkdir(mode=0o700)
    (destination / 'files').mkdir(mode=0o700)
    for name in stored:
        target = destination / 'files' / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.link(stage / 'files' / name, target)
        full._sync_directory(target.parent)
    os.link(stage / 'snapshot.json', destination / 'snapshot.json')
    full._sync_directory(destination / 'files')
    os.link(stage / 'manifest.json', destination / 'manifest.json')
    full._sync_directory(destination)
    full._sync_directory(destination.parent)


def create_incremental_backup(app, destination, *, base, ancestors=(), include_generated=False,
                              max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Store only changed files; retain the selected base chain for later restore."""
    max_bytes = _budget(max_bytes)
    if not isinstance(base, BackupReference):
        raise RestoreError('Choose the verified base backup and its independently retained checksum.')
    parents = _parents(ancestors)
    destination = _selected_destination(destination, (base, *parents.values()), library=app.root)
    with TemporaryDirectory(prefix='.carr-incremental-create-', dir=destination.parent) as temporary:
        temporary = Path(temporary)
        with _snapshot(base, parents, temporary, cancelled, max_bytes) as previous:
            if len(previous.chain) > MAX_CHAIN:
                raise RestoreError('Eight incremental layers are already present. Create a new full backup.')
            current = temporary / 'current'
            checksum = full.create_backup(app, current, include_generated=include_generated,
                                          max_bytes=max_bytes, cancelled=cancelled)
            manifest = full.verify_backup(current, checksum, cancelled=cancelled)
            files, directories = _inventory(manifest, max_bytes)
            snapshot_raw = _read(current / 'manifest.json', checksum)
            stored = sorted(name for name, row in files.items() if previous.files.get(name) != row)
            stage = temporary / 'delta'
            (stage / 'files').mkdir(mode=0o700, parents=True)
            for name in stored:
                _copy(current / name, stage / 'files' / name, files[name], cancelled)
            full._sync_file(stage / 'snapshot.json', snapshot_raw)
            descriptor = {'schema': SCHEMA, 'base_sha256': base.sha256,
                          'snapshot_sha256': checksum, 'stored': stored}
            raw = _encode(descriptor)
            if len(raw) > full.MAX_MANIFEST:
                raise RestoreError('Incremental manifest exceeds its size allowance.')
            full._sync_file(stage / 'manifest.json', raw)
            _namespace(stage, stored, cancelled)
            _check(cancelled)
            _recheck_chain(previous.chain, cancelled, max_bytes)
            _check(cancelled)
            _publish(stage, destination, stored)
            receipt = IncrementalReceipt(destination, sha256(raw).hexdigest(), base.sha256, checksum,
                len(files), len(stored), sum(row['size_bytes'] for row in files.values()),
                sum(files[name]['size_bytes'] for name in stored) + len(snapshot_raw) + len(raw))
    return receipt


def verify_incremental_backup(source, expected_manifest_sha256, *, bases=(),
                              max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Verify every required layer without changing a backup or starting a worker."""
    reference, parents = BackupReference(source, expected_manifest_sha256), _parents(bases)
    max_bytes = _budget(max_bytes)
    with TemporaryDirectory(prefix='carr-incremental-verify-') as temporary:
        with _snapshot(reference, parents, temporary, cancelled, max_bytes) as snapshot:
            return {'manifest_sha256': reference.sha256,
                    'snapshot_sha256': sha256(snapshot.manifest_raw).hexdigest(),
                    'files': len(snapshot.files), 'logical_bytes': sum(
                        row['size_bytes'] for row in snapshot.files.values()),
                    'chain': [item.sha256 for item in snapshot.chain],
                    'schema_version': snapshot.manifest['schema_version']}


def restore_incremental_backup(source, destination, expected_manifest_sha256, *, bases=(),
                               max_bytes=DEFAULT_MAX_BYTES, cancelled=lambda: False):
    """Use canonical schema/typed-sidecar/source verification before fresh restore."""
    reference, parents = BackupReference(source, expected_manifest_sha256), _parents(bases)
    max_bytes = _budget(max_bytes)
    destination = _selected_destination(destination, (reference, *parents.values()))
    with TemporaryDirectory(prefix='.carr-incremental-restore-', dir=destination.parent) as temporary:
        with _snapshot(reference, parents, temporary, cancelled, max_bytes) as snapshot:
            _check(cancelled)
            return full.restore_backup(snapshot.root, destination,
                sha256(snapshot.manifest_raw).hexdigest(), cancelled=cancelled)
