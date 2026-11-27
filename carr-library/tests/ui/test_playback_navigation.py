"""Navigation intent, generation and lifecycle; native device tests are separate."""
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.ui.playback_navigation import (PlaybackNavigation, PlaybackNavigationHost,
    create_navigation_dialog)


class Player:
    def __init__(self, path):
        self.path, self.duration_seconds, self.position_seconds = path, 10.0, 0.0
        self.state, self.position_is_approximate = 'playing', True
        self.calls = []
    def seek(self, seconds):
        self.calls.append(('seek', seconds))
        self.position_seconds = seconds
        if self.state != 'paused':
            self.state = 'playing'
    def stop(self):
        self.calls.append(('stop',))
        self.state, self.position_seconds = 'stopped', 0.0


class Pending:
    def __init__(self): self.actions = []
    def __call__(self, action, *, alive, on_error): self.actions.append((action, alive, on_error))
    def flush(self):
        actions, self.actions = self.actions, []
        for action, alive, failed in actions:
            try:
                if alive(): action()
            except Exception as exc:
                failed(exc)


@pytest.fixture
def transport(tmp_path):
    path = tmp_path / 'selected.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\0' * 80000)
    player, generation, clock, alive, notices = Player(path), [1], [100.0], [True], []
    def stop():
        generation[0] += 1
        player.stop()
    controller = PlaybackNavigation(player, generation=lambda: generation[0], alive=lambda: alive[0],
        clock=lambda: clock[0], stop_current=stop, report=notices.append)
    return SimpleNamespace(player=player, generation=generation, clock=clock, alive=alive,
        notices=notices, controller=controller, path=path)


def test_construction_and_binding_never_start_audio(transport):
    t = transport
    assert not t.controller.snapshot()['active'] and t.player.calls == []
    t.controller.bind()
    assert t.controller.snapshot()['active'] and t.player.calls == []
    assert t.controller.snapshot()['approximate'] is True


def test_configured_skip_percent_and_paused_seek_preserve_transport_intent(transport):
    t = transport; t.controller.bind()
    t.controller.set_skip(3)
    t.controller.skip(-1)
    t.controller.skip(1)
    t.controller.seek_percent(37.5)
    assert t.player.calls == [('seek', 0), ('seek', 3), ('seek', 3.75)]
    assert 'estimated' in t.notices[-1]
    t.player.state = 'paused'
    t.controller.seek_percent(100)
    assert t.player.state == 'paused' and t.player.position_seconds == 10
    t.player.state, t.player.position_is_approximate = 'playing', False
    t.controller.seek_percent(25)
    assert 'estimated' not in t.notices[-1]


@pytest.mark.parametrize('action,value', [
    ('set_skip', 0), ('set_skip', 3601), ('set_skip', True), ('set_skip', float('nan')),
    ('seek_percent', -1), ('seek_percent', 101), ('seek_percent', float('inf')), ('seek_percent', True),
    ('set_sleep', 0), ('set_sleep', 86401), ('set_sleep', float('nan')), ('set_sleep', False),
])
def test_invalid_budgets_never_change_transport_or_automation(transport, action, value):
    t = transport; t.controller.bind()
    with pytest.raises(ValueError): getattr(t.controller, action)(value)
    assert t.player.calls == [] and t.controller.sleep_deadline is None and not t.controller.repeat


def test_stopped_audio_is_not_started_by_seek_or_repeat_settings(transport):
    t = transport; t.controller.bind(); t.player.stop(); t.player.calls.clear()
    for operation in (lambda: t.controller.skip(1), lambda: t.controller.seek_percent(50),
                      lambda: t.controller.set_sleep(10), lambda: t.controller.set_range(1, 2)):
        with pytest.raises(ValueError, match='never starts stopped audio'):
            operation()
    assert t.player.calls == []


@pytest.mark.parametrize('change', ['generation', 'path', 'bytes', 'closed'])
def test_deferred_seek_and_sleep_cannot_touch_new_or_invalid_audio(transport, tmp_path, change):
    t = transport; t.controller.bind()
    pending = t.controller.dispatch = Pending()
    t.controller.seek_percent(50); t.controller.set_sleep(5)
    if change == 'generation': t.generation[0] += 1
    elif change == 'path': t.player.path = tmp_path / 'other.wav'
    elif change == 'bytes': t.path.write_bytes(b'Changed selected source')
    else: t.alive[0] = False
    t.clock[0] += 6
    pending.flush(); t.controller.tick()
    assert t.player.calls == []
    assert t.controller.audio is None and t.controller.sleep_deadline is None


