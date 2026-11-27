"""Finite contracts for the probe harness; these are not Windows acceptance.

The scheduler models loop ownership and failed input delivery explicitly. The
real probes still have to prove native dispatch and durable product outcomes.
"""
import heapq
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


class Window:
    def __init__(self, runtime, name, parent=None):
        self.runtime, self.name, self.parent = runtime, name, parent
        self.handle = len(runtime.windows) + 1
        self.active = True
        self.focus_target = self
        runtime.windows.append(self)

    def GetParent(self):
        return self.parent

    def GetName(self):
        return self.name

    def GetHandle(self):
        return self.handle

    def IsActive(self):
        return self.active

    def Raise(self):
        self.active = True

    def SetFocus(self):
        self.runtime.focus = self.focus_target


class Dialog(Window):
    def IsModal(self):
        return True


class Runtime:
    def __init__(self):
        self.windows, self.queue, self.timers, self.events, self.delivered = [], [], [], [], []
        self.focus, self.loop = None, None
        self.now, self.sequence, self.main_loops = 0, 0, 0
        self.foreground = 0
        self.allow_foreground, self.release_ok = True, True
        self.held = None
        self.stop_after_keydown = False
        self.wx = SimpleNamespace(
            Window=SimpleNamespace(FindFocus=lambda: self.focus), Dialog=Dialog,
            GetTopLevelParent=self.top,
            GetTopLevelWindows=lambda: [w for w in self.windows if w.parent is None],
            CallLater=self.later, UIActionSimulator=lambda: self)
        self.user32 = SimpleNamespace(GetForegroundWindow=lambda: self.foreground,
            SetForegroundWindow=self.activate, GetFocus=lambda: self.focus.handle if self.focus else 0)
        self.frame = Window(self, 'frame')
        self.control = Window(self, 'control', self.frame)
        self.focus, self.foreground = self.control, self.frame.handle

    @staticmethod
    def top(window):
        while window.parent is not None:
            window = window.parent
        return window

    def activate(self, handle):
        if self.allow_foreground:
            self.foreground = handle
        return self.allow_foreground

    def later(self, delay, callback):
        timer = SimpleNamespace(stopped=False, fired=False)
        timer.Stop = lambda: setattr(timer, 'stopped', True)
        self.sequence += 1
        heapq.heappush(self.queue, (self.now + delay, self.sequence, timer, callback))
        self.timers.append(timer)
        return timer

    def GetMainLoop(self):
        return self.loop

    def MainLoop(self):
        self.main_loops += 1
        self.loop = SimpleNamespace(running=True)
        self.loop.IsRunning = lambda: self.loop is not None and self.loop.running
        try:
            while self.loop.running and self.queue:
                due, _, timer, callback = heapq.heappop(self.queue)
                if not timer.stopped:
                    self.now = due
                    timer.fired = True
                    callback()
        finally:
            self.loop = None

    def ExitMainLoop(self):
        self.loop.running = False

    def Char(self, key, modifiers=0):
        self.events.append(('char', key, modifiers, self.loop is not None))
        if self.loop is not None or any(isinstance(w, Dialog) for w in self.windows):
            self.delivered.append((key, modifiers))
        return True

    def KeyDown(self, key, modifiers):
        running = self.loop is not None and self.loop.IsRunning()
        self.events.append(('down', key, modifiers, running))
        self.held = (key, modifiers)
        if running:
            self.later(1, lambda: self.delivered.append((key, modifiers)))
        if self.stop_after_keydown:
            self.ExitMainLoop()
        return True

    def KeyUp(self, key, modifiers):
        self.events.append(('up', key, modifiers, self.loop is not None))
        assert self.held == (key, modifiers)
        self.held = None
        return self.release_ok


