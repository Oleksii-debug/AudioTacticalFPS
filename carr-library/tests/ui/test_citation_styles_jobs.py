"""Production dialog handlers with controlled Futures and wx test doubles.

These verify asynchronous job lifetimes and real export cancellation. Native
Windows widgets, keyboard traversal and screen readers are separate checks.
"""
from concurrent.futures import Future
import sys
from types import ModuleType, SimpleNamespace as N
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import ExportReceipt, write_export
from carr_library.ui.citation_styles_dialog import create_citation_styles_dialog


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.selection = -1
        self.enabled = True
        self.focused = self.destroyed = self.hidden = False
        self.bindings = {}
        self.modal_result = None

    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, value): self.name = value
    def SetValue(self, value): self.value = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def Enable(self, value): self.enabled = value
    def SetFocus(self): self.focused = True
    def Destroy(self): self.destroyed = True
    def IsModal(self): return True
    def EndModal(self, result): self.modal_result = result
    def Hide(self): self.hidden = True
    def SetReturnCode(self, result): self.modal_result = result
    def Add(self, *args, **kwargs): pass
    def AddGrowableCol(self, *args): pass
    def Wrap(self, *args): pass
    def SetMinSize(self, *args): pass
    def SetSizer(self, *args): pass
    def SetEscapeId(self, *args): pass
    def SetInsertionPoint(self, *args): pass


class DeferredExecutor:
    def __init__(self):
        self.calls = []
        self.shutdown = Mock()

    def submit(self, operation, *args, **kwargs):
        future = Future()
        # Cancellation must also handle work that is already running.
        assert future.set_running_or_notify_cancel()
        self.calls.append((operation, args, kwargs, future))
        return future

    def finish(self, index):
        operation, args, kwargs, future = self.calls[index]
        try:
            future.set_result(operation(*args, **kwargs))
        except Exception as exc:
            future.set_exception(exc)


@pytest.fixture
def form(tmp_path, monkeypatch):
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'FlexGridSizer', 'StaticText', 'Choice', 'Button', 'TextCtrl'):
        setattr(wx, name, Control)
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
        'EXPAND', 'ALL', 'ALIGN_CENTER_VERTICAL', 'TE_MULTILINE', 'TE_READONLY',
        'LEFT', 'RIGHT', 'BOTTOM', 'ALIGN_RIGHT', 'ID_CANCEL', 'ID_OK', 'EVT_BUTTON',
        'EVT_CHOICE', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY', 'FD_SAVE', 'FD_OVERWRITE_PROMPT')
    for index, name in enumerate(constants):
        setattr(wx, name, 1 << index)
    queued = []
    wx.CallAfter = lambda callback, *args: queued.append((callback, args))
    wx.SingleChoiceDialog = Mock(return_value=N(ShowModal=lambda: wx.ID_OK,
        GetSelection=lambda: 0, Destroy=Mock()))
    destination = tmp_path / 'bibliography.txt'
    wx.FileDialog = Mock(return_value=N(ShowModal=lambda: wx.ID_OK,
        GetPath=lambda: str(destination), Destroy=Mock()))
    wx.TheClipboard = N(Open=Mock(return_value=True), SetData=Mock(return_value=True), Close=Mock())
    wx.TextDataObject = lambda value: value
    monkeypatch.setitem(sys.modules, 'wx', wx)
    executor = DeferredExecutor()
    dialog = create_citation_styles_dialog(None, N(workspace='selected-library'),
        selected_identity='selected-reference', executor=executor)
    dialog.workbench = N(preview=Mock(), check=Mock(),
                         export=Mock(return_value=ExportReceipt(destination)), copy_text=Mock())

    def drain():
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    return N(dialog=dialog, executor=executor, wx=wx, drain=drain, destination=destination,
             private=tmp_path / 'private')


def preview(text):
    return N(explanation=text, result=N(entries=(text,)))


def test_options_change_invalidates_running_preview_before_new_completion(form):
    dialog, executor = form.dialog, form.executor
    old, current = preview('Old style'), preview('Current style')
    dialog.workbench.preview.side_effect = [old, current]
    dialog.on_preview()
    dialog.style_choice.SetSelection(2)
    dialog.on_options_changed()
    dialog.on_preview()
    current_job = dialog.pending
    executor.finish(0)
    form.drain()
    assert dialog.preview is None and dialog.pending is current_job
    assert dialog.output.value == ''
    dialog.workbench.check.assert_not_called()
    executor.finish(1)
    form.drain()
    assert dialog.preview is current and dialog.output.value == 'Current style'
    assert dialog.copy_bibliography.enabled and dialog.pending is None


