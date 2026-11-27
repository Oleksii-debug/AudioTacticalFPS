"""Production dialog handlers with controlled Futures and wx test doubles.

These verify asynchronous job lifetimes and real export cancellation. Native
Windows widgets, keyboard traversal and screen readers are separate checks.
"""
from concurrent.futures import Future
import sys
from types import ModuleType, SimpleNamespace as N
from unittest.mock import Mock

import pytest

from carr_library.research.export_file import write_export
from carr_library.ui import citation_styles_dialog as style_dialog
from carr_library.ui.citation_styles_dialog import create_citation_styles_dialog


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.label = kwargs.get('label', '')
        self.choices = list(kwargs.get('choices', ()))
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
    def Append(self, value): self.choices.append(value)
    def SetString(self, index, value): self.choices[index] = value
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
        'EVT_CHOICE', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY', 'FD_SAVE', 'FD_OVERWRITE_PROMPT',
        'FD_OPEN', 'FD_FILE_MUST_EXIST', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION',
        'ID_YES', 'ID_NO')
    for index, name in enumerate(constants):
        setattr(wx, name, 1 << index)
    queued = []
    wx.CallAfter = lambda callback, *args: queued.append((callback, args))
    wx.SingleChoiceDialog = Mock(return_value=N(ShowModal=lambda: wx.ID_OK,
        GetSelection=lambda: 0, Destroy=Mock()))
    destination = tmp_path / 'bibliography.txt'
    wx.FileDialog = Mock(return_value=N(ShowModal=lambda: wx.ID_OK,
        GetPath=lambda: str(destination), Destroy=Mock()))
    wx.MessageDialog = Mock(return_value=N(ShowModal=lambda: wx.ID_YES, Destroy=Mock()))
    wx.TheClipboard = N(Open=Mock(return_value=True), SetData=Mock(return_value=True), Close=Mock())
    wx.TextDataObject = lambda value: value
    monkeypatch.setitem(sys.modules, 'wx', wx)
    executor = DeferredExecutor()
    dialog = create_citation_styles_dialog(None,
        N(workspace='selected-library', library=N(root=tmp_path / 'private')),
        selected_identity='selected-reference', executor=executor)
    dialog.workbench = N(preview=Mock(), check=Mock(), export=Mock(), copy_text=Mock())

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
    dialog.workbench.export.assert_not_called()
    assert len(form.executor.calls) == 2 and not dialog.generate.enabled
    form.executor.finish(1)
    form.drain()
    assert dialog.workbench.export.call_args.args == (selected, str(form.destination))
    assert 'saved to' in dialog.status.value and dialog.pending is None
    assert dialog.generate.enabled and dialog.copy_bibliography.enabled


def test_new_job_does_not_clear_old_export_cancellation_or_publish_its_file(form):
    dialog = form.dialog
    form.private.mkdir()
    dialog.preview = preview('Previous preview')

    def export_file(selected, destination, *, format, cancelled, require_absent, approval):
        write_export(destination, 'Obsolete export', private_root=form.private,
                     cancelled=cancelled, require_absent=require_absent, approval=approval)

    dialog.workbench.export.side_effect = export_file
    dialog.on_export()
    form.executor.finish(0)
    form.drain()
    old_cancelled = form.executor.calls[1][2]['cancelled']
    dialog.on_options_changed()
    dialog.workbench.preview.return_value = preview('New preview')
    dialog.on_preview()
    assert old_cancelled() and not dialog.cancelled.is_set()
    current_job = dialog.pending
    form.executor.finish(1)
    form.drain()
    assert not form.destination.exists()
    assert dialog.pending is current_job and 'not exported' not in dialog.status.value
    form.executor.finish(2)
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


