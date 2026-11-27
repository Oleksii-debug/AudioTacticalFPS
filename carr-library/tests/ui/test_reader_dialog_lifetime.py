"""Bound native destruction handlers and real queued workers; no wx acceptance claim."""
from queue import Queue
import sys
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui.ebook_audio_dialog import EbookAudioDialog
from carr_library.ui.reading_desk import ReadingDialog


class NativeWidget:
    destroy_event = 'destroy'
    violations = []

    def __init__(self, parent=None, *args, value='', label='', **kwargs):
        self.owner = get_ident()
        self.parent = parent if isinstance(parent, NativeWidget) else None
        self.children, self.handlers = [], {}
        self.value, self.label, self.name = value, label, ''
        self.destroyed, self.enabled, self.selection = False, True, -1
        self.identity = id(self)
        if self.parent is not None:
            self.parent.children.append(self)

    def native(self):
        if get_ident() != self.owner or self.destroyed:
            self.violations.append((self.name or self.label, get_ident() != self.owner, self.destroyed))
            raise AssertionError('Native control accessed from a worker or after deletion.')

    def __bool__(self): return not self.destroyed
    def GetId(self): self.native(); return self.identity
    def Bind(self, event, callback, *args, **kwargs): self.native(); self.handlers[event] = callback
    def SetName(self, name): self.native(); self.name = name
    def SetToolTip(self, value): self.native(); self.tooltip = value
    def GetDefaultAttributes(self): self.native(); return SimpleNamespace(font=NativeFont())
    def SetFont(self, value): self.native(); self.font = value
    def SetForegroundColour(self, value): self.native(); self.foreground = value
    def SetBackgroundColour(self, value): self.native(); self.background = value
    def Refresh(self): self.native()
    def SetMinSize(self, *args): self.native()
    def SetSizer(self, *args): self.native()
    def CreateStdDialogButtonSizer(self, *args): self.native(); return Sizer()
    def SetEscapeId(self, *args): self.native()
    def SetAcceleratorTable(self, *args): self.native()
    def Wrap(self, *args): self.native()
    def Enable(self, value=True): self.native(); self.enabled = value
    def Disable(self): self.Enable(False)
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.native(); self.value = value
    def SetFocus(self): self.native()
    def GetSelection(self): self.native(); return self.selection
    def SetSelection(self, value): self.native(); self.selection = value
    def IsModal(self): self.native(); return False
    def Set(self, values): self.native(); self.items = list(values)
    def GetValue(self): self.native(); return self.value
    def AddPage(self, *args): self.native()
    def GetParent(self): self.native(); return self.parent
    def SetInsertionPoint(self, value): self.native()
    def Hide(self): self.native()

    def Destroy(self):
        self.native()
        for child in tuple(self.children):
            NativeWidget.Destroy(child)
        self.destroyed = True
        # Deliberately rewrapped native owner, with the original wrapper already dead.
        handler = self.handlers.get(self.destroy_event)
        if handler is not None:
            handler(SimpleNamespace(GetId=lambda: self.identity, GetEventObject=lambda: object(), Skip=lambda: None))
        if self.parent is not None:
            self.parent.children.remove(self)
        return True


class NativeFont:
    def __init__(self, other=None):
        self.points = other.GetFractionalPointSize() if other is not None else 10.0
    def GetFractionalPointSize(self): return self.points
    def SetFractionalPointSize(self, value): self.points = value


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class Timer:
    def __init__(self, owner): self.stopped = False
    def Start(self, interval): self.stopped = False
    def Stop(self): self.stopped = True


