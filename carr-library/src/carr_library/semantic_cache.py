"""Disposable, bounded local embeddings; never an authority for source access.

Only exact model/content/locator identities and numeric vectors are retained.
The caller must authorize and tokenize the current source before any lookup.
Corrupt, busy or unavailable caches are misses; originals are never modified.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from itertools import islice
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
from time import time_ns

from .data_paths import guard_data_directory

DIMENSIONS = 384
MAX_BATCH = 512
MAX_SQLITE_ROW_BYTES = 16 * 1024
_PACK = struct.Struct('<384d')
_HEX = re.compile('[0-9a-f]{64}')
_APPLICATION_ID = 0x43415245
_SCHEMA = (
    'CREATE TABLE embedding (identity TEXT PRIMARY KEY, vector BLOB NOT NULL, '
    'checksum TEXT NOT NULL, touched INTEGER NOT NULL) WITHOUT ROWID',
    'CREATE INDEX embedding_recent ON embedding(touched,identity)',
)


class _Interrupted(Exception):
    def __init__(self, error):
        self.error = error


@dataclass(frozen=True)
class CacheBudget:
    max_entries: int = 50_000
    max_bytes: int = 256 * 1024 * 1024

    def __post_init__(self):
        if (type(self.max_entries) is not int or not 1 <= self.max_entries <= 1_000_000
                or type(self.max_bytes) is not int
                or not 1024 * 1024 <= self.max_bytes <= 8 * 1024 * 1024 * 1024):
            raise ValueError('Invalid local embedding cache budget.')


def _digest(value):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError('An exact SHA256 cache identity is required.')
    return value


def model_identity(manifest, *, runtime='fastembed/0.9.1', contract='minilm-128-unit-f64-v1'):
    """Version the entire approved manifest, runtime and preprocessing contract."""
    return sha256(json.dumps([manifest, runtime, contract], sort_keys=True,
                             ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()


def content_identity(model, text, locator=None):
    """Hash text and exact workspace/edition/revision/window identity, not paths."""
    _digest(model)
    if not isinstance(text, str) or not text or len(text) > 200_000 or '\x00' in text:
        raise ValueError('Invalid semantic cache text.')
    if locator is not None and (not isinstance(locator, tuple) or len(locator) != 4
            or any(not isinstance(value, str) or not value or len(value) > 512
                   or '\x00' in value for value in locator)):
        raise ValueError('An exact semantic source locator is required.')
    payload = json.dumps([model, 'query' if locator is None else 'source', locator,
                          sha256(text.encode('utf-8')).hexdigest()], ensure_ascii=True,
                         separators=(',', ':')).encode('ascii')
    return sha256(payload).hexdigest()


def _vector(value):
    result = tuple(float(number) for number in islice(value, DIMENSIONS + 1))
    if (len(result) != DIMENSIONS or not all(map(math.isfinite, result))
            or not math.isclose(math.hypot(*result), 1.0, rel_tol=1e-10, abs_tol=1e-10)):
        raise ValueError('A normalized finite MiniLM vector is required.')
    return result


def _local_directory(path):
    for parent in (*reversed(path.parents), path):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Embedding cache directories must be local and unlinked.')


def _regular(path, max_bytes):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or info.st_size > max_bytes):
        raise ValueError('Embedding cache files must be bounded regular files without links.')


class EmbeddingCache:
    """Independent SQLite transactions with finite disk, memory and lock budgets.

    There is no live connection to share across UI worker threads. A failed
    operation disables this instance; the current search can still use inference.
    Cache contents are private derived data and must not be exported as evidence.
    """
    def __init__(self, directory, model, *, budget=None):
        self.model = _digest(model)
        self.budget = budget or CacheBudget()
        if type(self.budget) is not CacheBudget:
            raise ValueError('An explicit validated embedding cache budget is required.')
        self.directory = guard_data_directory(directory, purpose='embedding cache')
        if '..' in self.directory.parts:
            raise ValueError('Embedding cache paths must not contain parent traversal.')
        self.path = self.directory / ('embeddings-' + self.model + '.sqlite')
        self.available = True
        self.last_error = ''

    def _disable(self, exc):
        self.available = False
        # No paths, source text or exception messages in public diagnostics.
        self.last_error = type(exc).__name__

    @contextmanager
    def _open(self, check=lambda: None):
        def checkpoint():
            try:
                check()
            except BaseException as exc:
                raise _Interrupted(exc) from exc

        checkpoint()
        if not self.available:
            raise ValueError('The embedding cache is unavailable.')
        # The library root already exists. Validate it before creating our own
        # child, and repeat validation before every SQLite open.
        _local_directory(self.directory.parent)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        _local_directory(self.directory)
        for suffix in ('', '-journal', '-wal', '-shm'):
            _regular(Path(str(self.path) + suffix), self.budget.max_bytes)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        _regular(self.path, self.budget.max_bytes)
        db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        interrupted = []

        def progress():
            try:
                checkpoint()
            except _Interrupted as exc:
                interrupted.append(exc)
                return 1
            return 0

        try:
            # Schema validation is not a width bound: SQLite permits oversized
            # BLOB/TEXT values in an otherwise valid schema. Reject a wide row
            # inside SQLite before it becomes a Python allocation.
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_ROW_BYTES)
            db.set_progress_handler(progress, 1000)
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA temp_store=MEMORY')
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA mmap_size=0')
            db.execute('PRAGMA synchronous=FULL')
            # No WAL growth or long busy waits; another search can recompute.
            if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
                raise ValueError('Unexpected embedding cache journal mode.')
            schema = tuple(row[0] for row in db.execute(
                'SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC,name'))
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if not schema and not app_id and not version:
                db.execute('BEGIN IMMEDIATE')
                try:
                    for statement in _SCHEMA:
                        db.execute(statement)
                    db.execute('PRAGMA application_id=' + str(_APPLICATION_ID))
                    db.execute('PRAGMA user_version=1')
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
            elif schema != _SCHEMA or app_id != _APPLICATION_ID or version != 1:
                raise ValueError('Unrecognized embedding cache schema.')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            maximum = self.budget.max_bytes // page_size
            if db.execute('PRAGMA max_page_count=' + str(maximum)).fetchone()[0] > maximum:
                raise ValueError('Embedding cache exceeds its disk budget.')
            checkpoint()
            yield db
            checkpoint()
        except sqlite3.Error:
            if interrupted:
                raise interrupted[0]
            raise
        finally:
            db.set_progress_handler(None, 0)
            db.close()

    def get_many(self, identities, *, check=lambda: None):
        if not callable(check):
            raise ValueError('A cache budget checkpoint must be callable.')
        check()
        identities = tuple(islice(identities, MAX_BATCH + 1))
        if len(identities) > MAX_BATCH:
            raise ValueError('Embedding cache lookup batch is too large.')
        identities = tuple(dict.fromkeys(_digest(value) for value in identities))
        if not self.available or not identities:
            return {}
        result = {}
        try:
            with self._open(check) as db:
                rows = db.execute('SELECT identity,vector,checksum FROM embedding WHERE identity IN ('
                                  + ','.join('?' for _ in identities) + ') '
                                  "AND typeof(vector)='blob' AND length(vector)=? "
                                  "AND typeof(checksum)='text' AND length(checksum)=64",
                                  (*identities, _PACK.size))
                for key, packed, checksum in rows:
                    if (not isinstance(packed, bytes) or len(packed) != _PACK.size
                            or sha256((self.model + key).encode('ascii') + packed).hexdigest() != checksum):
                        continue
                    try:
                        result[key] = _vector(_PACK.unpack(packed))
                    except (ValueError, TypeError, OverflowError, struct.error):
                        continue
                if result:
                    db.execute('BEGIN IMMEDIATE')
                    db.executemany('UPDATE embedding SET touched=? WHERE identity=?',
                                   ((time_ns(), key) for key in result))
                    db.commit()
        except _Interrupted as exc:
            raise exc.error
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._disable(exc)
            return {}
        return result

    def put_many(self, vectors, *, check=lambda: None):
        if not callable(check):
            raise ValueError('A cache budget checkpoint must be callable.')
        check()
        if not isinstance(vectors, dict) or len(vectors) > MAX_BATCH:
            raise ValueError('Embedding cache write batch is too large.')
        if not self.available or not vectors:
            return False
        rows = []
        for identity, vector in vectors.items():
            _digest(identity)
            packed = _PACK.pack(*_vector(vector))
            checksum = sha256((self.model + identity).encode('ascii') + packed).hexdigest()
            rows.append((identity, packed, checksum, time_ns()))
        # A small configured cache still accepts the most recent part of a batch.
        rows = rows[-self.budget.max_entries:]
        try:
            with self._open(check) as db:
                extra_evictions = 0
                for _ in range(24):
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        # Free rows before insertion, so the entry bound also
                        # holds inside the transaction. Replacements keep peers.
                        keys = tuple(row[0] for row in rows)
                        existing = db.execute('SELECT COUNT(*) FROM embedding WHERE identity IN ('
                            + ','.join('?' for _ in keys) + ')', keys).fetchone()[0]
                        count = db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0]
                        remove = max(extra_evictions,
                                     count + len(rows) - existing - self.budget.max_entries, 0)
                        if remove:
                            db.execute('DELETE FROM embedding WHERE identity IN (SELECT identity FROM embedding '
                                       'WHERE identity NOT IN (' + ','.join('?' for _ in keys) + ') '
                                       'ORDER BY touched,identity LIMIT ?)', (*keys, remove))
                        db.executemany('INSERT OR REPLACE INTO embedding VALUES(?,?,?,?)', rows)
                        db.commit()
                        return True
                    except sqlite3.OperationalError as exc:
                        db.rollback()
                        if getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                            raise
                        # A byte budget may fill before the entry budget. Keep
                        # the last valid transaction and retry with more LRU
                        # eviction, then a smaller tail of an oversized batch.
                        # Never permanently disable a healthy full cache.
                        eligible = count - existing
                        if extra_evictions < eligible:
                            extra_evictions = min(eligible, max(16, extra_evictions * 2))
                        elif len(rows) > 1:
                            rows = rows[len(rows) // 2:]
                            extra_evictions = count
                        else:
                            return False
                    except BaseException:
                        db.rollback()
                        raise
        except _Interrupted as exc:
            raise exc.error
        except (OSError, ValueError, sqlite3.Error) as exc:
            self._disable(exc)
            return False
        return False