def test_custom_load_replaces_one_slot_and_invalidates_the_previous_preview(form, monkeypatch):
    dialog = form.dialog
    first, second = N(info=N(title='First local style')), N(info=N(title='Second local style'))
    loader = Mock(side_effect=(first, second))
    monkeypatch.setattr(style_dialog, 'load_style_file', loader)
    installed = tuple(dialog.style_choice.choices)
    dialog.preview = preview('Previous bibliography')
    dialog.output.SetValue('Previous bibliography')
    dialog.controls(pending=False)
    previous_event = dialog.cancelled
    dialog.style_choice.focused = False

    dialog.on_load_style()

    loader.assert_called_once_with(str(form.destination))
    assert dialog.custom_style is first
    assert tuple(dialog.style_choice.choices) == (*installed, 'Custom: First local style')
    assert dialog.style_choice.GetSelection() == len(installed)
    assert dialog.preview is None and dialog.output.value == ''
    assert not dialog.copy_bibliography.enabled and not dialog.export_button.enabled
    assert previous_event.is_set()
    assert dialog.style_choice.focused and dialog.custom_style_button.enabled
    assert '&' in dialog.custom_style_button.label
    assert dialog.custom_style_button.bindings[form.wx.EVT_BUTTON] == dialog.on_load_style

    dialog.on_load_style()

    assert dialog.custom_style is second
    assert tuple(dialog.style_choice.choices) == (*installed, 'Custom: Second local style')
    assert dialog.style_choice.GetSelection() == len(installed)
    assert form.wx.FileDialog.return_value.Destroy.call_count == 2
    assert 'loaded locally' in dialog.status.value


@pytest.mark.parametrize('outcome', ('cancel', 'invalid'))
def test_cancelled_or_invalid_custom_load_preserves_the_valid_style_and_preview(form, monkeypatch, outcome):
    dialog = form.dialog
    custom = N(info=N(title='Previously loaded style'))
    loader = Mock(return_value=custom)
    monkeypatch.setattr(style_dialog, 'load_style_file', loader)
    dialog.on_load_style()
    completed = preview('Valid previous bibliography')
    dialog.workbench.preview.return_value = completed
    dialog.on_preview()
    form.executor.finish(0)
    form.drain()
    selection, choices = dialog.style_choice.GetSelection(), tuple(dialog.style_choice.choices)
    previous_event = dialog.cancelled
    loader.reset_mock()
    if outcome == 'cancel':
        form.wx.FileDialog.return_value.ShowModal = lambda: form.wx.ID_CANCEL
    else:
        loader.side_effect = ValueError('The selected CSL file is invalid')

    dialog.on_load_style()

    assert dialog.custom_style is custom and dialog.preview is completed
    assert dialog.output.value == completed.explanation
    assert dialog.style_choice.GetSelection() == selection
    assert tuple(dialog.style_choice.choices) == choices
    assert dialog.cancelled is previous_event and not previous_event.is_set()
    assert dialog.copy_bibliography.enabled and dialog.export_button.enabled
    assert dialog.pending is None and form.wx.FileDialog.return_value.Destroy.call_count == 2
    if outcome == 'cancel':
        loader.assert_not_called()
    else:
        assert 'invalid' in dialog.status.value and dialog.status.focused


@pytest.mark.parametrize('operation', ('preview', 'export'))
def test_pending_job_disables_custom_load_and_ignores_queued_load_events(form, monkeypatch, operation):
    dialog = form.dialog
    loader = Mock(return_value=N(info=N(title='Must not be loaded')))
    monkeypatch.setattr(style_dialog, 'load_style_file', loader)
    dialog.workbench.preview.return_value = preview('Completed bibliography')
    if operation == 'preview':
        dialog.on_preview()
    else:
        dialog.preview = preview('Previous bibliography')
        dialog.on_export()
    job = dialog.pending
    form.wx.FileDialog.reset_mock()

    assert not dialog.custom_style_button.enabled and dialog.close_button.enabled
    dialog.on_load_style()

    form.wx.FileDialog.assert_not_called()
    loader.assert_not_called()
    assert dialog.pending is job
    form.executor.finish(0)
    form.drain()
    if operation == 'export':
        assert dialog.pending is not None and not dialog.custom_style_button.enabled
        form.executor.finish(1)
        form.drain()
    assert dialog.pending is None and dialog.custom_style_button.enabled


def test_custom_jobs_capture_their_own_style_and_replaced_completion_stays_hidden(form, monkeypatch):
    dialog = form.dialog
    first, second = N(info=N(title='First custom style')), N(info=N(title='Second custom style'))
    monkeypatch.setattr(style_dialog, 'load_style_file', Mock(side_effect=(first, second)))
    obsolete, current = preview('Obsolete custom output'), preview('Current custom output')
    dialog.workbench.preview.side_effect = lambda **options: obsolete if options['style'] is first else current
    dialog.on_load_style()
    dialog.on_preview()
    old_event = dialog.cancelled
    assert form.executor.calls[0][2]['style'] is first

    dialog.on_options_changed()
    dialog.on_load_style()
    dialog.on_preview()

    assert form.executor.calls[1][2]['style'] is second
    assert old_event.is_set() and dialog.cancelled is not old_event and not dialog.cancelled.is_set()
    assert len(dialog.style_choice.choices) == len(dialog.styles) + 1
    # Deliver the replacement first; an older running completion must not
    # overwrite the already visible, freshly authorized custom bibliography.
    form.executor.finish(1)
    form.drain()
    assert dialog.preview is current and dialog.output.value == current.explanation
    form.executor.finish(0)
    form.drain()
    assert dialog.preview is current and dialog.output.value == current.explanation
    dialog.workbench.check.assert_called_once_with(current)


def test_installed_selection_after_custom_load_submits_the_installed_style_id(form, monkeypatch):
    dialog = form.dialog
    custom = N(info=N(title='Local style'))
    monkeypatch.setattr(style_dialog, 'load_style_file', Mock(return_value=custom))
    dialog.on_load_style()
    dialog.style_choice.SetSelection(2)
    dialog.on_options_changed()
    dialog.on_preview()
    assert dialog.custom_style is custom
    assert form.executor.calls[0][2]['style'] == dialog.styles[2].id


@pytest.mark.parametrize('loaded', (False, True))
def test_unavailable_custom_selection_does_not_submit_a_formatting_job(form, monkeypatch, loaded):
    dialog = form.dialog
    if loaded:
        monkeypatch.setattr(style_dialog, 'load_style_file', Mock(return_value=N(info=N(title='Local style'))))
        dialog.on_load_style()
    dialog.style_choice.SetSelection(len(dialog.styles) + int(loaded))
    dialog.on_preview()
    assert form.executor.calls == [] and dialog.pending is None
    assert dialog.preview is None and not dialog.copy_bibliography.enabled
    assert dialog.generate.enabled and dialog.custom_style_button.enabled
    assert 'load a custom CSL file' in dialog.status.value


def use_real_writer(form):
    def publish(selected, destination, *, format, cancelled, require_absent, approval):
        write_export(destination, selected.explanation, private_root=form.private,
                     cancelled=cancelled, require_absent=require_absent, approval=approval)
    form.dialog.workbench.export.side_effect = publish
    form.dialog.preview = preview('New approved bibliography')


def test_native_new_destination_preserves_file_created_while_export_is_queued(form):
    use_real_writer(form)
    form.dialog.on_export()
    form.executor.finish(0)
    form.drain()
    assert form.executor.calls[1][2]['require_absent'] is True
    form.wx.MessageDialog.assert_not_called()
    form.destination.write_bytes(b'Another program created this after file selection')

    form.executor.finish(1)
    form.drain()

    assert form.destination.read_bytes() == b'Another program created this after file selection'
    assert 'not exported' in form.dialog.status.value
    assert 'changed' in form.dialog.status.value
    assert not list(form.destination.parent.glob('.research-export-*'))


def test_native_existing_destination_requires_confirmation_and_can_be_replaced(form):
    use_real_writer(form)
    form.destination.write_bytes(b'Existing bibliography')
    form.dialog.on_export()
    form.wx.MessageDialog.assert_not_called()
    form.executor.finish(0)
    form.drain()
    form.wx.MessageDialog.assert_called_once()
    assert form.wx.MessageDialog.call_args.kwargs['style'] & form.wx.NO_DEFAULT
    assert form.executor.calls[1][2]['require_absent'] is False
    assert form.executor.calls[1][2]['approval'].state is not None

    form.executor.finish(1)
    form.drain()

    assert form.destination.read_text() == 'New approved bibliography'
    assert 'saved to' in form.dialog.status.value
    form.wx.MessageDialog.return_value.Destroy.assert_called_once_with()


def test_native_declined_overwrite_does_not_submit_or_change_file(form):
    use_real_writer(form)
    form.destination.write_bytes(b'Keep existing bibliography')
    form.wx.MessageDialog.return_value.ShowModal = lambda: form.wx.ID_NO

    form.dialog.on_export()
    assert len(form.executor.calls) == 1
    form.executor.finish(0)
    form.drain()

    assert len(form.executor.calls) == 1 and form.dialog.pending is None
    form.dialog.workbench.export.assert_not_called()
    assert form.destination.read_bytes() == b'Keep existing bibliography'
    form.wx.MessageDialog.return_value.Destroy.assert_called_once_with()


@pytest.mark.parametrize('when', ('confirmation', 'queued'))
def test_native_existing_approval_is_bound_before_confirmation_and_async_work(form, when):
    use_real_writer(form)
    form.destination.write_bytes(b'Original file presented for replacement')
    changed = b'Newer work must survive the old approval'
    if when == 'confirmation':
        def confirm_while_another_program_writes():
            form.destination.write_bytes(changed)
            return form.wx.ID_YES
        form.wx.MessageDialog.return_value.ShowModal = confirm_while_another_program_writes

    form.dialog.on_export()
    form.executor.finish(0)
    form.drain()
    if when == 'queued':
        form.destination.write_bytes(changed)
    form.executor.finish(1)
    form.drain()

    assert form.destination.read_bytes() == changed
    assert 'not exported' in form.dialog.status.value
    assert 'changed' in form.dialog.status.value
    assert not list(form.destination.parent.glob('.research-export-*'))


@pytest.mark.parametrize('when', ('snapshot', 'confirmation'))
@pytest.mark.parametrize('action', ('close', 'options'))
def test_snapshot_or_confirmation_invalidated_attempt_cannot_export(form, when, action):
    use_real_writer(form)
    form.destination.write_bytes(b'Existing owner content')
    dialog = form.dialog

    def invalidate():
        if action == 'close':
            dialog.on_close()
        else:
            dialog.on_options_changed()

    def interrupted_confirmation():
        invalidate()
        return form.wx.ID_YES

    if when == 'confirmation':
        form.wx.MessageDialog.return_value.ShowModal = interrupted_confirmation
    dialog.on_export()
    assert len(form.executor.calls) == 1
    form.wx.MessageDialog.assert_not_called()
    if when == 'snapshot':
        invalidate()
    form.executor.finish(0)
    form.drain()
    assert len(form.executor.calls) == 1
    dialog.workbench.export.assert_not_called()
    assert form.destination.read_bytes() == b'Existing owner content'
    assert not list(form.destination.parent.glob('.research-export-*'))
    if when == 'snapshot':
        form.wx.MessageDialog.assert_not_called()


def test_delayed_destination_snapshot_runs_off_ui_thread_and_close_cancels(form, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, get_ident

    use_real_writer(form)
    original = b'Existing owner content'
    form.destination.write_bytes(original)
    entered, release = Event(), Event()
    ui_thread = get_ident()
    capture = style_dialog.capture_export_destination

    def delayed_capture(*args, **kwargs):
        assert get_ident() != ui_thread, 'Destination hashing blocked the native event handler.'
        entered.set()
        assert release.wait(3), 'The controlled snapshot worker was not released.'
        return capture(*args, **kwargs)

    monkeypatch.setattr(style_dialog, 'capture_export_destination', delayed_capture)
    executor = ThreadPoolExecutor(max_workers=1)
    form.dialog.executor = executor
    try:
        form.dialog.on_export()
        assert entered.wait(2)
        future = form.dialog.pending
        assert future is not None and not future.done()
        assert form.dialog.close_button.enabled and not form.dialog.export_button.enabled
        form.wx.MessageDialog.assert_not_called()
        form.dialog.on_close()
        release.set()
        with pytest.raises(TimeoutError, match='destination was not changed'):
            future.result(timeout=2)
        form.drain()
        form.dialog.workbench.export.assert_not_called()
        form.wx.MessageDialog.assert_not_called()
        assert form.destination.read_bytes() == original
        assert not list(form.destination.parent.glob('.research-export-*'))
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_destination_snapshot_checks_cancellation_between_real_content_chunks(form):
    original = b'Existing content\n' * 200_000
    form.destination.write_bytes(original)
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(TimeoutError, match='destination was not changed'):
        style_dialog.capture_export_destination(form.destination, private_root=form.private,
                                                cancelled=cancelled)
    assert checks == 3 and form.destination.read_bytes() == original


@pytest.mark.parametrize('picker', ('format', 'file'))
@pytest.mark.parametrize('action', ('close', 'options'))
def test_picker_nested_events_cannot_reset_cancellation_and_start_export(form, picker, action):
    use_real_writer(form)
    form.destination.write_bytes(b'Preserved while picker is open')
    dialog = form.dialog

    def interrupt_picker():
        if action == 'close':
            dialog.on_close()
        else:
            dialog.on_options_changed()
        return form.wx.ID_OK

    chooser = form.wx.SingleChoiceDialog if picker == 'format' else form.wx.FileDialog
    chooser.return_value.ShowModal = interrupt_picker
    dialog.on_export()
    assert form.executor.calls == []
    form.wx.MessageDialog.assert_not_called()
    dialog.workbench.export.assert_not_called()
    assert form.destination.read_bytes() == b'Preserved while picker is open'
