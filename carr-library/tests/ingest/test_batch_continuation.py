"""Actual-file scan pages, ordinary queue reuse and passive backup recovery."""
from copy import deepcopy
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import carr_library
from bootstrap.vendor.cairn_extractors import hasher as bootstrap_hasher
from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan, batch_pages
from carr_library.ingest.batches import ImportBatchStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations import ResourceBroker


def write(folder, name, data=None):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else ('Exact source ' + name + '.\n').encode('utf-8'))
    return path


@pytest.fixture
def peer(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit paged intake')
    folder = tmp_path / 'selected'
    folder.mkdir()
    return app, workspace, folder, ImportBatchStore(app)


def environment():
    roots = (Path(carr_library.__file__).resolve().parents[1],
             Path(bootstrap_hasher.__file__).resolve().parents[3])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, dict.fromkeys(roots))))


def child(program, *arguments, timeout=180):
    return subprocess.run([sys.executable, '-B', '-s', '-P', '-X', 'utf8', '-c', program,
        *map(str, arguments)], env=environment(), capture_output=True, text=True,
        encoding='utf-8', errors='strict', timeout=timeout)


def test_more_than_5000_actual_sources_continue_after_process_restart(peer, monkeypatch):
    app, workspace, folder, store = peer
    count = 5007
    for number in range(count):
        write(folder, f'{number:05d}.txt')
    first = store.plan_page(workspace, [folder], max_files=5000, timeout=120, max_bytes=8 * 1024**2)
    assert first['version'] == 2 and first['total'] == 5000
    assert not first['scan']['cursor']['complete'] and first['scan']['cursor']['planned'] == 5000
    assert app.browse(workspace) == ()
    assert not (app.root / 'imports.sqlite').exists()
    program = '''
import json,sys
from carr_library.application import LibraryApp
app=LibraryApp(sys.argv[1])
result=app.continue_import_scan(sys.argv[2],sys.argv[3],max_files=5000,timeout=120,max_bytes=8*1024**2)
print(json.dumps(result,ensure_ascii=False))
'''
    completed = child(program, app.root, workspace, first['batch_id'])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    second = json.loads(completed.stdout)
    assert second['total'] == 7 and second['scan']['cursor']['complete']
    assert second['scan']['cursor']['planned'] == count
    assert first['scan']['next'] == second['batch_id']
    all_rows = first['rows'] + second['rows']
    assert [row['file'] for row in all_rows] == [f'{number:05d}.txt' for number in range(count)]
    assert len({row['request_id'] for row in all_rows}) == count
    for row in all_rows:
        expected = ('Exact source ' + row['file'] + '.\n').encode('utf-8')
        assert row['manifest'] == {'sha256': sha256(expected).hexdigest(), 'mime': 'text/plain', 'size': len(expected)}
        assert row['state'] == 'queued'
    monkeypatch.setattr(batch_pages, 'scan_page', lambda *a, **k: pytest.fail('Published next page was rescanned.'))
    reopened = ImportBatchStore(LibraryApp(app.root))
    reused = reopened.continue_plan(workspace, first['batch_id'], max_files=2)
    assert reused == second
    assert len(reopened.list(workspace)) == 2
    assert app.browse(workspace) == () and ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_cancelled_source_review_saves_prefix_without_advancing_unaccepted_file(peer, monkeypatch):
    app, workspace, folder, store = peer
    for name in ('a.txt', 'b.txt', 'c.txt', 'd.txt'):
        write(folder, name)
    original, opened, stopped = batch_scan.planned_snapshot, [], threading.Event()
    def selected(path, **kwargs):
        value = original(path, **kwargs)
        opened.append(Path(path).name)
        if len(opened) == 3:
            stopped.set()
        return value
    monkeypatch.setattr(batch_scan, 'planned_snapshot', selected)
    first = store.plan_page(workspace, [folder], cancelled=stopped.is_set)
    assert [row['file'] for row in first['rows']] == ['a.txt', 'b.txt']
    assert opened == ['a.txt', 'b.txt', 'c.txt']
    assert first['scan']['reason'] == 'cancelled'
    assert first['scan']['cursor']['stack'][-1]['after'] == 'b.txt'
    assert first['state'] == 'paused'
    stopped.clear()
    monkeypatch.setattr(batch_scan, 'planned_snapshot', original)
    second = ImportBatchStore(LibraryApp(app.root)).continue_plan(workspace, first['batch_id'])
    assert [row['file'] for row in second['rows']] == ['c.txt', 'd.txt']
    assert second['scan']['cursor']['complete'] and second['scan']['cursor']['planned'] == 4
    assert app.browse(workspace) == ()


