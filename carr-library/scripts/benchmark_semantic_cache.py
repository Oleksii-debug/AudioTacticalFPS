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


# The opt-in source-page experiment shares this entry point, not the cache
# experiment's result schema. Its corpus and owned workers never use user data.
_SOURCE_PAGE_SCHEMA = 'carr-source-page-benchmark/1'
_SOURCE_PAGE_QUERY = 'sourcewindowneedle'
_SOURCE_PAGE_FILES = 4096
_SOURCE_PAGE_BYTES = 64 * 1024**2


def _page_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _page_read(path, maximum):
    import stat
    path = Path(path)
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, 'st_file_attributes', 0) & 0x400
            or before.st_size > maximum):
        raise ValueError('Benchmark evidence must be a bounded regular single-link file.')
    with path.open('rb') as stream:
        value = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if (len(value) != before.st_size or identity(before) != identity(after)
            or identity(before) != identity(path.lstat())):
        raise ValueError('Benchmark evidence changed while it was read.')
    return value


def _page_modules(package):
    """Bind the selected Python implementation; do not import a checkout fallback."""
    import stat
    rows, total = [], 0
    package = Path(package).resolve(strict=True)
    for path in package.rglob('*.py'):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024**2:
            raise ValueError('Selected package contains an unsupported source file.')
        total += info.st_size
        if len(rows) >= _SOURCE_PAGE_FILES or total > _SOURCE_PAGE_BYTES:
            raise ValueError('Selected package exceeds the benchmark source inventory allowance.')
        value = _page_read(path, 2 * 1024**2)
        rows.append({'name': path.relative_to(package).as_posix(),
                     'bytes': len(value), 'sha256': sha256(value).hexdigest()})
    if not rows or not any(row['name'] == 'application.py' for row in rows):
        raise ValueError('The selected package has no inspectable application implementation.')
    return sorted(rows, key=lambda row: row['name'])


def _page_environment():
    hardware = {'machine': platform.machine(), 'processor': platform.processor(),
                'logical_processors': os.cpu_count()}
    runtime = {'platform': platform.platform(), 'python': sys.version,
               'sqlite': sqlite3.sqlite_version, 'rss_method': rss_method()}
    return {'hardware': hardware, 'hardware_id': sha256(_page_json(hardware)).hexdigest(),
            'hardware_id_meaning': 'Fingerprint of reported hardware fields; not a unique machine serial.',
            'runtime': runtime, 'runtime_id': sha256(_page_json(runtime)).hexdigest()}


