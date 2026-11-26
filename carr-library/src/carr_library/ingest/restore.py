"""Restore a trusted, closed CARR-05 revision snapshot to a new local file.

Not a full workspace backup or a sandbox for arbitrary downloaded databases.
Both parent directories and the independently obtained manifest must be trusted.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from carr_library.storage.revisions import Storage


class RestoreError(ValueError):
    pass


def _sidecars(path):
    return any(os.path.lexists(str(path) + suffix)
               for suffix in ('-wal', '-shm', '-journal'))


def _signature(db):
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


def restore_revision_snapshot(source, destination, manifest, *, max_bytes=512 * 1024 * 1024):
    """Validate a versioned manifest and publish without replacing any file.

    Manifest: version=1, kind='revision-storage', schema_version=1,
    size_bytes and sha256. Hashes check integrity, not authenticity. Call only
    with a locally created, closed SQLite backup and trusted manifest. Parents
    must be private, stable and free of concurrent writers to the target name.
    """
    if not isinstance(manifest, dict) or set(manifest) != {
            'version', 'kind', 'schema_version', 'size_bytes', 'sha256'}:
        raise RestoreError('invalid manifest fields')
    if (type(manifest['version']) is not int or manifest['version'] != 1 or
            manifest['kind'] != 'revision-storage' or
            type(manifest['schema_version']) is not int or manifest['schema_version'] != 1):
        raise RestoreError('unsupported manifest version or kind')
    size = manifest['size_bytes']
    expected = manifest['sha256']
    if (type(max_bytes) is not int or max_bytes < 1 or type(size) is not int or
            not 0 < size <= max_bytes or not isinstance(expected, str) or
            len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected)):
        raise RestoreError('invalid manifest size or digest')
    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination) or _sidecars(destination):
        raise RestoreError('destination already exists or has sidecars')
    info = source.lstat()
    if (not stat.S_ISREG(info.st_mode) or
            getattr(info, 'st_file_attributes', 0) & 0x400 or _sidecars(source)):
        raise RestoreError('source must be a closed regular snapshot without sidecars')
    if info.st_size != size:
        raise RestoreError('snapshot size mismatch')
    with tempfile.TemporaryDirectory(prefix='carr-restore-', dir=destination.parent) as tmp:
        staged = Path(tmp) / 'snapshot.sqlite'
        fd = os.open(source, os.O_RDONLY | getattr(os, 'O_BINARY', 0) |
                     getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as src, staged.open('xb') as dst:
            opened = os.fstat(src.fileno())
            if (not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened) or
                    opened.st_size != size):
                raise RestoreError('source changed before copy')
            remaining = size
            while remaining:
                chunk = src.read(min(65536, remaining))
                if not chunk:
                    raise RestoreError('snapshot truncated')
                dst.write(chunk)
                remaining -= len(chunk)
            if src.read(1) or _sidecars(source):
                raise RestoreError('snapshot changed during copy')
            dst.flush()
            os.fsync(dst.fileno())
        if hash_file(staged) != expected:
            raise RestoreError('snapshot digest mismatch')
        with staged.open('rb') as f:
            if f.read(16) != b'SQLite format 3\x00':
                raise RestoreError('not a SQLite snapshot')
        reference = Storage(':memory:')
        try:
            with closing(sqlite3.connect(staged.absolute().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                if db.execute('PRAGMA user_version').fetchone()[0] != 1 or _signature(db) != _signature(reference.db):
                    raise RestoreError('snapshot schema mismatch')
        finally:
            reference.close()
        candidate = Storage(staged)
        try:
            candidate.check()
        finally:
            candidate.close()
        if _sidecars(staged) or hash_file(staged) != expected:
            raise RestoreError('validation changed snapshot bytes')
        if _sidecars(destination):
            raise RestoreError('destination has sidecars')
        # Atomic no-clobber publication on filesystems supporting hard links.
        # Fail closed rather than falling back to a potentially partial copy.
        os.link(staged, destination)
    return destination