def test_scan_lease_survives_through_durable_page_publication(peer, monkeypatch):
    app, workspace, folder, store = peer
    write(folder, 'one.txt')
    original, observations = store._store, []
    def save(record, **options):
        snapshot = ResourceBroker.for_app(app.root).snapshot()
        observations.append(snapshot)
        assert snapshot['usage']['cpu_slots'] == 1
        assert len(snapshot['leases']) == 1
        return original(record, **options)
    monkeypatch.setattr(store, '_store', save)
    saved = store.plan_page(workspace, [folder])
    assert observations and saved['counts']['queued'] == 1
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_changed_active_directory_refuses_continuation_before_any_new_source_read(peer, monkeypatch):
    app, workspace, folder, store = peer
    for name in ('a.txt', 'b.txt', 'c.txt'):
        write(folder, name)
    first = store.plan_page(workspace, [folder], max_files=1)
    journal = app.root / 'import-batches' / (first['batch_id'] + '.json')
    before = journal.read_bytes()
    write(folder, 'new.txt')
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a, **k: pytest.fail('Changed scan roots were opened.'))
    with pytest.raises(ValueError, match='directory changed'):
        store.continue_plan(workspace, first['batch_id'])
    assert journal.read_bytes() == before
    assert not (journal.parent / (first['scan']['next'] + '.json')).exists()
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_actual_child_death_after_next_page_commit_reuses_exact_row_identities(peer, monkeypatch):
    app, workspace, folder, store = peer
    write(folder, 'a.txt'); write(folder, 'b.txt')
    first = store.plan_page(workspace, [folder], max_files=1)
    program = '''
import os,sys
from carr_library.application import LibraryApp
from carr_library.ingest.batches import ImportBatchStore
app=LibraryApp(sys.argv[1])
store=ImportBatchStore(app)
save=store._store
def commit(record,**options):
    save(record,**options)
    if record['version']==2 and record['scan']['page']==1:
        os._exit(23)
store._store=commit
store.continue_plan(sys.argv[2],sys.argv[3],max_files=1)
'''
    terminated = child(program, app.root, workspace, first['batch_id'])
    assert terminated.returncode == 23, terminated.stdout + terminated.stderr
    path = app.root / 'import-batches' / (first['scan']['next'] + '.json')
    before = path.read_bytes()
    expected = json.loads(before)
    monkeypatch.setattr(batch_pages, 'scan_page', lambda *a, **k: pytest.fail('A committed next page was duplicated.'))
    recovered = store.continue_plan(workspace, first['batch_id'])
    assert recovered == expected and path.read_bytes() == before
    assert recovered['rows'][0]['file'] == 'b.txt'
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []
    assert app.browse(workspace) == ()


def test_entry_allowance_continues_empty_directories_and_files_without_skipping(peer):
    app, workspace, folder, store = peer
    for number in range(5):
        (folder / f'{number:02d}-empty').mkdir()
        write(folder / f'{number:02d}-full', 'source.txt', f'Unique original {number}.'.encode())
    page = store.plan_page(workspace, [folder], max_entries=1)
    assert page['total'] == 0 and page['state'] == 'paused'
    rows, pages = [], 0
    while not page['scan']['cursor']['complete']:
        page = store.continue_plan(workspace, page['batch_id'], max_entries=3)
        rows.extend(page['rows'])
        pages += 1
        assert pages < 20
    assert [row['file'] for row in rows] == [f'{number:02d}-full/source.txt' for number in range(5)]
    assert all(row['state'] == 'queued' for row in rows)
    assert len({row['request_id'] for row in rows}) == 5 and app.browse(workspace) == ()


def test_cross_page_duplicate_reference_is_scoped_and_can_be_explicitly_included(peer):
    app, workspace, folder, store = peer
    write(folder, 'a.txt', b'The same selected original.')
    write(folder, 'b.txt', b'The same selected original.')
    first = store.plan_page(workspace, [folder], max_files=1)
    second = store.continue_plan(workspace, first['batch_id'], max_files=1)
    duplicate, = second['rows']
    assert duplicate['state'] == 'duplicate'
    assert duplicate['duplicate_of'] == 'batch:' + first['batch_id'] + ':' + first['rows'][0]['row_id']
    first = store.set_row(workspace, first['batch_id'], first['rows'][0]['row_id'], included=False)
    selected = store.set_row(workspace, second['batch_id'], duplicate['row_id'], included=True)
    assert selected['rows'][0]['state'] == 'queued' and selected['rows'][0]['duplicate_of'] is None
    result = store.run(workspace, second['batch_id'])
    assert result['counts']['complete'] == 1
    assert result['rows'][0]['result']['isolated'] and result['rows'][0]['result']['verified']
    assert len(app.browse(workspace)) == 1
    assert store.get(workspace, first['batch_id'])['counts']['excluded'] == 1


