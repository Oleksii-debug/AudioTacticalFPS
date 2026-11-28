"""Bounded real source walks and exact durable continuation authority."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import os
import stat

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_scan
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.batch_pages import validate_scan_relations
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.ingest.streamed_text import TextProfile


def sources(tmp_path, names=('c.txt', 'a.txt', 'b.txt')):
    root = tmp_path / 'originals'
    root.mkdir()
    for name in names:
        (root / name).write_text('Exact ' + name, encoding='utf-8')
    return root


def test_entry_windows_continue_without_losing_names_or_importing(tmp_path):
    root = sources(tmp_path, ('a.txt', 'b.txt', 'c.txt', 'd.txt'))
    cursor, found = None, []
    for _ in range(8):
        result = batch_scan.scan_page([root] if cursor is None else (),
            cursor=cursor, max_files=2, max_entries=2)
        assert len(result['rows']) <= 2
        found.extend(row['source'] for row in result['rows'])
        assert all(row['state'] == 'queued' for row in result['rows'])
        previous = cursor
        cursor = result['cursor']
        assert previous is None or cursor['visited'] > previous['visited'] or cursor['complete']
        if cursor['complete']:
            break
    assert cursor['complete'] and cursor['planned'] == 4
    assert found == [str(root / (name + '.txt')) for name in 'abcd']


def test_cancelled_hash_keeps_next_source_unadvanced_for_explicit_retry(tmp_path, monkeypatch):
    root = sources(tmp_path)
    actual = batch_scan.planned_snapshot
    calls, stopped = [], []
    def snapshot(path, **kwargs):
        calls.append(Path(path).name)
        result = actual(path, **kwargs)
        if Path(path).name == 'b.txt':
            stopped.append(True)
        return result
    monkeypatch.setattr(batch_scan, 'planned_snapshot', snapshot)
    first = batch_scan.scan_page([root], cancelled=lambda: bool(stopped))
    assert first['reason'] == 'cancelled'
    assert [row['file'] for row in first['rows']] == ['a.txt']
    assert calls == ['a.txt', 'b.txt']
    assert first['cursor']['stack'][-1]['after'] == 'a.txt'
    stopped.clear()
    monkeypatch.setattr(batch_scan, 'planned_snapshot', actual)
    second = batch_scan.scan_page(cursor=first['cursor'])
    assert [row['file'] for row in second['rows']] == ['b.txt', 'c.txt']
    assert second['cursor']['complete'] and second['cursor']['planned'] == 3


def test_live_directory_identity_refuses_replaced_continuation_before_open(tmp_path, monkeypatch):
    root = sources(tmp_path)
    first = batch_scan.scan_page([root], max_files=1)
    old = tmp_path / 'original-generation'
    root.rename(old)
    root.mkdir()
    (root / 'b.txt').write_bytes(b'Replacement must not be admitted')
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('A replaced continuation root opened a source.'))
    with pytest.raises(ValueError, match='changed'):
        batch_scan.scan_page(cursor=first['cursor'])
    assert (old / 'a.txt').read_bytes() == b'Exact a.txt'


@pytest.mark.parametrize('facet', ['hardlink', 'reparse'])
def test_page_rejects_linked_leaf_before_any_source_read(tmp_path, monkeypatch, facet):
    root = sources(tmp_path, ('a.txt',))
    source = root / 'a.txt'
    original = Path.lstat
    if facet == 'hardlink':
        alias = tmp_path / 'alias.txt'
        os.link(source, alias)
    else:
        def lstat(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == source:
                fields = {key: getattr(value, key) for key in dir(value) if key.startswith('st_')}
                fields['st_file_attributes'] = stat.FILE_ATTRIBUTE_REPARSE_POINT
                return SimpleNamespace(**fields)
            return value
        monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('Linked source reached a byte reader.'))
    result = batch_scan.scan_page([source])
    row, = result['rows']
    assert row['state'] == 'rejected' and row['manifest'] is None
    assert source.read_bytes() == b'Exact a.txt'


def test_paged_scandir_is_closed_when_cancellation_interrupts_lexical_selection(tmp_path, monkeypatch):
    root = sources(tmp_path)
    actual = batch_scan.os.scandir
    seen, closed = [], []
    class Entries:
        def __init__(self, path):
            self.inner = actual(path)
        def __enter__(self):
            return self
        def __iter__(self):
            return self
        def __next__(self):
            value = next(self.inner)
            seen.append(value.name)
            return value
        def __exit__(self, *error):
            self.inner.close()
            closed.append(True)
    monkeypatch.setattr(batch_scan.os, 'scandir', lambda path: Entries(path))
    result = batch_scan.scan_page([root], cancelled=lambda: len(seen) >= 2)
    assert result['reason'] == 'cancelled' and result['rows'] == ()
    assert closed == [True] and len(seen) == 2
    assert result['cursor']['stack'][-1]['after'] is None


def test_persisted_page_duplicate_is_bound_to_prior_exact_source_and_profile(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Paged originals')
    root = sources(tmp_path, ('a.txt', 'b.txt'))
    (root / 'b.txt').write_bytes((root / 'a.txt').read_bytes())
    policy = replace(ImportBudget(), segments=1000, steps=2)
    first = app.plan_import_page(workspace, [root], max_files=1, import_budget=policy)
    second = LibraryApp(app.root).continue_import_scan(workspace, first['batch_id'], max_files=1)
    assert second['version'] == 3 and second['source_profile'] == first['source_profile']
    row, = second['rows']
    assert row['state'] == 'duplicate'
    assert row['duplicate_of'] == 'batch:' + first['batch_id'] + ':' + first['rows'][0]['row_id']
    assert row['manifest'] == first['rows'][0]['manifest']
    validate_scan_relations({first['batch_id']: first, second['batch_id']: second})
    bad = deepcopy(second)
    bad['source_profile'] = TextProfile(64 * 1024**2, replace(policy, steps=3)).to_dict()
    with pytest.raises(ValueError, match='preceding|scope'):
        validate_scan_relations({first['batch_id']: first, bad['batch_id']: bad})
    assert app.browse(workspace) == ()


def test_restore_marker_keeps_saved_rows_but_never_reopens_historical_scan_roots(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Passive cursor')
    root = sources(tmp_path)
    page = app.plan_import_page(workspace, [root], max_files=1)
    store = ImportBatchStore(app)
    page['scan']['review_required'] = True
    store._store(page)
    monkeypatch.setattr(batch_scan, '_live_scan_frame',
        lambda *a: pytest.fail('A restored cursor opened its historical external root.'))
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('A restored cursor opened a source.'))
    with pytest.raises(ValueError, match='fresh explicit selection'):
        app.continue_import_scan(workspace, page['batch_id'])
    current = app.import_batch(workspace, page['batch_id'])
    assert current['rows'] == page['rows'] and current['scan']['review_required'] is True


@pytest.mark.parametrize('field,value', [
    ('root_index', True), ('visited', -1), ('planned', 2**63),
    ('complete', 'yes'), ('recursive', 1),
])
def test_malformed_saved_cursor_is_refused_purely_before_external_io(tmp_path, monkeypatch, field, value):
    root = sources(tmp_path)
    cursor = batch_scan.new_scan_cursor([root])
    cursor[field] = value
    monkeypatch.setattr(Path, 'lstat',
        lambda *a, **k: pytest.fail('Malformed cursor reached filesystem authority.'))
    with pytest.raises(ValueError):
        batch_scan.validate_scan_cursor(cursor)


def test_repeated_cancel_cannot_append_unadvanced_durable_pages(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('No automatic work')
    root = sources(tmp_path)
    first = app.plan_import_page(workspace, [root], cancelled=lambda: True)
    assert first['total'] == 0 and first['scan']['reason'] == 'admission'
    before = tuple((app.root / 'import-batches').glob('*.json'))
    with pytest.raises(InterruptedError, match='No scan progress'):
        app.continue_import_scan(workspace, first['batch_id'], cancelled=lambda: True)
    assert tuple((app.root / 'import-batches').glob('*.json')) == before
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('automatic', [False, True], ids=['manual', 'automatic'])
def test_paged_ocr_config_keeps_legacy_bytes_and_prepared_request_identity(tmp_path, automatic):
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, request_identity

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact reviewed OCR settings')
    root = sources(tmp_path, ('a.txt', 'b.txt'))
    chosen = Config(languages='eng+ukr', automatic_languages=automatic)
    expected = dict(languages='eng+ukr', dpi=300, timeout=30, max_pixels=20_000_000,
                    orientation=False, model_policy='default', model_profile='installed')
    if automatic:
        expected['automatic_languages'] = True
    legacy = app.plan_import_batch(workspace, [root], config=chosen)
    first = app.plan_import_page(workspace, [root], config=chosen, max_files=1)
    reopened = LibraryApp(app.root)
    second = reopened.continue_import_scan(workspace, first['batch_id'], max_files=1)
    assert legacy['config'] == first['config'] == second['config'] == expected
    assert ('automatic_languages' in first['config']) is automatic
    assert Config(**second['config']) == chosen
    assert second['scan']['previous'] == first['batch_id']
    validate_scan_relations({first['batch_id']: first, second['batch_id']: second})

    row, = first['rows']
    metadata = ImportBatchStore(reopened)._metadata(row, first)
    arguments = dict(request_id=row['request_id'], expected_manifest=row['manifest'])
    request = prepare_request(app.root, workspace, row['source'], metadata,
                              Config(**first['config']), **arguments)
    path = app.root / 'import-requests' / (row['request_id'] + '.json')
    before = path.read_bytes()
    repeated = prepare_request(app.root, workspace, row['source'], metadata, chosen, **arguments)
    assert request['config'] == repeated['config'] == expected
    assert request_identity(repeated) == request_identity(request)
    assert path.read_bytes() == before
    changed = replace(chosen, automatic_languages=not automatic)
    with pytest.raises(ValueError, match='planned identity'):
        prepare_request(app.root, workspace, row['source'], metadata, changed, **arguments)
    assert path.read_bytes() == before
    assert reopened.import_batch(workspace, first['batch_id'])['config'] == expected
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('guard', ['journals', 'entries', 'time'])
def test_cancelled_admission_still_checks_the_complete_private_inventory(tmp_path, monkeypatch, guard):
    from carr_library.ingest import batch_pages, batches

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bounded cancelled admission')
    root = sources(tmp_path)
    first = app.plan_import_page(workspace, [root], max_files=1)
    directory = app.root / 'import-batches'
    originals = {path.name: path.read_bytes() for path in directory.glob('*.json')}
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('A cancelled admission opened another source.'))
    if guard == 'journals':
        monkeypatch.setattr(batches, 'MAX_BATCHES', 1)
        message = 'saved import-journal allowance is full'
    elif guard == 'entries':
        monkeypatch.setattr(batches, 'MAX_BATCHES', 2)
        for number in range(41):
            (directory / ('private-' + str(number))).write_bytes(b'')
        message = 'bounded inventory'
    else:
        ticks = iter((0.0, 2.0))
        monkeypatch.setattr(batch_pages, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
        message = 'selected time allowance'
    with pytest.raises((ValueError, InterruptedError), match=message):
        app.plan_import_page(workspace, [root], cancelled=lambda: True, timeout=1)
    assert {path.name: path.read_bytes() for path in directory.glob('*.json')} == originals
    assert app.import_batch(workspace, first['batch_id'])['rows'] == first['rows']
    assert app.browse(workspace) == ()


def test_inventory_cancellation_remains_latched_until_inert_admission(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('One observed cancellation')
    root = sources(tmp_path)
    observed = []
    def cancelled():
        observed.append(True)
        return len(observed) == 1
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('A latched cancellation reached source bytes.'))
    result = app.plan_import_page(workspace, [root], cancelled=cancelled)
    assert observed and result['total'] == 0 and result['scan']['reason'] == 'admission'
    assert result['scan']['cursor']['visited'] == result['scan']['cursor']['planned'] == 0
    assert len(tuple((app.root / 'import-batches').glob('*.json'))) == 1
    assert app.browse(workspace) == ()


def test_inventory_cancellation_callback_failure_never_creates_a_journal(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Callback failure is not cancellation')
    root = sources(tmp_path)
    expected = RuntimeError('Caller cancellation observer failed.')
    def cancelled():
        raise expected
    monkeypatch.setattr(batch_scan, 'planned_snapshot',
        lambda *a, **k: pytest.fail('A failed cancellation observer reached source bytes.'))
    with pytest.raises(RuntimeError) as failure:
        app.plan_import_page(workspace, [root], cancelled=cancelled)
    assert failure.value is expected
    assert tuple((app.root / 'import-batches').glob('*.json')) == ()
    assert app.browse(workspace) == ()