@pytest.fixture
def windows(monkeypatch, tmp_path):
    NativeWidget.violations = []
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'EXPAND',
             'ALL', 'LEFT', 'RIGHT', 'TE_READONLY', 'TE_MULTILINE', 'ID_CANCEL', 'ALIGN_RIGHT',
             'ACCEL_CTRL', 'ACCEL_SHIFT', 'ACCEL_ALT', 'ACCEL_NORMAL', 'WXK_PAGEUP',
             'WXK_PAGEDOWN', 'WXK_LEFT', 'WXK_RIGHT', 'WXK_F1', 'TOP', 'BOTTOM',
             'ALIGN_CENTER_VERTICAL', 'TE_PROCESS_ENTER', 'WXK_F6', 'WXK_F7', 'WXK_F8', 'WXK_F9',
             'OK', 'CANCEL', 'ID_OK', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'ID_YES',
             'SYS_COLOUR_WINDOWTEXT', 'SYS_COLOUR_WINDOW')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('EVT_CLOSE', 'EVT_BUTTON', 'EVT_LISTBOX', 'EVT_TIMER', 'EVT_MENU',
                 'EVT_TEXT', 'EVT_TEXT_ENTER', 'EVT_CHOICE', 'EVT_SYS_COLOUR_CHANGED'):
        setattr(wx, name, name)
    wx.EVT_WINDOW_DESTROY = NativeWidget.destroy_event
    wx.Dialog = wx.StaticText = wx.TextCtrl = wx.ListBox = wx.Button = NativeWidget
    wx.Notebook = wx.Panel = wx.Choice = wx.SpinCtrl = wx.CheckBox = NativeWidget
    wx.NOT_FOUND = -1
    wx.Font = NativeFont
    wx.NullFont, wx.NullColour = object(), object()
    wx.SystemSettings = SimpleNamespace(GetColour=lambda colour: colour)
    wx.BoxSizer = wx.WrapSizer = Sizer
    wx.Timer = Timer
    wx.NewIdRef = lambda: 100
    wx.AcceleratorTable = tuple
    callbacks = Queue()
    wx.CallAfter = lambda callback: callbacks.put(callback)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    owners = []
    parent = SimpleNamespace(operation_progress=None, library=SimpleNamespace(root=tmp_path))
    def make(kind):
        desk = SimpleNamespace(title='Retained source', cancelled=lambda: False,
                               library=parent.library)
        owner = (EbookAudioDialog(parent, desk, 1, lambda: None) if kind == 'audio'
                 else ReadingDialog(parent, desk, object(), lambda: None))
        owners.append(owner)
        return owner
    yield SimpleNamespace(make=make, parent=parent, callbacks=callbacks, wx=wx)
    for owner in owners:
        owner.dispose()
        owner.executor.shutdown(wait=True, cancel_futures=True)
        if hasattr(owner, 'live'):
            future = owner.live.core.close()
            if future is not None:
                future.result(5)
        if owner.dialog:
            owner.dialog.Destroy()
    while not callbacks.empty():
        callbacks.get_nowait()()
    assert not NativeWidget.violations, 'Even caught native-access failures must fail this lifetime proof.'


@pytest.mark.parametrize('kind', ['reading', 'audio'])
@pytest.mark.parametrize('termination', ['direct', 'native-parent'])
def test_native_destroy_latches_before_queued_reader_result(kind, termination, windows):
    owner = windows.make(kind)
    entered, release = Event(), Event()
    discarded, accepted = [], []
    result = object()
    def work():
        entered.set()
        assert release.wait(5), 'Fixture worker was not released.'
        return result
    if kind == 'audio':
        owner._submit(work, accepted.append, discarded.append)
    else:
        owner._run('Opening synthetic source', work, accepted.append)
    try:
        assert entered.wait(5)
        owner_id = owner.dialog.GetId()
        skipped = []
        child = SimpleNamespace(GetId=lambda: owner_id + 1, GetEventObject=lambda: owner.dialog,
                                Skip=lambda: skipped.append(True))
        owner.dialog.handlers[windows.wx.EVT_WINDOW_DESTROY](child)
        assert not owner.closed and not owner.cancelled.is_set() and skipped == [True]
        if termination == 'direct':
            owner.dialog.Destroy()
        else:
            NativeWidget.Destroy(owner.dialog)
        assert owner.closed and owner.cancelled.is_set() and not owner.dialog
        assert owner.dialog.Destroy() is False
    finally:
        release.set()
        owner.executor.shutdown(wait=True, cancel_futures=True)
    while not windows.callbacks.empty():
        windows.callbacks.get_nowait()()
    assert not accepted
    if kind == 'reading':
        assert owner.live.closed and owner.live.core.close() is None
        assert owner.live.timer.stopped
    assert discarded == ([result] if kind == 'audio' else [])
    if kind == 'audio':
        assert owner.timer.stopped
        for callback in (owner.select, owner.pause, owner.resume, owner.stop, owner.where, owner.tick, owner.close):
            callback()
        owner.move(1)
        owner._error(ValueError('Late failure'))
        owner.report('Late report', focus=True)
    else:
        for callback in (owner.go_to, owner.add_bookmark, owner.help, owner.close):
            callback()
        owner._error(ValueError('Late failure'))
        owner._message('Late message')


def test_queued_audio_result_is_discarded_when_parent_deletes_after_completion(windows):
    owner = windows.make('audio')
    accepted, discarded = [], []
    result = object()
    owner._submit(lambda: result, accepted.append, discarded.append)
    callback = windows.callbacks.get(timeout=5)
    NativeWidget.Destroy(owner.dialog)
    callback()
    assert owner.closed and discarded == [result] and not accepted


def test_native_parent_deletion_closes_nested_recorded_audio(windows):
    reader, audio = windows.make('reading'), windows.make('audio')
    reader._audio_dialog = audio
    NativeWidget.Destroy(reader.dialog)
    assert reader.closed and audio.closed and audio.cancelled.is_set()
    assert audio.timer.stopped


def test_forced_audio_destruction_retains_uncertain_device_and_reports_no_silence(windows):
    owner = windows.make('audio')
    uncertain = RuntimeError('Synthetic native close refused')
    observed = object()
    entered, permitted = Event(), Event()
    ui_thread = get_ident()
    def close():
        assert get_ident() != ui_thread, 'Native destruction waited for device cleanup on the GUI thread.'
        entered.set()
        if not permitted.is_set():
            raise uncertain
    owner.player = object()
    owner.transport = SimpleNamespace(observed=observed, close=Mock(side_effect=close))
    owner.progress = SimpleNamespace(unregister_player=Mock())
    try:
        NativeWidget.Destroy(owner.dialog)
        assert owner.closed and owner.cancelled.is_set()
        assert windows.parent._reader_playback_retained == [owner]
        assert 'unconfirmed' in owner.cleanup_warning
        owner.progress.unregister_player.assert_called_once_with(observed, silence_confirmed=False)
        assert entered.wait(5), 'The retained native owner never attempted cleanup.'
        assert not owner._cleanup_owner.quiescent
        owner.dispose()
        assert windows.parent._reader_playback_retained == [owner]
    finally:
        permitted.set()
        cleanup = getattr(owner, '_cleanup_owner', None)
        if cleanup is not None:
            assert cleanup.quiescent_event.wait(5), 'Retained cleanup did not finish after release.'
    windows.callbacks.get(timeout=5)()
    assert owner._silence_confirmed and not windows.parent._reader_playback_retained
    assert owner.progress.unregister_player.call_count == 2
    owner.progress.unregister_player.assert_called_with(observed, silence_confirmed=True)


@pytest.mark.parametrize('termination', ['direct', 'native-parent'])
def test_language_inventory_queued_callbacks_use_cached_native_scope(windows, termination):
    from carr_library.ui import language_matrix as ui
    work = []
    host = SimpleNamespace(cancel_event=Event(), _closing=False, working=False, busy=False,
                           library=object(), run_job=lambda *args: work.append(args))
    controller = ui.LanguageMatrixController()
    dialog = ui._native_dialog_type()(host, controller)
    try:
        dialog.refresh()
        assert dialog.pending and len(work) == 1
        skipped = []
        owner_id = dialog.GetId()
        dialog.on_destroy(SimpleNamespace(GetId=lambda: owner_id + 1, GetEventObject=lambda: dialog,
                                          Skip=lambda: skipped.append(True)))
        assert dialog._alive and not host.cancel_event.is_set() and skipped == [True]
        if termination == 'direct':
            dialog.Destroy()
        else:
            NativeWidget.Destroy(dialog)
        assert not dialog._alive and host.cancel_event.is_set() and not dialog
        assert dialog.Destroy() is False
        _, operation, completed, failed = work[0]
        inventory = ui.InstalledVoiceInventory((), (), 'synthetic completion')
        completed(inventory)
        failed(RuntimeError('Late inventory failure'))
        for callback in (dialog.filter, dialog.select, dialog.refresh, dialog.cancel, dialog.close):
            callback()
        assert len(work) == 1
        # A later unrelated host operation must not be cancelled by repeated close.
        host.cancel_event.clear()
        dialog.shutdown()
        assert not host.cancel_event.is_set()
        assert len(controller.catalog.entries) == 135
    finally:
        dialog.shutdown()
        if dialog:
            dialog.Destroy()


def test_idle_language_view_destruction_does_not_cancel_host(windows):
    from carr_library.ui import language_matrix as ui
    host = SimpleNamespace(cancel_event=Event())
    dialog = ui._native_dialog_type()(host, ui.LanguageMatrixController())
    NativeWidget.Destroy(dialog)
    assert not dialog._alive and not host.cancel_event.is_set()