def test_source_manifest_is_rechecked_by_isolated_import_before_publication(peer, monkeypatch):
    app, workspace, folder, store = peer
    selected = write(folder, 'source.txt', b'Original reviewed bytes.')
    first = store.plan_page(workspace, [selected])
    retained_before = tuple(sorted(path.name for path in app.assets_dir.iterdir()))
    selected.write_bytes(b'Changed outside the plan.')
    # Preparation only persists identities; current imports authenticate the
    # actual bytes inside the admitted worker before retention or publication.
    original_resume, attempts = app.resume_import, []
    def resume(workspace_id, request_id, **options):
        attempts.append((workspace_id, request_id))
        return original_resume(workspace_id, request_id, **options)
    monkeypatch.setattr(app, 'resume_import', resume)
    refused = store.run(workspace, first['batch_id'])
    assert attempts == [(workspace, first['rows'][0]['request_id'])]
    assert refused['counts']['failed'] == 1 and app.browse(workspace) == ()
    assert refused['rows'][0]['result']['isolated'] is True
    assert 'Source changed after import planning' in refused['rows'][0]['message']
    assert tuple(sorted(path.name for path in app.assets_dir.iterdir())) == retained_before
    assert selected.read_bytes() == b'Changed outside the plan.'
    assert refused['rows'][0]['manifest'] == first['rows'][0]['manifest']


def test_actual_backup_restores_pages_but_revokes_external_cursor_authority(peer, tmp_path, monkeypatch):
    app, workspace, folder, store = peer
    for name in ('a.txt', 'b.txt', 'c.txt'):
        write(folder, name, b'Exact retained original for passive scan backup.')
    first = store.plan_page(workspace, [folder], max_files=1)
    second = store.continue_plan(workspace, first['batch_id'], max_files=1)
    backup = tmp_path / 'saved'
    digest = create_backup(app, backup)
    original_rows = [deepcopy(first['rows']), deepcopy(second['rows'])]
    shutil.rmtree(app.root); shutil.rmtree(folder)
    restored_root = restore_backup(backup, tmp_path / 'restored', digest)
    restored_app = LibraryApp(restored_root)
    restored = ImportBatchStore(restored_app)
    before = restored.get(workspace, first['batch_id'])
    after = restored.get(workspace, second['batch_id'])
    assert before['scan']['review_required'] and after['scan']['review_required']
    assert before['scan']['cursor'] == first['scan']['cursor']
    assert after['rows'][0]['duplicate_of'] == second['rows'][0]['duplicate_of']
    for old_rows, page in zip(original_rows, (before, after)):
        assert [row['request_id'] for row in page['rows']] == [row['request_id'] for row in old_rows]
        assert [row['manifest'] for row in page['rows']] == [row['manifest'] for row in old_rows]
        assert all(Path(row['source']).is_relative_to(restored_root) for row in page['rows'])
    monkeypatch.setattr(batch_pages, 'scan_page', lambda *a, **k: pytest.fail('Restored external cursor executed.'))
    with pytest.raises(ValueError, match='fresh explicit selection'):
        restored.continue_plan(workspace, second['batch_id'])
    assert restored_app.browse(workspace) == ()
    completed = restored.run(workspace, first['batch_id'])
    assert completed['counts']['complete'] == 1
    assert completed['rows'][0]['result']['isolated'] and completed['rows'][0]['result']['verified']
    assert restored.run(workspace, second['batch_id'])['counts']['duplicate'] == 1
    assert len(restored_app.browse(workspace)) == 1


