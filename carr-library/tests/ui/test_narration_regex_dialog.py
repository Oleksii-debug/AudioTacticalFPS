"""Real editor handlers/worker; native controls are explicit test doubles.

The subprocess tests separately verify real termination. These cases exercise
preview/apply choices and delayed GUI delivery; they do not claim wx/NVDA use.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import sys

import pytest

from carr_library.reader.narration import NarrationStore, new_project
from carr_library.ui import narration_editor as module
from test_local_planner_dialog import NativeDoubles


class Dialogs:
    def __init__(self):
        self.pattern, self.replacement = r'(\d+) kg', r'\1 kilograms'
        self.answers = [0, 0, 0]  # case sensitive, read preview, explicitly apply
        self.shown = []
        self.before_choice = lambda title: None
    def fields(self, *args, **kwargs):
        return {'pattern':self.pattern, 'replacement':self.replacement}
    def choose(self, parent, title, *args):
        self.before_choice(title)
        return self.answers.pop(0)
    def show(self, parent, title, text):
        self.shown.append((title,text))


class TrackedExecutor:
    def __init__(self):
        self.actual = ThreadPoolExecutor(max_workers=1)
        self.futures = []
        self.reject = False
    def submit(self, *args, **kwargs):
        if self.reject:
            raise RuntimeError('Synthetic executor rejection')
        future = self.actual.submit(*args, **kwargs)
        self.futures.append(future)
        return future
    def shutdown(self, **kwargs):
        self.actual.shutdown(**kwargs)


def editor_doubles():
    native = NativeDoubles()
    wx = native.module
    control = wx.TextCtrl
    original_init = control.__init__
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.identity = len(native.windows) + 100
        self.style = kwargs.get('style', 0)
        self.editable = True
    control.__init__ = initialize
    def bind(self, event, callback, source=None, id=None):
        self.touch()
        self.events[event if id is None else (event,id)] = callback
    control.Bind = bind
    def destroy(self):
        if self.dead:
            return
        callback = self.events.get('destroy')
        if callback:
            alias = SimpleNamespace(native_id=self.identity)
            callback(SimpleNamespace(GetId=lambda:self.identity,
                GetEventObject=lambda:alias, Skip=lambda:None))
        self.dead = True
        for item in list(native.windows):
            if item.parent is self:
                item.Destroy()
    control.Destroy = destroy
    control.GetId = lambda self:self.identity
    control.SetMinSize = lambda self,value:self.touch()
    control.SetInsertionPoint = lambda self,value:self.touch()
    control.GetWindowStyleFlag = lambda self:self.style
    def set_style(self, value): self.touch(); self.style=value
    control.SetWindowStyleFlag = set_style
    def editable(self, value): self.touch(); self.editable=value
    control.SetEditable = editable
    wx.Dialog.SetTitle = lambda self,value:self.touch()
    wx.Dialog.SetEscapeId = lambda self,value:self.touch()
    wx.Dialog.SetAcceleratorTable = lambda self,value:self.touch()
    wx.WrapSizer = wx.BoxSizer
    wx.AcceleratorTable = lambda value:value
    wx.TE_PROCESS_TAB, wx.RIGHT, wx.BOTTOM, wx.ACCEL_CTRL, wx.ACCEL_SHIFT = 1024,2048,4096,1,2
    wx.EVT_MENU, wx.EVT_TIMER = 'menu','timer'
    class Timer:
        def __init__(self, owner): self.owner=owner; self.pending=False; native.timer=self
        def StartOnce(self, value): self.owner.touch(); self.pending=True
        def Stop(self): self.owner.touch(); self.pending=False
    wx.Timer = Timer
    return native


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path/'library'
    root.mkdir()
    store = NarrationStore(root)
    project = new_project('Original 😀: 12 kg.\r\n')
    digest = store.save(project)
    native, executor, dialogs = editor_doubles(), TrackedExecutor(), Dialogs()
    monkeypatch.setitem(sys.modules, 'wx', native.module)
    monkeypatch.setattr(module, 'ThreadPoolExecutor', lambda **kwargs:executor)
    value = SimpleNamespace(store=store, project=project, digest=digest, native=native,
                            executor=executor, dialogs=dialogs, allowed=True)
    def guard():
        if not value.allowed:
            raise ValueError('Source scope changed')
    value.guard = guard
    yield value
    executor.shutdown(wait=True, cancel_futures=True)
    native.drain()


def show(host, exercise):
    return module.open_narration_editor(None, host.store, host.project,
        expected_digest=host.digest, guard=host.guard, dialogs=host.dialogs,
        on_ready=lambda dialog,controller,controls:exercise(dialog,controller,controls))


def finish(host, controls):
    host.executor.futures[-1].result(timeout=10)
    host.native.drain()
    assert not controls['cancel_regex'].enabled and controls['editor'].editable


def test_real_preview_is_not_applied_before_explicit_choice_and_can_undo_save(host):
    def exercise(dialog, controller, controls):
        old = controller.project
        def inspect(title):
            if title == 'Regular-expression replacement':
                assert controller.project is old and controls['editor'].value == old.current_text
        host.dialogs.before_choice = inspect
        controls['regex'].fire()
        assert not controls['save'].enabled and not controls['editor'].editable
        # A direct accelerator callback must also refuse writes while pending.
        dialog.events[('menu',controls['save'].GetId())](None)
        assert host.store.load(old.id)[1] == host.digest
        finish(host, controls)
        assert host.dialogs.shown and '12 kilograms' in host.dialogs.shown[0][1]
        assert controller.pending_text == 'Original 😀: 12 kilograms.\r\n'
        assert controls['original'].value == old.original_text
        controls['undo'].fire()
        assert controls['editor'].value == old.current_text
        controls['redo'].fire()
        controls['save'].fire()
        assert host.store.load(old.id)[0].current_text == controller.pending_text
        controls['close'].fire()
    show(host,exercise)


@pytest.mark.parametrize('answers', [[0,2], [0,0,1]])
def test_cancel_at_either_review_step_keeps_saved_and_in_memory_history(host, answers):
    host.dialogs.answers = answers
    def exercise(dialog, controller, controls):
        old = controller.project
        controls['regex'].fire()
        finish(host,controls)
        assert controller.project is old and not controller.dirty
        assert host.store.load(old.id)[1] == host.digest
        controls['close'].fire()
    show(host,exercise)


def test_cancel_after_actual_worker_result_before_gui_delivery_never_applies(host):
    def exercise(dialog, controller, controls):
        controls['regex'].fire()
        host.executor.futures[-1].result(timeout=10)
        controls['cancel_regex'].fire()
        host.native.drain()
        assert 'Nothing was applied' in controls['status'].value
        assert not host.dialogs.shown and not controller.dirty
        controls['close'].fire()
    show(host,exercise)


@pytest.mark.parametrize('change', ['text','scope','root','destroy'])
def test_nested_preview_changes_or_destruction_cannot_apply_old_result(host, change):
    def exercise(dialog, controller, controls):
        old = controller.project
        def changed(title):
            if title != 'Regular-expression replacement':
                return
            if change == 'text':
                controls['editor'].SetValue('Newer unsaved words.')
            elif change == 'scope':
                host.allowed = False
            elif change == 'root':
                host.store.root.rename(host.store.root.with_name('kept-root'))
                host.store.root.mkdir()
            else:
                dialog.Destroy()
        host.dialogs.before_choice = changed
        controls['regex'].fire()
        host.executor.futures[-1].result(timeout=10)
        host.native.drain()
        assert controller.project is old and not host.dialogs.shown
        if change == 'text':
            assert controls['editor'].value == 'Newer unsaved words.'
        if not dialog.dead:
            dialog.Destroy()
    show(host,exercise)


def test_forced_destroy_with_alias_event_waits_for_real_future_without_dead_controls(host, monkeypatch):
    actual = module.regex_preview
    entered, release = Event(), Event()
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)
    monkeypatch.setattr(module,'regex_preview',held)
    def exercise(dialog, controller, controls):
        controls['regex'].fire()
        assert entered.wait(5)
        try:
            # Propagated child destruction must not close the parent editor.
            dialog.events['destroy'](SimpleNamespace(GetId=lambda:controls['editor'].GetId(), Skip=lambda:None))
            assert not dialog.dead
            dialog.Destroy()  # event wrapper is deliberately not the original object
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            host.executor.futures[-1].result(timeout=10)
        host.native.drain()
        assert not controller.dirty and host.store.load(host.project.id)[1] == host.digest
    show(host,exercise)


def test_close_while_result_delivery_pending_waits_then_closes_without_apply(host):
    def exercise(dialog, controller, controls):
        controls['regex'].fire()
        host.executor.futures[-1].result(timeout=10)
        controls['close'].fire()
        assert dialog.modal
        host.native.drain()
        assert not dialog.modal and not controller.dirty and not host.dialogs.shown
    show(host,exercise)


def test_rejected_executor_restores_controls_and_recovery_timer(host):
    host.executor.reject = True
    def exercise(dialog, controller, controls):
        controls['regex'].fire()
        assert 'executor rejection' in controls['status'].value
        assert controls['save'].enabled and controls['regex'].enabled
        assert controls['editor'].editable and not controls['cancel_regex'].enabled
        assert host.native.timer.pending and not controller.dirty
        host.executor.reject = False
        controls['regex'].fire()
        finish(host,controls)
        controls['save'].fire()
        controls['close'].fire()
    show(host,exercise)


@pytest.mark.parametrize('choice', [0, 1], ids=['save', 'draft'])
@pytest.mark.parametrize('change', ['destroy', 'root', 'text', 'project'])
def test_nested_close_cannot_save_after_destruction_or_changed_binding(host, choice, change):
    original_root = host.store.root
    kept_root = original_root.with_name('kept-root')
    host.dialogs.answers = [choice]
    def exercise(dialog, controller, controls):
        controls['editor'].SetValue('An edit awaiting the close decision.')
        def change_during_close(title):
            assert title == 'Unsaved reading copy'
            if change == 'destroy':
                dialog.Destroy()  # delivered through an alias event object
            elif change == 'root':
                original_root.rename(kept_root)
                original_root.mkdir()
            elif change == 'text':
                controls['editor'].SetValue('Newer words entered during the choice.')
            else:
                controller.project = new_project('A different reading project.')
                controller.pending_text = controller.project.current_text
        host.dialogs.before_choice = change_during_close
        controls['close'].fire()
        preserved = NarrationStore(kept_root) if change == 'root' else host.store
        assert preserved.load(host.project.id)[1] == host.digest
        assert not any(item['draft'] for item in preserved.list_projects())
        if change == 'root':
            assert not tuple(original_root.rglob('*.json'))
        if not dialog.dead:
            assert dialog.modal
            if change == 'text':
                assert controls['editor'].value == 'Newer words entered during the choice.'
            dialog.Destroy()
    show(host, exercise)