@pytest.fixture
def harness(monkeypatch):
    helper = runpy.run_path(str(Path(__file__).with_name('native_input_support.py')))['NativeInput']
    # Keep these finite contracts independent of native user32 even on Windows.
    monkeypatch.setitem(helper.__init__.__globals__, 'sys', SimpleNamespace(platform='harness'))
    runtime = Runtime()
    native = helper(runtime.wx, runtime, simulator=runtime)
    native.user32 = runtime.user32
    return native, runtime


def test_thread_focus_without_foreground_is_not_authorization_to_send_keys(harness):
    native, runtime = harness
    runtime.foreground, runtime.allow_foreground = 999, False
    assert runtime.focus is runtime.control
    with pytest.raises(AssertionError, match='foreground activation'):
        native.focus(runtime.control, lambda predicate: predicate())
    with pytest.raises(AssertionError, match='focused foreground'):
        native.press(ord('J'), 6)
    assert runtime.events == []
    assert runtime.main_loops == 0


def test_focus_accepts_native_child_only_with_matching_top_window(harness):
    native, runtime = harness
    child = Window(runtime, 'native edit child', runtime.control)
    runtime.control.focus_target = child
    runtime.foreground = 999
    native.focus(runtime.control, lambda predicate: predicate())
    assert runtime.focus is child
    assert runtime.foreground == runtime.frame.handle
    runtime.frame.active = False
    with pytest.raises(AssertionError, match='focused foreground'):
        native.press(ord('J'), 6)
    assert runtime.events == []


def test_accepted_input_needs_live_mainloop_and_paired_modifier_release(harness):
    native, runtime = harness
    assert runtime.Char(ord('J'), 6)  # An input API can accept without dispatch.
    assert runtime.delivered == []
    runtime.events.clear()
    assert native.press(ord('J'), 6)
    assert runtime.main_loops == 1
    assert runtime.events == [('down', ord('J'), 6, True), ('up', ord('J'), 6, True)]
    assert runtime.delivered == [(ord('J'), 6)]
    assert runtime.held is None
    assert all(timer.stopped or timer.fired for timer in runtime.timers)


@pytest.mark.parametrize('owner', ['modal', 'main_loop'])
def test_existing_native_loop_keeps_the_original_char_delivery(harness, owner):
    native, runtime = harness
    if owner == 'modal':
        dialog = Dialog(runtime, 'reader')
        runtime.focus, runtime.foreground = dialog, dialog.handle
    else:
        runtime.loop = SimpleNamespace(IsRunning=lambda: True)
    assert native.press(ord('B'), 6)
    assert runtime.main_loops == 0
    assert runtime.events[0][:3] == ('char', ord('B'), 6)
    assert runtime.delivered == [(ord('B'), 6)]


def test_callback_abort_releases_exact_key_and_modifiers_without_success(harness):
    native, runtime = harness
    calls = 0

    def healthy():
        nonlocal calls
        calls += 1
        if calls == 3:  # First callback already pressed the key.
            raise RuntimeError('watchdog stopped scenario')

    native.healthy = healthy
    with pytest.raises(RuntimeError, match='watchdog stopped'):
        native.press(ord('J'), 6)
    assert runtime.events == [('down', ord('J'), 6, True), ('up', ord('J'), 6, False)]
    assert runtime.held is None
    assert runtime.loop is None
    assert all(timer.stopped or timer.fired for timer in runtime.timers)


def test_early_native_loop_exit_releases_keys_but_cannot_report_completion(harness):
    native, runtime = harness
    runtime.stop_after_keydown = True
    with pytest.raises(AssertionError, match='stopped before delivering'):
        native.press(ord('J'), 6)
    assert runtime.events[-1] == ('up', ord('J'), 6, False)
    assert runtime.held is None
    assert runtime.delivered == []


def test_native_key_release_failure_is_not_a_successful_probe(harness):
    native, runtime = harness
    runtime.release_ok = False
    with pytest.raises(AssertionError, match='rejected key-up'):
        native.press(ord('J'), 6)
    assert runtime.held is None
    assert runtime.loop is None
