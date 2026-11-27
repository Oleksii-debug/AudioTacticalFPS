"""Measure real retained TXT books through the existing library and search APIs.

This is an explicit offline measurement tool, not a capacity or release verdict.
Each requested size gets a fresh library, canonical imports, one complete result
traversal, and an independently started restart probe. Nothing is auto-pruned.

    python -B scripts/benchmark_library_scale.py --output-dir /new/measurements \
        --sizes 1000 10000 50000 --segments-per-book 20 --segment-bytes 2048

Use --installed with the selected installed interpreter. No model, microphone,
provider or network service is used. Large runs are deliberately opt-in commands.
"""
from array import array
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from time import monotonic, perf_counter

SCHEMA = 'carr-library-corpus-benchmark/1'
QUERY = 'CARRCorpusNeedle'
MAX_BOOKS = 50_000
MAX_SEGMENTS = 2_000_000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_JSON = 8 * 1024 * 1024
MODULES = (
    'bootstrap', 'carr_library.application', 'carr_library.search.paging',
    'carr_library.reader.reading_desk', 'carr_library.catalog.read_views',
    'carr_library.storage.revisions', 'carr_library.durable_state',
)
METHOD = {
    'corpus': 'Deterministic UTF-8 TXT books imported through LibraryApp.add_text; no direct SQL population.',
    'scope': 'One selected workspace plus a deliberately excluded workspace containing the same query.',
    'search': 'Lexical terms; exact full result/source continuation; ranking remains local to each source window.',
    'percentiles': 'Nearest-rank empirical P50/P95, with actual sample count; no acceptance latency threshold.',
    'restart': 'Build, traversal and restart run in distinct owned interpreter processes.',
    'cache_temperature': 'Fresh interpreter is not cold disk: OS page cache is retained.',
    'memory': 'Whole-process high-water RSS via the unchanged semantic-cache measurement kernel.',
    'disk_budget': 'Cooperative measured checkpoint after every 64 imports and at phase boundaries; not a filesystem quota.',
    'timeout': 'Cooperative phase deadline plus a direct-interpreter outer timeout; remaining phases stop on failure.',
    'source_checks': 'All retained source hashes and catalog identities before/after measurements; quote actions sampled per result page.',
}
CLAIMS = {
    'release_capacity': 'NOT_ESTABLISHED_BY_THIS_HARNESS',
    'neural_retrieval_quality': 'NOT_MEASURED',
    'real_world_document_diversity': 'NOT_MEASURED',
    'pdf_ocr_audio': 'NOT_RUN',
    'native_gui_nvda': 'NOT_RUN',
    'power_loss_recovery': 'NOT_RUN',
    'cold_disk_latency': 'NOT_MEASURED',
}


class BenchmarkFailure(ValueError):
    pass


class BudgetExceeded(TimeoutError):
    pass


def require(value, message):
    if not value:
        raise BenchmarkFailure(message)


def _identity(info):
    # Windows executable path/fd permission bits can differ. Type and stable
    # object identity are compared; regular/reparse/link guards are separate.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, stat.S_IFMT(info.st_mode), info.st_nlink,
            getattr(info, 'st_file_attributes', 0) & 0x400)


def plain(path, *, directory=False):
    value = Path(path).lstat()
    require(not stat.S_ISLNK(value.st_mode)
            and not getattr(value, 'st_file_attributes', 0) & 0x400
            and (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))
            and (directory or value.st_nlink == 1),
            'Benchmark paths must be ordinary local files/directories without links.')
    return value


def local(value, *, exists=False):
    raw = os.fspath(value)
    require('\x00' not in raw and not raw.startswith(('//', '\\\\')),
            'Choose a local benchmark path.')
    path = Path(raw).absolute()
    require('..' not in path.parts, 'Parent traversal is not a benchmark path.')
    for parent in reversed((path, *path.parents)):
        if os.path.lexists(parent):
            plain(parent, directory=parent != path or parent.is_dir())
    return path.resolve(strict=exists)


