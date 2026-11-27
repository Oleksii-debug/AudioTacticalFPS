"""Independent persistent-cache boundaries using real SQLite and subprocesses."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tracemalloc
from time import monotonic, sleep

import pytest

from carr_library import semantic_cache as module
from carr_library.semantic_cache import (
    CacheBudget, EmbeddingCache, content_identity, model_identity,
)


MODEL = sha256(b'independent synthetic cache boundary fixture').hexdigest()
UNIT = (1.0,) + (0.0,) * 383
OTHER_UNIT = (0.0, 1.0) + (0.0,) * 382


def key(index, *, model=MODEL):
    return content_identity(model, f'Synthetic passage {index}',
                            ('workspace', 'edition', 'revision', str(index)))


def populated(tmp_path, *, budget=None):
    cache = EmbeddingCache(tmp_path / 'cache', MODEL, budget=budget)
    assert cache.put_many({key(0): UNIT, key(1): OTHER_UNIT})
    return cache


@pytest.mark.parametrize('mutation', [
    'CREATE TRIGGER unexpected AFTER UPDATE ON embedding '
    'BEGIN DELETE FROM embedding; END',
    'CREATE INDEX unexpected ON embedding(checksum)',
    'ALTER TABLE embedding ADD COLUMN unexpected TEXT',
    'PRAGMA application_id=1',
    'PRAGMA user_version=3',
])
def test_changed_schema_is_rejected_before_any_cache_hit_or_trigger(tmp_path, mutation):
    cache = populated(tmp_path)
    with sqlite3.connect(cache.path) as db:
        db.execute(mutation)
    before = cache.path.read_bytes()
    assert cache.get_many([key(0), key(1)]) == {}
    assert not cache.available
    assert cache.path.read_bytes() == before
    with sqlite3.connect(cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 2


@pytest.mark.parametrize('kind', ['short', 'text', 'checksum', 'zero', 'nan', 'infinite'])
def test_corrupt_or_non_normalized_vector_cannot_become_a_hit(tmp_path, kind):
    cache = populated(tmp_path)
    packed = struct.pack('<384d', *UNIT)
    if kind == 'short':
        packed = packed[:-1]
    elif kind == 'text':
        packed = 'not a vector blob'
    elif kind in ('zero', 'nan', 'infinite'):
        value = {'zero': 0.0, 'nan': float('nan'), 'infinite': float('inf')}[kind]
        packed = struct.pack('<384d', value, *((0.0,) * 383))
    checksum = ('0' * 64 if kind == 'checksum' or isinstance(packed, str)
                else sha256((MODEL + key(0)).encode('ascii') + packed).hexdigest())
    with sqlite3.connect(cache.path) as db:
        db.execute('UPDATE embedding SET vector=?,checksum=? WHERE identity=?',
                   (packed, checksum, key(0)))
    assert cache.get_many([key(0), key(1)]) == {key(1): OTHER_UNIT}
    assert cache.available


def test_row_checksum_binds_the_model_and_content_identity(tmp_path):
    cache = populated(tmp_path)
    different = EmbeddingCache(tmp_path / 'cache', sha256(b'another model').hexdigest())
    assert different.put_many({key(2): UNIT})
    with sqlite3.connect(cache.path) as db:
        copied = db.execute('SELECT vector,checksum,touched FROM embedding WHERE identity=?',
                            (key(0),)).fetchone()
        db.execute('UPDATE embedding SET vector=?,checksum=?,touched=? WHERE identity=?',
                   (*copied, key(1)))
    with sqlite3.connect(different.path) as db:
        db.execute('INSERT INTO embedding VALUES(?,?,?,?)', (key(0), *copied))
    assert cache.get_many([key(0), key(1)]) == {key(0): UNIT}
    assert different.get_many([key(0), key(2)]) == {key(2): UNIT}


@pytest.mark.parametrize('field', ['vector', 'checksum'])
def test_oversized_row_is_rejected_before_python_materializes_it(tmp_path, field):
    cache = populated(tmp_path)
    oversized = 4 * 1024 * 1024
    with sqlite3.connect(cache.path) as db:
        if field == 'vector':
            db.execute('UPDATE embedding SET vector=zeroblob(?) WHERE identity=?',
                       (oversized, key(0)))
        else:
            db.execute('UPDATE embedding SET checksum=? WHERE identity=?',
                       ('x' * oversized, key(0)))
    # The malformed database remains below the supported disk limit. The
    # lookup must skip its corrupt row without allocating the stored payload
    # in Python, while still returning another valid requested vector.
    assert cache.path.stat().st_size < cache.budget.max_bytes
    tracemalloc.start()
    try:
        result = cache.get_many([key(0), key(1)])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == {key(1): OTHER_UNIT}
    assert cache.available
    assert peak < 1024 * 1024


@pytest.mark.parametrize('field', ['vector', 'checksum'])
def test_corrupt_record_native_memory_is_bounded_in_fresh_readers(tmp_path, field):
    # Python tracemalloc cannot observe SQLite allocations. Separate creator
    # processes prevent their large payloads from contaminating reader RSS.
    creator = r'''
import sqlite3, sys
from carr_library.semantic_cache import EmbeddingCache
root, model, first, second, field, size = sys.argv[1:]
cache = EmbeddingCache(root, model)
assert cache.put_many({first: (1.0,) + (0.0,) * 383,
                      second: (0.0, 1.0) + (0.0,) * 382})
with sqlite3.connect(cache.path) as db:
    if field == 'vector':
        db.execute('UPDATE embedding SET vector=zeroblob(?) WHERE identity=?',
                   (int(size), first))
    else:
        db.execute('UPDATE embedding SET checksum=? WHERE identity=?',
                   ('x' * int(size), first))
'''
    reader = r'''
import json, sys
from carr_library.semantic_cache import EmbeddingCache
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                          ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    def peak_bytes():
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
else:
    import resource
    def peak_bytes():
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == 'darwin' else value * 1024
root, model, first, second = sys.argv[1:]
cache = EmbeddingCache(root, model)
before = peak_bytes()
assert cache.get_many([first, second]) == {second: (0.0, 1.0) + (0.0,) * 382}
assert cache.available
print(json.dumps({'before': before, 'after': peak_bytes()}))
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH'] = str(Path(module.__file__).resolve().parent.parent)
    measurements = []
    for size in (4 * 1024 * 1024, 64 * 1024 * 1024):
        directory = tmp_path / str(size)
        subprocess.run([sys.executable, '-B', '-c', creator, str(directory),
                        MODEL, key(0), key(1), field, str(size)],
                       env=env, check=True, capture_output=True, timeout=30)
        process = subprocess.run(
            [sys.executable, '-B', '-c', reader, str(directory),
             MODEL, key(0), key(1)],
            env=env, check=True, capture_output=True, text=True, timeout=30)
        measurements.append(json.loads(process.stdout))
    assert all(item['after'] - item['before'] < 8 * 1024 * 1024
               for item in measurements), measurements
    assert measurements[1]['after'] - measurements[0]['after'] < 8 * 1024 * 1024, measurements


def test_v1_cache_is_untouched_and_v2_rebuilds_derived_vectors(tmp_path):
    directory = tmp_path / 'cache'
    directory.mkdir()
    legacy = directory / ('embeddings-' + MODEL + '.sqlite')
    with sqlite3.connect(legacy) as db:
        db.execute(module._SCHEMA[0].replace(' PRIMARY KEY NOT NULL,', ' PRIMARY KEY,')
                   + ' WITHOUT ROWID')
        db.execute(module._SCHEMA[1])
        db.execute('PRAGMA application_id=' + str(module._APPLICATION_ID))
        db.execute('PRAGMA user_version=1')
        packed = struct.pack('<384d', *UNIT)
        checksum = sha256((MODEL + key(0)).encode('ascii') + packed).hexdigest()
        db.execute('INSERT INTO embedding VALUES(?,?,?,?)', (key(0), packed, checksum, 1))
    before = legacy.read_bytes()
    cache = EmbeddingCache(directory, MODEL)
    assert cache.path.name == 'embeddings-v2-' + MODEL + '.sqlite'
    assert cache.get_many([key(0)]) == {}
    assert cache.available
    assert cache.put_many({key(0): UNIT})
    assert cache.get_many([key(0)]) == {key(0): UNIT}
    assert legacy.read_bytes() == before
    with sqlite3.connect(cache.path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


def test_exact_workspace_edition_revision_window_and_text_keys_remain_separate(tmp_path):
    locator = ('workspace', 'edition', 'revision', 'window')
    identities = [content_identity(MODEL, 'Exact\ntext', locator),
                  content_identity(MODEL, 'Exact text', locator),
                  content_identity(MODEL, 'Exact\ntext')]
    for position in range(4):
        changed = tuple('changed' if i == position else value
                        for i, value in enumerate(locator))
        identities.append(content_identity(MODEL, 'Exact\ntext', changed))
    assert len(set(identities)) == len(identities)
    cache = EmbeddingCache(tmp_path / 'cache', MODEL)
    assert cache.put_many({identities[0]: UNIT})
    assert cache.get_many(identities) == {identities[0]: UNIT}
    base = {'hashes': {'model': 'a' * 64}, 'revision': 'fixed'}
    assert len({model_identity(base), model_identity(base, runtime='changed'),
                model_identity(base, contract='changed'),
                model_identity(dict(base, revision='different'))}) == 4


@pytest.mark.parametrize('location', ['cache', 'ancestor'])
def test_directory_links_cannot_redirect_cache_writes(tmp_path, location):
    outside = tmp_path / 'outside'
    outside.mkdir()
    if location == 'cache':
        link = tmp_path / 'cache'
        os.symlink(outside, link, target_is_directory=True)
        target = link
    else:
        link = tmp_path / 'linked-library'
        os.symlink(outside, link, target_is_directory=True)
        target = link / 'cache'
    cache = EmbeddingCache(target, MODEL)
    assert not cache.put_many({key(0): UNIT})
    assert not cache.available
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('suffix', ['', '-journal', '-wal', '-shm'])
def test_cache_and_sqlite_sidecar_links_are_rejected_without_touching_target(tmp_path, suffix):
    cache = populated(tmp_path)
    outside = tmp_path / 'unrelated-file'
    outside.write_bytes(b'Unrelated content must remain unchanged.')
    destination = Path(str(cache.path) + suffix)
    if not suffix:
        destination.unlink()
    os.symlink(outside, destination)
    assert cache.get_many([key(0)]) == {}
    assert not cache.available
    assert outside.read_bytes() == b'Unrelated content must remain unchanged.'


def test_new_hardlink_to_database_disables_reads_and_writes(tmp_path):
    cache = populated(tmp_path)
    alias = tmp_path / 'database-alias'
    os.link(cache.path, alias)
    before = alias.read_bytes()
    assert cache.get_many([key(0)]) == {}
    assert not cache.put_many({key(2): UNIT})
    assert alias.read_bytes() == cache.path.read_bytes() == before


def test_recent_reads_and_replacements_preserve_entry_budget(tmp_path):
    cache = populated(tmp_path, budget=CacheBudget(max_entries=2))
    assert cache.put_many({key(0): OTHER_UNIT})
    assert cache.get_many([key(1)]) == {key(1): OTHER_UNIT}
    assert cache.put_many({key(2): UNIT})
    assert cache.get_many([key(0), key(1), key(2)]) == {
        key(1): OTHER_UNIT, key(2): UNIT}
    reopened = EmbeddingCache(cache.directory, MODEL, budget=cache.budget)
    assert reopened.get_many([key(1), key(2)]) == {key(1): OTHER_UNIT, key(2): UNIT}
    with sqlite3.connect(cache.path) as db:
        assert db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0] == 2
    assert cache.path.stat().st_size <= cache.budget.max_bytes


def test_oversized_existing_cache_is_a_miss_and_remains_untouched(tmp_path):
    cache = populated(tmp_path, budget=CacheBudget(max_entries=2, max_bytes=1024 * 1024))
    with cache.path.open('r+b') as stream:
        stream.truncate(cache.budget.max_bytes + 1)
    before = sha256(cache.path.read_bytes()).hexdigest()
    assert cache.get_many([key(0)]) == {}
    assert not cache.available
    assert sha256(cache.path.read_bytes()).hexdigest() == before


def test_busy_cache_does_not_wait_and_reopens_after_the_writer_releases(tmp_path):
    cache = populated(tmp_path)
    db = sqlite3.connect(cache.path, timeout=0, isolation_level=None)
    db.execute('BEGIN IMMEDIATE')
    try:
        assert cache.get_many([key(0)]) == {}
        assert not cache.available
    finally:
        db.rollback()
        db.close()
    fresh = EmbeddingCache(cache.directory, MODEL)
    assert fresh.get_many([key(0), key(1)]) == {key(0): UNIT, key(1): OTHER_UNIT}


def test_killed_writer_rolls_back_and_a_new_instance_resumes(tmp_path):
    cache = populated(tmp_path)
    marker = tmp_path / 'transaction-before-commit'
    child = r'''
import json, pathlib, sqlite3, sys
from carr_library import semantic_cache as module
root, model, old_key, new_key, marker = sys.argv[1:]
real_connect = sqlite3.connect
class PausedCommit(sqlite3.Connection):
    def commit(self):
        pathlib.Path(marker).write_text('transaction-open', encoding='utf-8')
        sys.stdin.buffer.read(1)
        return super().commit()
def connect(*args, **kwargs):
    return real_connect(*args, **kwargs, factory=PausedCommit)
module.sqlite3.connect = connect
cache = module.EmbeddingCache(root, model)
assert cache.put_many({old_key: (0.0, 1.0) + (0.0,) * 382,
                       new_key: (1.0,) + (0.0,) * 383})
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH'] = str(Path(module.__file__).resolve().parent.parent)
    process = subprocess.Popen(
        [sys.executable, '-B', '-c', child, str(cache.directory), MODEL,
         key(0), key(2), str(marker)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        deadline = monotonic() + 10
        while not marker.exists() and process.poll() is None and monotonic() < deadline:
            sleep(0.01)
        assert marker.exists(), process.poll()
        assert process.poll() is None
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
    reopened = EmbeddingCache(cache.directory, MODEL)
    assert reopened.get_many([key(0), key(1), key(2)]) == {
        key(0): UNIT, key(1): OTHER_UNIT}
    assert reopened.put_many({key(2): UNIT})
    assert reopened.get_many([key(2)]) == {key(2): UNIT}
    with sqlite3.connect(cache.path) as db:
        assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