@pytest.mark.parametrize('kind', ['fields', 'choose', 'show'])
def test_existing_native_dialog_helpers_do_not_read_or_redestroy_deleted_peer(windows, monkeypatch, kind):
    from carr_library.ui.research_actions import NativeDialogs
    parent = NativeWidget()
    created = []
    class Modal(NativeWidget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
        def ShowModal(self):
            NativeWidget.Destroy(parent)
            return windows.wx.ID_OK
    monkeypatch.setattr(windows.wx, 'Dialog', Modal)
    monkeypatch.setattr(windows.wx, 'SingleChoiceDialog', Modal, raising=False)
    helper = NativeDialogs()
    if kind == 'fields':
        result = helper.fields(parent, 'Native fields', (('value', 'Exact value', 'retained', False),))
    elif kind == 'choose':
        result = helper.choose(parent, 'Native choice', 'Choose', ('first', 'second'))
    else:
        result = helper.show(parent, 'Native text', 'Retained source')
    assert result is None and len(created) == 1
    assert not parent and not created[0] and not NativeWidget.violations


@pytest.mark.parametrize('boundary', ['go_to', 'add', 'list', 'action', 'rename', 'delete', 'help'])
def test_reading_desk_does_not_resume_after_nested_native_parent_deletion(windows, monkeypatch, boundary):
    owner = windows.make('reading')
    owner.controller.desk.pages = (object(),)
    location = SimpleNamespace(kind='text', index=1, revision='abc')
    bookmark = SimpleNamespace(label='Saved page', location=location)
    owner.controller.current = SimpleNamespace(ordinal=1, location=location,
                                               locator=SimpleNamespace(display=lambda: 'source 1'))
    owner.controller.bookmarks = Mock(return_value=(bookmark,))
    admissions = {name: Mock() for name in ('go', 'add_bookmark', 'open_bookmark',
                                           'rename_bookmark', 'delete_bookmark')}
    for name, function in admissions.items():
        setattr(owner.controller, name, function)
    calls = []
    def detached_return(value):
        NativeWidget.Destroy(owner.dialog)
        return value
    def choose(*args):
        calls.append(args[1])
        if len(calls) == 1:
            return detached_return(0) if boundary == 'list' else 0
        if boundary == 'action':
            return detached_return(0)
        return 1 if boundary == 'rename' else 2
    owner.dialogs = SimpleNamespace(
        text=lambda *args: detached_return('1'),
        choose=choose,
        show=lambda *args: detached_return(None))
    class Confirmation(NativeWidget):
        def ShowModal(self):
            return detached_return(windows.wx.ID_YES)
    monkeypatch.setattr(windows.wx, 'MessageDialog', Confirmation, raising=False)
    if boundary == 'go_to':
        owner.go_to()
    elif boundary == 'add':
        owner.add_bookmark()
    elif boundary == 'help':
        owner.help()
    else:
        owner.manage_bookmarks()
        windows.callbacks.get(timeout=5)()
    assert owner.closed and not owner.dialog and owner.cancelled.is_set()
    assert not NativeWidget.violations
    for admitted in admissions.values():
        admitted.assert_not_called()


def test_nested_recorded_audio_error_cannot_report_through_destroyed_reader(windows, monkeypatch):
    from carr_library.ui import ebook_audio_dialog
    owner = windows.make('reading')
    owner.controller.current = SimpleNamespace(ordinal=1)
    class Nested:
        def __init__(self, *args, **kwargs): self.closed = False
        def dispose(self): self.closed = True
        def show(self):
            NativeWidget.Destroy(owner.dialog)
            raise RuntimeError('The nested native modal lost its parent.')
    monkeypatch.setattr(ebook_audio_dialog, 'EbookAudioDialog', Nested)
    owner.open_audio()
    assert owner.closed and owner._audio_dialog is None and not owner.dialog
    assert not NativeWidget.violations


@pytest.mark.parametrize('failure', ['live', 'recorded', 'executor'])
def test_reading_disposal_attempts_all_owned_cleanup_and_retains_failed_retry(windows, failure):
    owner = windows.make('reading')
    live, shutdown = owner.live.close, owner.executor.shutdown
    actual = {'live': Mock(wraps=live), 'recorded': Mock(),
              'executor': Mock(wraps=shutdown)}
    owner.live.close = actual['live']
    owner.executor.shutdown = actual['executor']
    owner._audio_dialog = SimpleNamespace(dispose=actual['recorded'])
    actual[failure].side_effect = RuntimeError('Synthetic unconfirmed cleanup')
    try:
        owner.dispose()
        assert owner.closed and owner._cleanup_pending
        assert 'unconfirmed' in owner.cleanup_warning
        assert windows.parent._reader_playback_retained == [owner]
        assert all(operation.call_count == 1 for operation in actual.values())
        actual[failure].side_effect = None
        owner.dispose()
        assert not owner._cleanup_pending and not owner.cleanup_warning
        assert not windows.parent._reader_playback_retained
        assert all(operation.call_count == 2 for operation in actual.values())
    finally:
        actual[failure].side_effect = None
        owner.dispose()
        owner.live.close = live
        owner.executor.shutdown = shutdown


@pytest.mark.parametrize('detachment', ['native-deletion', 'missing-event-queue'])
def test_failed_late_audio_discard_keeps_retry_owner_after_native_completion(windows, monkeypatch, tmp_path, detachment):
    owner = windows.make('audio')
    path = tmp_path / 'late-owned-recording.wav'
    path.write_bytes(b'Synthetic retained result; no audio decoding claim.')
    permitted, attempted, removed = Event(), Event(), Event()
    accepted = []
    class Result:
        def close(self):
            attempted.set()
            if not permitted.is_set():
                raise OSError('Synthetic late-file cleanup temporarily refused')
            path.unlink()
            removed.set()
    result = Result()
    if detachment == 'missing-event-queue':
        def unavailable(callback):
            raise RuntimeError('Synthetic native event queue is gone')
        monkeypatch.setattr(windows.wx, 'CallAfter', unavailable)
    owner._submit(lambda: result, accepted.append, lambda value: value.close())
    try:
        if detachment == 'native-deletion':
            completed = windows.callbacks.get(timeout=5)
            NativeWidget.Destroy(owner.dialog)
            completed()
        assert attempted.wait(5), 'Late completion never attempted its owned discard.'
        owner.executor.shutdown(wait=True, cancel_futures=True)
        assert not accepted and path.is_file()
        assert owner._cleanup_owners and not owner._cleanup_owners[-1].quiescent
        assert 'cleanup is unconfirmed' in owner.cleanup_warning
    finally:
        permitted.set()
        if owner._cleanup_owners:
            assert owner._cleanup_owners[-1].quiescent_event.wait(5)
    assert removed.is_set() and not path.exists()


@pytest.mark.parametrize(('busy', 'quiescent'), [(True, True), (False, False), (True, False)])
def test_recorded_audio_waits_for_acknowledged_live_stop_and_clears_old_cancel(
        windows, monkeypatch, busy, quiescent):
    from carr_library.ui import ebook_audio_dialog
    owner = windows.make('reading')
    original_core = owner.live.core
    core = SimpleNamespace(busy=busy, quiescent=quiescent,
        status=lambda: SimpleNamespace(state='stopping'), stop=Mock())
    owner.live.core = core
    owner.controller.current = SimpleNamespace(ordinal=1)
    errors, opened = [], []
    owner._error = lambda error: errors.append(str(error))
    class Nested:
        def __init__(self, parent, desk, ordinal, guard, **options):
            self.guard, self.closed = guard, False
        def show(self):
            self.guard()
            opened.append(True)
            self.closed = True
    monkeypatch.setattr(ebook_audio_dialog, 'EbookAudioDialog', Nested)
    try:
        owner.open_audio()
        assert not opened and len(errors) == 1 and 'finish stopping' in errors[0]
        core.stop.assert_called_once()
        assert owner._audio_dialog is None
        core.busy, core.quiescent = False, True
        owner.cancelled.set()
        owner.open_audio()
        assert opened == [True] and len(errors) == 1 and not owner.cancelled.is_set()
        assert owner._audio_dialog is None
    finally:
        owner.live.core = original_core


def test_recorded_audio_guard_rechecks_live_ownership_after_modal_handoff(windows, monkeypatch):
    from carr_library.ui import ebook_audio_dialog
    owner = windows.make('reading')
    original_core = owner.live.core
    core = SimpleNamespace(busy=False, quiescent=True,
        status=lambda: SimpleNamespace(state='stopping'), stop=Mock())
    owner.live.core = core
    owner.controller.current = SimpleNamespace(ordinal=1)
    guarded = []
    class Nested:
        def __init__(self, parent, desk, ordinal, guard, **options):
            self.guard, self.closed = guard, False
        def show(self):
            self.guard()
            core.busy, core.quiescent = True, False
            with pytest.raises(ValueError, match='finish stopping'):
                self.guard()
            guarded.append(True)
            self.closed = True
    monkeypatch.setattr(ebook_audio_dialog, 'EbookAudioDialog', Nested)
    try:
        owner.open_audio()
        assert guarded == [True] and owner._audio_dialog is None
    finally:
        owner.live.core = original_core