def file_hash(path, *, maximum=32 * 1024 * 1024, check=lambda: None):
    path = local(path, exists=True)
    before = plain(path)
    require(before.st_size <= maximum, 'File exceeds its benchmark verification bound.')
    digest = sha256()
    with path.open('rb') as stream:
        require(_identity(os.fstat(stream.fileno())) == _identity(before),
                'File changed before benchmark verification.')
        remaining = before.st_size
        while remaining:
            check()
            block = stream.read(min(1024 * 1024, remaining))
            require(bool(block), 'File shortened during benchmark verification.')
            digest.update(block)
            remaining -= len(block)
        require(stream.read(1) == b'', 'File grew during benchmark verification.')
        require(_identity(os.fstat(stream.fileno())) == _identity(before),
                'File changed during benchmark verification.')
    require(_identity(plain(path)) == _identity(before), 'File identity changed.')
    check()
    return digest.hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate benchmark JSON key.')
        result[key] = value
    return result


def read_json(path, *, expected_sha256=None):
    path = local(path, exists=True)
    before = plain(path)
    require(before.st_size <= MAX_JSON, 'Benchmark JSON exceeds its bound.')
    with path.open('rb') as stream:
        require(_identity(os.fstat(stream.fileno())) == _identity(before), 'JSON identity changed.')
        raw = stream.read(before.st_size + 1)
        require(len(raw) == before.st_size
                and _identity(os.fstat(stream.fileno())) == _identity(before), 'JSON bytes changed.')
    require(_identity(plain(path)) == _identity(before), 'JSON path changed.')
    if expected_sha256 is not None:
        require(type(expected_sha256) is str and re.fullmatch('[0-9a-f]{64}', expected_sha256)
                and sha256(raw).hexdigest() == expected_sha256,
                'Decoded benchmark JSON does not match its expected digest.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          BenchmarkFailure('Non-finite benchmark JSON value.')))


def save_json(path, value):
    from carr_library.durable_state import atomic_json
    atomic_json(path, value)  # Existing create-only writer; no overwrite/recovery claim.


def activate(source_root=None):
    if source_root is not None:
        selected = local(source_root, exists=True)
        plain(selected, directory=True)
        expected = selected / 'carr_library' / 'application.py'
        plain(expected)
        # Production retains the reviewed bootstrap package beside src. The
        # isolated interpreter does not inherit the caller's checkout path.
        bootstrap_root = local(selected.parent / 'bootstrap', exists=True)
        plain(bootstrap_root, directory=True)
        plain(bootstrap_root / '__init__.py')
        sys.path.insert(0, str(selected.parent))
        sys.path.insert(0, str(selected))
    origins = {}
    for name in MODULES:
        module = importlib.import_module(name)
        path = local(module.__file__, exists=True)
        require(path.suffix == '.py', 'Benchmark requires inspectable production source.')
        if source_root is not None:
            package_root = bootstrap_root if name == 'bootstrap' else selected / 'carr_library'
            require(path.is_relative_to(package_root),
                    'An already imported package differs from the selected source root.')
        origins[name] = {'path': str(path), 'sha256': file_hash(path)}
    return origins


def check_origins(expected):
    actual = activate()
    require(actual == expected, 'Production module origin or bytes changed.')
    return actual


def memory_kernel():
    path = local(Path(__file__).with_name('benchmark_semantic_cache.py'), exists=True)
    spec = importlib.util.spec_from_file_location('_carr_scale_memory_kernel', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, {'path': str(path), 'sha256': file_hash(path)}


def validate_case(spec):
    require(type(spec) is dict and set(spec) == {
        'books', 'segments_per_book', 'segment_bytes', 'result_page_size',
        'repeats', 'phase_timeout', 'max_disk_bytes', 'min_free_bytes'},
        'Invalid benchmark case fields.')
    bounds = {'books': (1, MAX_BOOKS), 'segments_per_book': (1, 1000),
              'segment_bytes': (128, 65536), 'result_page_size': (1, 100),
              'repeats': (1, 1000), 'max_disk_bytes': (1, 1024 ** 4),
              'min_free_bytes': (0, 1024 ** 4)}
    for name, (minimum, maximum) in bounds.items():
        require(type(spec[name]) is int and minimum <= spec[name] <= maximum,
                'Invalid benchmark limit: ' + name)
    require(type(spec['phase_timeout']) in (int, float)
            and math.isfinite(spec['phase_timeout']) and 0 < spec['phase_timeout'] <= 86400,
            'Invalid phase timeout.')
    require(spec['books'] * spec['segments_per_book'] <= MAX_SEGMENTS,
            'Choose at most two million actual segments per case.')
    require(spec['segments_per_book'] * (spec['segment_bytes'] + 1) <= MAX_SOURCE_BYTES,
            'One fixture book must stay within the inline TXT bound.')
    return dict(spec)


def edition_id(number):
    return f'bench-edition-{number:08d}'


def title(number):
    return f'Corpus book {number:08d}'


def segment(number, ordinal, size):
    prefix = f'{QUERY} book {number:08d} segment {ordinal:04d}. Україна Ω.\n'
    remaining = size - len(prefix.encode('utf-8'))
    require(remaining >= 0, 'Segment budget is too small.')
    fill = ' retained source measurement.'
    return prefix + (fill * (remaining // len(fill) + 1))[:remaining]


def source_text(number, spec):
    return '\f'.join(segment(number, n, spec['segment_bytes'])
                     for n in range(1, spec['segments_per_book'] + 1))


def samples(values):
    if not values:
        return {'count': 0, 'p50_seconds': None, 'p95_seconds': None, 'total_seconds': 0.0}
    ordered = sorted(values)
    return {'count': len(ordered), 'p50_seconds': ordered[math.ceil(len(ordered) * .50) - 1],
            'p95_seconds': ordered[math.ceil(len(ordered) * .95) - 1],
            'total_seconds': math.fsum(ordered), 'minimum_seconds': ordered[0],
            'maximum_seconds': ordered[-1]}


def footprint(root, *, check=lambda: None):
    total = files = directories = 0
    stack = [local(root, exists=True)]
    while stack:
        check()
        directory = stack.pop()
        before = plain(directory, directory=True)
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                path = Path(entry.path)
                info = entry.stat(follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode)
                        and not getattr(info, 'st_file_attributes', 0) & 0x400,
                        'Unexpected linked path in benchmark corpus.')
                if stat.S_ISDIR(info.st_mode):
                    directories += 1
                    stack.append(path)
                else:
                    info = plain(path)
                    files += 1
                    total += info.st_size
                require(files + directories <= 2_000_000, 'Benchmark inventory bound exceeded.')
        after = plain(directory, directory=True)
        require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
                'Benchmark directory identity changed.')
    return {'bytes': total, 'files': files, 'directories': directories}


class Budget:
    def __init__(self, root, spec, *, cancelled=lambda: False):
        self.root, self.spec, self.cancelled = local(root, exists=True), spec, cancelled
        self.root_id = self.directory_id()
        self.deadline = monotonic() + spec['phase_timeout']
        self.last_disk = None
        self._next_free = 0.0

    def directory_id(self):
        value = plain(self.root, directory=True)
        return value.st_dev, value.st_ino

    def check(self):
        require(self.directory_id() == self.root_id, 'Benchmark root was replaced.')
        if self.cancelled():
            raise InterruptedError('Benchmark cancelled at a canonical checkpoint.')
        if monotonic() >= self.deadline:
            raise BudgetExceeded('Benchmark phase time budget exhausted.')
        if monotonic() >= self._next_free:
            if shutil.disk_usage(self.root).free < self.spec['min_free_bytes']:
                raise BudgetExceeded('Benchmark free-disk reserve reached.')
            self._next_free = monotonic() + .25

    def stopped(self):
        self.check()
        return False

    def disk(self):
        self.last_disk = footprint(self.root, check=self.check)
        if self.last_disk['bytes'] > self.spec['max_disk_bytes']:
            raise BudgetExceeded('Measured benchmark disk budget exhausted.')
        return self.last_disk


def verify_corpus(app, metadata, spec, budget):
    """Use bounded catalog reads and actual retained bytes, never old external paths."""
    count, after, total_bytes = 0, None, 0
    chain = sha256()
    while True:
        budget.check()
        views = app.browse(metadata['workspace'], after=after, limit=500)
        if not views:
            break
        for view in views:
            budget.check()
            require(count < spec['books'] and view.edition_id == edition_id(count)
                    and view.title == title(count), 'Catalog differs from requested book corpus.')
            expected = source_text(count, spec).encode('utf-8')
            digest = sha256(expected).hexdigest()
            require(view.asset == digest, 'Catalog original digest differs from deterministic corpus.')
            retained = app.assets.resolve(view.asset, cancelled=budget.stopped)
            require(file_hash(retained, maximum=MAX_SOURCE_BYTES, check=budget.check) == digest,
                    'Retained book bytes differ from expected original.')
            chain.update((view.edition_id + ':' + digest + '\n').encode('ascii'))
            total_bytes += len(expected)
            count += 1
        after = views[-1].edition_id
    require(count == spec['books'], 'Missing actual imported books.')
    with app.read(metadata['workspace']) as db:
        pages = db.execute('SELECT count(*) FROM active_page WHERE workspace=?',
                           (metadata['workspace'],)).fetchone()[0]
    require(pages == count * spec['segments_per_book'], 'Actual retained segment count differs.')
    require(len(app.browse(metadata['excluded_workspace'], limit=2)) == 1,
            'Isolation control edition is missing.')
    return {'books': count, 'segments': pages, 'source_utf8_bytes': total_bytes,
            'corpus_sha256': chain.hexdigest()}


def build_corpus(directory, spec, *, cancelled=lambda: False, progress=None):
    from carr_library.application import LibraryApp
    spec = validate_case(spec)
    directory = local(directory, exists=True)
    require(not os.path.lexists(directory / 'library'), 'Corpus library already exists.')
    budget = Budget(directory, spec, cancelled=cancelled)
    progress = progress if progress is not None else {}
    progress.update({'imported_books': 0, 'phase': 'build'})
    budget.check()
    started = perf_counter()
    app = LibraryApp(directory / 'library')
    startup = perf_counter() - started
    workspace = app.create_workspace('Measured selected corpus')
    excluded = app.create_workspace('Excluded source-isolation control')
    metadata = {'schema': SCHEMA, 'spec': spec, 'workspace': workspace,
                'excluded_workspace': excluded}
    times = array('d')
    chain = sha256()
    budget.disk()
    for number in range(spec['books']):
        budget.check()
        text = source_text(number, spec)
        expected = sha256(text.encode('utf-8')).hexdigest()
        began = perf_counter()
        result = app.add_text(workspace, title(number), text,
            edition_id=edition_id(number), work_id=f'bench-work-{number:08d}',
            cancelled=budget.stopped)
        times.append(perf_counter() - began)
        require(result.get('state') == 'complete' and result.get('edition') == edition_id(number)
                and result.get('asset') == expected, 'Canonical import did not return exact completion.')
        chain.update((edition_id(number) + ':' + expected + '\n').encode('ascii'))
        progress['imported_books'] = number + 1
        if number % 64 == 63:
            budget.disk()
    result = app.add_text(excluded, 'Excluded corpus control', QUERY + ' outside selected workspace.',
                          edition_id='excluded-control', work_id='excluded-work',
                          cancelled=budget.stopped)
    require(result.get('state') == 'complete', 'Isolation fixture import did not complete.')
    metadata['corpus_sha256'] = chain.hexdigest()
    verified = verify_corpus(app, metadata, spec, budget)
    require(verified['corpus_sha256'] == metadata['corpus_sha256'], 'Import chain mismatch.')
    disk = budget.disk()
    save_json(directory / 'corpus.json', metadata)
    return {'status': 'PASS', 'phase': 'build', **verified,
            'startup_seconds': startup, 'imports': samples(times),
            'elapsed_seconds': perf_counter() - started, 'disk': disk}


def _query(app, workspace, spec, budget, **options):
    budget.check()
    return app.search_page(workspace, QUERY, mode='terms',
        limit=spec['result_page_size'], timeout=min(300.0, spec['phase_timeout']),
        cancelled=budget.stopped, **options)


def traverse(app, metadata, spec, budget):
    expected = spec['books'] * spec['segments_per_book']
    seen = bytearray(expected)
    cursor = None
    count = requests = windows = copies = 0
    times = array('d')
    started = perf_counter()
    while True:
        offset = 0
        current_window = None
        while True:
            began = perf_counter()
            page = _query(app, metadata['workspace'], spec, budget, cursor=cursor, offset=offset)
            times.append(perf_counter() - began)
            requests += 1
            require(requests <= expected + spec['books'] + 10, 'Cursor traversal did not make bounded progress.')
            require(page.source_pages <= 100 and page.source_characters <= 5_000_000
                    and page.source_editions <= 500, 'Production source window bound changed.')
            if current_window is None:
                current_window = page.window_identity
            require(current_window == page.window_identity, 'Result continuation changed its source window.')
            for row in page.results:
                match = re.fullmatch(r'bench-edition-(\d{8})', row.edition_id)
                require(match is not None and row.workspace == metadata['workspace'] and row.verified,
                        'A search result escaped selected verified source scope.')
                number = int(match.group(1))
                # SearchResult.page is display text. The current completed
                # action handle carries the canonical reading-order ordinal.
                selected = app._selections.get(row.ref)
                require(selected is not None and selected.workspace == row.workspace
                        and selected.edition == row.edition_id and selected.revision == row.revision,
                        'Result and completed source-action identity differ.')
                ordinal = selected.ordinal
                require(0 <= number < spec['books'] and type(ordinal) is int
                        and 1 <= ordinal <= spec['segments_per_book'], 'Unexpected result source identity.')
                index = number * spec['segments_per_book'] + ordinal - 1
                require(not seen[index], 'Duplicate result across source/result pages.')
                seen[index] = 1
                count += 1
            if page.results:
                row = page.results[-1]
                require(app.copy(metadata['workspace'], row.ref, row.revision, 'quote',
                                 cancelled=budget.stopped) == QUERY, 'Source-bound quote differs.')
                copies += 1
            if not page.has_more_results:
                break
            require(bool(page.results), 'Result continuation returned an empty page.')
            cursor = page.cursor
            offset += spec['result_page_size']
        windows += 1
        cursor = page.next_source_cursor
        if cursor is None:
            break
    require(count == expected and all(seen), 'Full source traversal lost results.')
    return {'results': count, 'source_windows': windows, 'requests': requests,
            'copied_result_samples': copies, 'latency': samples(times),
            'elapsed_seconds': perf_counter() - started, 'exact_no_loss_no_duplicates': True}


def reader_samples(app, metadata, spec, budget):
    from carr_library.reader.reading_desk import ReadingDesk
    count = min(spec['books'], 16)
    numbers = sorted({(spec['books'] - 1) * i // max(1, count - 1) for i in range(count)})
    times = array('d')
    for number in numbers:
        budget.check()
        began = perf_counter()
        desk = ReadingDesk(app, metadata['workspace'], edition_id(number),
                           cancelled=budget.stopped, timeout=min(300.0, spec['phase_timeout']))
        page = desk.page(spec['segments_per_book'])
        times.append(perf_counter() - began)
        require(len(desk.pages) == spec['segments_per_book'] and page.verified
                and page.raw_text == segment(number, spec['segments_per_book'], spec['segment_bytes']),
                'Restarted reader did not preserve exact original source text.')
    return {'indices': numbers, 'open_and_last_page': samples(times)}


def cancellation_probe(app, metadata, spec, budget):
    first = _query(app, metadata['workspace'], spec, budget)
    require(bool(first.results), 'Cancellation baseline has no verified result.')
    row = first.results[0]
    old_handles = dict(app._selections)
    calls = 0

    def cancelled():
        nonlocal calls
        budget.check()
        calls += 1
        return calls >= 3

    began = perf_counter()
    try:
        app.search_page(metadata['workspace'], QUERY, mode='terms', cancelled=cancelled,
                        timeout=min(300.0, spec['phase_timeout']))
    except (InterruptedError, TimeoutError):
        pass
    else:
        raise BenchmarkFailure('Actual search did not observe cancellation.')
    elapsed = perf_counter() - began
    require(calls >= 3 and app._selections == old_handles,
            'Cancelled search replaced prior completed action handles.')
    require(app.copy(metadata['workspace'], row.ref, row.revision, 'quote',
                     cancelled=budget.stopped) == QUERY, 'Cancellation damaged prior source action.')
    try:
        _query(app, metadata['excluded_workspace'], spec, budget, cursor=first.cursor)
    except ValueError:
        pass
    else:
        raise BenchmarkFailure('Source cursor crossed workspace authorization.')
    recovered = _query(app, metadata['workspace'], spec, budget)
    require(bool(recovered.results), 'Search did not recover after cancellation.')
    return {'status': 'PASS', 'checkpoint_calls': calls, 'seconds_to_return': elapsed,
            'previous_handles_preserved': True, 'cross_scope_cursor_refused': True,
            'subsequent_query_completed': True}


def measure_corpus(directory, spec, *, restart=False, cancelled=lambda: False, progress=None):
    from carr_library.application import LibraryApp
    spec = validate_case(spec)
    directory = local(directory, exists=True)
    metadata = read_json(directory / 'corpus.json')
    require(metadata.get('schema') == SCHEMA and metadata.get('spec') == spec, 'Corpus configuration changed.')
    budget = Budget(directory, spec, cancelled=cancelled)
    progress = progress if progress is not None else {}
    progress.update({'phase': 'restart' if restart else 'measure'})
    budget.check()
    started = perf_counter()
    app = LibraryApp(directory / 'library')
    startup = perf_counter() - started
    integrity_started = perf_counter()
    verified = verify_corpus(app, metadata, spec, budget)
    require(verified['corpus_sha256'] == metadata['corpus_sha256'], 'Saved corpus digest differs.')
    integrity_seconds = perf_counter() - integrity_started
    database_before = file_hash(app.database, maximum=spec['max_disk_bytes'], check=budget.check)
    first_times, late_times = array('d'), array('d')
    for _ in range(spec['repeats']):
        began = perf_counter()
        page = _query(app, metadata['workspace'], spec, budget)
        first_times.append(perf_counter() - began)
        require(bool(page.results), 'First source window has no result.')
        began = perf_counter()
        late = _query(app, metadata['workspace'], spec, budget, title=title(spec['books'] - 1))
        late_times.append(perf_counter() - began)
        require(late.results and all(row.edition_id == edition_id(spec['books'] - 1)
                                    for row in late.results), 'Late-edition scoped search differs.')
    result = {'status': 'PASS', 'phase': 'restart' if restart else 'measure', **verified,
              'startup_seconds': startup, 'initial_integrity_seconds': integrity_seconds,
              'first_result_page': samples(first_times), 'late_edition_first_page': samples(late_times),
              'reading': reader_samples(app, metadata, spec, budget),
              'cancellation': cancellation_probe(app, metadata, spec, budget)}
    if not restart:
        result['full_traversal'] = traverse(app, metadata, spec, budget)
    else:
        result['full_traversal'] = {'status': 'NOT_REPEATED', 'reason': 'Full traversal belongs to the previous measured process.'}
    final_integrity_started = perf_counter()
    final = verify_corpus(app, metadata, spec, budget)
    result['final_integrity_seconds'] = perf_counter() - final_integrity_started
    require(final == verified, 'Corpus bytes or identity changed during read measurements.')
    require(file_hash(app.database, maximum=spec['max_disk_bytes'], check=budget.check) == database_before,
            'Read-only measurement changed the canonical database.')
    result.update(database_unchanged=True, disk=budget.disk(),
                  elapsed_seconds=perf_counter() - started)
    return result


def _deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.bind'):
        raise PermissionError('The corpus benchmark is offline.')


def child_main(config_path, expected_digest, phase, case_dir):
    bootstrap_started = perf_counter()
    require(phase in ('build', 'measure', 'restart'), 'Invalid benchmark phase.')
    require(file_hash(config_path, maximum=MAX_JSON) == expected_digest, 'Child configuration digest differs.')
    # The preliminary check alone cannot authorize a later opened snapshot.
    config = read_json(config_path, expected_sha256=expected_digest)
    require(config.get('schema') == SCHEMA, 'Invalid benchmark configuration.')
    directory = local(case_dir, exists=True)
    require(str(directory.parent) == config['output'], 'Child corpus is outside the selected output.')
    require(directory.name in {str(value) for value in config['sizes']}, 'Unknown child corpus size.')
    source_root = config['source_root']
    origins = activate(source_root)
    require(origins == config['origins'], 'Child imported different production code.')
    kernel, kernel_identity = memory_kernel()
    require(kernel_identity == config['memory_kernel']
            and file_hash(Path(__file__)) == config['harness_sha256'], 'Child measurement code changed.')
    sys.addaudithook(_deny_network)
    spec = validate_case({**config['case'], 'books': int(directory.name)})
    progress = {}
    bootstrap_seconds = perf_counter() - bootstrap_started
    before = kernel.peak_rss_bytes()
    try:
        if phase == 'build':
            result = build_corpus(directory, spec, progress=progress)
        else:
            result = measure_corpus(directory, spec, restart=phase == 'restart', progress=progress)
        check_origins(origins)
        require(file_hash(Path(__file__)) == config['harness_sha256']
                and file_hash(kernel_identity['path']) == kernel_identity['sha256'],
                'Measurement code changed during measurement.')
    except (InterruptedError, TimeoutError, KeyboardInterrupt) as exc:
        result = {'status': 'PARTIAL', 'phase': phase, 'progress': progress,
                  'reason': type(exc).__name__}
    except Exception as exc:
        result = {'status': 'FAIL', 'phase': phase, 'progress': progress,
                  'reason': type(exc).__name__, 'diagnostic': str(exc)[:2000]}
    result.update(pid=os.getpid(), python_executable=sys.executable, python_prefix=sys.prefix,
                  bootstrap_seconds=bootstrap_seconds,
                  baseline_peak_rss_bytes=before, process_peak_rss_bytes=kernel.peak_rss_bytes(),
                  peak_rss_method=kernel.rss_method(), origins=origins,
                  harness_sha256=config['harness_sha256'], memory_kernel=kernel_identity)
    save_json(directory / (phase + '.json'), result)
    return 0 if result['status'] == 'PASS' else 2 if result['status'] == 'PARTIAL' else 1


def interpreter():
    """Own the actual Windows venv interpreter, preserving its selected environment."""
    env = dict(os.environ)
    env.pop('PYTHONEXECUTABLE', None)
    env.pop('__PYVENV_LAUNCHER__', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    executable = sys.executable
    if os.name == 'nt' and sys.prefix != sys.base_prefix:
        base = getattr(sys, '_base_executable', None)
        require(bool(base), 'Selected Windows venv has no CPython base executable.')
        executable = base
        env['__PYVENV_LAUNCHER__'] = sys.executable
    return executable, env


def run_phase(config_path, config_digest, phase, directory, config):
    executable, env = interpreter()
    command = [executable, '-I', '-B', str(Path(__file__).resolve()), '--child',
               str(config_path), config_digest, phase, str(directory)]
    phase_started = perf_counter()
    with (directory / (phase + '.stdout.txt')).open('xb') as output, (
            directory / (phase + '.stderr.txt')).open('xb') as error:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                   shell=False, env=env)
        interrupted = None
        try:
            exit_code = process.wait(timeout=config['case']['phase_timeout'] + 30)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            interrupted = type(exc).__name__
            process.kill()
            exit_code = process.wait(timeout=10)
    path = directory / (phase + '.json')
    if interrupted is not None:
        return {'status': 'PARTIAL', 'phase': phase, 'reason': interrupted,
                'pid': process.pid, 'process_reaped': True, 'exit_code': exit_code,
                'process_wall_seconds': perf_counter() - phase_started}
    require(os.path.lexists(path), 'Child exited without a complete measurement report.')
    result = read_json(path)
    require(result.get('pid') == process.pid, 'Child interpreter ownership differs.')
    require(result.get('origins') == config['origins']
            and result.get('harness_sha256') == config['harness_sha256']
            and result.get('memory_kernel') == config['memory_kernel'], 'Child source evidence differs.')
    require(result.get('status') in ('PASS', 'PARTIAL', 'FAIL'), 'Invalid child result status.')
    require((exit_code == 0) == (result['status'] == 'PASS'), 'Child exit/report status differs.')
    return {**result, 'process_reaped': True, 'exit_code': exit_code,
            'process_wall_seconds': perf_counter() - phase_started}


def run_benchmark(output_dir, sizes, case, *, source_root=None, expected_application=None):
    require(__debug__, 'Run the benchmark without optimization flags.')
    require(type(sizes) in (list, tuple) and 1 <= len(sizes) <= 10
            and len(set(sizes)) == len(sizes), 'Choose distinct corpus sizes.')
    for count in sizes:
        validate_case({**case, 'books': count})
    origins = activate(source_root)
    if expected_application is not None:
        require(origins['carr_library.application']['path'] == str(local(expected_application, exists=True)),
                'Actual application origin differs from the requested parent origin.')
    kernel, kernel_identity = memory_kernel()
    output = local(output_dir)
    require(not os.path.lexists(output), 'Benchmark output already exists; no overwrites are allowed.')
    plain(output.parent, directory=True)
    # Avoid adding measurement corpora to a source/package directory.
    package = Path(origins['carr_library.application']['path']).parent
    require(not output.is_relative_to(package), 'Choose an output outside the application package.')
    output.mkdir(mode=0o700, exist_ok=False)
    config = {'schema': SCHEMA, 'output': str(output), 'sizes': list(sizes),
              'case': dict(case), 'source_root': str(local(source_root, exists=True)) if source_root else None,
              'origins': origins, 'memory_kernel': kernel_identity,
              'harness_sha256': file_hash(Path(__file__))}
    config_path = output / 'configuration.json'
    save_json(config_path, config)
    config_digest = file_hash(config_path, maximum=MAX_JSON)
    cases = []
    stop = False
    for count in sizes:
        if stop:
            cases.append({'requested_books': count, 'status': 'NOT_RUN', 'reason': 'An earlier case did not complete.'})
            continue
        directory = output / str(count)
        directory.mkdir(mode=0o700, exist_ok=False)
        phases = []
        for phase in ('build', 'measure', 'restart'):
            try:
                result = run_phase(config_path, config_digest, phase, directory, config)
                if result['status'] == 'PASS':
                    require(result.get('books') == count
                            and result.get('segments') == count * case['segments_per_book'],
                            'Child actual corpus size differs from requested size.')
                phases.append(result)
            except Exception as exc:
                phases.append({'status': 'FAIL', 'phase': phase, 'reason': type(exc).__name__,
                               'diagnostic': str(exc)[:2000]})
            if phases[-1]['status'] != 'PASS':
                stop = True
                break
        status = phases[-1]['status']
        if status == 'PASS':
            require([value['phase'] for value in phases] == ['build', 'measure', 'restart']
                    and all(value['process_reaped'] for value in phases),
                    'Fresh-process phase completion could not be established.')
            require(len({value['corpus_sha256'] for value in phases}) == 1, 'Restarted corpus identity differs.')
        cases.append({'requested_books': count, 'status': status, 'phases': phases})
        print(json.dumps({'completed_case': count, 'status': status}, ensure_ascii=True), flush=True)
    check_origins(origins)
    status = ('PASS' if all(item['status'] == 'PASS' for item in cases)
              else 'FAIL' if any(item['status'] == 'FAIL' for item in cases) else 'PARTIAL')
    report = {'schema': SCHEMA, 'status': status, 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'configuration_sha256': config_digest, 'requested_sizes': list(sizes),
              'mode': 'source' if source_root else 'installed',
              'environment': {'platform': platform.platform(), 'python': sys.version,
                              'sqlite': sqlite3.sqlite_version, 'peak_rss_method': kernel.rss_method(),
                              'machine': platform.machine(), 'logical_cpus': os.cpu_count()},
              'origins': origins, 'harness_sha256': config['harness_sha256'],
              'memory_kernel': kernel_identity, 'method': METHOD, 'claims': CLAIMS, 'cases': cases}
    save_json(output / 'library-corpus-scale.json', report)
    return report


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--child':
        require(len(argv) == 5, 'Invalid internal phase invocation.')
        return child_main(*argv[1:])
    parser = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', required=True, type=Path, help='Fresh output directory under an existing local parent.')
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000, 10000, 50000])
    parser.add_argument('--segments-per-book', type=int, default=20)
    parser.add_argument('--segment-bytes', type=int, default=2048)
    parser.add_argument('--result-page-size', type=int, default=37)
    parser.add_argument('--repeats', type=int, default=9)
    parser.add_argument('--phase-timeout', type=float, default=3600)
    parser.add_argument('--max-disk-bytes', type=int, default=24 * 1024 ** 3,
                        help='Measured per-case checkpoint budget; not a filesystem quota.')
    parser.add_argument('--min-free-bytes', type=int, default=1024 ** 3)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--installed', action='store_true')
    selection.add_argument('--source-root', type=Path)
    parser.add_argument('--expected-application-origin', type=Path,
                        help='Require the exact imported application.py path, useful in source/installed gates.')
    args = parser.parse_args(argv)
    root = None if args.installed else args.source_root or Path(__file__).resolve().parents[1] / 'src'
    case = {name: getattr(args, name) for name in (
        'segments_per_book', 'segment_bytes', 'result_page_size', 'repeats',
        'phase_timeout', 'max_disk_bytes', 'min_free_bytes')}
    report = run_benchmark(args.output_dir, args.sizes, case, source_root=root,
                           expected_application=args.expected_application_origin)
    print(json.dumps({'status': report['status'],
                      'evidence': str(args.output_dir / 'library-corpus-scale.json')}, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'PASS' else 2 if report['status'] == 'PARTIAL' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
