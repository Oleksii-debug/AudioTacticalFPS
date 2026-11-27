"""Measure a declared synthetic corpus through the actual LibraryApp APIs.

Example (RAM-backed storage; not a physical-disk throughput claim):
    python scripts/benchmark_library_capacity.py --work-parent /dev/shm \
        --editions 1000 --queries 64 --report /new/capacity.json

Imports, source verification, publication and cursor guards are unchanged.
The private synthetic corpus is removed on exit. Only the small report remains.
Run from a committed clean source checkout without Python optimization flags.
"""
from argparse import ArgumentParser
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

from benchmark_semantic_cache import peak_rss_bytes, rss_method


ROOT = Path(__file__).resolve().parents[1]


def source_identity():
    def git(*arguments):
        return subprocess.check_output(['git', *arguments], cwd=ROOT, text=True).strip()
    if git('status', '--porcelain', '--untracked-files=normal', '--', 'src'):
        raise ValueError('Commit the production source before measuring it.')
    digest = sha256()
    for path in sorted((ROOT / 'src').rglob('*.py')):
        relative = path.relative_to(ROOT).as_posix().encode('utf-8')
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(4, 'big') + relative)
        digest.update(len(raw).to_bytes(8, 'big') + raw)
    return {'commit': git('rev-parse', 'HEAD'), 'production_python_sha256': digest.hexdigest(),
            'script_sha256': sha256(Path(__file__).read_bytes()).hexdigest()}


def distribution(seconds):
    values = sorted(value * 1000 for value in seconds)
    if not values:
        raise ValueError('A measured distribution needs actual observations.')
    def percentile(fraction):
        return values[max(0, math.ceil(len(values) * fraction) - 1)]
    return {'samples': len(values), 'unit': 'milliseconds',
            'p50': percentile(.5), 'p95': percentile(.95),
            'minimum': values[0], 'maximum': values[-1],
            'mean': sum(values) / len(values), 'total_seconds': sum(seconds),
            'percentile_method': 'nearest rank; ceil(n*p), one-based'}


def hardware(parent):
    cpu_model, filesystem = platform.processor() or 'not reported', 'not reported'
    if sys.platform.startswith('linux'):
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith('model name'):
                cpu_model = line.split(':', 1)[1].strip()
                break
        mounts = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            before, after = line.split(' - ', 1)
            mount = Path(before.split()[4].replace('\\040', ' '))
            if parent.is_relative_to(mount):
                mounts.append((len(str(mount)), after.split()[0]))
        if mounts:
            filesystem = max(mounts)[1]
    summary = {'platform': platform.platform(), 'architecture': platform.machine(),
               'cpu_model': cpu_model, 'logical_cpus': os.cpu_count(),
               'filesystem': filesystem, 'python': platform.python_version(),
               'sqlite': sqlite3.sqlite_version}
    if hasattr(os, 'sched_getaffinity'):
        summary['available_cpu_affinity_count'] = len(os.sched_getaffinity(0))
    if hasattr(os, 'sysconf'):
        summary['reported_host_memory_bytes'] = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    for name in ('memory.max', 'cpu.max'):
        path = Path('/sys/fs/cgroup') / name
        if path.is_file():
            summary['cgroup_' + name.replace('.', '_')] = path.read_text().strip()
    return summary


def fixture_text(number):
    return (f'Synthetic academic fixture {number:06d}.\n'
            f'capacitytoken{number:06d} preserves exact original bytes.\n'
            'Common capacity passage for complete paged retrieval.\n'
            'Контрольний український рядок; no private documents or model output.\n')


def footprint(root):
    logical = allocated = count = 0
    categories = {}
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        info = path.stat()
        category = path.relative_to(root).parts[0]
        if len(path.relative_to(root).parts) == 1:
            category = 'catalog_database' if path.name == 'library.sqlite' else (
                'import_database' if path.name == 'imports.sqlite' else 'other_root_metadata')
        categories[category] = categories.get(category, 0) + info.st_size
        logical += info.st_size
        allocated += getattr(info, 'st_blocks', 0) * 512
        count += 1
    return {'regular_files': count, 'logical_bytes': logical,
            'allocated_bytes_if_reported': allocated or None, 'logical_bytes_by_category': categories}