def test_export_is_queued_and_does_not_render_in_native_event_handler(form):
    dialog = form.dialog
    selected = preview('Approved preview')
    dialog.preview = selected
    dialog.on_export()
    dialog.workbench.export.assert_not_called()
    assert dialog.pending is not None and not dialog.generate.enabled
    form.executor.finish(0)
    form.drain()
    assert dialog.workbench.export.call_args.args == (selected, str(form.destination))
    assert dialog.workbench.export.call_args.kwargs['return_receipt'] is True
    assert 'saved to' in dialog.status.value and dialog.pending is None
    assert dialog.generate.enabled and dialog.copy_bibliography.enabled


def test_saved_export_with_cleanup_warning_remains_successful_and_accessible(form):
    dialog = form.dialog
    dialog.preview = preview('Approved preview')
    warning = 'The file is saved. Temporary-file cleanup is still pending.'
    dialog.workbench.export.return_value = ExportReceipt(form.destination, warning)
    dialog.on_export()
    form.executor.finish(0)
    form.drain()
    assert 'saved to' in dialog.status.value
    assert warning in dialog.status.value and 'not exported' not in dialog.status.value
    assert dialog.status.focused and dialog.pending is None
    assert dialog.generate.enabled and dialog.export_button.enabled


def test_new_job_does_not_clear_old_export_cancellation_or_publish_its_file(form):
    dialog = form.dialog
    form.private.mkdir()
    dialog.preview = preview('Previous preview')

    def export_file(selected, destination, *, format, cancelled, return_receipt):
        return write_export(destination, 'Obsolete export', private_root=form.private, cancelled=cancelled)

    dialog.workbench.export.side_effect = export_file
    dialog.on_export()
    old_cancelled = form.executor.calls[0][2]['cancelled']
    dialog.on_options_changed()
    dialog.workbench.preview.return_value = preview('New preview')
    dialog.on_preview()
    assert old_cancelled() and not dialog.cancelled.is_set()
    current_job = dialog.pending
    form.executor.finish(0)
    form.drain()
    assert not form.destination.exists()
    assert dialog.pending is current_job and 'not exported' not in dialog.status.value
    form.executor.finish(1)
    form.drain()
    assert dialog.output.value == 'New preview'


@pytest.mark.parametrize('destroy', [False, True])
def test_closed_or_destroyed_dialog_ignores_late_preview_delivery(form, destroy):
    dialog = form.dialog
    dialog.workbench.preview.return_value = preview('Late preview must stay hidden')
    dialog.on_preview()
    if destroy:
        event = N(GetEventObject=lambda: dialog, Skip=Mock())
        dialog.on_destroy(event)
        event.Skip.assert_called_once_with()
        form.executor.shutdown.assert_not_called()  # The parent owns this executor.
    else:
        dialog.on_close()
        assert dialog.modal_result == form.wx.ID_CANCEL
    before = dialog.status.value
    form.executor.finish(0)
    form.drain()
    assert dialog.output.value == '' and dialog.status.value == before
    dialog.workbench.check.assert_not_called()


def test_completed_preview_is_reauthorized_before_controls_enable_copy(form):
    dialog = form.dialog
    dialog.workbench.preview.return_value = preview('Stale references')
    dialog.workbench.check.side_effect = ValueError('References changed before delivery')
    dialog.on_preview()
    form.executor.finish(0)
    form.drain()
    assert dialog.preview is None and dialog.output.value == ''
    assert not dialog.copy_bibliography.enabled and not dialog.export_button.enabled
    assert 'changed before delivery' in dialog.status.value


def test_copy_authorization_failure_never_opens_clipboard(form):
    dialog = form.dialog
    dialog.preview = preview('Stale bibliography')
    dialog.workbench.copy_text.side_effect = ValueError('The selected library is archived')
    dialog.on_copy('bibliography')
    form.wx.TheClipboard.Open.assert_not_called()
    assert 'archived' in dialog.status.value