def _page_text(number, characters, match_every):
    marker = _SOURCE_PAGE_QUERY if number % match_every == 0 else 'differenttopic'
    prefix = f'Synthetic passage {number:08d}. {marker}. Незмінний текст. '
    filler = 'Local synthetic evidence with a bounded original paragraph. '
    return (prefix + filler * (characters // len(filler) + 1))[:characters]


def _page_recipe(count, characters, per_book, match_every):
    return {'generator': 'strict-utf8-form-feed/1', 'pages': count,
            'characters_per_page': characters, 'pages_per_book': per_book,
            'match_every': match_every, 'query': _SOURCE_PAGE_QUERY,
            'construction': 'LibraryApp.add_text; ordinary retained TXT import, outside search timing'}


def _page_documents(recipe):
    for start in range(0, recipe['pages'], recipe['pages_per_book']):
        stop = min(start + recipe['pages_per_book'], recipe['pages'])
        yield start, '\f'.join(_page_text(number, recipe['characters_per_page'],
                                        recipe['match_every']) for number in range(start, stop))


def _page_content_id(recipe):
    digest, byte_count = sha256(), 0
    for _, raw in _page_documents(recipe):
        value = raw.encode('utf-8')
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
        byte_count += len(value)
    return digest.hexdigest(), byte_count


def _page_percentiles(values):
    import math
    ordered = sorted(values)
    return {'samples': len(ordered), 'raw_seconds': list(values),
            'p50_seconds': ordered[max(0, math.ceil(len(ordered) * .50) - 1)] if ordered else None,
            'p95_seconds': ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else None,
            'definition': 'Nearest-rank percentiles of measured calls; no performance threshold.'}


def _page_build(case, recipe):
    from carr_library.application import LibraryApp
    from carr_library.durable_state import atomic_json
    case = Path(case)
    case.mkdir(exist_ok=False, mode=0o700)
    started = perf_counter()
    app = LibraryApp(case / 'library')
    workspace = app.create_workspace('Synthetic source-page benchmark')
    digest, byte_count = _page_content_id(recipe)
    documents = 0
    for start, raw in _page_documents(recipe):
        imported = app.add_text(workspace, f'Synthetic book {start:08d}', raw,
                                authors=('Synthetic benchmark',), language='uk')
        if imported['asset'] != sha256(raw.encode('utf-8')).hexdigest():
            raise ValueError('The retained original differs from the synthetic source bytes.')
        documents += 1
    with app.read(workspace) as db:
        pages, characters = db.execute(
            'SELECT count(*),coalesce(sum(length(raw)),0) FROM active_page WHERE workspace=?',
            (workspace,)).fetchone()
        editions = db.execute('SELECT count(*) FROM catalog_edition WHERE workspace=?',
                              (workspace,)).fetchone()[0]
    if (pages != recipe['pages'] or characters != pages * recipe['characters_per_page']
            or editions != documents):
        raise ValueError('Actual retained corpus differs from the deterministic recipe.')
    corpus = {'recipe': recipe, 'recipe_id': sha256(_page_json(recipe)).hexdigest(),
              'content_sha256': digest, 'original_utf8_bytes': byte_count,
              'workspace': workspace, 'actual_pages': pages, 'actual_editions': editions,
              'actual_characters': characters, 'setup_seconds': perf_counter() - started,
              'setup_peak_rss_bytes': peak_rss_bytes()}
    atomic_json(case / 'corpus.json', corpus)
    return corpus


def _page_measure(case, corpus, mode, settings):
    from carr_library.application import LibraryApp
    case = Path(case)
    started = perf_counter()
    app = LibraryApp(case / 'library')
    opening_seconds = perf_counter() - started
    baseline = peak_rss_bytes()
    records = []
    workspace = corpus['workspace']
    expected_pages = corpus['recipe']['pages']
    for trial in range(settings['repeats']):
        cursor, seen_cursors, seen_results = None, set(), set()
        for index in range(settings['pages_per_trial']):
            before_rss = peak_rss_bytes()
            started = perf_counter()
            try:
                page = app.search_page(workspace, _SOURCE_PAGE_QUERY, mode=mode,
                    cursor=cursor, limit=settings['result_limit'],
                    page_budget=settings['page_budget'],
                    character_budget=settings['character_budget'], timeout=settings['search_timeout'])
            except TimeoutError as exc:
                records.append({'trial': trial, 'page_index': index, 'outcome': 'search-timeout',
                    'seconds': perf_counter() - started, 'error_type': type(exc).__name__,
                    'before_peak_rss_bytes': before_rss, 'after_peak_rss_bytes': peak_rss_bytes()})
                break
            elapsed = perf_counter() - started
            if (page.scope_editions != corpus['actual_editions']
                    or not 0 <= page.scanned_segments <= settings['page_budget']
                    or not 0 <= len(page.results) <= settings['result_limit']):
                raise ValueError('The public page result exceeded its reviewed corpus or budgets.')
            for result in page.results:
                key = (result.edition_id, result.revision, result.page, result.quote)
                if (not result.verified or result.workspace != workspace or key in seen_results
                        or _SOURCE_PAGE_QUERY not in result.quote.casefold()):
                    raise ValueError('The measured search returned an unverified or duplicate source result.')
                seen_results.add(key)
            verification_started = perf_counter()
            if page.results:
                first = page.results[0]
                if app.copy(workspace, first.ref, first.revision, 'quote') != first.quote:
                    raise ValueError('The measured result differs from the retained original quotation.')
            verification_seconds = perf_counter() - verification_started
            if page.next_cursor is not None:
                if page.next_cursor in seen_cursors:
                    raise ValueError('The public source-page continuation did not advance.')
                seen_cursors.add(page.next_cursor)
            records.append({'trial': trial, 'page_index': index, 'outcome': 'returned',
                'seconds': elapsed, 'reported_scanned_segments': page.scanned_segments,
                'scope_editions': page.scope_editions, 'results': len(page.results),
                'complete': page.complete, 'ordering': page.ordering,
                'sample_quote_verification_seconds': verification_seconds,
                'before_peak_rss_bytes': before_rss, 'after_peak_rss_bytes': peak_rss_bytes()})
            cursor = page.next_cursor
            if cursor is None:
                break
    completed = [row for row in records if row['outcome'] == 'returned']
    return {'mode': mode, 'opening_seconds': opening_seconds,
        'baseline_peak_rss_bytes': baseline, 'process_peak_rss_bytes': peak_rss_bytes(),
        'full_snapshot_scope_pages': expected_pages,
        'sampling_complete_trials': sum(row.get('complete', False) for row in completed),
        'search_timeouts': sum(row['outcome'] == 'search-timeout' for row in records),
        'first_page': _page_percentiles([row['seconds'] for row in completed if row['page_index'] == 0]),
        'continuation_pages': _page_percentiles([row['seconds'] for row in completed if row['page_index'] > 0]),
        'samples': records}


def _source_page_case(arguments):
    if len(arguments) != 5:
        raise ValueError('Invalid private source-page benchmark invocation.')
    config_name, expected, action, case_name, corpus_digest = arguments
    raw = _page_read(config_name, 4 * 1024**2)
    if not re.fullmatch('[0-9a-f]{64}', expected) or sha256(raw).hexdigest() != expected:
        raise ValueError('Source-page benchmark configuration changed before child admission.')
    config = json.loads(raw)
    if config['schema'] != _SOURCE_PAGE_SCHEMA or action not in ('build', 'phrase', 'thematic'):
        raise ValueError('Invalid source-page benchmark action.')
    package = Path(config['package']).resolve(strict=True)
    if not config['installed']:
        sys.path.insert(0, str(package.parent))
    import carr_library
    if Path(carr_library.__file__).resolve().parent != package:
        raise ValueError('Child imported a different production package.')
    if _page_modules(package) != config['modules']:
        raise ValueError('Selected production source changed before the benchmark.')
    if sha256(_page_read(__file__, 2 * 1024**2)).hexdigest() != config['harness_sha256']:
        raise ValueError('Benchmark harness snapshot changed.')
    case = Path(case_name)
    if case.parent.resolve() != Path(config['output']).resolve():
        raise ValueError('The synthetic case is outside this newly created benchmark directory.')
    count = int(case.name)
    if count not in config['sizes']:
        raise ValueError('The synthetic case was not explicitly selected.')
    recipe = _page_recipe(count, config['characters'], config['pages_per_book'], config['match_every'])
    if action == 'build':
        result = _page_build(case, recipe)
    else:
        corpus_raw = _page_read(case / 'corpus.json', 128 * 1024)
        if sha256(corpus_raw).hexdigest() != corpus_digest:
            raise ValueError('The retained synthetic corpus receipt changed.')
        corpus = json.loads(corpus_raw)
        if corpus['recipe'] != recipe or corpus['recipe_id'] != sha256(_page_json(recipe)).hexdigest():
            raise ValueError('The measured corpus differs from its admitted recipe.')
        result = _page_measure(case, corpus, action, config['settings'])
    if _page_modules(package) != config['modules']:
        raise ValueError('Selected production source changed during the benchmark.')
    result.update(action=action, configuration_sha256=expected,
                  environment=_page_environment(), outcome='measured')
    from carr_library.durable_state import atomic_json
    destination = case / (action + '.json')
    atomic_json(destination, result)
    print(json.dumps({'action': action, 'pages': count, 'evidence': destination.name,
                      'sha256': sha256(_page_read(destination, 4 * 1024**2)).hexdigest()}))
    return 0


def _source_pages_main(arguments):
    parser = argparse.ArgumentParser(description='Opt-in synthetic retained-source page measurements.')
    parser.add_argument('--source-pages', action='store_true', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000])
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--characters', type=int, default=512)
    parser.add_argument('--pages-per-book', type=int, default=128)
    parser.add_argument('--match-every', type=int, default=7)
    parser.add_argument('--modes', nargs='+', choices=('phrase', 'thematic'), default=['phrase', 'thematic'])
    parser.add_argument('--page-budget', type=int, default=64)
    parser.add_argument('--character-budget', type=int, default=1_000_000)
    parser.add_argument('--result-limit', type=int, default=20)
    parser.add_argument('--pages-per-trial', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--search-timeout', type=float, default=60)
    parser.add_argument('--case-timeout', type=float, default=600)
    parser.add_argument('--hardware-label', default='', help='Optional operator label; never a machine serial.')
    args = parser.parse_args(arguments)
    if (not args.sizes or len(args.sizes) > 6 or len(set(args.sizes)) != len(args.sizes)
            or any(not 1 <= value <= 50000 for value in args.sizes)
            or len(set(args.modes)) != len(args.modes)
            or not 128 <= args.characters <= 4096 or not 1 <= args.pages_per_book <= 256
            or not 1 <= args.match_every <= 1000 or not 1 <= args.page_budget <= 20000
            or not args.characters <= args.character_budget <= 5_000_000
            or not 1 <= args.result_limit <= 100 or not 1 <= args.pages_per_trial <= 20
            or not 1 <= args.repeats <= 10 or not 0 < args.search_timeout <= 60
            or not 1 <= args.case_timeout <= 3600 or len(args.hardware_label) > 80
            or any(ord(c) < 32 for c in args.hardware_label)):
        parser.error('Choose finite synthetic corpus, sample and execution bounds.')
    if (args.page_budget, args.character_budget) == (500, 5_000_000):
        parser.error('Choose a non-default window budget so small literal corpora also exercise source windows.')
    source = source_location(args.installed)
    package = source.parent
    modules = _page_modules(package)
    output = args.output_dir.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output = output.resolve(strict=True)
    harness_bytes = _page_read(__file__, 2 * 1024**2)
    harness = output / 'benchmark-harness.py'
    with harness.open('xb') as stream:
        stream.write(harness_bytes)
    settings = {name: getattr(args, name) for name in
        ('page_budget', 'character_budget', 'result_limit', 'pages_per_trial', 'repeats', 'search_timeout')}
    config = dict(schema=_SOURCE_PAGE_SCHEMA, package=str(package), installed=args.installed,
        output=str(output), sizes=args.sizes, characters=args.characters, pages_per_book=args.pages_per_book,
        match_every=args.match_every, settings=settings, modules=modules,
        harness_sha256=sha256(harness_bytes).hexdigest())
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    from carr_library.durable_state import atomic_json
    config_path = output / 'run.json'
    atomic_json(config_path, config)
    config_digest = sha256(_page_read(config_path, 4 * 1024**2)).hexdigest()
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    environment.pop('PYTHONPATH', None)
    cases = []
    try:
        for count in args.sizes:
            case = output / str(count)
            phases = []
            corpus_digest = '-'
            for action in ('build', *args.modes):
                result = run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-X', 'utf8',
                    str(harness), '--source-page-case', str(config_path), config_digest,
                    action, str(case), corpus_digest],
                    budget=AudioBudget(timeout=args.case_timeout, max_output_bytes=768 * 1024**2,
                                       max_log_bytes=2 * 1024**2, max_staging_files=10000),
                    owned_tree=True, env=environment, cwd=str(output),
                    watch=(case,) if action == 'build' else (case / (action + '.json'),))
                returned = json.loads(result.stdout)
                evidence = case / (action + '.json')
                raw = _page_read(evidence, 4 * 1024**2)
                measured = json.loads(raw)
                if (returned.get('action') != action or returned.get('pages') != count
                        or returned.get('evidence') != evidence.name
                        or returned.get('sha256') != sha256(raw).hexdigest()
                        or measured.get('configuration_sha256') != config_digest
                        or measured.get('action') != action or measured.get('outcome') != 'measured'):
                    raise ValueError('The owned child returned mismatched measurement evidence.')
                phases.append(measured)
                if action == 'build':
                    corpus_digest = sha256(_page_read(case / 'corpus.json', 128 * 1024)).hexdigest()
                print(json.dumps({'source_pages': count, 'phase': action, 'outcome': measured['outcome'],
                    'worker_seconds': result.elapsed, 'search_timeouts': measured.get('search_timeouts', 0)}),
                    flush=True)
            cases.append({'source_pages': count, 'corpus': phases[0], 'measurements': phases[1:]})
        if _page_modules(package) != modules:
            raise ValueError('Selected production source changed before final evidence publication.')
        report = dict(schema=_SOURCE_PAGE_SCHEMA, outcome='measured',
            recorded_at_utc=datetime.now(timezone.utc).isoformat(), environment=_page_environment(),
            hardware_label=args.hardware_label, configuration_sha256=config_digest,
            production_python_inventory_sha256=sha256(_page_json(modules)).hexdigest(),
            harness_sha256=config['harness_sha256'], settings=settings, cases=cases,
            method={
                'corpus': 'Fresh retained UTF-8 TXT originals and sealed revisions through LibraryApp.add_text.',
                'isolation': 'Existing owned process boundary; fresh setup child and fresh measurement child per mode/size.',
                'timing': 'Library opening and sampled quote verification are separate from search_page latency.',
                'snapshot': 'Every search page revalidates the entire selected source snapshot; its cost is included.',
                'ranking': 'Thematic relevance is local to each source window, never a corpus-wide ranking.',
                'temperature': 'First call in a new process; OS file cache is not flushed. Repeats retain that process.',
                'rss': 'Whole measurement-child high-water RSS, including Python imports and source inventory; setup uses another child.',
                'coverage': 'Bounded sampled pages; an unfinished continuation is not corpus exhaustion.',
                'timeout': 'Public search deadline and owned-child wall limit are resource bounds, not PASS thresholds.'},
            claims={'performance_acceptance': 'NOT_ASSESSED', 'neural_inference': 'NOT_RUN',
                    'retrieval_quality': 'NOT_MEASURED', 'cold_disk': 'NOT_MEASURED',
                    'native_application_acceptance': 'NOT_RUN'})
        atomic_json(output / 'source-pages.json', report)
    except BaseException as exc:
        # Keep the owned synthetic directory and any immutable partial evidence.
        # In particular, never delete files while native cleanup may be pending.
        failure = dict(schema=_SOURCE_PAGE_SCHEMA, outcome='incomplete',
            configuration_sha256=config_digest, completed_cases=len(cases),
            error_type=type(exc).__name__, error=str(exc)[:2000])
        try:
            atomic_json(output / 'source-pages-incomplete.json', failure)
        except Exception:
            pass
        raise
    print(json.dumps({'evidence': str(output / 'source-pages.json'),
                      'outcome': 'measured', 'performance_acceptance': 'NOT_ASSESSED'}))
    return 0


def _benchmark_entry():
    if not __debug__:
        raise ValueError('Run the benchmark without Python optimization flags.')
    if len(sys.argv) > 1 and sys.argv[1] == '--source-page-case':
        return _source_page_case(sys.argv[2:])
    if '--source-pages' in sys.argv[1:]:
        return _source_pages_main(sys.argv[1:])
    return main()


if __name__ == '__main__':
    try:
        raise SystemExit(_benchmark_entry())
    except (OSError, ValueError, AssertionError, ImportError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
