"""Actual desktop handlers with headless controls; no native NVDA claim."""
import ast
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui.search_model import Hit, SearchModel


@pytest.fixture
def handlers():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in ('DesktopSearchPanel', 'DesktopFrame')]
    dialog = Mock()
    dialog.ShowModal.return_value = 1
    dialog.GetSelection.return_value = 1
    queries = [SimpleNamespace(id='first', name='First search'),
               SimpleNamespace(id='chosen', name='Chosen search')]
    saved = Mock()
    saved.return_value.list_queries.return_value = queries
    environment = {'PagedSearchPanel': object, 'WorkspaceFrame': object, 'Hit': Hit,
                   'wx': SimpleNamespace(ID_OK=1, SingleChoiceDialog=Mock(return_value=dialog)),
                   'SavedTopicQueries': saved, 'show_text': Mock(),
                   'format_topic_results': Mock(return_value='Readable saved quotations.')}
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), 'exec'), environment)
    return environment


@pytest.fixture
def panel(handlers):
    panel = object.__new__(handlers['DesktopSearchPanel'])
    panel.bound_workspace, panel.bound_topic = 'library', 'topic'
    panel.model = SearchModel()
    panel.model.set_workspace('library')
    panel.query = Mock()
    panel.query.GetValue.return_value = 'history'
    panel.retrieval = Mock()
    panel.retrieval.GetSelection.return_value = 4
    panel.collection = Mock()
    panel.collection.GetSelection.return_value = 0
    panel.collection_ids = [None]
    panel.filters, panel.results = {}, {}
    panel._context_epoch = 0
    panel.grid, panel.details, panel.status = Mock(), Mock(), Mock()
    panel._refresh_details = Mock()
    panel.frame = SimpleNamespace(working=False, cancel_event=Event(), run_job=Mock(),
        library=SimpleNamespace(search=Mock(), semantic_ranker=object()), report=Mock())
    return panel


def test_empty_semantic_fallback_is_readable_after_background_completion(panel):
    note = 'Semantic search budget exceeded; keyword search used.'
    def search(*args, on_status, cancelled, **kwargs):
        assert not cancelled()
        on_status(note)
        return ()
    panel.frame.library.search.side_effect = search
    panel.on_search(None)
    panel.frame.library.search.assert_not_called()
    _, operation, complete = panel.frame.run_job.call_args.args
    rows = operation()
    panel.status.ChangeValue.assert_not_called()
    complete(rows)
    assert note in panel.status.ChangeValue.call_args.args[0]
    assert 'No results.' in panel.status.ChangeValue.call_args.args[0]
    panel.status.SetFocus.assert_called_once()
    cancelled = panel.frame.library.search.call_args.kwargs['cancelled']
    panel.frame.cancel_event.set()
    assert cancelled()


def test_status_notes_belong_only_to_their_search_and_ignore_stale_completion(panel):
    def first(*args, on_status, **kwargs):
        on_status('Old fallback notice.')
        return ()
    panel.frame.library.search.side_effect = first
    panel.on_search(None)
    _, old_operation, old_complete = panel.frame.run_job.call_args.args
    old_rows = old_operation()
    panel.frame.library.search.side_effect = lambda *args, **kwargs: ()
    panel.on_search(None)
    _, operation, complete = panel.frame.run_job.call_args.args
    complete(operation())
    assert 'Old fallback notice.' not in panel.status.ChangeValue.call_args.args[0]
    previous = panel.status.ChangeValue.call_count
    old_complete(old_rows)
    assert panel.status.ChangeValue.call_count == previous


@pytest.fixture
def frame(handlers):
    frame = object.__new__(handlers['DesktopFrame'])
    frame.active = ('library', 'topic')
    frame._topic = Mock(return_value=frame.active)
    frame.cancel_event = Event()
    frame.library = SimpleNamespace(evidence_transaction=Mock(return_value=nullcontext(object())),
                                    run_query=Mock(return_value=()))
    frame.run_job, frame.report, frame.status = Mock(), Mock(), Mock()
    return frame


def test_saved_query_runs_in_job_and_keeps_chosen_scope_and_title(frame, handlers):
    frame.run_saved_query(None)
    frame.library.run_query.assert_not_called()
    handlers['show_text'].assert_not_called()
    handlers['wx'].SingleChoiceDialog.return_value.Destroy.assert_called_once()
    _, operation, complete = frame.run_job.call_args.args
    frame._topic.return_value = ('different library', 'different topic')
    result = operation()
    assert frame.library.run_query.call_args.args == ('library', 'topic', 'chosen')
    handlers['show_text'].assert_not_called()
    complete(result)
    handlers['show_text'].assert_called_once_with(frame, 'Chosen search', 'Readable saved quotations.')
    frame.status.SetFocus.assert_called_once()


def test_saved_query_receives_live_cancellation_and_does_not_show_partial_result(frame, handlers):
    def search(*args, cancelled):
        if cancelled():
            raise TimeoutError('Saved search cancelled.')
        return ()
    frame.library.run_query.side_effect = search
    frame.run_saved_query(None)
    _, operation, _ = frame.run_job.call_args.args
    frame.cancel_event.set()
    with pytest.raises(TimeoutError, match='cancelled'):
        operation()
    handlers['show_text'].assert_not_called()


def test_saved_query_completion_does_not_open_results_in_another_scope(frame, handlers):
    frame.run_saved_query(None)
    _, operation, complete = frame.run_job.call_args.args
    result = operation()
    frame.active = ('other library', 'other topic')
    complete(result)
    handlers['show_text'].assert_not_called()
    frame.status.SetFocus.assert_not_called()


def test_cancelled_query_selection_starts_no_job(frame, handlers):
    handlers['wx'].SingleChoiceDialog.return_value.ShowModal.return_value = 0
    frame.run_saved_query(None)
    frame.run_job.assert_not_called()
    frame.library.run_query.assert_not_called()
    handlers['wx'].SingleChoiceDialog.return_value.Destroy.assert_called_once()