def run_case(directory, *, editions, queries, timeout):
    sys.path.insert(0, str(ROOT / 'src'))
    from carr_library.application import LibraryApp

    started = perf_counter()
    deadline = started + timeout
    def stopped():
        return perf_counter() >= deadline
    def check():
        if stopped():
            raise TimeoutError('The declared benchmark time budget expired.')
    def measure(values, operation):
        check()
        before = perf_counter()
        result = operation()
        values.append(perf_counter() - before)
        check()
        return result

    baseline_rss = peak_rss_bytes()
    library = directory / 'library'
    sources = directory / 'synthetic-inputs'
    sources.mkdir()
    app = LibraryApp(library)
    workspace = app.create_workspace('Synthetic capacity corpus')
    collection = app.create_collection(workspace, 'Synthetic even-numbered sources')
    imported, import_times, total_source_bytes = [], [], 0
    corpus_digest = sha256()
    for number in range(editions):
        check()
        source = sources / f'source-{number:06d}.txt'
        raw = fixture_text(number).encode('utf-8')
        source.write_bytes(raw)
        total_source_bytes += len(raw)
        corpus_digest.update(len(raw).to_bytes(8, 'big') + raw)
        result = measure(import_times, lambda: app.import_file(workspace, source,
            title=f'Synthetic source {number:06d}', authors=(f'Synthetic Author {number % 10}',),
            language='en' if number % 2 == 0 else 'uk', edition=f'fixture-{number % 3}',
            work_id=f'capacity-work-{number:06d}', edition_id=f'capacity-edition-{number:06d}',
            collections=(collection,) if number % 2 == 0 else (), cancelled=stopped))
        assert result['state'] == 'complete' and result['asset'] == sha256(raw).hexdigest(), result
        imported.append(result)
        if (number + 1) % 100 == 0 or number + 1 == editions:
            print(json.dumps({'phase': 'import', 'completed': number + 1, 'total': editions,
                              'elapsed_seconds': perf_counter() - started}), flush=True)

    # Constructor checks real persisted schema/integrity; OS caches are not reset.
    reopen_times = []
    for _ in range(12):
        app = measure(reopen_times, lambda: LibraryApp(library))

    browse_pages, browse_complete = [], []
    for _ in range(24):
        began, after, found = perf_counter(), None, []
        while True:
            rows = measure(browse_pages, lambda: app.browse(workspace, after=after, limit=500))
            found.extend(row.edition_id for row in rows)
            if len(rows) < 500:
                break
            after = rows[-1].edition_id
        assert found == [f'capacity-edition-{number:06d}' for number in range(editions)]
        browse_complete.append(perf_counter() - began)

    search_pages, search_complete, contexts, copies, originals = [], [], [], [], []
    page_counts = []
    for sample in range(queries):
        number = sample * (editions - 1) // (queries - 1)
        query = f'capacitytoken{number:06d}'
        cursor, found, count = None, [], 0
        began = perf_counter()
        while True:
            page = measure(search_pages, lambda: app.search_page(workspace, query,
                cursor=cursor, edition_limit=500, limit=100, timeout=60, cancelled=stopped))
            count += 1
            found.extend(page.results)
            cursor = page.next_cursor
            if cursor is None:
                break
            assert count <= math.ceil(editions / 500) + 1, 'Non-advancing search cursor'
        search_complete.append(perf_counter() - began)
        page_counts.append(count)
        assert len(found) == 1 and found[0].edition_id == f'capacity-edition-{number:06d}'
        hit = found[0]
        assert hit.verified and hit.quote == query
        context = measure(contexts, lambda: app.context(workspace, hit.ref, hit.revision, cancelled=stopped))
        assert context.pages[0].raw_text == fixture_text(number) and context.pages[0].quote == query
        assert measure(copies, lambda: app.copy(workspace, hit.ref, hit.revision, 'quote', cancelled=stopped)) == query
        original = measure(originals, lambda: app.prepare_edition_file(workspace, hit.edition_id, cancelled=stopped))
        assert original.path.read_bytes() == fixture_text(number).encode('utf-8')
        if (sample + 1) % 16 == 0 or sample + 1 == queries:
            print(json.dumps({'phase': 'search-and-open', 'completed': sample + 1, 'total': queries}), flush=True)

    # Reach every source and every result page through the public cursor API.
    cursor, found, full_pages = None, [], 0
    full_started = perf_counter()
    while True:
        check()
        page = app.search_page(workspace, 'Common capacity passage', cursor=cursor,
            edition_limit=500, limit=100, timeout=60, cancelled=stopped)
        found.extend(row.edition_id for row in page.results)
        assert all(row.verified for row in page.results)
        full_pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert full_pages <= math.ceil(editions / 100) + math.ceil(editions / 500) + 1
    full_seconds = perf_counter() - full_started
    assert len(found) == len(set(found)) == editions
    cancel_hit = page.results[-1]

    # Cancel during a real catalog traversal, retaining an earlier valid handle.
    calls, cancel_requested = 0, None
    def cancelled_search():
        nonlocal calls, cancel_requested
        calls += 1
        if calls >= 64 and cancel_requested is None:
            cancel_requested = perf_counter()
        return cancel_requested is not None or stopped()
    before = perf_counter()
    try:
        app.search_page(workspace, 'Common capacity passage', cancelled=cancelled_search)
    except (TimeoutError, InterruptedError) as exc:
        cancellation_error = type(exc).__name__
    else:
        raise AssertionError('The cancelled search unexpectedly completed.')
    stopped_at = perf_counter()
    assert cancel_requested is not None
    assert app.copy(workspace, cancel_hit.ref, cancel_hit.revision, 'quote') == cancel_hit.quote
    search_cancel = {'outcome': cancellation_error, 'callback_count': calls,
        'operation_seconds': stopped_at - before, 'seconds_after_cancel_requested': stopped_at - cancel_requested,
        'previous_exact_quote_still_valid': True}

    recovery_workspace = app.create_workspace('Synthetic interrupted-import sample')
    recovery_source = sources / 'recovery.txt'
    recovery_raw = 'First checkpoint.\fSecond recovery checkpoint.'.encode('utf-8')
    recovery_source.write_bytes(recovery_raw)
    import_cancel_at = None
    def cancel_after_checkpoint():
        nonlocal import_cancel_at
        check()
        with closing(sqlite3.connect(library / 'imports.sqlite')) as db:
            count = db.execute('SELECT count(*) FROM checkpoint c JOIN job j ON j.id=c.job '
                              'WHERE j.edition=?', ('capacity-recovery-edition',)).fetchone()[0]
        if count and import_cancel_at is None:
            import_cancel_at = perf_counter()
        return bool(count)
    before = perf_counter()
    paused = app.import_file(recovery_workspace, recovery_source, title='Synthetic recovery sample',
        work_id='capacity-recovery-work', edition_id='capacity-recovery-edition', cancelled=cancel_after_checkpoint)
    paused_at = perf_counter()
    assert paused['state'] == 'cancelled' and paused['completed'] == 1 and import_cancel_at is not None
    def checkpoints():
        with closing(sqlite3.connect(library / 'imports.sqlite')) as db:
            return tuple(tuple(row) for row in db.execute('SELECT c.job,c.ordinal,c.digest '
                'FROM checkpoint c JOIN job j ON j.id=c.job WHERE j.edition=? ORDER BY c.ordinal',
                ('capacity-recovery-edition',)))
    retained_checkpoint = checkpoints()
    assert len(retained_checkpoint) == 1
    recovery_source.unlink()  # The normal resume must use its retained original.
    app = LibraryApp(library)
    resume_started = perf_counter()
    resumed = app.resume_import(recovery_workspace, paused['request_id'], cancelled=stopped, timeout=120)
    resume_seconds = perf_counter() - resume_started
    assert resumed['state'] == 'complete' and resumed['id'] == paused['id']
    assert len(checkpoints()) == 2 and checkpoints()[:1] == retained_checkpoint
    recovered, = app.search(recovery_workspace, 'Second recovery checkpoint.')
    assert app.copy(recovery_workspace, recovered.ref, recovered.revision, 'quote') == 'Second recovery checkpoint.'
    assert app.assets.resolve(resumed['asset']).read_bytes() == recovery_raw
    recovery = {'cancelled_state': paused['state'], 'completed_checkpoints_before_stop': 1,
        'operation_before_stop_seconds': paused_at - before,
        'seconds_after_cancel_requested': paused_at - import_cancel_at,
        'resume_seconds': resume_seconds, 'resumed_same_job': True,
        'retained_checkpoint_digest_unchanged': True, 'external_input_removed_before_resume': True,
        'exact_retained_source_and_quote_verified': True, 'auxiliary_editions': 1,
        'auxiliary_source_bytes': len(recovery_raw)}

    for number, record in enumerate(imported):
        check()
        assert app.assets.resolve(record['asset']).read_bytes() == fixture_text(number).encode('utf-8')
    with app.read(workspace) as db:
        stored = db.execute('SELECT count(*) FROM catalog_edition WHERE workspace=?', (workspace,)).fetchone()[0]
        schema = db.execute('PRAGMA user_version').fetchone()[0]
    assert stored == editions
    return {'status': 'PASS', 'elapsed_seconds': perf_counter() - started,
        'corpus': {'editions': stored, 'primary_workspace_count': 1, 'source_bytes': total_source_bytes,
            'ordered_length_prefixed_utf8_sha256': corpus_digest.hexdigest(), 'format': 'UTF-8 TXT',
            'content': 'Deterministic short synthetic English/Ukrainian documents; one logical segment each',
            'all_retained_originals_rechecked': True},
        'measurements': {'individual_import': distribution(import_times),
            'library_reopen': distribution(reopen_times), 'catalog_page_500': distribution(browse_pages),
            'complete_catalog_browse': distribution(browse_complete),
            'search_page': distribution(search_pages), 'complete_unique_query': distribution(search_complete),
            'verified_context_open': distribution(contexts), 'fresh_verified_quote_copy': distribution(copies),
            'prepare_exact_original_view': distribution(originals)},
        'search': {'queries': queries, 'mode': 'phrase', 'edition_limit_per_page': 500,
            'result_limit_per_page': 100, 'minimum_pages_per_query': min(page_counts),
            'maximum_pages_per_query': max(page_counts), 'complete_shared_query_pages': full_pages,
            'complete_shared_query_hits': len(found), 'complete_shared_query_seconds': full_seconds,
            'global_relevance_ranking': False, 'simultaneous_cross_page_snapshot': False},
        'search_cancellation': search_cancel, 'import_cancellation_and_recovery': recovery,
        'memory': {'baseline_parent_peak_rss_bytes': baseline_rss,
            'parent_peak_rss_bytes': peak_rss_bytes(), 'method': rss_method(),
            'scope': 'Whole parent process high-water mark; excludes separate resume worker RSS'},
        'library_footprint_after_recovery': footprint(library), 'schema_version': schema,
        'limits': ['Small synthetic TXT corpus only; no PDF/OCR, neural model, ASR or audio load.',
            'Warm filesystem caches; reopening LibraryApp does not flush OS caches.',
            'Original opening prepares a verified view copy; no external viewer was launched.',
            'No physical-disk throughput claim when the selected work filesystem is tmpfs.',
            'No multi-gigabyte, 50,000-edition or concurrent-writer capacity claim.']}


