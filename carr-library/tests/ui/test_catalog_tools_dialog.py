"""Real dialog handlers and checked disk exports with isolated wx controls.

Native widget construction and focus have a separate smoke probe; these tests
do not claim Windows or NVDA acceptance.
"""
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import pytest


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.items = list(kwargs.get('choices', ()))
        self.index = -1
        self.enabled = True
        self.focused = False
        self.destroyed = False
        self.bindings = {}
        self.modal_result = None
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, name): self.name = name
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, index): self.index = index
    def GetSelection(self): return self.index
    def SetFocus(self): self.focused = True
    def SetInsertionPoint(self, value): self.insertion = value
    def SetMaxLength(self, value): self.max_length = value
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def SetEscapeId(self, value): self.escape_id = value
    def Add(self, *args): pass
    def Enable(self, enabled): self.enabled = enabled
    def EndModal(self, result): self.modal_result = result
    def ShowModal(self): return self.modal_result
    def Destroy(self): self.destroyed = True


@pytest.fixture
def module(monkeypatch):
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'TextCtrl', 'Choice', 'Button'):
        setattr(wx, name, Control)
    for n, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT',
            'TOP', 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY', 'ID_CANCEL', 'ID_OK',
            'EVT_BUTTON', 'EVT_CLOSE', 'EVT_CHOICE', 'FD_SAVE', 'FD_OVERWRITE_PROMPT',
            'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'YES', 'NO')):
        setattr(wx, name, 1 << n)
    wx.callbacks = []
    wx.CallAfter = lambda callback: wx.callbacks.append(callback)
    wx.files = []
    wx.file_result = wx.ID_OK
    wx.file_path = ''
    wx.file_hook = None
    wx.MessageBox = Mock(return_value=wx.YES)

    class FileDialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.options = kwargs
            wx.files.append(self)
        def ShowModal(self):
            if wx.file_hook:
                wx.file_hook()
            return wx.file_result
        def GetPath(self): return wx.file_path
    wx.FileDialog = FileDialog
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/catalog_tools_dialog.py'
    spec = importlib.util.spec_from_file_location('carr_library.ui._catalog_tools_independent', path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    spec.loader.exec_module(mod)
    return mod


class Library:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.original = root / 'original.txt'
        self.original.write_bytes(b'unchanged original source')
        self.calls = []
        self.report_value = 'Sources: 2\nMissing author: 1\nPotential duplicate files: 1\n'
        self.export_value = 'title,note\n"Книга — Étude","Separate note"\r\n'
        self.hook = None
    def collections(self, workspace):
        self.calls.append(('collections', workspace))
        return (('c1', 'Журнали'), ('c2', 'History'))
    def catalog_report(self, workspace, *, collection, cancelled):
        self.calls.append(('report', workspace, collection))
        if self.hook: self.hook()
        return self.report_value
    def export_catalog(self, workspace, *, collection, format, cancelled):
        self.calls.append(('catalog', workspace, collection, format))
        if self.hook: self.hook()
        return self.export_value
    def export_review_matrix(self, workspace, *, collection, cancelled):
        self.calls.append(('matrix', workspace, collection))
        if self.hook: self.hook()
        return self.export_value


class Host:
    def __init__(self, library, wx):
        self.library, self.wx = library, wx
        self.active = ('a', None)
        self.working = self.busy = self._closing = False
        self.cancel_event = threading.Event()
        self.jobs = []
        self.messages = []
        self.status = Control()
        panel = SimpleNamespace(workspace='a', ready=True, _selection=lambda: ('c2', None, None))
        self.book = SimpleNamespace(GetPage=lambda index: panel)
        self.reject = False
    def _workspace(self):
        if self.active is None: raise ValueError('Open a library first.')
        return self.active[0]
    def report(self, message): self.messages.append(message)
    def run_job(self, title, operation, done=None, failed=None):
        if self.reject: return
        self.cancel_event.clear()
        self.working = self.busy = True
        self.jobs.append((title, operation, done, failed))
    def work(self):
        _, operation, done, failed = self.jobs.pop(0)
        try:
            value = operation()
        except Exception as exc:
            callback = lambda error=exc: failed(error)
        else:
            callback = lambda: done(value)
        def finish():
            self.working = self.busy = False
            callback()
        self.wx.CallAfter(finish)
    def finish(self):
        self.work()
        while self.wx.callbacks:
            self.wx.callbacks.pop(0)()


@pytest.fixture
def setup(module, tmp_path):
    library = Library(tmp_path / 'private')
    frame = Host(library, module.wx)
    form = module.LibraryToolsDialog(frame, 'a', library.collections('a'))
    library.calls.clear()
    module.wx.file_path = str(tmp_path / 'export.csv')
    return form, frame, library


def test_named_controls_scope_and_initial_keyboard_focus(module, setup):
    form, frame, _ = setup
    assert form.scope.name == 'Report and export scope'
    assert form.scope.items == ['Entire current library', 'Collection: Журнали', 'Collection: History']
    assert form.scope.focused and form.scope.GetSelection() == 0
    assert form.report.name == 'Library overview and quality report'
    assert form.report.max_length == module.REPORT_MAX_CHARACTERS
    assert form.format.items == ['RIS (.ris)', 'BibTeX (.bib)', 'CSL JSON (.json)']
    assert form.format.GetSelection() == 2
    assert not form.cancel_button.enabled and form.close_button.enabled
    assert form.escape_id == module.wx.ID_CANCEL


def test_report_runs_asynchronously_and_is_shown_completely_with_focus(setup):
    form, frame, library = setup
    form.scope.SetSelection(1)
    form.on_scope_changed()
    assert form.refresh_report()
    assert form.pending and library.calls == [] and form.report.GetValue() == ''
    assert not form.scope.enabled and not form.close_button.enabled
    assert form.cancel_button.enabled
    assert form.refresh_report() is False and len(frame.jobs) == 1
    frame.work()
    assert form.pending and form.report.GetValue() == ''
    frame.wx.callbacks.pop(0)()
    assert library.calls == [('report', 'a', 'c1')]
    assert form.report.GetValue() == library.report_value
    assert not form.pending and form.report.focused and form.report.insertion == 0
    assert form.scope.enabled and form.close_button.enabled and not form.cancel_button.enabled


def test_scope_change_clears_old_report_without_submitting_a_job(setup):
    form, frame, _ = setup
    form.report.SetValue('Previous collection report')
    form.scope.SetSelection(2)
    form.on_scope_changed()
    assert form.report.GetValue() == '' and frame.jobs == []
    assert 'Scope changed' in form.status.GetValue()


@pytest.mark.parametrize('kind, index, expected', [
    ('bibliography', 0, ('catalog', 'a', 'c2', 'ris')),
    ('bibliography', 1, ('catalog', 'a', 'c2', 'bibtex')),
    ('bibliography', 2, ('catalog', 'a', 'c2', 'csl-json')),
    ('inventory', 2, ('catalog', 'a', 'c2', 'csv')),
    ('matrix', 2, ('matrix', 'a', 'c2')),
])
def test_exports_forward_exact_scope_format_and_write_complete_utf8(module, setup, kind, index, expected):
    form, frame, library = setup
    form.scope.SetSelection(2)
    form.format.SetSelection(index)
    assert getattr(form, 'export_' + kind)()
    assert module.wx.files[0].destroyed
    style = module.wx.files[0].options['style']
    assert style & module.wx.FD_SAVE and not style & module.wx.FD_OVERWRITE_PROMPT
    destination = Path(module.wx.file_path)
    assert not destination.exists() and library.calls == []
    frame.finish()
    assert library.calls == [expected]
    assert destination.read_bytes() == library.export_value.encode('utf-8')
    assert library.original.read_bytes() == b'unchanged original source'
    assert 'exported to' in form.status.GetValue() and form.status.focused
    assert form.close_button.enabled and not form.pending


def test_completed_export_keeps_cleanup_warning_visible(module, setup, monkeypatch):
    from dataclasses import replace

    form, frame, library = setup
    actual_write = module.write_export
    warning = 'Synthetic private staging cleanup needs attention.'

    def write_with_warning(*args, **kwargs):
        return replace(actual_write(*args, **kwargs), cleanup_warning=warning)

    monkeypatch.setattr(module, 'write_export', write_with_warning)
    assert form.export_inventory()
    frame.finish()
    assert Path(module.wx.file_path).read_bytes() == library.export_value.encode('utf-8')
    assert library.original.read_bytes() == b'unchanged original source'
    assert 'exported to' in form.status.GetValue() and warning in form.status.GetValue()
    assert form.status.focused and not form.pending


def test_cancel_file_dialog_restores_initiating_button_without_job(module, setup):
    form, frame, library = setup
    module.wx.file_result = module.wx.ID_CANCEL
    assert form.export_inventory() is False
    assert module.wx.files[0].destroyed and frame.jobs == [] and library.calls == []
    assert form.inventory_button.focused and 'No file was written' in form.status.GetValue()


@pytest.mark.parametrize('change', ['workspace', 'library', 'collection', 'invalid_collection'])
def test_scope_change_during_destination_dialog_blocks_export(module, setup, change):
    form, frame, library = setup
    def alter():
        if change == 'workspace': frame.active = ('other', None)
        elif change == 'library': frame.library = object()
        elif change == 'collection': form.scope.SetSelection(1)
        else: form.scope.SetSelection(-1)
    module.wx.file_hook = alter
    assert form.export_inventory() is False
    assert frame.jobs == [] and library.calls == []
    assert 'changed' in form.status.GetValue()
    assert not Path(module.wx.file_path).exists()


def test_cancellation_during_export_keeps_existing_file_unchanged(module, setup):
    form, frame, library = setup
    destination = Path(module.wx.file_path)
    destination.write_bytes(b'previous export')
    library.hook = frame.cancel_event.set
    assert form.export_matrix()
    frame.finish()
    assert destination.read_bytes() == b'previous export'
    assert 'cancelled' in form.status.GetValue() and 'not changed' in form.status.GetValue()
    assert not form.pending


def test_changed_library_before_publication_prevents_stale_export(module, setup):
    form, frame, library = setup
    library.hook = lambda: setattr(frame, 'active', ('other', None))
    assert form.export_bibliography()
    frame.finish()
    assert not Path(module.wx.file_path).exists()
    assert form.report.GetValue() == '' and 'changed' in form.status.GetValue()


def test_late_report_callback_does_not_display_foreign_scope_data(setup):
    form, frame, _ = setup
    assert form.refresh_report()
    frame.work()
    frame.active = ('other', None)
    frame.wx.callbacks.pop(0)()
    assert form.report.GetValue() == '' and 'changed' in form.status.GetValue()
    assert frame.messages == []


def test_checked_writer_refuses_private_source_overwrite(module, setup):
    form, frame, library = setup
    module.wx.file_path = str(library.original)
    assert form.export_inventory() is False
    assert frame.jobs == [] and library.calls == []
    assert library.original.read_bytes() == b'unchanged original source'
    assert 'outside the private library folder' in form.status.GetValue()
    assert not form.pending and form.inventory_button.enabled


@pytest.mark.parametrize('kind', ['bibliography', 'inventory', 'matrix'])
def test_catalog_export_preserves_file_changed_during_render(module, setup, kind):
    form, frame, library = setup
    target = Path(module.wx.file_path)
    target.write_bytes(b'Approved previous export')
    library.hook = lambda: target.write_bytes(b'Independent edit during rendering')
    assert getattr(form, 'export_' + kind)()
    frame.finish()
    assert target.read_bytes() == b'Independent edit during rendering'
    assert 'destination changed' in form.status.GetValue()
    assert not list(target.parent.glob('.research-export-*'))


def test_report_limit_is_explicit_and_does_not_show_a_partial_result(module, setup):
    form, frame, library = setup
    library.report_value = 'X' * (module.REPORT_MAX_CHARACTERS + 1)
    assert form.refresh_report()
    frame.finish()
    assert form.report.GetValue() == ''
    assert '500,000' in form.status.GetValue() and 'smaller collection' in form.status.GetValue()


def test_native_text_truncation_is_detected_and_cleared(setup):
    form, frame, _ = setup
    form.report.SetValue = lambda value: setattr(form.report, 'value', value[:10])
    assert form.refresh_report()
    frame.finish()
    assert form.report.GetValue() == ''
    assert 'complete report could not be displayed' in form.status.GetValue()


def test_cancel_operation_waits_for_job_acknowledgement(module, setup):
    form, frame, library = setup
    assert form.refresh_report()
    form.cancel_operation()
    assert form.pending and frame.cancel_event.is_set()
    assert form.modal_result is None and 'Cancellation requested' in form.status.GetValue()
    frame.finish()
    assert library.calls == [] and not form.pending and form.modal_result is None
    assert 'cancelled' in form.status.GetValue()


def test_close_during_job_vetoes_then_closes_after_cancellation(module, setup):
    form, frame, _ = setup
    assert form.refresh_report()
    event = SimpleNamespace(vetoed=False, skipped=False)
    event.CanVeto = lambda: True
    event.Veto = lambda: setattr(event, 'vetoed', True)
    event.Skip = lambda: setattr(event, 'skipped', True)
    form.on_close(event)
    assert event.vetoed and not event.skipped and form.modal_result is None
    frame.finish()
    assert not form.pending and form.modal_result == module.wx.ID_CANCEL


def test_forced_close_drops_late_callbacks_without_touching_destroyed_controls(module, setup):
    form, frame, _ = setup
    assert form.refresh_report()
    event = SimpleNamespace(skipped=False, CanVeto=lambda: False)
    event.Skip = lambda: setattr(event, 'skipped', True)
    form.on_close(event)
    assert event.skipped and frame.cancel_event.is_set()
    form.status.SetValue = lambda value: (_ for _ in ()).throw(AssertionError('destroyed control touched'))
    frame.finish()


def test_rejected_job_submission_reenables_controls_for_retry(setup):
    form, frame, _ = setup
    frame.reject = True
    form.refresh_report()
    assert not form.pending and form.close_button.enabled and form.refresh_button.enabled
    assert 'could not be started' in form.status.GetValue()


@pytest.mark.parametrize('problem', ['busy', 'no_workspace', 'changed_library', 'invalid_format', 'invalid_scope'])
def test_invalid_state_stops_before_export_file_dialog(module, setup, problem):
    form, frame, library = setup
    if problem == 'busy': frame.busy = True
    elif problem == 'no_workspace': frame.active = None
    elif problem == 'changed_library': frame.library = object()
    elif problem == 'invalid_format': form.format.SetSelection(-1)
    else: form.scope.SetSelection(-1)
    assert form.export_bibliography() is False
    assert module.wx.files == [] and frame.jobs == [] and library.calls == []
    assert form.status.focused


def test_wrapper_uses_fresh_collections_and_active_catalog_scope(module, setup, monkeypatch):
    _, frame, library = setup
    created = []
    real = module.LibraryToolsDialog
    class Dialog(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
    monkeypatch.setattr(module, 'LibraryToolsDialog', Dialog)
    module.show_catalog_tools(frame)
    form, = created
    assert library.calls == [('collections', 'a')]
    assert form._selected_collection() == 'c2'
    assert form.destroyed and form._closed and frame.status.focused
    assert len(module.wx.callbacks) == 1
    module.wx.callbacks.pop(0)()
    assert frame.jobs == []  # A queued initial refresh cannot outlive a closed dialog.


def test_wrapper_rechecks_scope_after_collection_fetch(module, setup, monkeypatch):
    _, frame, library = setup
    def changed(workspace):
        frame.active = ('other', None)
        return (('c1', 'Collection'),)
    monkeypatch.setattr(library, 'collections', changed)
    module.show_catalog_tools(frame)
    assert not module.wx.callbacks and not frame.jobs
    assert 'changed' in frame.messages[-1]


@pytest.mark.parametrize('rows', [(('same', 'A'), ('same', 'B')), ((None, 'A'),), (('a', ''),)])
def test_wrapper_rejects_ambiguous_collection_identifiers(module, setup, monkeypatch, rows):
    _, frame, library = setup
    monkeypatch.setattr(library, 'collections', lambda workspace: rows)
    module.show_catalog_tools(frame)
    assert not module.wx.callbacks and 'Collection choices changed' in frame.messages[-1]


@pytest.mark.parametrize('kind', ['bibliography', 'inventory', 'matrix'])
@pytest.mark.parametrize('existing', [False, True])
def test_each_catalog_export_preserves_competing_file_during_render(module, setup, kind, existing):
    form, frame, library = setup
    destination = Path(module.wx.file_path)
    if existing: destination.write_bytes(b'Previously approved output')
    library.hook = lambda: destination.write_bytes(b'Newer independent work')
    assert getattr(form, 'export_' + kind)()
    frame.finish()
    assert destination.read_bytes() == b'Newer independent work'
    assert 'destination changed' in form.status.GetValue()
    assert not list(destination.parent.glob('.research-export-*'))
    assert not form.pending


def test_catalog_overwrite_decline_restores_button_without_render(module, setup):
    form, frame, library = setup
    destination = Path(module.wx.file_path)
    destination.write_bytes(b'Keep work')
    module.wx.MessageBox.return_value = module.wx.NO
    assert form.export_inventory() is False
    assert frame.jobs == [] and library.calls == [] and form.inventory_button.focused
    assert destination.read_bytes() == b'Keep work'
    module.wx.MessageBox.assert_called_once()
