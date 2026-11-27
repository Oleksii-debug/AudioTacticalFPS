"""Exercise the actual native-dialog handlers with controls, not Windows/NVDA."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.quality import CatalogQualitySnapshot, QualityIssue, QualitySummary
from carr_library.catalog.read_views import EditionView


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.items = list(kwargs.get('choices', ()))
        self.selection = -1
        self.enabled, self.focused, self.destroyed = True, False, False
        self.bindings, self.modal_result = {}, None
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, value): self.name = value
    def SetValue(self, value): self.value = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def SetItems(self, items): self.items, self.selection = list(items), -1
    def SetFocus(self): self.focused = True
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def SetInsertionPoint(self, value): pass
    def Add(self, *args): pass
    def Enable(self, enabled): self.enabled = enabled
    def EndModal(self, result): self.modal_result = result
    def Destroy(self): self.destroyed = True


@pytest.fixture
def module():
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'TextCtrl', 'Button', 'Choice', 'ListBox'):
        setattr(wx, name, Control)
    for n, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT',
            'TOP', 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY', 'LB_SINGLE',
            'ID_CANCEL', 'ID_OK', 'ID_APPLY', 'EVT_BUTTON', 'EVT_CHOICE', 'EVT_LISTBOX')):
        setattr(wx, name, 1 << n)
    path = Path(__import__('carr_library').__file__).resolve().parent / 'ui/catalog_quality.py'
    spec = importlib.util.spec_from_file_location('_catalog_quality_independent', path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx}):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def snapshot():
    a = EditionView('e1', 'w1', 'a' * 64, 'Same title', 'uk', 'First', 'I', ('Олена',))
    b = EditionView('e2', 'w2', 'b' * 64, 'Same title', 'uk', 'Revised', 'II', ('Олена',))
    return CatalogQualitySnapshot('library', None, (('c', 'Research'),), (a, b), (
        QualityIssue('unfiled', ('e1',), 'Not in an active collection.'),
        QualityIssue('possible_duplicate', ('e1', 'e2'),
                     'Tentative metadata match. Different editions remain separate.')),
        QualitySummary(2, 2, 1, 1, 1, 2, 2))


def test_readable_issue_details_disambiguate_repeated_titles_and_preserve_reasons(module, snapshot):
    rows = module.issue_rows(snapshot)
    assert [row.edition_id for row, _ in rows] == ['e1', 'e2']
    assert len(rows[0][1]) == 2
    details = module.issue_details(snapshot, *rows[0])
    assert 'edition ID e1' in details and 'edition ID e2' in details
    assert 'First' in details and 'Revised' in details and 'volume II' in details
    assert 'Tentative' in details and 'Not in an active collection' in details
    assert len(module.issue_rows(snapshot, 'unfiled')) == 1
    assert module.issue_rows(snapshot, 'missing_title') == ()
    with pytest.raises(ValueError):
        module.issue_rows(snapshot, 'unsupported')


def test_dialog_has_named_native_controls_filter_and_exact_edit_identity(module, snapshot):
    dialog = module.CatalogQualityDialog(None, snapshot, can_edit=True)
    assert dialog.summary.focused and '2 source editions' in dialog.summary.value
    for control in (dialog.summary, dialog.collection, dialog.filter, dialog.results,
                    dialog.details, dialog.status, dialog.previous, dialog.next, dialog.edit):
        assert control.name
    assert len(dialog.results.items) == 2
    dialog.results.SetSelection(1)
    dialog.on_selection()
    assert 'edition ID e2' in dialog.details.value
    dialog.on_edit()
    assert dialog.edit_edition_id == 'e2' and dialog.modal_result == module.wx.ID_OK
    dialog.filter.SetSelection(dialog.category_ids.index('missing_title'))
    dialog.on_filter()
    assert not dialog.results.items and not dialog.edit.enabled
    assert dialog.details.value == '' and 'No editions match' in dialog.status.value


def test_paged_results_keep_last_page_and_filter_resets_cursor(module, snapshot):
    editions = tuple(replace(snapshot.editions[0], edition_id=f'e{i:04}') for i in range(401))
    issues = tuple(QualityIssue('unfiled', (row.edition_id,), 'No active collection.') for row in editions)
    snapshot = replace(snapshot, editions=editions, issues=issues)
    dialog = module.CatalogQualityDialog(None, snapshot)
    assert len(dialog.results.items) == 200 and dialog.next.enabled and not dialog.previous.enabled
    dialog.on_next()
    assert dialog.page == 1 and dialog.visible[0][0].edition_id == 'e0200'
    dialog.on_next()
    assert len(dialog.visible) == 1 and dialog.visible[0][0].edition_id == 'e0400'
    assert not dialog.next.enabled and dialog.previous.enabled
    dialog.on_previous()
    assert dialog.page == 1 and dialog.results.focused
    dialog.filter.SetSelection(dialog.category_ids.index('unfiled'))
    dialog.on_filter()
    assert dialog.page == 0 and not dialog.edit.enabled


def test_rescan_closes_dialog_and_passes_selected_collection_identity(module, snapshot):
    dialog = module.CatalogQualityDialog(None, snapshot)
    dialog.collection.SetSelection(-1)
    dialog.on_refresh()
    assert dialog.modal_result is None and dialog.collection.focused
    dialog.collection.SetSelection(1)
    dialog.on_refresh()
    assert dialog.requested_collection == 'c' and dialog.modal_result == module.wx.ID_APPLY


def test_large_duplicate_group_details_are_explicitly_bounded_without_losing_rows(module, snapshot):
    editions = tuple(replace(snapshot.editions[0], edition_id=f'e{i:04}') for i in range(201))
    issue = QualityIssue('possible_duplicate', tuple(row.edition_id for row in editions), 'Tentative.')
    snapshot = replace(snapshot, editions=editions, issues=(issue,))
    details = module.issue_details(snapshot, editions[-1], (issue,))
    assert 'edition ID e0200' in details  # The selected edition is always identifiable.
    assert 'first 200 of 201' in details
    assert len(module.issue_rows(snapshot)) == 201


def host(library):
    frame = SimpleNamespace(library=library, active=('library', 'topic'), working=False, busy=False,
                            _closing=False, cancel_event=Event(), jobs=[], reports=[])
    frame._workspace = lambda: frame.active[0]
    frame.report = frame.reports.append
    def run_job(title, operation, done):
        frame.cancel_event.clear()
        frame.jobs.append((title, operation, done))
    frame.run_job = run_job
    return frame


def fake_dialog(module, monkeypatch, *, result, after_show=None, collection='c', identity='e2'):
    dialogs = []
    class Dialog(Control):
        def __init__(self, frame, snapshot, *, can_edit):
            super().__init__()
            self.snapshot, self.can_edit = snapshot, can_edit
            self.requested_collection, self.edit_edition_id = collection, identity
            dialogs.append(self)
        def ShowModal(self):
            if after_show:
                after_show()
            return result
    monkeypatch.setattr(module, 'CatalogQualityDialog', Dialog)
    return dialogs


def test_host_uses_existing_job_and_real_read_only_library_then_opens_report(module, monkeypatch, tmp_path):
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Research')
    frame = host(library)
    frame.active = (workspace, None)
    dialogs = fake_dialog(module, monkeypatch, result=module.wx.ID_CANCEL)
    module.open_catalog_quality(frame)
    assert len(frame.jobs) == 1 and not dialogs
    _, operation, done = frame.jobs[0]
    result = operation()
    assert result.workspace == workspace and result.summary.sources == 0
    assert not dialogs
    done(result)
    assert len(dialogs) == 1 and dialogs[0].destroyed and frame.reports == []


@pytest.mark.parametrize('change', ['workspace', 'topic', 'folder', 'cancel', 'close'])
def test_late_completion_does_not_show_report_in_changed_or_cancelled_scope(module, snapshot, monkeypatch, change):
    frame = host(object())
    dialogs = fake_dialog(module, monkeypatch, result=module.wx.ID_CANCEL)
    module.open_catalog_quality(frame)
    if change == 'workspace': frame.active = ('other', 'topic')
    elif change == 'topic': frame.active = ('library', 'other')
    elif change == 'folder': frame.library = object()
    elif change == 'cancel': frame.cancel_event.set()
    elif change == 'close': frame._closing = True
    frame.jobs[0][2](snapshot)
    assert not dialogs


def test_operation_captures_library_and_uses_live_cancellation(module, monkeypatch):
    original = object()
    frame = host(original)
    captured = []
    def check(library, workspace, *, collection, cancelled):
        captured.append((library, workspace, collection))
        if cancelled():
            raise InterruptedError('cancelled')
    monkeypatch.setattr(module, 'catalog_quality', check)
    frame.cancel_event.set()  # A past cancellation must not prevent a fresh job.
    module.open_catalog_quality(frame, collection='c')
    frame.library = object()
    frame.cancel_event.set()
    with pytest.raises(InterruptedError):
        frame.jobs[0][1]()
    assert captured == [(original, 'library', 'c')]


def test_rescan_uses_same_job_runner_after_dialog_is_destroyed(module, snapshot, monkeypatch):
    frame = host(object())
    calls = []
    monkeypatch.setattr(module, 'catalog_quality', lambda *args, **kwargs: calls.append(kwargs))
    dialogs = fake_dialog(module, monkeypatch, result=module.wx.ID_APPLY, collection='c')
    module.open_catalog_quality(frame)
    frame.jobs[0][2](snapshot)
    assert dialogs[0].destroyed and len(frame.jobs) == 2
    frame.jobs[1][1]()
    assert calls[0]['collection'] == 'c'


def test_editor_callback_receives_scoped_identity_only_after_dialog_closes(module, snapshot, monkeypatch):
    frame = host(object())
    editor = Mock()
    dialogs = fake_dialog(module, monkeypatch, result=module.wx.ID_OK)
    module.open_catalog_quality(frame, edit_edition=editor)
    frame.jobs[0][2](snapshot)
    assert dialogs[0].can_edit and dialogs[0].destroyed
    editor.assert_called_once_with('library', 'e2')


def test_scope_change_while_modal_is_open_prevents_followup_edit(module, snapshot, monkeypatch):
    frame = host(object())
    frame.edit_catalog_edition = Mock()
    dialogs = fake_dialog(module, monkeypatch, result=module.wx.ID_OK,
        after_show=lambda: setattr(frame, 'active', ('other', None)))
    module.open_catalog_quality(frame)
    frame.jobs[0][2](snapshot)
    assert dialogs[0].destroyed
    frame.edit_catalog_edition.assert_not_called()


@pytest.mark.parametrize('problem', ['working', 'busy', 'closing', 'missing_workspace'])
def test_busy_or_closed_host_cannot_start_check(module, problem):
    frame = host(object())
    if problem == 'missing_workspace':
        def missing(): raise ValueError('Open a library first.')
        frame._workspace = missing
    else:
        setattr(frame, '_closing' if problem == 'closing' else problem, True)
    module.open_catalog_quality(frame)
    assert not frame.jobs
    assert frame.reports or problem == 'closing'
