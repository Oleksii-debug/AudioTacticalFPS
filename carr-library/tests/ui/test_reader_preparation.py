"""Faithful modal lifetime plus actual borrowed-thread preparation and source guards."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sys
import threading

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.reading_tools import ReadingSnapshot
from carr_library.ui.reader_preparation import ReaderPreparationDialog
from carr_library.ui.keyboard_settings import voice_reader_commands


class Host:
    def __init__(self):
        self.document = ReadingDocument('😀 12 + 3')
        self.range = (0, 0)
        self.main = threading.get_ident()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = self.callbacks = None
        self.running = self.dead = self.revoked = self.stopped = False
        self.applied = []
        self.reports = []

    def gui(self):
        assert threading.get_ident() == self.main, 'Native document callbacks must stay on the GUI thread.'

    def guard(self):
        self.gui()
        if self.revoked or self.dead:
            raise ValueError('The current reading source was revoked.')

    def capture(self):
        self.guard()
        return ReadingSnapshot.capture(self.document)

    def selection(self):
        self.gui()
        return self.range

    def select(self, start, end):
        self.gui()
        self.range = start, end

    def verify(self, snapshot):
        self.guard()
        snapshot.check(self.document)

    def apply(self, snapshot, start, end, replacement, *, label):
        self.verify(snapshot)
        self.applied.append((start, end, replacement))
        return self.document.replace(start, end, replacement, label=label)

    def submit(self, work, done, *, on_error):
        self.gui()
        if self.running:
            return False
        self.running = True
        self.stopped = False
        self.callbacks = done, on_error
        self.future = self.pool.submit(work)
        return True

    def finish(self):
        self.gui()
        done, error = self.callbacks
        self.callbacks = None
        self.running = False
        try:
            value = self.future.result(timeout=5)
        except Exception as exc:
            error(exc)
        else:
            done(value)

    def alive(self):
        return not self.dead

    def busy(self):
        return self.running

    def cancelled(self):
        return self.stopped

    def report(self, text, *, focus=False):
        self.gui()
        self.reports.append(text)

    def voice(self):
        pytest.fail('Voice language must not silently choose a preparation profile.')


@pytest.fixture
def view(monkeypatch):
    host = Host()
    wx = SimpleNamespace(on_show=lambda dialog: None)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL',
            'HORIZONTAL', 'TE_MULTILINE', 'TE_READONLY', 'HSCROLL', 'EXPAND', 'ALL',
            'LEFT', 'RIGHT', 'TOP')):
        setattr(wx, name, 1 << index)
    wx.ID_ANY, wx.ID_CANCEL = -1, 5101
    wx.EVT_CHOICE, wx.EVT_CHECKBOX, wx.EVT_BUTTON = 'choice', 'check', 'button'
    wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'close', 'destroy'
    counter = iter(range(100, 10000))

    class Widget:
        def __init__(self, parent=None, *args, value='', choices=(), label='', **kwargs):
            self.parent, self.value, self.choices, self.label = parent, value, list(choices), label
            self.id = args[0] if args and isinstance(args[0], int) and args[0] != -1 else next(counter)
            self.dead = False
            self.enabled = True
            self.selection = 0
            self.name = ''
            self.bindings = {}
            self.destroy_calls = 0

        def live(self):
            host.gui()
            node = self
            while isinstance(node, Widget):
                assert not node.dead, 'A callback touched a deleted native wrapper.'
                node = node.parent

        def SetName(self, name):
            self.live(); self.name = name
        def GetId(self):
            self.live(); return self.id
        def SetValue(self, value):
            self.live(); self.value = value
        def ChangeValue(self, value):
            self.SetValue(value)
        def GetValue(self):
            self.live(); return self.value
        def SetSelection(self, value):
            self.live(); self.selection = value
        def GetSelection(self):
            self.live(); return self.selection
        def Enable(self, value=True):
            self.live(); self.enabled = value
        def SetFocus(self):
            self.live()
        def Bind(self, event, handler):
            self.live(); self.bindings[event] = handler
        def SetSizer(self, value):
            self.live()
        def SetEscapeId(self, value):
            self.live()
        def EndModal(self, value):
            self.live(); self.modal_result = value
        def ShowModal(self):
            self.live(); wx.on_show(self); return wx.ID_CANCEL
        def Destroy(self):
            self.live()
            self.destroy_calls += 1
            self.dead = True
            # Real wx/SIP can invalidate the original wrapper before this event.
            event = SimpleNamespace(GetId=lambda: self.id, Skip=lambda: None,
                GetEventObject=lambda: pytest.fail('Never inspect the deleted event object.'))
            callback = self.bindings.get(wx.EVT_WINDOW_DESTROY)
            if callback:
                callback(event)

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): host.gui()

    for name in ('Dialog', 'TextCtrl', 'StaticText', 'Choice', 'CheckBox', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.WrapSizer = Sizer
    monkeypatch.setitem(sys.modules, 'wx', wx)
    controller = ReaderPreparationDialog(None, host)
    try:
        yield host, controller, wx
    finally:
        if not controller.destroyed:
            controller.dialog.Destroy()
        host.pool.shutdown(wait=True, cancel_futures=True)


def choose(controller, language=1):
    controller.language.SetSelection(language)
    controller.options['numbers'].SetValue(True)
    controller.changed()


def test_language_and_transformations_require_explicit_choices(view):
    host, controller, _ = view
    assert controller.language.GetSelection() == 0
    assert not any(field.GetValue() for field in controller.options.values())
    assert not controller.preview_button.enabled and not controller.apply_button.enabled
    assert controller.preview() is False and host.future is None
    assert host.document.text == '😀 12 + 3' and not host.applied


def test_real_borrowed_worker_previews_then_one_explicit_apply_and_undo(view, monkeypatch):
    host, controller, _ = view
    import carr_library.ui.reader_preparation as module
    original = module.preview_preparation
    threads = []
    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'preview_preparation', observed)
    choose(controller, language=2)
    assert controller.preview()
    host.finish()
    assert threads and all(thread != host.main for thread in threads)
    assert host.document.text == '😀 12 + 3' and not host.applied
    assert controller.apply_button.enabled
    controller.accept()
    assert host.document.text == '😀 один два + три'
    assert len(host.applied) == 1 and len(host.document._edits) == 1
    assert host.document.source_text == '😀 12 + 3'
    assert host.document.undo() and host.document.text == '😀 12 + 3'


@pytest.mark.parametrize('mutation', ['document-edit', 'same-text-other-tab', 'source-revocation'])
def test_stale_worker_result_cannot_be_accepted(view, mutation):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    if mutation == 'document-edit':
        host.document.set_text('Other 45')
    elif mutation == 'same-text-other-tab':
        host.document = ReadingDocument(host.document.text)
    else:
        host.revoked = True
    expected = host.document.to_dict()
    host.finish()
    assert controller.session.preview is None and not controller.apply_button.enabled
    controller.accept()
    assert host.document.to_dict() == expected and not host.applied


def test_open_dialog_cannot_adopt_another_identical_tab_before_preview(view):
    host, controller, _ = view
    choose(controller)
    host.document = ReadingDocument(host.document.text)
    assert controller.preview() is False and host.future is None and not host.applied
    assert 'changed' in controller.status.value


def test_option_change_invalidates_queued_completion(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    controller.options['symbols'].SetValue(True)
    controller.changed()
    host.finish()
    assert controller.session.preview is None and not controller.apply_button.enabled
    assert not host.applied and host.document.text == '😀 12 + 3'


def test_reject_and_cancel_never_apply_a_preview(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    host.finish()
    controller.reject()
    controller.accept()
    assert not host.applied
    assert controller.preview()
    host.finish()
    controller.cancel()
    controller.accept()
    assert not host.applied and host.document.text == '😀 12 + 3'
    assert not host.stopped  # A tool does not clear or invent the owner's stop state.


def test_direct_destroy_revokes_exact_queued_result_without_native_calls(view):
    host, controller, wx = view
    choose(controller)
    assert controller.preview()
    host.future.result(timeout=5)  # The real result exists, GUI delivery is held.
    wx.on_show = lambda dialog: dialog.Destroy()
    controller.show()
    assert controller.destroyed and controller.session.closed
    host.finish()
    assert controller.dialog.destroy_calls == 1
    assert controller.session.preview is None and not host.applied


def test_child_destroy_event_does_not_close_the_modal_owner(view):
    _, controller, _ = view
    seen = []
    event = SimpleNamespace(GetId=lambda: controller._window_id + 1, Skip=lambda: seen.append('skip'),
                            GetEventObject=lambda: pytest.fail('No native owner dereference required.'))
    controller._destroyed(event)
    assert seen == ['skip'] and not controller.destroyed and not controller.session.closed


def test_close_while_worker_pending_does_not_transfer_or_apply_result(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    controller.close()
    host.finish()
    assert controller.session.closed and not host.applied
    assert host.document.text == '😀 12 + 3'


def test_selected_scope_and_second_explicit_preview_use_current_derivative(view):
    host, controller, _ = view
    controller.scope = (2, 4)
    controller.selected_only.SetValue(True)
    choose(controller)
    assert controller.preview()
    host.finish()
    controller.accept()
    assert host.document.text == '😀 one two + 3'
    assert host.range == (2, 9)
    controller.selected_only.SetValue(False)
    controller.changed()
    assert controller.preview()
    host.finish()
    controller.accept()
    assert host.document.text == '😀 one two + three'
    assert len(host.applied) == 2


def test_existing_busy_worker_is_not_replaced(view):
    host, controller, _ = view
    choose(controller)
    host.running = True
    assert controller.preview() is False
    assert host.future is None and not host.applied


def test_preparation_is_one_empty_default_action_in_the_existing_voice_reader_registry():
    commands = voice_reader_commands()
    selected = [command for command in commands if command.identity == 'voice_reader.prepare']
    assert len(selected) == 1 and selected[0].shortcut == ''
    assert selected[0].context == 'voice_reader'
    assert next(command for command in commands if command.identity == 'voice_reader.read').shortcut == 'F6'
