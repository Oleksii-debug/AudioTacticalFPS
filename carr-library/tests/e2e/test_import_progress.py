"""Measured batch results from real local TXT imports, with no GUI doubles."""
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.progress import operation_outcome


def sources(tmp_path, count=3):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Measured imports')
    paths = []
    for index in range(count):
        path = tmp_path / f'book-{index}.txt'
        path.write_text(f'Original railway source number {index}.', encoding='utf-8')
        paths.append(path)
    return app, workspace, paths


def test_multi_file_measurements_follow_actual_durable_file_outcomes(tmp_path):
    app, workspace, paths = sources(tmp_path)
    observed = []
    def progress(completed, total, phase):
        observed.append((completed, total, phase))
        assert len(app.browse(workspace)) == completed
    results = app.import_files(workspace, paths, progress=progress)
    assert [r['state'] for r in results] == ['complete'] * 3
    assert observed == [(n, 3, 'Processing files') for n in range(4)]
    reopened = LibraryApp(app.root)
    assert len(reopened.search(workspace, 'railway')) == 3
    assert operation_outcome(results) == 'succeeded'


@pytest.mark.parametrize('unsized', [False, True], ids=['single-file', 'generator'])
def test_single_file_or_unsized_input_does_not_invent_a_total(tmp_path, unsized):
    app, workspace, paths = sources(tmp_path, count=2 if unsized else 1)
    consumed, observed = [], []
    def generate():
        for path in paths:
            consumed.append(path)
            yield path
    def progress(completed, total, phase):
        observed.append((completed, total))
        if unsized:
            assert len(consumed) == completed
    results = app.import_files(workspace, generate() if unsized else paths, progress=progress)
    assert len(results) == len(paths)
    assert observed == [(n, None) for n in range(len(paths) + 1)]


def test_broken_observer_after_commit_never_adds_failed_row_or_retries(tmp_path):
    app, workspace, paths = sources(tmp_path, count=2)
    observed = []
    def progress(completed, total, phase):
        observed.append(completed)
        if completed:
            assert len(LibraryApp(app.root).browse(workspace)) == 1
            raise OSError('Progress output disconnected after commit')
    results = app.import_files(workspace, paths, progress=progress)
    assert observed == [0, 1]
    assert [r['state'] for r in results] == ['complete', 'complete']
    assert len(LibraryApp(app.root).search(workspace, 'railway')) == 2


def test_unavailable_advisory_length_does_not_prevent_valid_iteration(tmp_path):
    app, workspace, paths = sources(tmp_path, count=2)
    class InputPaths:
        def __iter__(self):
            return iter(paths)
        def __len__(self):
            raise RuntimeError('The source provider cannot measure its remaining size')
    observed = []
    results = app.import_files(workspace, InputPaths(), progress=lambda *event: observed.append(event))
    assert [row['state'] for row in results] == ['complete', 'complete']
    assert [event[:2] for event in observed] == [(0, None), (1, None), (2, None)]
    assert len(LibraryApp(app.root).browse(workspace)) == 2


def test_failed_file_is_a_measured_outcome_and_other_files_can_complete(tmp_path):
    app, workspace, paths = sources(tmp_path, count=2)
    paths.insert(1, tmp_path / 'missing.txt')
    observed = []
    results = app.import_files(workspace, paths, progress=lambda *event: observed.append(event))
    assert [r['state'] for r in results] == ['complete', 'failed', 'complete']
    assert [event[:2] for event in observed] == [(n, 3) for n in range(4)]
    assert len(LibraryApp(app.root).browse(workspace)) == 2
    assert operation_outcome(results) == 'failed'


def test_cancel_between_files_preserves_completed_book_and_stops_remaining_work(tmp_path):
    app, workspace, paths = sources(tmp_path)
    cancel, observed = Event(), []
    def progress(completed, total, phase):
        observed.append((completed, total))
        if completed == 1:
            cancel.set()
    results = app.import_files(workspace, paths, cancelled=cancel.is_set, progress=progress)
    assert [r['state'] for r in results] == ['complete', 'cancelled']
    assert [r['file'] for r in results] == ['book-0.txt', 'book-1.txt']
    assert observed == [(0, 3), (1, 3), (2, 3)]
    assert len(LibraryApp(app.root).browse(workspace)) == 1
    assert operation_outcome(results) == 'cancelled'


def test_pre_cancelled_import_never_calls_single_file_service(tmp_path, monkeypatch):
    app, workspace, paths = sources(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled batch entered an import transaction')
    monkeypatch.setattr(app, 'import_file', forbidden)
    results = app.import_files(workspace, paths, cancelled=lambda: True)
    assert len(results) == 1 and results[0]['state'] == 'cancelled'
    assert app.browse(workspace) == ()


@pytest.mark.parametrize('as_exception', [True, False], ids=['interrupted-exception', 'cancelled-receipt'])
def test_acknowledged_cancellation_stops_after_one_file(tmp_path, monkeypatch, as_exception):
    app, workspace, paths = sources(tmp_path)
    attempted = []
    def cancelled_import(workspace, path, **metadata):
        attempted.append(path)
        if as_exception:
            raise InterruptedError('Import acknowledged cancellation')
        return {'state': 'cancelled'}
    monkeypatch.setattr(app, 'import_file', cancelled_import)
    results = app.import_files(workspace, paths)
    assert attempted == paths[:1]
    assert len(results) == 1 and results[0]['state'] == 'cancelled'


def test_late_cancel_after_last_commit_does_not_relabel_a_completed_import(tmp_path):
    app, workspace, paths = sources(tmp_path, count=1)
    cancel = Event()
    def progress(completed, total, phase):
        if completed:
            cancel.set()
    results = app.import_files(workspace, paths, cancelled=cancel.is_set, progress=progress)
    assert cancel.is_set() and results[0]['state'] == 'complete'
    assert operation_outcome(results) == 'succeeded'
    assert len(LibraryApp(app.root).browse(workspace)) == 1
