"""Measure actual retained book corpora through existing library services.

Corpus construction uses the existing catalog/immutable revision writers in
bounded transactions, followed by the full storage check. It is deliberately
reported separately from sampled public import_file throughput. Each book has
its own retained UTF-8 original and sealed source segments, not vector rows.
No model inference, native window startup, NVDA or real-book quality is implied.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import stat
import subprocess
import sys
from time import monotonic, perf_counter

MAX_BOOKS = 50_000
MAX_SEGMENTS = 1_000_000
MAX_SECONDS = 7200
TOKEN = 'CorpusNeedle'
AUTHOR = 'Synthetic capacity fixture'
MODULES = (
    'carr_library.application', 'carr_library.storage.revisions',
    'carr_library.ingest.pipeline', 'carr_library.ingest.retained_assets',
    'carr_library.search.pagination', 'carr_library.catalog.read_views',
    'carr_library.reader.reading_desk', 'carr_library.reader.source_file',
    'carr_library.library_backup', 'carr_library.backup_capacity',
)


def distribution(values):
    if not values or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
        raise ValueError('A finite nonempty measurement series is required.')
    ordered = sorted(values)
    return dict(observations=len(values), minimum_seconds=ordered[0],
        p50_seconds=ordered[math.ceil(.50 * len(ordered)) - 1],
        p95_seconds=ordered[math.ceil(.95 * len(ordered)) - 1],
        maximum_seconds=ordered[-1], total_seconds=sum(ordered),
        quantiles='nearest rank; inspect observation count before interpreting tails')


def peak_rss_bytes():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                *[(name, ctypes.c_size_t) for name in (
                    'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                    'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        value = Counters()
        value.cb = ctypes.sizeof(value)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(value.PeakWorkingSetSize)
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == 'darwin' else value * 1024)


def disk_inventory(root):
    """Phase-sampled private files; no symlinks or hidden external traversal."""
    pending, files, total = [Path(root)], 0, 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ValueError('The measurement directory acquired a linked/reparse entry.')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    files += 1
                    total += info.st_size
                else:
                    raise ValueError('The measurement directory contains a special file.')
                if files + len(pending) > 200_000:
                    raise ValueError('The declared corpus exceeded its measurement file budget.')
    return dict(files=files, logical_bytes=total)


class Deadline:
    def __init__(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= MAX_SECONDS:
            raise ValueError('Choose a total measurement deadline from 1 through 7200 seconds.')
        self.end = monotonic() + seconds

    def check(self):
        if monotonic() >= self.end:
            raise TimeoutError('The declared corpus measurement exceeded its total deadline.')

    def stopped(self):
        self.check()
        return False

    def remaining(self, maximum=600):
        self.check()
        return min(maximum, max(.1, self.end - monotonic()))


def source_identity(*, installed):
    checkout = Path(__file__).resolve().parents[1] / 'src'
    sources = {}
    for name in MODULES:
        component = importlib.import_module(name)
        path = Path(component.__file__).resolve(strict=True)
        if installed and not path.is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError('The capacity probe imported checkout code instead of the installed package.')
        digest = sha256(path.read_bytes()).hexdigest()
        expected = checkout / Path(*name.split('.')).with_suffix('.py')
        if expected.is_file() and sha256(expected.read_bytes()).hexdigest() != digest:
            raise ValueError('The installed capacity implementation differs from the selected source.')
        sources[name] = dict(path=str(path), sha256=digest)
    return sources


def book_text(number, segments):
    rows = []
    for segment in range(segments):
        marker = TOKEN + ' ' if segment == 0 else ''
        rows.append(marker + f'Book {number:08d}, segment {segment + 1:04d}. '
            'Exact retained UTF-8. Україна; naïve; e\u0301; العربية. '
            'This reproducible capacity fixture preserves text and source identity. '
            'It is not a copyrighted publication or a language-quality benchmark.\r\n')
    return '\f'.join(rows)


def book_ids(number):
    suffix = f'{number:08d}'
    return 'work-' + suffix, 'edition-' + suffix, 'revision-' + suffix


def seed_corpus(app, workspace, collection, books, segments, deadline, progress=lambda row: None):
    """Real production writers; never report this as supervised import speed."""
    from carr_library.catalog.membership import Catalog
    from carr_library.catalog.registration import Registration, register_batch
    from carr_library.citations.text_attestation import CONFIG
    from carr_library.ingest.writer_guard import exclusive_writer
    from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Page, Storage
    if type(books) is not int or not 1 <= books <= MAX_BOOKS:
        raise ValueError('Choose from 1 through 50000 actual fixture books.')
    if type(segments) is not int or not 1 <= segments <= 1000 or books * segments > MAX_SEGMENTS:
        raise ValueError('Choose a corpus of at most one million actual source segments.')
    started, source_bytes = perf_counter(), 0
    with exclusive_writer(app.root), closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        for first in range(0, books, 250):
            deadline.check()
            with storage.transaction():
                catalog = Catalog(storage.db, workspace)
                for number in range(first, min(books, first + 250)):
                    deadline.check()
                    raw = book_text(number, segments)
                    data = raw.encode('utf-8')
                    asset = sha256(data).hexdigest()
                    retained = app.assets.retain(data, 'text/plain', asset)
                    if retained.stat().st_size != len(data):
                        raise ValueError('A corpus original was not retained exactly.')
                    work, edition, revision = book_ids(number)
                    title = f'Synthetic book {number:08d}'
                    record = Registration(work, edition,
                        dict(sha256=asset, size=len(data), mime='text/plain'), title,
                        (AUTHOR,), 'uk', collections=(collection,))
                    register_batch(catalog, (record,))
                    storage.stage_revision(workspace, edition, revision, asset, CONFIG,
                        (Page(part) for part in raw.split('\f')))
                    storage.db.execute('INSERT INTO source_metadata '
                        '(workspace,edition,revision,author,title,volume,edition_label) VALUES(?,?,?,?,?,?,?)',
                        (workspace, edition, revision, AUTHOR, title, None, None))
                    source_bytes += len(data)
            progress(dict(phase='fixture-construction', books=min(books, first + 250),
                          requested_books=books, seconds=perf_counter() - started))
        deadline.check()
        validation_started = perf_counter()
        storage.db.set_progress_handler(lambda: int(deadline.stopped()), 10000)
        try:
            storage.check()
        finally:
            storage.db.set_progress_handler(None, 0)
        validation = perf_counter() - validation_started
    with app.read(workspace) as db:
        actual_books = db.execute('SELECT count(*) FROM catalog_edition WHERE workspace=?', (workspace,)).fetchone()[0]
        actual_segments = db.execute('SELECT count(*) FROM active_page WHERE workspace=?', (workspace,)).fetchone()[0]
        actual_originals = db.execute('SELECT count(DISTINCT asset) FROM catalog_edition WHERE workspace=?',
                                     (workspace,)).fetchone()[0]
    if (actual_books, actual_segments, actual_originals) != (books, books * segments, books):
        raise ValueError('The actual corpus does not match its declared book/segment/original counts.')
    return dict(books=books, source_segments=actual_segments, distinct_originals=actual_originals,
        source_bytes=source_bytes, construction_seconds=perf_counter() - started,
        full_integrity_seconds=validation,
        method='Existing Catalog/register_batch, RetainedAssets and Storage.stage_revision; 250 books/transaction; full Storage.check.',
        supervised_import_throughput='NOT_MEASURED_BY_FIXTURE_CONSTRUCTION')


def measure_catalog(app, workspace, books, deadline):
    after, count, times = None, 0, []
    while True:
        deadline.check()
        started = perf_counter()
        rows = app.browse(workspace, after=after, limit=500)
        times.append(perf_counter() - started)
        if not rows:
            break
        for row in rows:
            if row.edition_id != book_ids(count)[1]:
                raise ValueError('Catalog continuation omitted or reordered a retained fixture book.')
            count += 1
        after = rows[-1].edition_id
    if count != books:
        raise ValueError('Catalog continuation did not reach every retained fixture book.')
    return dict(books=count, pages=len(times), latency=distribution(times))


def measure_search(app, workspace, collection, books, *, page_size, deadline, progress=lambda row: None):
    from carr_library.application import LibraryApp
    cursor, count, pages, times, reopened = None, 0, 0, [], []
    reopen_page = max(1, math.ceil(books / page_size) // 2)
    while True:
        deadline.check()
        started = perf_counter()
        page = app.search_page(workspace, TOKEN, collection=collection, cursor=cursor,
            limit=page_size, page_budget=1000, timeout=deadline.remaining(600),
            cancelled=deadline.stopped)
        times.append(perf_counter() - started)
        if page.scope_editions != books or page.scanned_segments > 1000:
            raise ValueError('Search exceeded or changed its exact corpus scope.')
        for row in page.results:
            if (row.workspace != workspace or row.edition_id != book_ids(count)[1]
                    or row.quote != TOKEN or not row.verified):
                raise ValueError('A result lost exact order, original text or scope.')
            if count in (0, books // 2, books - 1):
                if app.copy(workspace, row.ref, row.revision, 'quote') != TOKEN:
                    raise ValueError('Actual Copy did not preserve the exact source bytes.')
            count += 1
        pages += 1
        cursor = page.next_cursor
        if pages == reopen_page and cursor is not None:
            start = perf_counter()
            app = LibraryApp(app.root)
            reopened.append(perf_counter() - start)
        if pages % 100 == 0 or cursor is None:
            progress(dict(phase='exact-result-pages', pages=pages, exact_hits=count, requested_books=books))
        if cursor is None:
            break
        if pages > books + 2:
            raise ValueError('The corpus search did not make bounded continuation progress.')
    if count != books:
        raise ValueError('Search did not reach exactly one authenticated result per book.')
    return app, dict(exact_hits=count, result_pages=pages, latency=distribution(times),
        reopen_with_saved_cursor=distribution(reopened) if reopened else None,
        ordering='Exact source order; all hits visited, no sampling or duplicate counting',
        semantic_inference='NOT_RUN')


def measure_sources(app, workspace, books, segments, samples, deadline):
    from carr_library.reader.reading_desk import ReadingDesk
    opened, viewed, exact = [], [], 0
    for index in range(samples):
        deadline.check()
        number = min(books - 1, index * books // samples)
        edition = book_ids(number)[1]
        started = perf_counter()
        desk = ReadingDesk(app, workspace, edition, cancelled=deadline.stopped,
                           timeout=deadline.remaining(60))
        page = desk.page(segments)
        opened.append(perf_counter() - started)
        if page.raw_text != book_text(number, segments).split('\f')[-1] or not page.verified:
            raise ValueError('ReadingDesk did not recover the exact final source segment.')
        started = perf_counter()
        source = app.prepare_edition_file(workspace, edition, cancelled=deadline.stopped)
        viewed.append(perf_counter() - started)
        expected = book_text(number, segments).encode('utf-8')
        if source.path.read_bytes() != expected or source.identity.asset_sha256 != sha256(expected).hexdigest():
            raise ValueError('The real original-view preparation changed the retained bytes.')
        exact += 1
    return dict(sampled_books=exact, source_page=distribution(opened),
                original_view_preparation=distribution(viewed), external_viewer='NOT_LAUNCHED')


def measure_cancellation(app, workspace, collection, deadline):
    first = app.search_page(workspace, TOKEN, collection=collection, limit=1,
        page_budget=1000, timeout=deadline.remaining(60), cancelled=deadline.stopped)
    if first.next_cursor is None:
        return dict(status='NOT_APPLICABLE', reason='A single fixture book has no continuation.')
    before = dict(app._selections)
    checks, requested = 0, None
    def stop():
        nonlocal checks, requested
        deadline.check()
        checks += 1
        if checks >= 15:
            requested = perf_counter() if requested is None else requested
            return True
        return False
    try:
        app.search_page(workspace, TOKEN, collection=collection, cursor=first.next_cursor,
            limit=1, page_budget=1000, timeout=deadline.remaining(60), cancelled=stop)
    except (TimeoutError, InterruptedError):
        completed = perf_counter()
    else:
        raise ValueError('Actual search did not honor the cancellation request.')
    if requested is None or app._selections != before:
        raise ValueError('Cancelled search changed an earlier authenticated selection.')
    resumed = app.search_page(workspace, TOKEN, collection=collection, cursor=first.next_cursor,
        limit=1, page_budget=1000, timeout=deadline.remaining(60), cancelled=deadline.stopped)
    if len(resumed.results) != 1 or resumed.results[0].edition_id != book_ids(1)[1]:
        raise ValueError('The saved continuation did not recover exactly after cancellation.')
    return dict(status='PASS', request_to_return_seconds=completed - requested,
                cooperative_checks=checks, earlier_selection_preserved=True, exact_cursor_retry=True)


def measure_imports(app, workspace, directory, samples, deadline):
    times = []
    for index in range(samples):
        deadline.check()
        source = directory / f'public-import-{index:04d}.txt'
        original = f'ImportSample {index:04d}.\r\nНезмінне джерело.\fSecond exact source segment.'.encode('utf-8')
        source.write_bytes(original)
        started = perf_counter()
        result = app.import_file(workspace, source, title=f'Measured public import {index:04d}',
            cancelled=deadline.stopped, timeout=deadline.remaining(600))
        times.append(perf_counter() - started)
        if result.get('state') != 'complete' or result.get('asset') != sha256(original).hexdigest():
            raise ValueError('The measured public import did not complete its exact original.')
        if source.read_bytes() != original:
            raise ValueError('Public import changed its external original.')
    return dict(latency=distribution(times), sampled_imports=samples,
        route='Actual LibraryApp.import_file, strict UTF-8 intake, journal, full publication validation',
        input='Distinct two-segment UTF-8 documents at the declared existing corpus size',
        books_per_second=samples / sum(times) if sum(times) else None)


def measure_worker_recovery(app, workspace, directory, deadline):
    from carr_library.application import LibraryApp
    from carr_library.ingest.streamed_text import TextProfile, FRAGMENT_CHARS
    from carr_library.ingest.worker import load_request
    source = directory / 'worker-recovery.txt'
    hashed = sha256()
    with source.open('xb') as output:
        first = b'RecoveryNeedle exact source.\r\n'
        output.write(first)
        hashed.update(first)
        block = b'x' * (FRAGMENT_CHARS - 1) + b'\n'
        for _ in range(256):
            output.write(block)
            hashed.update(block)
    asset = hashed.hexdigest()
    observed, requested = 0, None
    def cancelled():
        nonlocal observed, requested
        deadline.check()
        try:
            with closing(sqlite3.connect((app.root / 'imports.sqlite').as_uri() + '?mode=ro',
                                         uri=True, timeout=0)) as db:
                observed = db.execute('SELECT count(*) FROM checkpoint c JOIN job j ON j.id=c.job '
                                      'WHERE j.asset=?', (asset,)).fetchone()[0]
        except sqlite3.Error:
            return False
        if observed >= 2:
            requested = perf_counter() if requested is None else requested
            return True
        return False
    started = perf_counter()
    stopped = app.import_file(workspace, source, source_profile=TextProfile(),
        title='Recovery source', cancelled=cancelled, timeout=deadline.remaining(600))
    returned = perf_counter()
    if stopped.get('state') != 'cancelled' or observed < 2 or requested is None:
        raise ValueError('The actual worker did not stop after durable source checkpoints.')
    record = load_request(app.root, stopped['request_id'])
    if record.get('asset_sha256') != asset or not record.get('job'):
        raise ValueError('The stopped worker did not retain its exact original and job.')
    source.unlink()  # Only this newly created synthetic external input.
    reopened_at = perf_counter()
    reopened = LibraryApp(app.root)
    reopen_seconds = perf_counter() - reopened_at
    resumed_at = perf_counter()
    resumed = reopened.resume_import(workspace, stopped['request_id'], timeout=deadline.remaining(600),
                                    cancelled=deadline.stopped)
    resume_seconds = perf_counter() - resumed_at
    if resumed.get('state') != 'complete' or resumed.get('id') != record['job']:
        raise ValueError('The real worker did not resume the same job from retained bytes.')
    page = reopened.search_page(workspace, 'RecoveryNeedle', title='Recovery source',
        limit=10, page_budget=500, timeout=deadline.remaining(60), cancelled=deadline.stopped)
    if len(page.results) != 1 or reopened.copy(workspace, page.results[0].ref,
            page.results[0].revision, 'quote') != 'RecoveryNeedle':
        raise ValueError('Recovered source did not retain its exact independent quotation.')
    return dict(status='PASS', operation_seconds=returned - started,
        cancellation_seconds=returned - requested, observed_checkpoints=observed,
        reopen_seconds=reopen_seconds, resume_seconds=resume_seconds,
        external_original_removed=True, same_job=True, actual_owned_worker=True,
        exact_source_quote=True)


def measure_startup(root, samples, deadline):
    program = (
        "import json,pathlib,socket,sys,time; "
        "deny=lambda *a,**k: (_ for _ in ()).throw(PermissionError('Network denied')); "
        "socket.create_connection=socket.getaddrinfo=socket.socket.connect=socket.socket.connect_ex=deny; "
        "from carr_library.application import LibraryApp; "
        "start=time.perf_counter(); app=LibraryApp(pathlib.Path(sys.argv[1])); "
        "print(json.dumps(dict(seconds=time.perf_counter()-start,workspaces=len(app.workspaces()))))")
    cold = []
    for _ in range(samples):
        deadline.check()
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root)],
            cwd=root.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=deadline.remaining(600), check=False)
        if result.returncode:
            raise RuntimeError('Fresh-process application startup failed: ' + result.stderr[-1500:])
        value = json.loads(result.stdout)
        if value['workspaces'] != 2:
            raise ValueError('Fresh-process startup did not reopen both isolated libraries.')
        cold.append(value['seconds'])
    return dict(latency=distribution(cold), fresh_process=True,
        os_page_cache='Not evicted; these are process restarts, not cold-storage measurements',
        native_window='NOT_RUN')



def catalog_receipt(app, deadline):
    """Bounded ordered identity receipt over every actual catalog, not a sample."""
    digest, count, libraries = sha256(), 0, tuple(sorted(item.id for item in app.workspaces()))
    for workspace in libraries:
        deadline.check()
        with app.read(workspace) as db:
            cursor = db.execute('SELECT id,asset FROM catalog_edition WHERE workspace=? ORDER BY id',
                                (workspace,))
            while rows := cursor.fetchmany(500):
                deadline.check()
                for row in rows:
                    digest.update(json.dumps([workspace, row['id'], row['asset']],
                        ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')
                    count += 1
    return dict(libraries=libraries, editions=count, ordered_identity_sha256=digest.hexdigest())


def measure_complete_backup(app, workspace, books, segments, samples, output, deadline,
                            progress=lambda row: None):
    """Actual current complete snapshot, source retirement and independent restore."""
    import shutil
    from carr_library.application import open_existing_library
    from carr_library.backup_capacity import LARGE_SOURCE_PROFILE
    from carr_library.library_backup import verify_backup, restore_backup
    expected = catalog_receipt(app, deadline)
    backup, target = Path(output) / 'complete-library-backup', Path(output) / 'restored-library'
    times = {}
    def measured(name, action):
        deadline.check()
        progress(dict(phase='complete-library-' + name))
        started = perf_counter()
        value = action()
        times[name + '_seconds'] = perf_counter() - started
        deadline.check()
        return value
    identity = measured('backup', lambda: app.backup(backup,
        capacity_profile=LARGE_SOURCE_PROFILE, cancelled=deadline.stopped))
    measured('verify', lambda: verify_backup(backup, identity, cancelled=deadline.stopped))
    inventory = disk_inventory(backup)
    # Both directories are exclusively constructed by this finite measurement.
    # Removing its source proves that no old absolute path supplies a restore.
    deadline.check()
    shutil.rmtree(app.root)
    root = measured('restore', lambda: restore_backup(backup, target, identity,
                                                      cancelled=deadline.stopped))
    restored = open_existing_library(root)
    actual = catalog_receipt(restored, deadline)
    if actual != expected:
        raise ValueError('Complete restore changed the actual ordered catalog or retained source identities.')
    sources = measure_sources(restored, workspace, books, segments, samples, deadline)
    startup = measure_startup(restored.root, min(3, samples), deadline)
    return dict(status='PASS', capacity_profile=LARGE_SOURCE_PROFILE,
        manifest_sha256=identity, catalog=actual, original_fixture_library_removed=True,
        restored_root_differs=True, all_manifest_files_verified=True,
        source_open=sources, fresh_restored_startup=startup,
        snapshot_inventory=inventory, **times,
        scope='Complete current library state; external models/credentials and whole-machine backup are not included.')


def write_report(path, value):
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
    temporary = path.with_suffix(path.suffix + '.partial')
    with temporary.open('wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(args):
    if not __debug__:
        raise ValueError('Run the measurement with assertions enabled.')
    from carr_library.application import LibraryApp
    from carr_library.audio.jobs import checked_directory
    output = Path(args.output_dir).absolute()
    checked_directory(output.parent)
    output.mkdir(mode=0o700, exist_ok=False)
    deadline = Deadline(args.timeout)
    sources = source_identity(installed=args.installed)
    def denied(*a, **k):
        raise PermissionError('Network is disabled during the corpus measurement.')
    socket.create_connection = socket.getaddrinfo = denied
    socket.socket.connect = socket.socket.connect_ex = denied
    started = perf_counter()
    result = dict(schema='carr-library-corpus-measurement/1', status='RUNNING',
        recorded_at_utc=datetime.now(timezone.utc).isoformat(), source_commit=args.source_commit,
        source_modules=sources, harness_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        environment=dict(platform=platform.platform(), python=sys.version, sqlite=sqlite3.sqlite_version,
            logical_cpus=os.cpu_count(), process_peak_rss_method='OS process high-water working set/RSS',
            network='Parent and startup Python sockets denied; worker/native egress not established', model_inference='NOT_RUN', vram='NOT_MEASURED; no model loaded'),
        profile=dict(books=args.books, segments_per_book=args.segments_per_book,
            sampled_imports=args.samples, source_samples=args.samples, result_page_size=args.page_size,
            total_timeout_seconds=args.timeout),
        disk_samples=[], claims=dict(real_book_quality='NOT_MEASURED', semantic_inference='NOT_RUN',
            native_ui='NOT_RUN', manual_nvda='NOT_RUN', distribution='NOT_ESTABLISHED'))
    report = output / 'library-corpus.json'
    def progress(row):
        deadline.check()
        result['current'] = row
        result['elapsed_seconds'] = perf_counter() - started
        write_report(report, result)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    def disk(phase):
        result['disk_samples'].append(dict(phase=phase, **disk_inventory(output)))
    try:
        app = LibraryApp(output / 'library')
        workspace = app.create_workspace('Measured retained corpus')
        other = app.create_workspace('Isolated control corpus')
        collection = app.create_collection(workspace, 'Declared fixture population')
        inputs = output / 'synthetic-inputs'
        inputs.mkdir(mode=0o700)
        result['fixture'] = seed_corpus(app, workspace, collection, args.books,
            args.segments_per_book, deadline, progress)
        disk('constructed-corpus')
        result['catalog'] = measure_catalog(app, workspace, args.books, deadline)
        isolation = app.add_text(other, 'Outside selected library', 'CorpusNeedle ForeignNeedle.')
        if isolation.get('state') != 'complete':
            raise ValueError('Isolation control import failed.')
        probe = app.search_page(workspace, 'ForeignNeedle', collection=collection,
            page_budget=1000, limit=25, timeout=deadline.remaining(600), cancelled=deadline.stopped)
        if probe.results or probe.scope_editions != args.books:
            raise ValueError('The selected corpus leaked another library.')
        result['scope_isolation'] = 'PASS'
        app, result['search'] = measure_search(app, workspace, collection, args.books,
            page_size=args.page_size, deadline=deadline, progress=progress)
        result['source_open'] = measure_sources(app, workspace, args.books,
            args.segments_per_book, args.samples, deadline)
        result['search_cancellation'] = measure_cancellation(app, workspace, collection, deadline)
        disk('search-and-source-opening')
        progress(dict(phase='public-import-sampling', observations=args.samples))
        result['public_import'] = measure_imports(app, workspace, inputs, args.samples, deadline)
        disk('after-public-imports')
        progress(dict(phase='actual-worker-cancellation-and-recovery'))
        result['worker_recovery'] = measure_worker_recovery(app, workspace, inputs, deadline)
        disk('after-worker-recovery')
        result['startup'] = measure_startup(app.root, min(3, args.samples), deadline)
        if args.complete_backup:
            result['complete_library_backup'] = measure_complete_backup(app, workspace, args.books,
                args.segments_per_book, args.samples, output, deadline, progress)
            disk('complete-library-backup-and-restore')
        else:
            result['complete_library_backup'] = {'status': 'NOT_RUN', 'reason': 'Explicit --complete-backup not selected.'}
        final_sources = source_identity(installed=args.installed)
        if final_sources != sources:
            raise ValueError('The measured production modules changed during this run.')
        result.update(status='PASS', elapsed_seconds=perf_counter() - started,
            parent_process_peak_rss_bytes=peak_rss_bytes(),
            phase_sampled_peak_private_file_bytes=max(row['logical_bytes'] for row in result['disk_samples']),
            disk_peak_method='Logical file bytes at declared phase boundaries; transient high-water disk is not claimed',
            complete_machine_backup='NOT_MEASURED_BY_THIS_CORPUS_PROBE')
        result.pop('current', None)
        write_report(report, result)
        print(json.dumps(dict(status='PASS', evidence=str(report),
            books=args.books, segments=args.books * args.segments_per_book,
            result_pages=result['search']['result_pages']), ensure_ascii=False), flush=True)
        return 0
    except BaseException as error:
        result.update(status='FAIL', elapsed_seconds=perf_counter() - started,
            error=type(error).__name__ + ': ' + str(error)[:2000],
            parent_process_peak_rss_bytes=peak_rss_bytes())
        write_report(report, result)
        raise


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='New dedicated synthetic measurement folder')
    parser.add_argument('--books', type=int, required=True)
    parser.add_argument('--segments-per-book', type=int, default=20)
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--page-size', type=int, default=25)
    parser.add_argument('--timeout', type=float, default=5400)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--complete-backup', action='store_true',
        help='Measure current large-profile complete backup and independent restore, then remove only the synthetic source library')
    args = parser.parse_args(arguments)
    if (not 1 <= args.books <= MAX_BOOKS or not 1 <= args.segments_per_book <= 1000
            or args.books * args.segments_per_book > MAX_SEGMENTS
            or not 3 <= args.samples <= 100 or not 1 <= args.page_size <= 100
            or len(args.source_commit) != 40 or any(char not in '0123456789abcdef' for char in args.source_commit)):
        parser.error('Choose the declared bounded corpus, samples, pages and exact source commit.')
    if not args.installed:
        parser.error('Install the exact selected source and pass --installed; owned isolated workers require it.')
    return run(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('FAIL: ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