def main():
    if not __debug__:
        raise ValueError('Run without Python optimization flags; invariants must execute.')
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--work-parent', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--editions', type=int, default=1000)
    parser.add_argument('--queries', type=int, default=64)
    parser.add_argument('--timeout', type=float, default=1800)
    args = parser.parse_args()
    if not 10 <= args.editions <= 5000 or not 16 <= args.queries <= 256 or not 60 <= args.timeout <= 3600:
        parser.error('Choose 10–5000 editions, 16–256 queries and 60–3600 seconds.')
    parent = args.work_parent.resolve(strict=True)
    report = args.report.absolute()
    if not parent.is_dir() or report.exists() or not report.parent.is_dir():
        parser.error('Use an existing work parent and a new report in an existing directory.')
    source = source_identity()
    sys.dont_write_bytecode = True
    def offline(*args, **kwargs):
        raise OSError('The synthetic capacity benchmark does not permit network access.')
    socket.socket.connect = socket.socket.connect_ex = offline
    socket.create_connection = socket.getaddrinfo = offline
    with TemporaryDirectory(prefix='carr-capacity-', dir=parent) as temporary:
        result = run_case(Path(temporary), editions=args.editions, queries=args.queries, timeout=args.timeout)
        assert source_identity() == source, 'The measured source changed during execution.'
    result.update(report_version=1, measured_at_utc=datetime.now(timezone.utc).isoformat(),
        source=source, environment=hardware(parent),
        declared_budget={'editions': args.editions, 'queries': args.queries, 'seconds': args.timeout},
        synthetic_corpus_removed_after_measurement=True)
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    with report.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({'status': 'PASS', 'editions': args.editions, 'report': str(report),
                      'report_sha256': sha256(raw).hexdigest(), 'source_commit': source['commit']}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
