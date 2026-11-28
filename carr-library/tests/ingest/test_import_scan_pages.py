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
