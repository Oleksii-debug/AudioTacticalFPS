"""Cancellation stops the selected-file loop before opening further originals."""
from pathlib import Path

from carr_library.application import LibraryApp


def test_cancel_after_committed_text_leaves_next_original_unopened(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected files')
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('Published first railway source.', encoding='utf-8')
    second.write_text('Unattempted second railway source.', encoding='utf-8')
    results = app.import_files(workspace, [first, second],
                               cancelled=lambda: bool(app.browse(workspace)))
    assert len(results) == 1 and results[0]['state'] == 'complete'
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
    assert app.import_files(workspace, [tmp_path / 'one.txt'], cancelled=lambda: True) == ()
    assert list(app.assets_dir.iterdir()) == []


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
