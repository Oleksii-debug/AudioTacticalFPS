"""Cancellation stops the selected-file loop before opening further originals."""
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.progress import operation_outcome


def test_cancel_after_committed_text_leaves_next_original_unopened(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected files')
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('Published first railway source.', encoding='utf-8')
    second.write_text('Unattempted second railway source.', encoding='utf-8')
    originals = {path: path.read_bytes() for path in (first, second)}
    attempted = []
    import_file = app.import_file
    def tracked(scope, path, **options):
        attempted.append(Path(path))
        assert Path(path) != second, 'The next original must not be opened after cancellation.'
        return import_file(scope, path, **options)
    monkeypatch.setattr(app, 'import_file', tracked)
    results = app.import_files(workspace, [first, second],
                               cancelled=lambda: bool(app.browse(workspace)))
    assert [row['state'] for row in results] == ['complete', 'cancelled']
    assert results[1]['file'] == second.name and 'before processing' in results[1]['message']
    assert operation_outcome(results) == 'cancelled'
    assert attempted == [first]
    assert {path: path.read_bytes() for path in originals} == originals
    assert app.assets.resolve(results[0]['asset']).read_bytes() == originals[first]
    assert len(list(app.assets_dir.iterdir())) == 1
    reopened = LibraryApp(app.root)
    assert len(reopened.browse(workspace)) == 1
    assert len(reopened.search(workspace, 'Published')) == 1


def test_precancelled_selection_opens_no_source(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Before selected files')
    def forbidden(*args, **kwargs):
        raise AssertionError('A cancelled selection must not open another source.')
    monkeypatch.setattr(app, 'import_file', forbidden)
    results = app.import_files(workspace, [tmp_path / 'one.txt', tmp_path / 'two.txt'],
                               cancelled=lambda: True)
    assert len(results) == 1 and results[0]['state'] == 'cancelled'
    assert results[0]['file'] == 'one.txt' and 'before processing' in results[0]['message']
    assert operation_outcome(results) == 'cancelled'
    assert list(app.assets_dir.iterdir()) == []
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('count', [1, 2])
def test_cancel_after_every_selected_file_committed_keeps_success(tmp_path, monkeypatch, count):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Late stop after completion')
    paths = [tmp_path / f'source-{index}.txt' for index in range(count)]
    for index, path in enumerate(paths):
        path.write_text(f'Already committed railway source number {index}.', encoding='utf-8')
    stop, attempted = Event(), []
    import_file = app.import_file
    def finish_then_cancel(scope, path, **options):
        result = import_file(scope, path, **options)
        assert result['state'] == 'complete'
        attempted.append(Path(path))
        if len(attempted) == count:
            stop.set()
        return result
    monkeypatch.setattr(app, 'import_file', finish_then_cancel)
    results = app.import_files(workspace, paths, cancelled=stop.is_set)
    assert stop.is_set() and attempted == paths
    assert [row['state'] for row in results] == ['complete'] * count
    assert operation_outcome(results) == 'succeeded'
    assert len(LibraryApp(app.root).browse(workspace)) == count


def test_interrupted_error_is_acknowledged_cancellation_and_stops_selection(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Interrupted selection')
    attempted = []
    def interrupted(scope, path, **options):
        attempted.append(Path(path).name)
        raise InterruptedError('The import acknowledged cancellation.')
    monkeypatch.setattr(app, 'import_file', interrupted)
    results = app.import_files(workspace, [tmp_path / 'one.txt', tmp_path / 'two.txt'])
    assert attempted == ['one.txt']
    assert len(results) == 1 and results[0]['state'] == 'cancelled'


def test_cancelled_receipt_stops_even_if_later_callback_is_clear(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Acknowledged worker stop')
    attempted = []
    def stopped(scope, path, **options):
        attempted.append(Path(path).name)
        return {'state': 'cancelled', 'message': 'Worker stopped.'}
    monkeypatch.setattr(app, 'import_file', stopped)
    results = app.import_files(workspace, [tmp_path / 'one.txt', tmp_path / 'two.txt'],
                               cancelled=lambda: False)
    assert attempted == ['one.txt']
    assert len(results) == 1 and results[0]['state'] == 'cancelled'


def test_ordinary_file_error_does_not_cancel_later_valid_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recoverable file error')
    valid = tmp_path / 'valid.txt'
    valid.write_text('A valid railway source after a missing file.', encoding='utf-8')
    results = app.import_files(workspace, [tmp_path / 'missing.txt', valid])
    assert [row['state'] for row in results] == ['failed', 'complete']
    assert len(app.browse(workspace)) == 1