def test_foreign_scan_duplicate_reference_cannot_pass_typed_backup(peer, tmp_path):
    app, workspace, folder, store = peer
    write(folder, 'a.txt', b'Shared exact bytes.')
    write(folder, 'b.txt', b'Shared exact bytes.')
    first = store.plan_page(workspace, [folder], max_files=1)
    second = store.continue_plan(workspace, first['batch_id'], max_files=1)
    foreign = store.plan_page(workspace, [folder / 'a.txt'])
    second['rows'][0]['duplicate_of'] = 'batch:' + foreign['batch_id'] + ':' + foreign['rows'][0]['row_id']
    with store._lock(second['batch_id']):
        store._store(second)
    with pytest.raises(ValueError, match='another scan'):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()
    assert (folder / 'a.txt').read_bytes() == b'Shared exact bytes.'


def test_cancelled_before_admission_does_not_open_sources_and_keeps_inert_cursor(peer, monkeypatch):
    app, workspace, folder, store = peer
    source = write(folder, 'a.txt')
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a, **k: pytest.fail('Cancelled scan read an original.'))
    saved = store.plan_page(workspace, [source], cancelled=lambda: True)
    assert saved['total'] == 0 and saved['state'] == 'paused'
    assert saved['scan']['cursor']['root_index'] == 0 and saved['scan']['reason'] == 'admission'
    assert app.browse(workspace) == () and ResourceBroker.for_app(app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('mutation', ['extra', 'outside', 'counter', 'completion'])
def test_invalid_cursor_refuses_before_any_directory_or_source_read(peer, monkeypatch, mutation):
    app, workspace, folder, store = peer
    write(folder, 'a.txt'); write(folder, 'b.txt')
    first = store.plan_page(workspace, [folder], max_files=1)
    cursor = deepcopy(first['scan']['cursor'])
    if mutation == 'extra':
        cursor['execute'] = True
    elif mutation == 'outside':
        cursor['stack'][0]['path'] = str(app.root)
    elif mutation == 'counter':
        cursor['planned'] = True
    else:
        cursor['complete'] = True
    monkeypatch.setattr(batch_scan.os, 'scandir', lambda *a, **k: pytest.fail('Malformed cursor opened a directory.'))
    monkeypatch.setattr(batch_scan, 'planned_snapshot', lambda *a, **k: pytest.fail('Malformed cursor opened an original.'))
    with pytest.raises(ValueError):
        batch_scan.scan_page(cursor=cursor)


def test_cursor_validation_is_passive_for_foreign_platform_selection(monkeypatch):
    cursor = {'schema': 'carr-import-scan/1', 'roots': [r'C:\Explicit source'], 'recursive': True,
        'root_index': 0, 'stack': [], 'visited': 0, 'planned': 0, 'complete': False}
    monkeypatch.setattr(Path, 'lstat', lambda *a, **k: pytest.fail('Passive cursor validation inspected external paths.'))
    assert batch_scan.validate_scan_cursor(cursor) is cursor
    assert len(batch_scan.scan_cursor_digest(cursor)) == 64



@pytest.mark.parametrize('admission_elapsed,expected', [(3.0, 7.0), (9.98, None)])
def test_actual_admission_consumes_page_budget_before_any_source_scan(peer, monkeypatch, admission_elapsed, expected):
    app, workspace, folder, store = peer
    write(folder, 'source.txt')
    clock = [100.0]
    monkeypatch.setattr(batch_pages, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    reserve = ResourceBroker.reserve
    @contextmanager
    def admitted(self, *args, **kwargs):
        # Retain the actual interprocess resource lease. Only the supervisor's
        # elapsed-time observation is deterministic; the reservation is real.
        with reserve(self, *args, **kwargs) as lease:
            assert self.snapshot()['usage']['cpu_slots'] == 1
            clock[0] += admission_elapsed
            yield lease
    monkeypatch.setattr(ResourceBroker, 'reserve', admitted)
    scan, budgets = batch_pages.scan_page, []
    def bounded(*args, **kwargs):
        budgets.append(kwargs['timeout'])
        if expected is None:
            pytest.fail('Exhausted admission budget opened a source scan.')
        assert kwargs['timeout'] == pytest.approx(expected)
        return scan(*args, **kwargs)
    monkeypatch.setattr(batch_pages, 'scan_page', bounded)
    saved = store.plan_page(workspace, [folder], timeout=10)
    assert saved['scan']['limits']['timeout'] == 10.0
    if expected is None:
        assert budgets == [] and saved['rows'] == []
        assert saved['scan']['reason'] == 'time'
        assert saved['scan']['cursor']['root_index'] == 0
    else:
        assert budgets == [pytest.approx(expected)]
        assert saved['rows'][0]['manifest']['sha256'] == sha256((folder / 'source.txt').read_bytes()).hexdigest()
    assert app.browse(workspace) == ()
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []
