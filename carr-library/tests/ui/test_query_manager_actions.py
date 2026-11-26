"""Real saved-search mutations via UI handlers and simulated native dialogs."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import format_citation
from carr_library.research.query_manager import QueryManager
from carr_library.ui.query_manager import QueryActions


@pytest.fixture
def setup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    topic = app.create_topic(workspace, 'Topic')
    assert app.add_text(workspace, 'Source', 'Railway source text.')['state'] == 'complete'
    hit, = app.search(workspace, 'Railway')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Independent researcher note.')
    frame = SimpleNamespace(library=app, active=(workspace, topic), working=False, busy=False,
        _closing=False, report=Mock(), run_job=Mock(), status=Mock(), cancel_event=Event(), book=object())
    dialogs = SimpleNamespace(choose=Mock(), definition=Mock(), text=Mock(), show=Mock())
    manager = QueryManager(app, workspace, topic)
    return QueryActions(frame, dialogs), manager, frame, dialogs


def test_create_note_query_and_cancelled_creation_preserve_evidence(setup):
    actions, manager, frame, dialogs = setup
    before = frame.library.evidence(*frame.active)
    dialogs.choose.return_value = 0
    dialogs.definition.return_value = dict(name='My notes', query='researcher', field='note', limit=3)
    actions.manage()
    saved, = manager.list().rows
    assert saved.field == 'note' and saved.limit == 3
    assert manager.run(saved)[0].evidence.note == before[0].note
    assert frame.library.evidence(*frame.active) == before
    dialogs.definition.return_value = None
    actions.manage()
    assert manager.list().rows == (saved,)


def test_edit_archive_restore_and_duplicate_use_existing_query_ids(setup):
    actions, manager, _, dialogs = setup
    original = manager.create('Original', 'Railway')
    dialogs.choose.side_effect = [1, 1]
    dialogs.definition.return_value = dict(name='Changed', query='researcher', field='note', limit=8)
    actions.manage()
    updated = manager.get(original.id)
    assert updated.query == 'researcher' and updated.field == 'note'
    dialogs.choose.side_effect = [1, 3]
    actions.manage()
    assert manager.get(original.id).archived
    dialogs.choose.side_effect = [1, 0]
    actions.manage()
    assert manager.get(original.id) == updated
    dialogs.choose.side_effect = [1, 2]
    dialogs.text.return_value = 'Independent copy'
    actions.manage()
    rows = manager.list().rows
    assert len(rows) == 2 and len({row.id for row in rows}) == 2
    assert {row.query for row in rows} == {'researcher'}


def test_competing_editor_change_is_not_overwritten_after_dialog(setup):
    actions, manager, frame, dialogs = setup
    original = manager.create('Original', 'Railway')
    dialogs.choose.side_effect = [1, 1]
    def competing_edit(*args):
        manager.update(original, 'Another editor', 'note', field='note', limit=4)
        return dict(name='Stale dialog', query='Railway', field='quote', limit=50)
    dialogs.definition.side_effect = competing_edit
    actions.manage()
    assert manager.get(original.id).name == 'Another editor'
    assert 'changed' in frame.report.call_args.args[0]


def test_scope_change_during_dialog_does_not_write_or_drop_notebook(setup):
    actions, manager, frame, dialogs = setup
    notebook = frame.book
    dialogs.choose.return_value = 0
    def changed(*args):
        frame.active = (frame.active[0], frame.library.create_topic(frame.active[0], 'Other'))
        return dict(name='Wrong scope', query='Railway', field='quote', limit=10)
    dialogs.definition.side_effect = changed
    actions.manage()
    assert manager.list().rows == () and frame.book is notebook
    assert 'changed' in frame.report.call_args.args[0]


def test_duplicate_names_remain_distinguishable_without_disclosing_long_ids(setup):
    actions, manager, _, dialogs = setup
    first = manager.create('Same title', 'Railway')
    manager.create('Same title', 'Railway')
    dialogs.choose.return_value = None
    actions.manage()
    choices = dialogs.choose.call_args.args[3]
    assert len(set(choices)) == 3
    assert first.id not in ' '.join(choices)


def test_query_manager_paginates_native_selection_list(setup):
    actions, manager, _, dialogs = setup
    for i in range(51):
        manager.create(f'Query {i:02}', 'Railway')
    dialogs.choose.side_effect = [51, None]
    actions.manage()
    assert dialogs.choose.call_count == 2
    first, second = [call.args[3] for call in dialogs.choose.call_args_list]
    assert 'Next page of saved searches' in first
    assert 'Previous page of saved searches' in second
    assert 'Query 50' in second[1] and 'Query 00' not in ' '.join(second)


def _native_formatter(monkeypatch):
    # Use the exact current source formatter while keeping wx unavailable in
    # headless acceptance. Only native widget construction is outside this test.
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name == 'format_topic_results']
    env = {'format_citation': format_citation}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), env)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app',
                        SimpleNamespace(format_topic_results=env['format_topic_results']))


def test_saved_search_runs_after_dialogs_and_displays_authenticated_results(setup, monkeypatch):
    actions, manager, frame, dialogs = setup
    _native_formatter(monkeypatch)
    manager.create('Source query', 'Railway')
    dialogs.choose.side_effect = [1, 0]
    actions.manage()
    dialogs.show.assert_not_called()
    _, operation, complete = frame.run_job.call_args.args
    result = operation()
    dialogs.show.assert_not_called()
    complete(result)
    text = dialogs.show.call_args.args[2]
    assert 'Exact quotation:' in text and 'Railway' in text
    assert 'Research note:' in text and 'Independent researcher note.' in text
    frame.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('change', ['scope', 'definition', 'closing', 'late_cancel'])
def test_late_saved_query_results_do_not_appear_after_scope_or_definition_drift(setup, change):
    actions, manager, frame, dialogs = setup
    query = manager.create('Source query', 'Railway')
    dialogs.choose.side_effect = [1, 0]
    actions.manage()
    _, operation, complete = frame.run_job.call_args.args
    result = operation()
    if change == 'scope':
        frame.active = (frame.active[0], 'different')
    elif change == 'definition':
        manager.update(query, 'Changed', 'another')
    elif change == 'late_cancel':
        frame.cancel_event.set()
    else:
        frame._closing = True
    complete(result)
    dialogs.show.assert_not_called()


def test_live_cancellation_prevents_saved_query_results(setup):
    actions, manager, frame, dialogs = setup
    manager.create('Source query', 'Railway')
    dialogs.choose.side_effect = [1, 0]
    actions.manage()
    _, operation, _ = frame.run_job.call_args.args
    frame.cancel_event.set()
    with pytest.raises(TimeoutError):
        operation()
    dialogs.show.assert_not_called()


def test_busy_or_missing_topic_manager_never_opens_dialog(setup):
    actions, _, frame, dialogs = setup
    frame.working = True
    actions.manage()
    frame.working = False
    frame.active = (frame.active[0], None)
    actions.manage()
    dialogs.choose.assert_not_called()