def test_repeat_requires_ordered_finite_marks_and_explicit_enable(transport):
    t = transport; t.controller.bind()
    with pytest.raises(ValueError, match='Mark both'): t.controller.enable_repeat(True)
    with pytest.raises(ValueError, match='Mark A'): t.controller.mark_b()
    t.player.position_seconds = 1; t.controller.mark_a()
    t.player.position_seconds = 1.1
    with pytest.raises(ValueError, match='at least'): t.controller.mark_b()
    for start, end in ((2, 1), (1, float('inf')), (-1, 2), (1, 11), (True, 2)):
        with pytest.raises(ValueError): t.controller.set_range(start, end)
    t.player.position_seconds = 2; t.controller.mark_b()
    t.controller.tick()
    assert t.player.calls == []
    t.controller.enable_repeat(True); t.controller.tick()
    assert t.player.calls == [('seek', 1)]


def test_repeat_does_not_resume_paused_audio_and_has_only_one_pending_seek(transport):
    t = transport; t.controller.bind(); t.controller.set_range(1, 2); t.controller.enable_repeat(True)
    t.player.state, t.player.position_seconds = 'paused', 2.1
    t.controller.tick(); assert t.player.calls == []
    t.player.state = 'playing'
    pending = t.controller.dispatch = Pending()
    for _ in range(5): t.controller.tick()
    assert len(pending.actions) == 1
    t.player.state = 'paused'; pending.flush()
    assert t.player.calls == []
    t.player.state = 'playing'; t.controller.tick(); pending.flush()
    assert t.player.calls == [('seek', 1)]


def test_natural_end_can_repeat_but_external_stop_cannot(transport):
    t = transport; t.controller.bind(); t.controller.set_range(2, 10); t.controller.enable_repeat(True)
    t.player.state, t.player.position_seconds = 'stopped', 10
    t.controller.tick()
    assert t.player.calls == [('seek', 2)]
    t.player.stop(); t.player.calls.clear()
    t.controller.tick()
    assert t.player.calls == [] and t.controller.audio is None and not t.controller.repeat


def test_disabling_repeat_discards_a_queued_loop_and_manual_seek_outside_disables_it(transport):
    t = transport; t.controller.bind(); t.controller.set_range(1, 2); t.controller.enable_repeat(True)
    pending = t.controller.dispatch = Pending()
    t.player.position_seconds = 3; t.controller.tick()
    t.controller.enable_repeat(False); pending.flush()
    assert t.player.calls == []
    t.controller.enable_repeat(True); t.controller.seek_percent(75); pending.flush()
    assert t.player.calls == [('seek', 7.5)] and not t.controller.repeat


@pytest.mark.parametrize('paused', [False, True])
def test_sleep_timer_includes_paused_time_and_stops_once_through_reader_lifecycle(transport, paused):
    t = transport; t.controller.bind(); t.controller.set_sleep(30)
    if paused: t.player.state = 'paused'
    t.clock[0] += 29.9; t.controller.tick(); assert t.player.calls == []
    t.clock[0] += .1; t.controller.tick(); t.controller.tick()
    assert t.player.calls == [('stop',)] and t.generation == [2]
    assert t.controller.audio is None and t.controller.sleep_deadline is None
    assert t.notices == ['Sleep timer stopped the current recording.']


def test_expired_sleep_wins_over_already_queued_seek_or_repeat(transport):
    t = transport; t.controller.bind(); t.controller.set_range(1, 2); t.controller.enable_repeat(True)
    pending = t.controller.dispatch = Pending()
    t.controller.set_sleep(1); t.player.position_seconds = 3; t.controller.tick()
    t.clock[0] += 2; pending.flush()
    assert t.player.calls == [('stop',)] and not t.controller.repeat


def test_cancelled_sleep_and_closed_controller_never_stop_borrowed_player(transport):
    t = transport; t.controller.bind(); t.controller.set_sleep(1); t.controller.cancel_sleep()
    t.clock[0] += 2; t.controller.tick(); assert t.player.calls == []
    pending = t.controller.dispatch = Pending()
    t.controller.seek_percent(50); t.controller.close(); pending.flush(); t.controller.tick()
    assert t.player.calls == []
    with pytest.raises(ValueError): t.controller.bind()


def test_failed_native_stop_is_not_reported_as_success_or_retried(transport):
    t = transport; t.controller.bind(); t.controller.set_sleep(1)
    def fail(): raise RuntimeError('Device refused stop')
    t.controller.stop_current = fail
    t.clock[0] += 2; t.controller.tick(); t.controller.tick()
    assert len(t.notices) == 1 and 'unavailable' in t.notices[0] and 'Device refused' in t.notices[0]


def test_new_explicit_playback_binding_discards_old_repeat_sleep_and_queued_seek(transport):
    t = transport; t.controller.bind(); t.controller.set_range(1, 2); t.controller.enable_repeat(True)
    t.controller.set_sleep(1)
    pending = t.controller.dispatch = Pending()
    t.controller.seek_percent(15)
    t.generation[0] += 1; t.controller.bind()
    t.clock[0] += 2; pending.flush(); t.controller.tick()
    assert t.player.calls == [] and t.controller.audio is not None
    assert not t.controller.repeat and t.controller.a is None and t.controller.sleep_deadline is None


