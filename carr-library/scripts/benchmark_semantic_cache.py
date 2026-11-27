"""Measure synthetic SQLite embedding storage, never neural inference or quality.

From any working directory, using this checkout's production implementation:
    python /path/to/carr-library/scripts/benchmark_semantic_cache.py \
        --output-dir /new/benchmark-directory --sizes 1000 10000 50000

Use --installed to resolve carr_library from the current Python environment.
The source file and a private snapshot are SHA256-bound to every child process.
A new output directory is required; existing data is never overwritten. The
result records real measurements for this execution, not a release verdict.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
from time import perf_counter


def peak_rss_bytes():
    if os.name == 'nt':
        # Microsoft documents PeakWorkingSetSize in bytes:
        # https://learn.microsoft.com/windows/win32/api/psapi/ns-psapi-process_memory_counters
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        *[(name, ctypes.c_size_t) for name in (
                            'PeakWorkingSetSize', 'WorkingSetSize',
                            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
                            'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
                            'PagefileUsage', 'PeakPagefileUsage')]]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters),
                                             wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                          ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def rss_method():
    if os.name == 'nt':
        return 'GetProcessMemoryInfo(PROCESS_MEMORY_COUNTERS).PeakWorkingSetSize; bytes'
    units = 'bytes' if sys.platform == 'darwin' else 'KiB converted to bytes'
    return 'resource.getrusage(RUSAGE_SELF).ru_maxrss; ' + units


def run_case(snapshot, directory, requested, source_root):
    sys.path.insert(0, str(source_root))
    name = 'carr_library._semantic_cache_scale_snapshot'
    spec = importlib.util.spec_from_file_location(name, snapshot)
    cache_module = importlib.util.module_from_spec(spec)
    sys.modules[name] = cache_module
    spec.loader.exec_module(cache_module)
    source_digest = sha256(Path(snapshot).read_bytes()).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    model = sha256(b'Synthetic scale fixture; not a neural model output').hexdigest()
    vectors = tuple(tuple(1.0 if i == coordinate else 0.0 for i in range(384))
                    for coordinate in range(384))
    def identity(i):
        return cache_module.content_identity(
            model, 'Synthetic fixture passage ' + str(i),
            ('workspace', 'edition-' + str(i // 64), 'revision-1', str(i)))
    cache = cache_module.EmbeddingCache(directory / 'cache', model)
    baseline_rss = peak_rss_bytes()
    started = perf_counter()
    writes = 0
    for start in range(0, requested, 512):
        end = min(start + 512, requested)
        assert cache.put_many({identity(i): vectors[i % 384] for i in range(start, end)}), (
            'write failed', start, cache.last_error)
        writes += 1
    write_seconds = perf_counter() - started
    db_path = cache.path
    del cache
    reopened = cache_module.EmbeddingCache(directory / 'cache', model)
    def read_all():
        start_time = perf_counter()
        hits = 0
        for start in range(0, requested, 512):
            end = min(start + 512, requested)
            expected = {identity(i): vectors[i % 384] for i in range(start, end)}
            actual = reopened.get_many(expected)
            assert actual == expected, ('read mismatch', start, len(actual), len(expected),
                                        reopened.last_error)
            hits += len(actual)
        return {'seconds': perf_counter() - start_time, 'hits': hits}
    after_reopen = read_all()
    repeated = read_all()
    with sqlite3.connect(db_path) as db:
        count = db.execute('SELECT COUNT(*) FROM embedding').fetchone()[0]
        check = db.execute('PRAGMA quick_check').fetchone()[0]
        pages = db.execute('PRAGMA page_count').fetchone()[0]
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        schema = [row[0] for row in db.execute(
            'SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC,name')]
    assert count == requested and check == 'ok' and reopened.available
    files = [{'name': p.name, 'size_bytes': p.stat().st_size}
             for p in sorted(reopened.directory.iterdir()) if p.is_file()]
    assert db_path.stat().st_size <= reopened.budget.max_bytes
    assert count <= reopened.budget.max_entries
    assert not any(p['name'].endswith(('-journal', '-wal', '-shm')) for p in files)
    return {
        'status': 'PASS', 'source_sha256': source_digest, 'requested_entries': requested,
        'stored_entries': count, 'dimensions': 384, 'maximum_batch_size': 512,
        'write_batches': writes, 'write_seconds': write_seconds,
        'reopened_read': after_reopen, 'repeated_read': repeated,
        'baseline_peak_rss_bytes': baseline_rss, 'process_peak_rss_bytes': peak_rss_bytes(),
        'database_size_bytes': db_path.stat().st_size,
        'page_count': pages, 'page_size_bytes': page_size, 'files': files,
        'entry_budget': reopened.budget.max_entries, 'byte_budget': reopened.budget.max_bytes,
        'sqlite_quick_check': check, 'schema': schema,
        'local_fixture_path': str(directory),
    }


def source_location(installed):
    if not installed:
        project_source = Path(__file__).resolve().parents[1] / 'src'
        if not (project_source / 'carr_library/semantic_cache.py').is_file():
            raise ValueError('Project source is unavailable; use --installed for an installed package.')
        sys.path.insert(0, str(project_source))
    spec = importlib.util.find_spec('carr_library.semantic_cache')
    if spec is None or spec.origin is None:
        raise ValueError('The selected environment has no carr_library.semantic_cache module.')
    source = Path(spec.origin).resolve(strict=True)
    if source.suffix != '.py' or not source.is_file():
        raise ValueError('The production cache must have an inspectable Python source file.')
    return source


def main():
    # The measurement kernel deliberately retains its executable invariants.
    if not __debug__:
        raise ValueError('Run the benchmark without Python optimization flags.')
    if len(sys.argv) > 1 and sys.argv[1] == '--case':
        snapshot, directory, requested, source_root, expected = sys.argv[2:]
        if sha256(Path(snapshot).read_bytes()).hexdigest() != expected:
            raise ValueError('Production source snapshot checksum mismatch.')
        result = run_case(snapshot, directory, int(requested), source_root)
        if result['source_sha256'] != expected:
            raise ValueError('Child measurement source checksum mismatch.')
        print(json.dumps(result))
        return 0
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', required=True, type=Path,
                        help='New directory for synthetic databases, source snapshot and JSON evidence.')
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000, 10000, 50000],
                        help='Distinct positive record counts up to the default 50,000-entry budget.')
    parser.add_argument('--installed', action='store_true',
                        help='Use the production module resolved by this Python environment.')
    parser.add_argument('--expected-source-sha256',
                        help='Optional exact SHA256 from an earlier evidence record.')
    args = parser.parse_args()
    if (not args.sizes or len(args.sizes) > 10 or len(set(args.sizes)) != len(args.sizes)
            or any(size < 1 or size > 50000 for size in args.sizes)):
        parser.error('Choose one to ten distinct sizes between 1 and 50,000.')
    if args.expected_source_sha256 is not None and not re.fullmatch(
            '[0-9a-f]{64}', args.expected_source_sha256):
        parser.error('Expected source SHA256 must be 64 lowercase hexadecimal characters.')
    source = source_location(args.installed)
    source_bytes = source.read_bytes()
    source_digest = sha256(source_bytes).hexdigest()
    if args.expected_source_sha256 is not None and source_digest != args.expected_source_sha256:
        raise ValueError('Selected production source differs from the expected SHA256.')
    output = args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = output / ('semantic-cache-' + source_digest + '.py')
    with snapshot.open('xb') as stream:
        stream.write(source_bytes)
    cases = []
    for count in args.sizes:
        child = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()), '--case', str(snapshot),
             str(output / str(count)), str(count), str(source.parent.parent), source_digest],
            check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        result = json.loads(child.stdout)
        if (result.get('status') != 'PASS' or result.get('source_sha256') != source_digest
                or result.get('requested_entries') != count or result.get('stored_entries') != count):
            raise ValueError('Child measurement identity or row-count mismatch.')
        cases.append(result)
        print(json.dumps({'completed': count, 'write_seconds': result['write_seconds'],
                          'reopened_read_seconds': result['reopened_read']['seconds'],
                          'database_size_bytes': result['database_size_bytes'],
                          'process_peak_rss_bytes': result['process_peak_rss_bytes']}), flush=True)
    if (sha256(source.read_bytes()).hexdigest() != source_digest
            or sha256(snapshot.read_bytes()).hexdigest() != source_digest):
        raise ValueError('Production source changed during the benchmark.')
    result = {
        'schema': 1, 'status': 'PASS', 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'source_path': str(source), 'source_sha256': source_digest,
        'source_matches_worktree_after_run': True,
        'harness_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
        'environment': {'platform': platform.platform(), 'python': sys.version,
                        'sqlite': sqlite3.sqlite_version, 'peak_rss_method': rss_method()},
        'method': {'storage': 'Actual SQLite files and production cache implementation snapshot',
                   'vectors': 'Deterministic normalized 384-dimensional unit coordinate vectors',
                   'identity': 'Synthetic model/content/workspace/edition/revision/window identities',
                   'isolation': 'Fresh child process and new database per size; read phases reopen connections',
                   'read_temperature': 'Reopened and repeated reads use the same OS page cache; neither is a cold-disk measurement',
                   'peak_memory': 'Whole child-process high-water RSS, not an allocation-only measurement',
                   'writes': 'Synchronous FULL SQLite transactions, at most 512 vectors per batch'},
        'claims': {'neural_inference': 'NOT_RUN', 'retrieval_quality': 'NOT_MEASURED',
                   'native_application_acceptance': 'NOT_RUN',
                   'source_authority': 'NOT_ESTABLISHED_BY_CACHE'},
        'cases': cases,
    }
    destination = output / 'semantic-cache-scale.json'
    temporary = output / 'semantic-cache-scale.json.partial'
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, destination)
    print(json.dumps({'evidence': str(destination), 'source_sha256': source_digest,
                      'source_unchanged': True}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, AssertionError, ImportError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
