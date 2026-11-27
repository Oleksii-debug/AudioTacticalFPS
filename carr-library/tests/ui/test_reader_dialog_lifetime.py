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
        assert get_ident() == self.owner, 'Native control accessed from worker.'
        assert not self.destroyed, 'Deleted native control accessed.'

    def __bool__(self): return not self.destroyed
    def GetId(self): self.native(); return self.identity
    def Bind(self, event, callback, *args, **kwargs): self.native(); self.handlers[event] = callback
    def SetName(self, name): self.native(); self.name = name
    def SetMinSize(self, *args): self.native()
    def SetSizer(self, *args): self.native()
    def SetEscapeId(self, *args): self.native()
    def SetAcceleratorTable(self, *args): self.native()
    def Wrap(self, *args): self.native()
    def Enable(self, value=True): self.native(); self.enabled = value
    def Disable(self): self.Enable(False)
    def ChangeValue(self, value): self.native(); self.value = value
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


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class Timer:
    def __init__(self, owner): self.stopped = False
    def Start(self, interval): self.stopped = False
    def Stop(self): self.stopped = True


@pytest.fixture
def windows(monkeypatch):
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'EXPAND',
             'ALL', 'LEFT', 'RIGHT', 'TE_READONLY', 'TE_MULTILINE', 'ID_CANCEL', 'ALIGN_RIGHT',
             'ACCEL_CTRL', 'ACCEL_SHIFT', 'ACCEL_ALT', 'ACCEL_NORMAL', 'WXK_PAGEUP',
             'WXK_PAGEDOWN', 'WXK_LEFT', 'WXK_RIGHT', 'WXK_F1', 'TOP', 'BOTTOM',
             'ALIGN_CENTER_VERTICAL', 'TE_PROCESS_ENTER')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(names)})
    for name in ('EVT_CLOSE', 'EVT_BUTTON', 'EVT_LISTBOX', 'EVT_TIMER', 'EVT_MENU',
                 'EVT_TEXT', 'EVT_TEXT_ENTER', 'EVT_CHOICE'):
        setattr(wx, name, name)
    wx.EVT_WINDOW_DESTROY = NativeWidget.destroy_event
    wx.Dialog = wx.StaticText = wx.TextCtrl = wx.ListBox = wx.Button = NativeWidget
    wx.Notebook = wx.Panel = wx.Choice = NativeWidget
    wx.BoxSizer = wx.WrapSizer = Sizer
    wx.Timer = Timer
    wx.NewIdRef = lambda: 100
    wx.AcceleratorTable = tuple
    callbacks = Queue()
    wx.CallAfter = lambda callback: callbacks.put(callback)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    owners = []
    parent = SimpleNamespace(operation_progress=None)
    def make(kind):
        desk = SimpleNamespace(title='Retained source', cancelled=lambda: False)
        owner = (EbookAudioDialog(parent, desk, 1, lambda: None) if kind == 'audio'
                 else ReadingDialog(parent, desk, object(), lambda: None))
        owners.append(owner)
        return owner
    yield SimpleNamespace(make=make, parent=parent, callbacks=callbacks, wx=wx)
    for owner in owners:
        owner.dispose()
        owner.executor.shutdown(wait=True, cancel_futures=True)
        if owner.dialog:
            owner.dialog.Destroy()
    while not callbacks.empty():
        callbacks.get_nowait()()


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
    owner.player = object()
    owner.transport = SimpleNamespace(observed=observed, close=Mock(side_effect=uncertain))
    owner.progress = SimpleNamespace(unregister_player=Mock())
    NativeWidget.Destroy(owner.dialog)
    assert owner.closed and owner.cancelled.is_set()
    assert windows.parent._reader_playback_retained == [owner]
    assert 'unconfirmed' in owner.cleanup_warning
    owner.progress.unregister_player.assert_called_once_with(observed, silence_confirmed=False)


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
