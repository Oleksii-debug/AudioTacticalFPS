"""Measure existing catalogue/search/source services on synthetic text editions.

Run in a source checkout with the normal development dependencies (including
pytest, used by the reused search fixture), without Python's -O flag:

    python scripts/benchmark_library_capacity.py --output-dir NEW_DIRECTORY

Each size runs in a fresh process. Completed fixture trees are removed only
after their JSON evidence is durable, unless --keep-fixtures is selected. This
uses the existing test corpus publisher and production services; it does not
measure a real book corpus or turn direct catalogue publication into import
throughput. A separate small TXT series exercises the actual import boundary.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
from threading import Event, Thread, Timer
from time import perf_counter

from benchmark_semantic_cache import peak_rss_bytes, rss_method


PROJECT = Path(__file__).resolve().parents[1]
TEXT = 'Needle exact source text. Український текст. Synthetic capacity fixture only.\n'
QUERY = 'Needle'
BATCH = 1000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_identity():
    paths = sorted((PROJECT / 'src').rglob('*.py'))
    paths += sorted((PROJECT / 'bootstrap').rglob('*.py'))
    paths += [PROJECT / 'tests/search/test_search_pagination.py',
              PROJECT / 'scripts/benchmark_semantic_cache.py', Path(__file__).resolve()]
    files = {str(path.relative_to(PROJECT)): sha256(path.read_bytes()).hexdigest()
             for path in paths}
    encoded = json.dumps(files, sort_keys=True, separators=(',', ':')).encode()
    return {'sha256': sha256(encoded).hexdigest(), 'files': files}


def statistics(values):
    require(bool(values), 'Cannot summarize an unmeasured operation.')
    ordered = sorted(values)
    return {'samples': len(values), 'p50_seconds': ordered[math.ceil(len(values) * .5) - 1],
            'p95_seconds': ordered[math.ceil(len(values) * .95) - 1],
            'min_seconds': ordered[0], 'max_seconds': ordered[-1],
            'total_seconds': sum(values), 'seconds': values,
            'percentile_method': 'nearest rank; ceil(p * sample count), one based'}


def timed(values, action):
    started = perf_counter()
    result = action()
    values.append(perf_counter() - started)
    return result


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.partial')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def fixture_size(root):
    logical = allocated = count = 0
    for path in root.rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():
                info = path.stat()
                logical += info.st_size
                allocated += getattr(info, 'st_blocks', 0) * 512
                count += 1
        except FileNotFoundError:
            pass  # A SQLite journal can disappear between directory/stat calls.
    return {'apparent_bytes': logical,
            'allocated_bytes': allocated if os.name == 'posix' else None,
            'files': count}


class DiskSampler:
    """Sample owned fixture files, including temporary SQLite journals."""
    def __init__(self, root):
        self.root = root
        self.stop = Event()
        self.peak = fixture_size(root)
        self.samples = 0
        self.errors = []
        self.thread = Thread(target=self.run, daemon=True)

    def sample(self):
        current = fixture_size(self.root)
        self.samples += 1
        for key, value in current.items():
            if value is not None:
                self.peak[key] = max(self.peak[key], value)

    def run(self):
        try:
            while not self.stop.wait(.05):
                self.sample()
        except BaseException as exc:
            self.errors.append(type(exc).__name__)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *unused):
        self.stop.set()
        self.thread.join()
        self.sample()
        require(not self.errors, 'Fixture disk sampler failed: ' + ','.join(self.errors))

    def report(self):
        return {'final': fixture_size(self.root), 'sampled_peak': self.peak,
                'samples': self.samples, 'interval_seconds': .05,
                'peak_is_sampled_lower_bound': True,
                'scope': 'Owned fixture files including originals, inputs, databases and journals; excludes interpreter/source/environment'}


def production():
    sys.path.insert(0, str(PROJECT))
    sys.path.insert(0, str(PROJECT / 'src'))
    from carr_library.application import LibraryApp
    fixture_path = PROJECT / 'tests/search/test_search_pagination.py'
    spec = importlib.util.spec_from_file_location('_capacity_existing_search_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return LibraryApp, fixture.publish_text_corpus


def verify_hit(app, workspace, hit):
    require(hit.workspace == workspace and hit.verified and hit.quote == QUERY,
            'A result lost workspace, verification or exact quotation identity.')
    context = app.context(workspace, hit.ref, hit.revision)
    require(len(context.pages) == 1 and context.pages[0].raw_text == TEXT,
            'Opening a result did not resolve the retained original text.')
    require(app.copy(workspace, hit.ref, hit.revision, 'quote') == QUERY,
            'Copy did not independently preserve the exact source quotation.')


def restart_probe(config):
    LibraryApp, _ = production()
    started = perf_counter()
    app = LibraryApp(config['root'])
    startup = perf_counter() - started
    workspace = config['workspace']
    started = perf_counter()
    page = app.search_page(workspace, QUERY, limit=100, cursor=config['cursor'])
    search_seconds = perf_counter() - started
    require([row.edition_id for row in page.results] == config['expected'],
            'Fresh-process continuation differed from its exact expected edition sequence.')
    if page.results:
        verify_hit(app, workspace, page.results[0])
    try:
        app.copy(workspace, config['old_ref'], config['old_revision'], 'quote')
    except ValueError:
        pass
    else:
        raise ValueError('A new process accepted a previous process selection handle.')
    return {'status': 'PASS', 'application_startup_seconds': startup,
            'continued_search_seconds': search_seconds, 'continued_results': len(page.results),
            'old_selection_rejected': True, 'source_and_copy_verified': True,
            'process_peak_rss_bytes': peak_rss_bytes()}


def run_case(root, requested, samples, max_search_pages, expected_source):
    require(source_identity()['sha256'] == expected_source, 'Child source identity mismatch.')
    LibraryApp, publish = production()
    baseline = peak_rss_bytes()
    started = perf_counter()
    root.mkdir(exist_ok=False)
    with DiskSampler(root) as disk:
        startup = []
        app = timed(startup, lambda: LibraryApp(root / 'library'))
        workspace = app.create_workspace('Synthetic capacity measurement')
        other = app.create_workspace('Isolation control')
        construction, identities = [], []
        build_started = perf_counter()
        for number, offset in enumerate(range(0, requested, BATCH)):
            values, digest = timed(construction, lambda number=number, offset=offset: publish(
                app, workspace, count=min(BATCH, requested - offset), text=TEXT,
                prefix=f'capacity-{number:05d}'))
            identities.extend(values)
        build_elapsed = perf_counter() - build_started
        require(len(identities) == requested and len(set(identities)) == requested,
                'Synthetic identities were omitted or duplicated.')
        publish(app, other, count=1, text='Isolation-only source.', prefix='foreign')
        require(sha256(app.assets.resolve(digest).read_bytes()).hexdigest() == digest,
                'Retained original digest mismatch.')
        with app.read(workspace) as db:
            counts = {name: db.execute(f'SELECT count(*) FROM {name} WHERE workspace=?',
                                      (workspace,)).fetchone()[0]
                      for name in ('catalog_edition', 'active_page')}
            require(all(value == requested for value in counts.values()), 'Stored scope count mismatch.')
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'SQLite quick_check failed.')
        post_build_disk = fixture_size(root)
        browse_times, browsed, after = [], [], None
        while True:
            page = timed(browse_times, lambda: app.browse(workspace, after=after, limit=500))
            if not page:
                break
            browsed.extend(row.edition_id for row in page)
            after = page[-1].edition_id
        require(browsed == identities, 'Full catalogue traversal lost order or identities.')
        search_times, open_times, copy_times, first_times = [], [], [], []
        first = None
        for _ in range(samples):
            first = timed(first_times, lambda: app.search_page(workspace, QUERY, limit=100))
            require([row.edition_id for row in first.results] == identities[:100],
                    'Repeated first page changed exact identities.')
        first_cursor = first.next_cursor
        seen, cursor, pages, opened = 0, None, 0, 0
        search_started = perf_counter()
        while max_search_pages == 0 or pages < max_search_pages:
            page = timed(search_times, lambda: app.search_page(workspace, QUERY, limit=100, cursor=cursor))
            require(page.total_editions == requested and page.total_source_pages == requested,
                    'Search snapshot count changed.')
            require([row.edition_id for row in page.results] == identities[seen:seen + len(page.results)],
                    'Search page omitted, repeated or reordered an edition.')
            require(all(row.verified and row.workspace == workspace and row.quote == QUERY
                        for row in page.results), 'Search result exactness/isolation failed.')
            if page.results and opened < samples:
                hit = page.results[0]
                context = timed(open_times, lambda: app.context(workspace, hit.ref, hit.revision))
                require(len(context.pages) == 1 and context.pages[0].raw_text == TEXT,
                        'Context failed retained-original validation.')
                copied = timed(copy_times, lambda: app.copy(workspace, hit.ref, hit.revision, 'quote'))
                require(copied == QUERY, 'Copy changed the source text.')
                opened += 1
            seen += len(page.results)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                require(seen == requested, 'Completed search omitted source editions.')
                break
        traversal_elapsed = perf_counter() - search_started
        # Extra opens reuse one live result; every call still rechecks the source.
        while len(open_times) < samples:
            context = timed(open_times, lambda: app.context(workspace, hit.ref, hit.revision))
            require(context.pages[0].raw_text == TEXT, 'Repeated context source mismatch.')
            require(timed(copy_times, lambda: app.copy(workspace, hit.ref, hit.revision, 'quote')) == QUERY,
                    'Repeated copy source mismatch.')
        current = app.search_page(workspace, QUERY, limit=100)
        selected = current.results[0]
        previous_selections = dict(app._selections)
        cancelled = Event()
        request_time = []
        def request_cancel():
            request_time.append(perf_counter())
            cancelled.set()
        timer = Timer(.02, request_cancel)
        timer.start()
        cancel_start = perf_counter()
        try:
            app.search_page(workspace, QUERY, limit=100, cursor=current.next_cursor,
                            cancelled=cancelled.is_set)
        except TimeoutError:
            cancel_end = perf_counter()
            require(bool(request_time), 'Search timed out before the cancellation request.')
            require(app._selections == previous_selections, 'Cancellation withdrew completed selections.')
            verify_hit(app, workspace, selected)
            cancellation = {'status': 'CANCELLED', 'requested_after_seconds': request_time[0] - cancel_start,
                            'response_seconds': cancel_end - request_time[0],
                            'operation_seconds': cancel_end - cancel_start,
                            'previous_selection_preserved': True, 'recovery_source_copy_verified': True}
        else:
            cancellation = {'status': 'COMPLETED_BEFORE_CANCEL',
                            'operation_seconds': perf_counter() - cancel_start,
                            'response_seconds': None}
        finally:
            timer.cancel()
            timer.join()
        recovery = app.search_page(workspace, QUERY, limit=100)
        require([row.edition_id for row in recovery.results] == identities[:100],
                'Search did not recover after cancellation.')
        try:
            app.copy(other, recovery.results[0].ref, recovery.results[0].revision, 'quote')
        except ValueError:
            pass
        else:
            raise ValueError('Cross-workspace source selection was accepted.')
        require(not app.search_page(other, QUERY).results, 'Foreign workspace leaked a result.')
        restarts = []
        restart_config = root / 'restart-config.json'
        write_json(restart_config, {'root': str(app.root), 'workspace': workspace,
            'cursor': first_cursor, 'expected': identities[100:200] if first_cursor else identities[:100],
            'old_ref': recovery.results[0].ref, 'old_revision': recovery.results[0].revision})
        for _ in range(min(samples, 5)):
            began = perf_counter()
            child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                    '--restart', str(restart_config)], check=True,
                                   capture_output=True, text=True, timeout=180)
            result = json.loads(child.stdout)
            result['fresh_process_wall_seconds'] = perf_counter() - began
            require(result['status'] == 'PASS', 'Restart probe did not pass.')
            restarts.append(result)
        restart_config.unlink()
        # Small actual TXT imports are deliberately separate from direct setup.
        importer = LibraryApp(root / 'import-library')
        import_workspace = importer.create_workspace('Actual synthetic TXT import sample')
        inputs = root / 'inputs'
        inputs.mkdir()
        import_times, imported_bytes = [], 0
        for number in range(samples):
            path = inputs / f'source-{number:05d}.txt'
            raw = (f'Actual import sample {number}. ' + TEXT).encode('utf-8')
            path.write_bytes(raw)
            result = timed(import_times, lambda: importer.import_file(import_workspace, path,
                title=f'Imported synthetic source {number}', language='uk'))
            require(result['state'] == 'complete' and result['verified'] is True,
                    'Actual TXT import did not finish with source verification.')
            require(importer.assets.resolve(result['asset']).read_bytes() == raw,
                    'Actual importer did not retain exact input bytes.')
            imported_bytes += len(raw)
        require(len(importer.browse(import_workspace, limit=500)) == samples,
                'Actual import sample count mismatch.')
        require(source_identity()['sha256'] == expected_source, 'Source changed during measurement.')
    return {'status': 'PASS', 'requested_synthetic_editions': requested,
            'source_sha256': expected_source, 'elapsed_seconds': perf_counter() - started,
            'baseline_peak_rss_bytes': baseline, 'process_peak_rss_bytes': peak_rss_bytes(),
            'fixture': {'unique_originals_in_measured_workspace': 1, 'original_bytes': len(TEXT.encode('utf-8')),
                        'source_sha256': digest, 'segments_per_edition': 1, 'stored_counts': counts,
                        'isolation_control_editions': 1, 'post_construction_disk': post_build_disk},
            'direct_catalogue_construction': {'elapsed_seconds': build_elapsed,
                'editions_per_second': requested / build_elapsed, 'batch_size': BATCH,
                'batch_latency': statistics(construction), 'is_actual_import_throughput': False},
            'fresh_empty_application': statistics(startup),
            'catalogue_pages': {'latency': statistics(browse_times), 'page_limit': 500,
                                'editions_checked': len(browsed), 'all_ids_exact': True},
            'search_first_page': statistics(first_times),
            'search_traversal': {'latency': statistics(search_times), 'page_limit': 100,
                'pages': pages, 'results_verified': seen, 'elapsed_seconds': traversal_elapsed,
                'all_pages_completed': cursor is None, 'configured_page_cap': max_search_pages,
                'all_observed_ids_exact': True},
            'open_context': statistics(open_times), 'copy_exact_quote': statistics(copy_times),
            'cancellation': cancellation, 'isolation': {'foreign_query_empty': True,
                'cross_workspace_selection_rejected': True},
            'restart': {'cases': restarts,
                'application_startup': statistics([row['application_startup_seconds'] for row in restarts]),
                'fresh_process_wall': statistics([row['fresh_process_wall_seconds'] for row in restarts]),
                'cursor_and_original_verified': True},
            'actual_small_txt_imports': {'count': samples, 'source_bytes': imported_bytes,
                'latency': statistics(import_times), 'files_per_second': samples / sum(import_times),
                'all_original_bytes_verified': True,
                'scope': 'Small unique synthetic TXT files in a separate empty library; no large-book throughput claim'},
            'disk': disk.report()}


def environment(output):
    cpu = platform.processor()
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.exists():
        cpu = next((line.split(':', 1)[1].strip() for line in cpuinfo.read_text().splitlines()
                    if line.startswith('model name')), cpu)
    limits = {}
    for name in ('cpu.max', 'memory.max'):
        path = Path('/sys/fs/cgroup') / name
        if path.is_file():
            limits[name] = path.read_text().strip()
    filesystem = 'not_detected'
    if Path('/proc/mounts').is_file():
        candidates = []
        for line in Path('/proc/mounts').read_text().splitlines():
            fields = line.split()
            mount = Path(fields[1])
            if output == mount or mount in output.parents:
                candidates.append((len(str(mount)), fields[2]))
        if candidates:
            filesystem = max(candidates)[1]
    return {'platform': platform.platform(), 'python': sys.version,
            'sqlite': sqlite3.sqlite_version, 'cpu_model': cpu,
            'logical_cpus': os.cpu_count(),
            'affinity_cpus': len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'cgroup_limits': limits, 'fixture_filesystem': filesystem,
            'free_fixture_filesystem_bytes_at_start': shutil.disk_usage(output).free,
            'peak_rss_method': rss_method(),
            'exclusive_host': False, 'gpu_or_vram': 'NOT_USED_OR_MEASURED'}


def main():
    require(__debug__, 'Run without Python optimization flags.')
    if len(sys.argv) > 1 and sys.argv[1] == '--restart':
        print(json.dumps(restart_probe(json.loads(Path(sys.argv[2]).read_text()))))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--case':
        directory, requested, samples, cap, expected = sys.argv[2:]
        print(json.dumps(run_case(Path(directory), int(requested), int(samples), int(cap), expected)))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000, 50000])
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--max-search-pages', type=int, default=0,
                        help='Zero traverses all result pages; positive caps are disclosed as partial search evidence.')
    parser.add_argument('--case-timeout', type=int, default=1800)
    parser.add_argument('--keep-fixtures', action='store_true')
    args = parser.parse_args()
    require(1 <= len(args.sizes) <= 10 and len(args.sizes) == len(set(args.sizes))
            and all(1 <= value <= 50000 for value in args.sizes), 'Choose distinct sizes in 1..50000.')
    require(1 <= args.samples <= 100, 'Samples must be in 1..100.')
    require(0 <= args.max_search_pages <= 10000, 'Search page cap must be in 0..10000.')
    require(1 <= args.case_timeout <= 14400, 'Case timeout must be in 1..14400 seconds.')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    identity = source_identity()
    write_json(output / 'source-manifest.json', identity)
    source_commit = subprocess.run(['git', '-C', str(PROJECT), 'rev-parse', 'HEAD'],
                                   check=True, capture_output=True, text=True).stdout.strip()
    result = {'schema': 1, 'status': 'RUNNING', 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'checkout_commit': source_commit, 'source_manifest_sha256': identity['sha256'],
              'harness_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
              'environment': environment(output), 'configuration': {
                  'sizes': args.sizes, 'samples': args.samples, 'max_search_pages': args.max_search_pages,
                  'case_timeout_seconds': args.case_timeout, 'keep_fixtures': args.keep_fixtures},
              'method': {
                  'reuse': ['scripts/benchmark_semantic_cache.py: RSS measurement',
                           'tests/search/test_search_pagination.py: publish_text_corpus',
                           'LibraryApp.browse/search_page/context/copy/import_file; current production implementations'],
                  'construction': 'Existing registration/revision fixture, 1000 editions per transaction; not actual file intake/import',
                  'corpus': f'Distinct synthetic edition/work/revision identities; one shared {len(TEXT.encode("utf-8"))}-byte UTF-8 original and one segment per edition',
                  'temperature': 'Warm OS page cache; fresh SQLite connections/processes do not make a cold storage benchmark',
                  'memory': 'Whole child-process peak RSS including corpus setup and checks; fresh-process restart peaks are separate',
                  'disk': 'File bytes and sampled 50ms peak including journals; on tmpfs these consume host memory',
                  'statistics': 'Nearest-rank p50/p95; raw duration samples retained; no extrapolated performance targets',
                  'load': 'Shared development host, concurrent verification can influence latency; no exclusivity claim'},
              'claims': {'real_corpus': 'NOT_RUN', 'large_book_import_throughput': 'NOT_MEASURED',
                         'millions_of_segments': 'NOT_RUN', 'neural_semantic_search': 'NOT_RUN',
                         'windows_native_or_nvda': 'NOT_RUN', 'release_capacity_qualification': 'NOT_ESTABLISHED'},
              'cases': []}
    began = perf_counter()
    for size in args.sizes:
        fixture = output / f'fixture-{size}'
        child = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--case',
                                str(fixture), str(size), str(args.samples), str(args.max_search_pages), identity['sha256']],
                               capture_output=True, text=True, timeout=args.case_timeout,
                               env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        if child.returncode:
            (output / f'failure-{size}.txt').write_text(child.stderr, encoding='utf-8')
            raise ValueError(f'Case {size} failed; inspect its failure log. Completed case JSON remains available.')
        case = json.loads(child.stdout)
        require(case['status'] == 'PASS' and case['source_sha256'] == identity['sha256'],
                'Child did not return a matching successful measurement.')
        write_json(output / f'case-{size}.json', case)
        result['cases'].append(case)
        if not args.keep_fixtures:
            shutil.rmtree(fixture)
        print(json.dumps({'completed_synthetic_editions': size, 'elapsed_seconds': case['elapsed_seconds'],
                          'search_pages': case['search_traversal']['pages'],
                          'all_search_pages_completed': case['search_traversal']['all_pages_completed'],
                          'fixture_removed': not args.keep_fixtures}), flush=True)
    require(source_identity() == identity, 'Source changed during the complete benchmark.')
    result.update(status='PASS', elapsed_seconds=perf_counter() - began,
                  source_matches_worktree_after_run=True)
    write_json(output / 'library-capacity.json', result)
    print(json.dumps({'evidence': str(output / 'library-capacity.json'), 'status': 'PASS'}), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
