"""Real owned TXT imports and explicit, durable scan-page continuation.

These are finite capacity and ownership checks, not large-corpus performance or
language-quality qualification. Non-TXT adapters keep their historical limits.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker
from carr_library.ingest.resource_budget import ImportBudget, MIB
from carr_library.ingest.streamed_text import TextProfile, FRAGMENT_CHARS


def budget(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=MIB,
        segments=1000, steps=1001, seconds=120, memory_bytes=768*MIB,
        library_bytes=64*MIB, temporary_bytes=128*MIB), **changes)


def _rows_identity(record):
    return tuple(tuple(row[key] for key in ('row_id', 'request_id', 'work_id', 'edition_id'))
                 for row in record['rows'])


def _digest(path):
    value = sha256()
    with Path(path).open('rb') as source:
        while data := source.read(MIB):
            value.update(data)
    return value.hexdigest()


def test_public_partial_source_resumes_same_profile_job_and_retained_original(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded public source')
    source = tmp_path / 'original.txt'
    raw = '\ufeffFirst exact Ω.\r\n\fSecond exact Ї.\n\fThird exact source.'
    source.write_bytes(raw.encode('utf-8'))
    selected = budget(steps=2)
    profile = TextProfile(selected.source_bytes, selected)
    first = app.import_file(workspace, source, title='Retained original', import_budget=selected)
    assert first['state'] == 'pending' and first['completed'] == 2, first
    assert first['isolated'] is True
    request = worker.load_request(app.root, first['request_id'])
    assert request['version'] == 2 and request['source_profile'] == profile.to_dict()
    assert request['job'] == first['id'] and request['asset_sha256'] == sha256(raw.encode('utf-8')).hexdigest()
    retained = app.assets.resolve(request['asset_sha256'], profile=profile)
    assert retained.read_bytes() == source.read_bytes()
    request_path = app.root / 'import-requests' / (first['request_id'] + '.json')
    original_request = request_path.read_bytes()
    changed = replace(selected, steps=3)
    with pytest.raises(ValueError, match='original source profile'):
        worker.run_request(app.root, workspace, source, {}, None,
            request_id=first['request_id'], source_profile=TextProfile(changed.source_bytes, changed))
    assert request_path.read_bytes() == original_request
    source.unlink()
    final = LibraryApp(app.root).resume_import(workspace, first['request_id'])
    assert final['state'] == 'complete' and final['completed'] == 3, final
    assert final['id'] == first['id'] and final['request_id'] == first['request_id']
    assert final['verified'] is True and retained.read_bytes() == raw.encode('utf-8')
    hit, = app.search(workspace, 'Third exact source')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third exact source'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as journal:
        assert journal.execute('SELECT count(*) FROM job').fetchone()[0] == 1
        assert journal.execute('SELECT count(*) FROM checkpoint').fetchone()[0] == 3


def test_each_existing_owned_request_resumes_partial_batch_rows_without_new_book_identity(tmp_path, monkeypatch):
    from carr_library.audio import process
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Current worker continuation')
    paths = []
    for index in range(3):
        path = tmp_path / ('book-%d.txt' % index)
        path.write_text('Book %d first.\fBook %d second.\fBook %d third.' %
                        (index, index, index), encoding='utf-8')
        paths.append(path)
    selected = budget(steps=2)
    plan = app.plan_import_page(workspace, paths, import_budget=selected)
    assert plan['version'] == 3 and plan['total'] == 3
    assert plan['source_profile'] == TextProfile(selected.source_bytes, selected).to_dict()
    assert plan['scan']['cursor']['complete'] is True
    identities, launches = _rows_identity(plan), []
    actual_run = process.run_process
    def observe(args, **options):
        if any('_load_worker_input' in str(value) for value in args):
            assert options['owned_tree'] is True
            launches.append(tuple(args))
        return actual_run(args, **options)
    monkeypatch.setattr(process, 'run_process', observe)
    first = app.run_import_batch(workspace, plan['batch_id'])
    assert len(launches) == 3
    assert _rows_identity(first) == identities and first['state'] == 'queued'
    assert all(row['state'] == 'queued' and row['result']['state'] == 'pending'
               and row['result']['completed'] == 2 for row in first['rows'])
    for path in paths:
        path.unlink()
    final = LibraryApp(app.root).run_import_batch(workspace, plan['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 3, final
    assert len(launches) == 6 and _rows_identity(final) == identities
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 3
    for index, row in enumerate(final['rows']):
        assert row['result']['isolated'] and row['result']['verified']
        assert row['result']['id'] == first['rows'][index]['result']['id']
        assert app.assets.resolve(row['manifest']['sha256'],
            profile=TextProfile(selected.source_bytes, selected)).read_text(encoding='utf-8') == (
                'Book %d first.\fBook %d second.\fBook %d third.' % (index, index, index))


def test_actual_5001_sources_continue_in_bounded_inert_pages(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('More than one scan window')
    folder = tmp_path / 'selected'
    folder.mkdir()
    expected = {}
    for index in range(5001):
        path = folder / ('book-%05d.txt' % index)
        data = ('Original numbered source %05d.\r\n' % index).encode('utf-8')
        path.write_bytes(data)
        expected[str(path.resolve())] = sha256(data).hexdigest()
    def forbidden(*args, **kwargs):
        raise AssertionError('Planning must not run an import worker.')
    monkeypatch.setattr(worker, '_invoke', forbidden)
    monkeypatch.setattr(worker, '_invoke_owned', forbidden)
    current = app.plan_import_page(workspace, [folder], max_files=1000, timeout=120)
    seen, pages, ids = {}, [], set()
    while True:
        assert current['version'] == 2 and 0 < current['total'] <= 1000
        assert current['state'] == 'queued'
        assert current['batch_id'] not in ids
        ids.add(current['batch_id'])
        pages.append(current)
        for row in current['rows']:
            assert row['state'] == 'queued' and row['result'] is None
            assert row['source'] not in seen
            seen[row['source']] = row['manifest']['sha256']
        if current['scan']['cursor']['complete']:
            break
        assert len(pages) <= 6
        previous = current
        current = LibraryApp(app.root).continue_import_scan(workspace, previous['batch_id'],
                                                           max_files=1000, timeout=120)
        assert current['scan']['previous'] == previous['batch_id']
        assert previous['scan']['next'] == current['batch_id']
        assert current['scan']['page'] == previous['scan']['page'] + 1
    assert seen == expected and len(pages) == 6
    assert app.continue_import_scan(workspace, current['batch_id']) == current
    assert app.browse(workspace) == ()
    assert not list((app.root / 'import-requests').glob('*.json'))
    assert not list(app.assets_dir.glob('*.txt'))


def test_explicit_actual_over_128_mib_original_uses_current_worker_seal_and_exact_quote(tmp_path):
    """Real bytes beyond the legacy streamed ceiling; no allocated giant string."""
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit larger retained original')
    source = tmp_path / 'large-original.txt'
    expected, count, width = sha256(), 129, MIB
    with source.open('xb') as output:
        for index in range(count):
            if index:
                output.write(b'\f')
                expected.update(b'\f')
            prefix = ('Capacityanchor%03d exact source.\r\n' % index).encode('ascii')
            output.write(prefix)
            expected.update(prefix)
            remaining = width - len(prefix)
            block = b' ' * (64 * 1024)
            while remaining:
                part = block[:min(remaining, len(block))]
                output.write(part)
                expected.update(part)
                remaining -= len(part)
    size = source.stat().st_size
    assert size > 128*MIB and size == count*width + count-1
    # Both ordinary intake and the old explicit profile retain their ceilings.
    with pytest.raises(ValueError, match='too large'):
        app.import_file(workspace, source)
    from carr_library.ingest.streamed_text import inspect_text
    with pytest.raises(ValueError):
        inspect_text(source, TextProfile())
    assert app.browse(workspace) == ()
    fragments = count * (width // FRAGMENT_CHARS)
    selected = budget(source_bytes=size, segment_bytes=FRAGMENT_CHARS,
        segments=fragments, steps=fragments+1, seconds=600,
        memory_bytes=3*1024**3, library_bytes=2*1024**3, temporary_bytes=256*MIB)
    profile = TextProfile(size, selected)
    result = app.import_file(workspace, source, title='Explicit large original', import_budget=selected)
    assert result['state'] == 'complete' and result['completed'] == fragments, result
    assert result['isolated'] is True and result['verified'] is True
    saved = worker.load_request(app.root, result['request_id'])
    assert saved['source_profile'] == profile.to_dict()
    assert saved['asset_sha256'] == expected.hexdigest() == _digest(source)
    retained = app.assets.resolve(saved['asset_sha256'], profile=profile)
    assert _digest(retained) == expected.hexdigest() and retained.stat().st_size == size
    hit, = app.search_page(workspace, 'Capacityanchor000', limit=5, timeout=60).results
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Capacityanchor000'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM page WHERE revision=?', (result['id'],)).fetchone()[0] == fragments
        assert db.execute('SELECT size FROM catalog_asset WHERE digest=?',
                          (saved['asset_sha256'],)).fetchone()[0] == size
        assert db.execute('SELECT count(*) FROM sealed WHERE revision=?', (result['id'],)).fetchone()[0] == 1
    assert (app.root / 'import-requests' / (result['request_id'] + '.json')).stat().st_size < 64_000
