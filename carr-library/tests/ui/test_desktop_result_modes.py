"""Desktop handler checks with control doubles; native NVDA remains separate."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.search_model import Hit, SearchModel


@pytest.fixture
def panel(tmp_path):
    path = Path(importlib.util.find_spec('carr_library.ui.desktop_app').origin).resolve()
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == 'DesktopSearchPanel')
    env = {'__name__': 'carr_library.ui._result_modes_fixture', '__package__': 'carr_library.ui', 'PagedSearchPanel': object, 'Hit': Hit}
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
    instance._search_page, instance._source_history = None, []
    instance.search_navigation = {name: Mock() for name in
        ('result_previous', 'result_next', 'source_previous', 'source_next')}
    for name, value in (('search_timeout', '300'), ('search_memory', '64'), ('search_disk', '2048')):
        setattr(instance, name, Mock())
        getattr(instance, name).GetValue.return_value = value
    instance._refresh_details = Mock()
    return instance


@pytest.mark.parametrize('selection', [0, 1])
def test_control_dispatches_presentation_to_real_application(panel, selection):
    panel.result_mode.GetSelection.return_value = selection
    panel.on_search(None)
    _, operation, done = panel.frame.run_job.call_args.args
    rows = operation()
    done(rows)
    assert rows.results and panel.model.hits[0].quote == rows.results[0].quote
    assert len(rows.results[0].quote) > len('railway')
    assert panel.frame.library.copy(rows.results[0].workspace, rows.results[0].ref, rows.results[0].revision, 'quote') == rows.results[0].quote
    assert ('Short source' if selection == 0 else 'Expanded source') in panel.status.ChangeValue.call_args.args[0]


def test_invalid_control_selection_is_rejected_before_search(panel):
    panel.result_mode.GetSelection.return_value = -1
    panel.on_search(None)
    panel.frame.run_job.assert_not_called()
    panel.result_mode.SetFocus.assert_called_once()


def test_changed_options_clear_selection_and_discard_pending_search(panel):
    panel.on_search(None)
    _, operation, done = panel.frame.run_job.call_args.args
    rows = operation()
    done(rows)
    assert panel.model.hits and panel.results
    event = Mock()
    panel.result_mode.GetSelection.return_value = 1
    panel.on_options_changed(event)
    assert not panel.model.hits and not panel.results
    assert panel.details.ChangeValue.call_args.args == ('',)
    event.Skip.assert_called_once()
    done(rows)
    assert not panel.model.hits and not panel.results


def test_advanced_control_dispatches_to_real_scoped_search(panel):
    panel.retrieval.GetSelection.return_value = panel.MODES.index('advanced')
    panel.query.GetValue.return_value = 'railway AND NOT airport'
    panel.on_search(None)
    _, operation, done = panel.frame.run_job.call_args.args
    rows = operation()
    done(rows)
    assert len(rows.results) == 1
    assert rows.results[0].quote == 'A meaningful railway source quotation.'
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
    _, operation, done = panel.frame.run_job.call_args.args
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
    _, operation, done = panel.frame.run_job.call_args.args
    rows = operation()
    panel.on_build_query()
    panel.retrieval.SetSelection.assert_called_once_with(panel.MODES.index('advanced'))
    panel.query.ChangeValue.assert_called_once_with('"railway" AND NOT "airport"')
    assert not panel.model.hits and not panel.results
    done(rows)
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


@pytest.mark.parametrize('change', ['options', 'query', 'topic', 'busy'])
def test_query_builder_cannot_overwrite_changed_form(panel, monkeypatch, change):
    import sys
    panel.frame.active = (panel.bound_workspace, None)
    def changed(*args, **kwargs):
        if change == 'options':
            panel.on_options_changed()
        elif change == 'query':
            panel.query.GetValue.return_value = 'another query'
        elif change == 'topic':
            panel.frame.active = (panel.bound_workspace, 'another topic')
        else:
            panel.frame.working = True
        return '"railway"'
    monkeypatch.setitem(sys.modules, 'carr_library.ui.advanced_search',
                        SimpleNamespace(show_query_builder=changed))
    panel.on_build_query()
    panel.query.ChangeValue.assert_not_called()
    panel.retrieval.SetSelection.assert_not_called()
    assert 'changed' in panel.frame.report.call_args.args[0]


def test_builder_and_search_center_shortcuts_are_distinct():
    """The two visible menu commands must retain independent accelerators."""
    root = Path(importlib.util.find_spec('carr_library.ui.desktop_app').origin).resolve().parent
    main = ast.parse((root / 'desktop_app.py').read_text(encoding='utf-8'))
    center = ast.parse((root / 'federated_search.py').read_text(encoding='utf-8'))
    main_labels = [node.value for node in ast.walk(main) if isinstance(node, ast.Constant)
                   and isinstance(node.value, str) and '\tCtrl+' in node.value]
    center_labels = [node.value for node in ast.walk(center) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and '\tCtrl+' in node.value]
    assert 'Build advanced query\tCtrl+Alt+Q' in main_labels
    assert not any(label.endswith('\tCtrl+Alt+F') for label in main_labels)
    assert 'Research Search &Center...\tCtrl+Alt+F' in center_labels


@pytest.mark.parametrize('stage', ['before_worker', 'after_worker'])
@pytest.mark.parametrize('change', ['folder', 'topic', 'data_path', 'cancel'])
def test_main_search_cannot_publish_after_scope_or_cancel_changes(panel, tmp_path, stage, change):
    panel.frame.active = (panel.bound_workspace, None)
    panel.on_search(None)
    _, operation, done = panel.frame.run_job.call_args.args
    rows = operation() if stage == 'after_worker' else None
    if change == 'folder':
        panel.frame.library = LibraryApp(tmp_path / 'replacement')
    elif change == 'topic':
        panel.frame.active = (panel.bound_workspace, 'other-topic')
    elif change == 'data_path':
        panel.frame.library.root = tmp_path / 'another-data-path'
    else:
        panel.frame.cancel_event.is_set = lambda: True
    if stage == 'before_worker':
        with pytest.raises(InterruptedError, match='changed before searching'):
            operation()
    else:
        done(rows)
    assert not panel.model.hits and not panel.results


def test_builder_cancel_after_scope_change_never_focuses_old_query(panel, monkeypatch):
    import sys
    def changed(*args, **kwargs):
        panel.frame.library = object()
        return None
    monkeypatch.setitem(sys.modules, 'carr_library.ui.advanced_search',
                        SimpleNamespace(show_query_builder=changed))
    panel.on_build_query()
    panel.query.ChangeValue.assert_not_called()
    panel.query.SetFocus.assert_not_called()
    panel.frame.report.assert_called_once()


def test_ranked_desktop_search_uses_complete_scope_and_cursor_pages(panel):
    app, workspace = panel.frame.library, panel.bound_workspace
    raw = "\f".join(f"Needle exact source page {number:04d}." for number in range(205))
    saved = app.add_text(workspace, "Many original pages", raw, edition_id="many")
    panel.retrieval.GetSelection.return_value = panel.MODES.index("thematic")
    panel.query.GetValue.return_value = "Needle"
    panel.on_search(None)
    _, operation, done = panel.frame.run_job.call_args.args
    first = operation()
    done(first)
    assert first.ranking_scope == "all" and first.total_results == 205
    assert first.has_more_results and first.next_source_cursor is None
    assert "complete selected scope" in panel.status.ChangeValue.call_args.args[0]
    panel.on_search_navigation("result_next")
    _, operation, done = panel.frame.run_job.call_args.args
    second = operation()
    done(second)
    assert second.offset == 100 and second.total_results == 205
    assert len(second.results) == 100
    assert {(r.edition_id, r.page) for r in first.results}.isdisjoint(
        {(r.edition_id, r.page) for r in second.results})
    assert app.assets.resolve(saved["asset"]).read_bytes() == raw.encode("utf-8")
    assert app.close_search()
