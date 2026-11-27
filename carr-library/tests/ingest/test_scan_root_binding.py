"""Import-page operations stay bound to the admitted library and journal directory."""
from contextlib import contextmanager

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import batch_pages
from carr_library.ingest.batches import ImportBatchStore
from carr_library.operations import ResourceBroker


def fixture(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bound source scan')
    selected = tmp_path / 'selected'
    selected.mkdir()
    (selected / 'one.txt').write_text('Exact first source.', encoding='utf-8')
    (selected / 'two.txt').write_text('Exact second source.', encoding='utf-8')
    return app, workspace, selected, ImportBatchStore(app)


def test_recreated_library_path_refuses_before_creating_a_replacement_queue(tmp_path, monkeypatch):
    app, workspace, selected, store = fixture(tmp_path)
    old_root = tmp_path / 'old-library'
    app.root.rename(old_root)
    app.root.mkdir()
    monkeypatch.setattr(batch_pages, 'scan_page',
                        lambda **options: pytest.fail('A stale library opened a source page.'))
    with pytest.raises(ValueError, match='library folder changed'):
        store.plan_page(workspace, [selected])
    assert not (app.root / 'import-batches').exists()
    assert not (app.root / 'library.sqlite').exists()
    assert (old_root / 'library.sqlite').is_file()


def test_retargeted_library_during_real_admission_refuses_source_scan_and_releases_lease(tmp_path, monkeypatch):
    app, workspace, selected, store = fixture(tmp_path)
    root = app.root
    replacement = LibraryApp(tmp_path / 'replacement').root
    reserve = ResourceBroker.reserve

    @contextmanager
    def admitted(self, *args, **kwargs):
        with reserve(self, *args, **kwargs) as lease:
            assert self.snapshot()['usage']['cpu_slots'] == 1
            app.root = replacement
            yield lease

    monkeypatch.setattr(ResourceBroker, 'reserve', admitted)
    monkeypatch.setattr(batch_pages, 'scan_page',
                        lambda **options: pytest.fail('Retargeted admission opened source data.'))
    try:
        with pytest.raises(ValueError, match='library folder changed'):
            store.plan_page(workspace, [selected])
    finally:
        app.root = root
    assert list((root / 'import-batches').glob('*.json')) == []
    assert not (replacement / 'import-batches').exists()
    assert ResourceBroker.for_app(root).snapshot()['leases'] == []


def test_retargeted_library_at_publication_preserves_sources_without_a_saved_page(tmp_path, monkeypatch):
    app, workspace, selected, store = fixture(tmp_path)
    root = app.root
    replacement = LibraryApp(tmp_path / 'replacement').root
    original = store._store
    saved = []

    def publish(record, **options):
        assert record['rows'] and ResourceBroker.for_app(root).snapshot()['leases']
        saved.append(record['batch_id'])
        app.root = replacement
        return original(record, **options)

    monkeypatch.setattr(store, '_store', publish)
    try:
        with pytest.raises(ValueError, match='library folder changed'):
            store.plan_page(workspace, [selected], max_files=1)
    finally:
        app.root = root
    assert len(saved) == 1
    assert list((root / 'import-batches').glob('*.json')) == []
    assert not (replacement / 'import-batches').exists()
    assert (selected / 'one.txt').read_text(encoding='utf-8') == 'Exact first source.'
    assert ResourceBroker.for_app(root).snapshot()['leases'] == []


def test_byte_identical_replacement_journal_directory_cannot_continue_old_store(tmp_path, monkeypatch):
    app, workspace, selected, store = fixture(tmp_path)
    first = store.plan_page(workspace, [selected], max_files=1)
    assert not first['scan']['cursor']['complete']
    directory = app.root / 'import-batches'
    saved = (directory / (first['batch_id'] + '.json')).read_bytes()
    previous = app.root / 'old-import-batches'
    directory.rename(previous)
    directory.mkdir()
    (directory / (first['batch_id'] + '.json')).write_bytes(saved)
    monkeypatch.setattr(batch_pages, 'scan_page',
                        lambda **options: pytest.fail('A replaced journal directory continued scanning.'))
    with pytest.raises(ValueError, match='queue directory changed'):
        store.continue_plan(workspace, first['batch_id'])
    assert sorted(item.name for item in directory.iterdir()) == [first['batch_id'] + '.json']
    assert (directory / (first['batch_id'] + '.json')).read_bytes() == saved
    assert app.browse(workspace) == ()