def test_ui_snapshot_fails_closed_if_native_position_query_loses_its_device(transport):
    t = transport; t.controller.bind(); t.controller.set_sleep(1)
    class LostDevice:
        path, duration_seconds, position_is_approximate = t.path, 10.0, False
        @property
        def state(self): raise RuntimeError('Device disconnected')
    t.controller.player = LostDevice()
    value = t.controller.snapshot()
    assert not value['active'] and value['sleep_remaining'] is None
    assert t.controller.audio is None and len(t.notices) == 1 and 'disconnected' in t.notices[0]
    t.controller.snapshot()
    assert len(t.notices) == 1


class Widget:
    def __init__(self, parent=None, *args, label='', value='', **kwargs):
        self.parent, self.label, self.value = parent, label, value
        self.name, self.enabled, self.bindings, self.modal = '', True, {}, False
    def SetName(self, value): self.name = value
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def GetLabel(self): return self.label
    def SetLabel(self, value): self.label = value
    def Bind(self, event, callback, source=None): self.bindings[event] = callback
    def Enable(self, value=True): self.enabled = value
    def SetSizer(self, value): pass
    def SetEscapeId(self, value): pass
    def IsModal(self): return self.modal
    def ShowModal(self): self.modal = True; return 0
    def EndModal(self, value): self.modal = False
    def Destroy(self): self.destroyed = True
    def Raise(self): pass


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


class Timer:
    def __init__(self, owner): self.owner, self.running = owner, False
    def Start(self, interval): self.interval, self.running = interval, True
    def Stop(self): self.running = False


@pytest.fixture
def wx():
    return SimpleNamespace(Dialog=Widget, StaticText=Widget, TextCtrl=Widget, Button=Widget,
        CheckBox=Widget, Timer=Timer, BoxSizer=Sizer, CallLater=lambda delay, callback: callback(),
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8,
        ALIGN_RIGHT=16, TE_READONLY=32, TE_MULTILINE=64, ID_CANCEL=99,
        EVT_BUTTON='button', EVT_CHECKBOX='check', EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy')


def test_named_ui_controls_dispatch_current_actions_and_explain_invalid_input(transport, wx):
    t = transport
    view = create_navigation_dialog(Widget(), t.controller, wx_module=wx)
    assert not view.buttons['forward'].enabled and t.player.calls == []
    t.controller.bind()
    assert view.buttons['forward'].enabled
    view.skip.SetValue('2'); view.buttons['forward'].bindings['button'](None)
    assert t.player.calls == [('seek', 2)]
    view.percent.SetValue('nan'); view.buttons['seek'].bindings['button'](None)
    assert 'finite' in view.status.GetValue() and len(t.player.calls) == 1
    assert view.position.GetName() == 'Current playback position and backend accuracy'
    assert view.repeat.GetName() == 'Enable A–B repeat'
    assert all(button.GetName() for button in view.buttons.values())
    t.controller.listeners.discard(view.refresh)


def test_host_owns_only_its_timer_and_ignores_child_destruction(transport, wx):
    t, owner = transport, Widget()
    host = PlaybackNavigationHost(owner, t.player, generation=lambda: t.generation[0],
        alive=lambda: t.alive[0], stop_current=t.player.stop, wx_module=wx)
    host.bind(); host.controller.set_sleep(20)
    assert host.timer.running and host.timer.interval == 200
    skipped = []
    host._destroyed(SimpleNamespace(GetEventObject=lambda: Widget(owner), Skip=lambda: skipped.append(True)))
    assert not host.closed and host.timer.running
    host._destroyed(SimpleNamespace(GetEventObject=lambda: owner, Skip=lambda: skipped.append(True)))
    assert host.closed and not host.timer.running and host.controller.sleep_deadline is None
    assert t.player.calls == [] and skipped == [True, True]


def test_closing_controls_view_keeps_reader_timer_but_host_close_cancels_it(transport, wx):
    t, owner = transport, Widget()
    host = PlaybackNavigationHost(owner, t.player, generation=lambda: t.generation[0],
        alive=lambda: t.alive[0], stop_current=t.player.stop, wx_module=wx)
    host.bind(); host.controller.set_sleep(20)
    host.show()
    assert host.view is None and host.timer.running and host.controller.sleep_deadline is not None
    host.close()
    assert not host.timer.running and t.player.calls == []


def test_host_invalidates_pending_controls_even_if_native_timer_close_fails(transport, wx):
    t, owner = transport, Widget()
    host = PlaybackNavigationHost(owner, t.player, generation=lambda: t.generation[0],
        alive=lambda: t.alive[0], stop_current=t.player.stop, wx_module=wx)
    host.bind(); host.controller.set_sleep(10)
    pending = host.controller.dispatch = Pending()
    host.controller.seek_percent(50)
    def fail(): raise RuntimeError('Native timer close failed')
    host.timer.Stop = fail
    with pytest.raises(RuntimeError, match='timer close'): host.close()
    pending.flush()
    assert host.closed and host.controller.closed and host.controller.sleep_deadline is None
    assert t.player.calls == []
