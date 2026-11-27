"""Desktop handler checks with control doubles; native NVDA remains separate."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.search_model import Hit, SearchModel
from carr_library.ui.search_paging import PageHistory


@pytest.fixture
def panel(tmp_path):
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/desktop_app.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    env = {'PagedSearchPanel': object, 'Hit': Hit}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    instance = object.__new__(env['DesktopSearchPanel'])
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('UI integration')
    app.add_text(workspace, 'Source', 'A meaningful railway source quotation. Other source context.')
    instance.frame = SimpleNamespace(working=False, library=app, report=Mock(),
                                     cancel_event=SimpleNamespace(is_set=lambda: False), run_job=Mock())
    instance.model = SearchModel()
    instance.model.set_workspace(workspace)
    instance.bound_workspace = workspace
    instance.bound_library = app
    instance.paging = PageHistory()
    instance.bound_topic = None
    for name in ('query', 'retrieval', 'result_mode', 'grid', 'details', 'status', 'collection'):
        setattr(instance, name, Mock())
    instance.query.GetValue.return_value = 'railway'
    instance.retrieval.GetSelection.return_value = 0
    instance.result_mode.GetSelection.return_value = 0
    instance.collection.GetSelection.return_value = 0
    instance.collection_ids = [None]
    instance.filters = {}
    instance.results = {}
    instance._context_epoch = 0
    instance._refresh_details = Mock()
    return instance


@pytest.mark.parametrize('selection', [0, 1])
def test_control_dispatches_presentation_to_real_application(panel, selection):
    panel.result_mode.GetSelection.return_value = selection
    panel.on_search(None)
    _, operation, done, failed = panel.frame.run_job.call_args.args
    page = operation()
    rows = page.results
    done(page)
    assert rows and panel.model.hits[0].quote == rows[0].quote
    assert len(rows[0].quote) > len('railway')
    assert panel.frame.library.copy(rows[0].workspace, rows[0].ref, rows[0].revision, 'quote') == rows[0].quote
    assert ('Short source' if selection == 0 else 'Expanded source') in panel.status.ChangeValue.call_args.args[0]


def test_invalid_control_selection_is_rejected_before_search(panel):
    panel.result_mode.GetSelection.return_value = -1
    panel.on_search(None)
    panel.frame.run_job.assert_not_called()
    panel.result_mode.SetFocus.assert_called_once()


def test_changed_options_clear_selection_and_discard_pending_search(panel):
    panel.on_search(None)
    _, operation, done, failed = panel.frame.run_job.call_args.args
    page = operation()
    rows = page.results
    done(page)
    assert panel.model.hits and panel.results
    event = Mock()
    panel.result_mode.GetSelection.return_value = 1
    panel.on_options_changed(event)
    assert not panel.model.hits and not panel.results
    assert panel.details.ChangeValue.call_args.args == ('',)
    event.Skip.assert_called_once()
    done(page)
    assert not panel.model.hits and not panel.results


def test_advanced_control_dispatches_to_real_scoped_search(panel):
    panel.retrieval.GetSelection.return_value = panel.MODES.index('advanced')
    panel.query.GetValue.return_value = 'railway AND NOT airport'
    panel.on_search(None)
    _, operation, done, failed = panel.frame.run_job.call_args.args
    page = operation()
    rows = page.results
    done(page)
    assert len(rows) == 1
    assert rows[0].quote == 'A meaningful railway source quotation.'
    assert 'one page or text segment' in panel.status.ChangeValue.call_args.args[0]


def test_invalid_advanced_syntax_starts_no_background_job(panel):
    panel.retrieval.GetSelection.return_value = panel.MODES.index('advanced')
    panel.query.GetValue.return_value = 'railway OR'
    panel.on_search(None)
    panel.frame.run_job.assert_not_called()
    assert 'Invalid advanced query' in panel.status.ChangeValue.call_args.args[0]
    panel.status.SetInsertionPoint.assert_called_once_with(0)
    panel.status.SetFocus.assert_called_once()


def test_query_builder_cancel_preserves_current_results(panel, monkeypatch):
    import sys
    builder = Mock(return_value=None)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.advanced_search',
                        SimpleNamespace(show_query_builder=builder))
    panel.on_search(None)
    _, operation, done, failed = panel.frame.run_job.call_args.args
    done(operation())
    before = panel.model.hits, dict(panel.results), panel._context_epoch
    panel.on_build_query()
    assert (panel.model.hits, panel.results, panel._context_epoch) == before
    panel.query.ChangeValue.assert_not_called()
    panel.retrieval.SetSelection.assert_not_called()


def test_query_builder_use_selects_advanced_and_invalidates_pending_results(panel, monkeypatch):
    import sys
    builder = Mock(return_value='"railway" AND NOT "airport"')
    monkeypatch.setitem(sys.modules, 'carr_library.ui.advanced_search',
                        SimpleNamespace(show_query_builder=builder))
    panel.on_search(None)
    _, operation, done, failed = panel.frame.run_job.call_args.args
    page = operation()
    rows = page.results
    panel.on_build_query()
    panel.retrieval.SetSelection.assert_called_once_with(panel.MODES.index('advanced'))
    panel.query.ChangeValue.assert_called_once_with('"railway" AND NOT "airport"')
    assert not panel.model.hits and not panel.results
    done(page)
    assert not panel.model.hits and not panel.results


def test_query_builder_cannot_apply_after_library_changes(panel, monkeypatch):
    import sys
    def changed(*args, **kwargs):
        panel.frame.library = object()
        return '"railway"'
    monkeypatch.setitem(sys.modules, 'carr_library.ui.advanced_search',
                        SimpleNamespace(show_query_builder=changed))
    panel.on_build_query()
    panel.query.ChangeValue.assert_not_called()
    panel.frame.report.assert_called_once_with('The library changed. Open its Search section again.')
