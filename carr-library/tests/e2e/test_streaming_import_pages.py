"""Real public imports and explicit continuation, with unchanged source authority."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import IntakeError
from carr_library.ingest import worker
from carr_library.ingest.resource_budget import ImportBudget, MIB


def budget(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=MIB,
        segments=1000, steps=1001, seconds=120, memory_bytes=512*MIB,
        library_bytes=64*MIB, temporary_bytes=128*MIB), **changes)


def _rows_identity(record):
    return tuple(tuple(row[key] for key in ('row_id','request_id','work_id','edition_id'))
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
    source.write_bytes(raw.encode())
    selected = budget(steps=2)
    first = app.import_file(workspace, source, title='Retained original', import_budget=selected)
    assert first['state'] == 'pending' and first['completed'] == 2, first
    assert first['isolated'] is True
    request = worker.load_request(app.root, first['request_id'])
    assert request['version'] == 2 and request['import_budget'] == selected.to_dict()
    assert request['job'] == first['id'] and request['asset_sha256'] == sha256(raw.encode()).hexdigest()
    retained = app.assets.resolve(request['asset_sha256'])
    assert retained.read_bytes() == source.read_bytes()
    old_request = (app.root / 'import-requests' / (first['request_id'] + '.json')).read_bytes()
    with pytest.raises(ValueError, match='exact reviewed'):
        app.resume_import(workspace, first['request_id'], import_budget=replace(selected, steps=3))
    assert (app.root / 'import-requests' / (first['request_id'] + '.json')).read_bytes() == old_request
    source.unlink()
    final = LibraryApp(app.root).resume_import(workspace, first['request_id'])
    assert final['state'] == 'complete' and final['completed'] == 3, final
    assert final['id'] == first['id'] and final['request_id'] == first['request_id']
    assert final['verified'] is True and retained.read_bytes() == raw.encode()
    hit, = app.search(workspace, 'Third exact source')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third exact source'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
    with sqlite3.connect(app.root / 'imports.sqlite') as journal:
        assert journal.execute('SELECT count(*) FROM job').fetchone()[0] == 1
        assert journal.execute('SELECT count(*) FROM checkpoint').fetchone()[0] == 3


def test_one_current_worker_slice_preserves_each_book_and_explicit_max_items(tmp_path, monkeypatch):
    from carr_library.audio import process
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('One bounded worker')
    paths = []
    for index in range(3):
        path = tmp_path / ('book-%d.txt' % index)
        path.write_text('Book %d first.\fBook %d second.\fBook %d third.' %
                        (index, index, index), encoding='utf-8')
        paths.append(path)
    plan = app.plan_import_page(workspace, paths, import_budget=budget(steps=2))
    assert plan['version'] == 3 and plan['total'] == 3
    assert plan['scan']['cursor']['complete'] is True
    identities = _rows_identity(plan)
    actual_run = process.run_process
    launches = []

    def observe(args, **options):
        assert options['owned_tree'] is True
        launches.append(tuple(args))
        return actual_run(args, **options)

    monkeypatch.setattr(process, 'run_process', observe)
    first = app.run_import_batch(workspace, plan['batch_id'], max_items=2)
    assert len(launches) == 1
    assert _rows_identity(first) == identities
    assert first['rows'][0]['result']['state'] == 'pending'
    assert first['rows'][0]['state'] == 'queued'
    assert first['rows'][1]['state'] == first['rows'][2]['state'] == 'queued'
    assert first['rows'][2]['result'] is None
    current = first
    for _ in range(6):
        if current['state'] == 'complete':
            break
        current = LibraryApp(app.root).run_import_batch(workspace, plan['batch_id'], max_items=2)
    assert current['state'] == 'complete' and current['counts']['complete'] == 3, current
    assert _rows_identity(current) == identities
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 3
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 3
    for index, row in enumerate(current['rows']):
        assert row['result']['isolated'] and row['result']['verified']
        assert app.assets.resolve(row['manifest']['sha256']).read_bytes() == paths[index].read_bytes()


def test_actual_5001_sources_continue_in_bounded_inert_pages(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('More than one scan window')
    folder = tmp_path / 'selected'
    folder.mkdir()
    expected = {}
    for index in range(5001):
        path = folder / ('book-%05d.txt' % index)
        data = ('Original numbered source %05d.\r\n' % index).encode()
        path.write_bytes(data)
        expected[str(path)] = sha256(data).hexdigest()

    def forbidden(*args, **kwargs):
        raise AssertionError('Planning must not run an import worker.')
    monkeypatch.setattr(worker, '_invoke', forbidden)
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


def test_explicit_large_original_uses_real_public_pipeline_and_exact_copy(tmp_path):
    """Byte capacity proof, not a large-corpus or language-quality benchmark."""
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large retained original')
    source = tmp_path / 'large-original.txt'
    expected, count, width = sha256(), 17, 4*MIB
    with source.open('xb') as output:
        for index in range(count):
            if index:
                output.write(b'\f')
                expected.update(b'\f')
            prefix = ('Capacityanchor%03d exact Ω.\r\n' % index).encode()
            output.write(prefix)
            expected.update(prefix)
            remaining = width - len(prefix)
            block = b' ' * MIB
            while remaining:
                part = block[:min(remaining, len(block))]
                output.write(part)
                expected.update(part)
                remaining -= len(part)
    size = source.stat().st_size
    assert size > 64*MIB and size == count*width + count-1
    with pytest.raises(IntakeError):
        app.import_file(workspace, source)
    assert app.browse(workspace) == ()
    selected = budget(source_bytes=size, segment_bytes=width, segments=count, steps=count+1,
                      seconds=300, library_bytes=1024*MIB, temporary_bytes=256*MIB)
    result = app.import_file(workspace, source, title='Explicit large original', import_budget=selected)
    assert result['state'] == 'complete' and result['completed'] == count, result
    assert result['isolated'] is True and result['verified'] is True
    saved = worker.load_request(app.root, result['request_id'])
    assert saved['import_budget'] == selected.to_dict()
    assert saved['asset_sha256'] == expected.hexdigest() == _digest(source)
    retained = app.assets.resolve(saved['asset_sha256'], max_bytes=size)
    assert _digest(retained) == expected.hexdigest() and retained.stat().st_size == size
    hit, = app.search(workspace, 'Capacityanchor016')
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Capacityanchor016'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM page WHERE revision=?', (result['id'],)).fetchone()[0] == count
        assert db.execute('SELECT size FROM catalog_asset WHERE digest=?',
                          (saved['asset_sha256'],)).fetchone()[0] == size
    assert (app.root / 'import-requests' / (result['request_id'] + '.json')).stat().st_size < 64_000
