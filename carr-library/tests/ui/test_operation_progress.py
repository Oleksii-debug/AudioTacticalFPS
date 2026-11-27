"""Measured event, generated PCM and headless native-control acceptance."""
from dataclasses import replace
from io import BytesIO
import json
import math
import os
import struct
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.ui.operation_progress import (OperationProgress, ProgressEvent, ProgressPreferences,
    ProgressTracker, WindowsProgressSound, describe_progress, load_preferences, progress_frequency,
    progress_tone, save_preferences, install_operation_progress)


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds=1): self.now += seconds


def test_known_units_report_zero_quarters_and_only_durable_success_reaches_100():
    clock = Clock()
    tracker = ProgressTracker(clock=clock)
    start = tracker.start('Export audio', 100)
    assert start.percent == 0 and start.completed == 0
    for completed in (25, 50, 75, 100):
        clock.advance()
        current = tracker.update(completed, 100, 'Export audio', token=start.token)
        assert current.percent == min(completed, 99)
    assert 'not yet been confirmed' in describe_progress(current)
    success = tracker.transition('succeeded', 'Output verified and committed', token=start.token)
    assert success.percent == 100 and success.state == 'succeeded'
    assert 'Completed' in describe_progress(success)
    assert tracker.update(90, 100, token=start.token) is None
    assert tracker.transition('failed', token=start.token) is None


def test_unknown_duration_never_invents_percent_or_eta():
    tracker = ProgressTracker()
    event = tracker.start('Inspecting source')
    assert event.percent is event.total is event.eta_seconds is None
    event = tracker.update(7, None)
    assert event.percent is event.eta_seconds is None
    assert 'indeterminate' in describe_progress(event) and '7 units completed' in describe_progress(event)
    assert tracker.transition('failed', 'Source could not be inspected').percent is None


def test_fixed_total_is_monotonic_and_changed_total_is_explicit():
    tracker = ProgressTracker()
    tracker.start('Extracting pages', 10)
    tracker.update(7, 10)
    with pytest.raises(ValueError, match='backwards'):
        tracker.update(6, 10, 'Another description')
    with pytest.raises(ValueError, match='changed progress total'):
        tracker.update(7, 20)
    event = tracker.update(7, 20, 'More pages discovered')
    assert event.total_changed and event.percent == 35
    assert 'measured total changed' in describe_progress(event)


def test_eta_requires_measured_samples_excludes_pause_and_resets_on_resume():
    clock = Clock()
    tracker = ProgressTracker(clock=clock)
    tracker.start('Converting', 100)
    assert tracker.update(10, 100).eta_seconds is None
    clock.advance(1)
    assert tracker.update(20, 100).eta_seconds is None
    clock.advance(1)
    event = tracker.update(30, 100)
    assert event.eta_seconds == pytest.approx(70 * 2 / 30)
    tracker.transition('paused')
    clock.advance(3600)
    assert tracker.event.eta_seconds is None
    tracker.transition('running')
    assert tracker.event.elapsed_seconds == 2
    clock.advance(1)
    assert tracker.update(40, 100).eta_seconds is None
    clock.advance(1)
    assert tracker.update(50, 100).eta_seconds == pytest.approx(5)
    assert tracker.event.elapsed_seconds == 4


@pytest.mark.parametrize('state', ['cancelled', 'failed', 'paused', 'incomplete'])
def test_non_success_states_never_reach_100(state):
    tracker = ProgressTracker()
    tracker.start('Finishing export', 1)
    tracker.update(1, 1)
    assert tracker.transition(state).percent == 99


def test_old_operation_tokens_and_independent_trackers_keep_jobs_separate():
    one, two = ProgressTracker(), ProgressTracker()
    old = one.start('Old job', 10)
    new = one.start('New job', 5)
    two.start('Independent worker', 50)
    assert one.update(10, 10, token=old.token) is None
    assert one.transition('failed', token=old.token) is None
    assert one.update(2, 5, token=new.token).percent == 40
    assert two.update(5, 50).percent == 10


@pytest.mark.parametrize(('completed', 'total'), [(True, 10), (-1, 10), (float('nan'), 10),
    (float('inf'), 10), (11, 10), (1, 0), (1, True), (None, 10), (1, 10**13)])
def test_invalid_measured_units_are_rejected(completed, total):
    tracker = ProgressTracker()
    tracker.start('Measured work', 10)
    with pytest.raises(ValueError):
        tracker.update(completed, total)


def test_phase_names_are_readable_and_bounded():
    tracker = ProgressTracker()
    for value in ('', 'A\nB', 'x' * 513, None):
        with pytest.raises(ValueError):
            tracker.start(value)


def _samples(raw):
    with wave.open(BytesIO(raw), 'rb') as stream:
        assert stream.getnchannels() == 1 and stream.getsampwidth() == 2
        return stream.getframerate(), [value[0] for value in struct.iter_unpack('<h', stream.readframes(stream.getnframes()))]


def _measured_frequency(rate, samples):
    crossings = []
    for index in range(round(rate * .012), len(samples) - round(rate * .012)):
        left, right = samples[index - 1], samples[index]
        if left < 0 <= right and right != left:
            crossings.append(index - 1 - left / (right - left))
    return rate * (len(crossings) - 1) / (crossings[-1] - crossings[0])


@pytest.mark.parametrize('percent', [0, 25, 50, 75, 100])
def test_actual_pcm_tones_have_measured_rising_pitch_duration_and_bounded_peak(percent):
    rate, samples = _samples(progress_tone(percent, volume=1))
    assert len(samples) / rate == pytest.approx(.08)
    assert _measured_frequency(rate, samples) == pytest.approx(progress_frequency(percent), abs=.1)
    assert max(abs(value) for value in samples) <= round(32767 * .35)
    assert samples[0] == samples[-1] == 0


def test_volume_scales_actual_pcm_and_zero_is_silent():
    _, full = _samples(progress_tone(50, volume=1))
    _, half = _samples(progress_tone(50, volume=.5))
    _, silent = _samples(progress_tone(50, volume=0))
    rms = lambda values: math.sqrt(sum(value * value for value in values) / len(values))
    assert rms(half) / rms(full) == pytest.approx(.5, abs=.0001)
    assert not any(silent)


@pytest.mark.parametrize('state', ['running', 'failed', 'cancelled', 'paused', 'resumed', 'succeeded', 'incomplete'])
def test_busy_and_terminal_cues_are_short_distinct_memory_waves(state):
    raw = progress_tone(None, state=state)
    rate, samples = _samples(raw)
    assert len(samples) / rate == pytest.approx(.19, abs=.00005)
    assert len(raw) < 10_000
    if state != 'running':
        assert raw != progress_tone(None, state='running')


def test_native_sound_playback_is_nonblocking_single_voice_and_never_uses_async_memory():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def play(raw, flags):
        calls.append((raw, flags, threading.get_ident()))
        entered.set()
        assert release.wait(2)
    backend = SimpleNamespace(PlaySound=play, SND_MEMORY=4, SND_NODEFAULT=2, SND_NOSTOP=16, SND_ASYNC=1)
    player = WindowsProgressSound(backend=backend)
    before = time.monotonic()
    assert player.play(25, volume=.2)
    assert time.monotonic() - before < .2
    assert entered.wait(1)
    assert not player.play(50, volume=.2)
    assert calls[0][2] != threading.get_ident()
    assert calls[0][1] == 22 and calls[0][1] & backend.SND_ASYNC == 0
    assert isinstance(calls[0][0], bytes)
    player.close()
    assert not player.play(100, volume=.2, state='succeeded')
    release.set()
    player._thread.join(2)
    assert len(calls) == 1  # no global PlaySound(None) that could stop other audio


def test_preferences_default_off_persist_and_reject_invalid_or_linked_state(tmp_path):
    assert load_preferences(tmp_path) == ProgressPreferences()
    settings = ProgressPreferences(mode='beep', interval_seconds=7, volume=.15, quiet=True, background=True)
    save_preferences(tmp_path, settings)
    assert load_preferences(tmp_path) == settings
    path = tmp_path / 'state/ui/progress.json'
    value = json.loads(path.read_bytes())
    value['preferences']['volume'] = 3
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='volume'):
        load_preferences(tmp_path)
    outside = tmp_path / 'outside.json'
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match='links'):
        load_preferences(tmp_path)


@pytest.mark.parametrize('changes', [{'mode': 'cloud'}, {'interval_seconds': 0}, {'interval_seconds': float('nan')},
    {'volume': float('inf')}, {'volume': -1}, {'quiet': 1}, {'background': 'yes'}, {'mute_during_audio': None}])
def test_preferences_require_valid_explicit_options(changes):
    with pytest.raises(ValueError):
        replace(ProgressPreferences(), **changes)


@pytest.mark.parametrize('field', ['interval_seconds', 'volume'])
@pytest.mark.parametrize('sign', [-1, 1])
def test_huge_numeric_preferences_keep_startup_silent_and_preserve_file(native, field, sign):
    from dataclasses import asdict

    controller = native.controller
    save_preferences(native.frame.library.root, ProgressPreferences(mode='beep'))
    path = native.frame.library.root / 'state/ui/progress.json'
    values = asdict(ProgressPreferences(mode='beep'))
    values[field] = sign * 10 ** 400
    raw = json.dumps({'schema': 1, 'preferences': values}).encode('utf-8')
    assert len(raw) < 4096
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='interval|volume'):
        load_preferences(native.frame.library.root)
    controller.reload_preferences()
    assert controller.preferences == ProgressPreferences()
    assert native.sound.muted and native.sound.calls == []
    assert native.frame.notices and 'could not be read' in native.frame.notices[-1]
    assert path.read_bytes() == raw


class Gauge:
    def __init__(self): self.names, self.values, self.pulses, self.ranges = [], [], 0, []
    def SetName(self, name): self.names.append(name)
    def SetRange(self, value): self.ranges.append(value)
    def SetValue(self, value): self.values.append(value)
    def Pulse(self): self.pulses += 1
    def SetFocus(self): pytest.fail('Progress update stole focus')


class Label:
    def __init__(self): self.labels, self.names = [], []
    def SetLabel(self, label): self.labels.append(label)
    def SetName(self, name): self.names.append(name)
    def SetFocus(self): pytest.fail('Progress label stole focus')


class Timer:
    def __init__(self, frame): self.running, self.interval = False, None
    def Start(self, interval): self.running, self.interval = True, interval
    def Stop(self): self.running = False
    def IsRunning(self): return self.running


class Sound:
    def __init__(self): self.available, self.muted, self.closed, self.calls = True, False, False, []
    def play(self, percent, **kwargs): self.calls.append((percent, kwargs)); return True
    def close(self): self.closed = True


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.active, self.reports, self.notices, self.bindings = True, [], [], []
        self.status = SimpleNamespace(SetValue=self.notices.append)
    def IsActive(self): return self.active
    def Bind(self, *args): self.bindings.append(args)
    def report(self, message): self.reports.append(message)


@pytest.fixture
def native(tmp_path):
    clock, queue, frame, gauge, label, sound = Clock(), [], Frame(tmp_path), Gauge(), Label(), Sound()
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', CallAfter=lambda callback: queue.append(callback))
    controller = OperationProgress(frame, gauge, label, wx_module=wx, clock=clock, sound=sound)
    return SimpleNamespace(controller=controller, clock=clock, queue=queue, frame=frame, gauge=gauge, label=label, sound=sound)


def test_native_indeterminate_gauge_pulses_with_name_state_and_no_focus(native):
    value = native
    token = value.controller.start('Reading source')
    assert value.gauge.pulses == 1
    assert value.controller.timer.interval == 250
    assert value.gauge.names[-1] == 'Operation progress: In progress'
    value.clock.advance(.25)
    value.controller._tick()
    assert value.gauge.pulses == 2
    value.controller.pause(token=token)
    assert 'Paused' in value.gauge.names[-1]
    value.clock.advance(.25)
    value.controller._tick()
    assert value.gauge.pulses == 2
    value.controller.resume(token=token)
    value.controller.cancel(token=token)
    assert not value.controller.timer.running
    assert 'Cancelled' in value.label.labels[-1]
    assert value.sound.calls == [] and value.frame.reports == []


def test_native_known_units_throttle_without_losing_last_value_or_fake_completion(native):
    controller = native.controller
    token = controller.start('Mixing', 4)
    controller.update(1, 4, token=token)
    controller.update(2, 4, token=token)
    assert native.gauge.values[-1] == 0
    native.clock.advance(.25)
    controller._tick()
    assert native.gauge.values[-1] == 50
    controller.update(4, 4, token=token)
    native.clock.advance(.25)
    controller._tick()
    assert native.gauge.values[-1] == 99
    controller.succeed('Output committed', token=token)
    assert native.gauge.values[-1] == 100 and not controller.timer.running
    controller.finalize(token=token)
    assert controller.event.state == 'succeeded'


def test_finalize_without_reported_success_is_explicitly_incomplete(native):
    token = native.controller.start('Import', 1)
    native.controller.update(1, 1)
    native.controller.finalize(token=token)
    assert native.controller.event.state == 'incomplete'
    assert native.gauge.values[-1] == 99 and not native.controller.timer.running


def test_worker_updates_are_coalesced_and_stale_callbacks_cannot_change_new_job(native):
    controller = native.controller
    token = controller.start('First operation', 1000)
    def work():
        for index in range(1001):
            controller.update(index, 1000, token=token)
    worker = threading.Thread(target=work)
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and len(native.queue) == 1
    new = controller.start('Next operation', 10)
    native.queue.pop()()
    native.clock.advance(.25)
    controller._tick()
    assert controller.event.token == new and native.gauge.values[-1] == 0
    assert native.label.labels[-1].startswith('In progress: Next operation')


def test_audible_preferences_intervals_quiet_background_and_audio_suppression(native):
    controller, clock, sound = native.controller, native.clock, native.sound
    controller.configure(ProgressPreferences(mode='beep', interval_seconds=3, volume=.2))
    controller.start('Render', 10)
    assert sound.calls == [(0, {'volume': .2, 'state': 'running'})]
    clock.advance(2)
    controller.update(2, 10)
    assert len(sound.calls) == 1
    clock.advance(1)
    controller._tick()
    assert len(sound.calls) == 2
    native.frame.active = False
    clock.advance(10)
    controller._tick()
    assert len(sound.calls) == 2
    native.frame.active = True
    controller.set_audio_active(True)
    controller._tick()
    assert len(sound.calls) == 2 and sound.muted
    controller.set_audio_active(False)
    controller.configure(ProgressPreferences(mode='beep', quiet=True))
    controller._tick()
    assert len(sound.calls) == 2
    controller.configure(ProgressPreferences(mode='beep', interval_seconds=3, background=True))
    controller.pause()
    controller.resume()
    controller.fail('Output verification failed')
    assert [call[1]['state'] for call in sound.calls[-3:]] == ['paused', 'resumed', 'failed']
    controller.close()
    assert sound.closed and not controller.timer.running


def test_explicit_progress_query_is_the_only_report_action(native):
    native.controller.start('Importing files', 5)
    native.controller.update(2, 5)
    assert native.frame.reports == []
    native.controller.query()
    assert '40 percent' in native.frame.reports[-1]


def test_root_switch_loads_independent_off_preference(native, tmp_path):
    controller = native.controller
    controller.configure(ProgressPreferences(mode='beep'))
    other = tmp_path / 'other-library'
    other.mkdir()
    native.frame.library = SimpleNamespace(root=other)
    controller.start('Other library')
    assert controller.preferences.mode == 'off' and native.sound.muted


def test_registered_wave_players_follow_actual_eof_and_device_error_without_stuck_mute(native):
    from carr_library.speech.player import WavePlayer

    controller, sound = native.controller, native.sound
    controller.configure(ProgressPreferences(mode='beep', interval_seconds=1))
    first, second = WavePlayer(), WavePlayer()
    first_process = SimpleNamespace(returncode=None)
    second_process = SimpleNamespace(returncode=None)
    first_process.poll = lambda: first_process.returncode
    second_process.poll = lambda: second_process.returncode
    for player, process in ((first, first_process), (second, second_process)):
        player._state, player._process, player.duration_seconds = 'playing', process, 2.0
        controller.register_player(player)
    controller.start('Rendering another output')
    assert sound.muted and not sound.calls

    first_process.returncode = 0
    controller._tick()
    assert first.state == 'stopped' and first.position_seconds == 2.0
    assert sound.muted  # The other recording is still playing.
    second_process.returncode = 1
    controller._tick()
    assert second.state == 'error' and not sound.muted
    assert len(sound.calls) == 1
    controller.unregister_player(first)
    controller.unregister_player(second)
    controller.pause()
    assert not controller.timer.IsRunning()


def test_playback_pause_resume_stop_and_multiple_players_are_observed_on_timer(native):
    class Player:
        state = 'playing'

    controller = native.controller
    controller.configure(ProgressPreferences(mode='beep'))
    player, other = Player(), Player()
    controller.register_player(player)
    assert native.sound.muted and controller.timer.IsRunning()
    player.state = 'paused'
    controller._tick()
    assert not native.sound.muted
    player.state = 'playing'
    controller._tick()
    assert native.sound.muted
    controller.register_player(other)
    player.state = 'stopped'
    controller._tick()
    assert native.sound.muted
    controller.unregister_player(other)
    assert not native.sound.muted
    controller.unregister_player(player)
    assert not controller.timer.IsRunning()


def test_player_observation_does_not_retain_closed_dialogs_and_keeps_device_errors_quiet(native):
    import gc
    import weakref

    class Player:
        @property
        def state(self):
            raise RuntimeError('Device is unavailable')

    controller = native.controller
    controller.configure(ProgressPreferences(mode='beep'))
    player = Player()
    reference = weakref.ref(player)
    controller.register_player(player)
    assert native.sound.muted and controller.timer.IsRunning()
    del player
    gc.collect()
    assert reference() is None
    controller._tick()
    assert not native.sound.muted and not controller.timer.IsRunning()


def test_worker_player_registration_and_removal_are_marshaled_to_ui(native):
    class Player:
        state = 'playing'

    controller, player = native.controller, Player()
    controller.configure(ProgressPreferences(mode='beep'))
    worker = threading.Thread(target=lambda: controller.register_player(player))
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and len(native.queue) == 1
    assert not native.sound.muted
    native.queue.pop()()
    assert native.sound.muted
    worker = threading.Thread(target=lambda: controller.unregister_player(player))
    worker.start()
    worker.join(2)
    assert not worker.is_alive() and len(native.queue) == 1
    native.queue.pop()()
    assert not native.sound.muted and not controller.timer.IsRunning()


def test_installer_adds_native_gauge_query_and_settings_to_configurable_shortcuts(tmp_path, monkeypatch):
    from carr_library.ui.keyboard_settings import KeyboardSettings
    items = []
    class Item:
        def __init__(self, label): self.label, self.identity = label, len(items) + 100
        def GetId(self): return self.identity
        def GetItemLabel(self): return self.label
        def SetItemLabel(self, label): self.label = label
        def IsSeparator(self): return False
        def GetSubMenu(self): return None
    class Menu:
        def Append(self, identity, label):
            value = Item(label)
            items.append(value)
            return value
        def GetMenuItems(self): return items
    menu = Menu()
    frame = Frame(tmp_path)
    frame.host = object()
    insertions = []
    frame.layout = SimpleNamespace(Insert=lambda *args: insertions.append(args))
    frame.menu_bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabel=lambda index: '&Help', GetMenu=lambda index: menu)
    frame.SetAcceleratorTable = lambda table: setattr(frame, 'table', table)
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', EVT_MENU='menu', EVT_WINDOW_DESTROY='destroy',
                         CallAfter=lambda call: call(), StaticText=lambda *args, **kwargs: Label(),
                         Gauge=lambda *args, **kwargs: Gauge(), GA_HORIZONTAL=1, EXPAND=2, ALL=4, ID_ANY=-1,
                         ACCEL_CTRL=1, ACCEL_ALT=2, ACCEL_SHIFT=4, AcceleratorTable=lambda entries: tuple(entries))
    monkeypatch.setitem(__import__('sys').modules, 'wx', wx)
    controller = install_operation_progress(frame)
    assert frame.operation_progress is controller and len(insertions) == 2
    assert len(items) == 2
    shortcuts = KeyboardSettings(frame, wx_module=wx)
    query = next(command for command in shortcuts.commands if command.action == 'help/current operation progress')
    assert query.default == 'Ctrl+Alt+P'
    assert (3, ord('P'), query.menu_id) in frame.table
    controller.start('Exporting', 4)
    controller.update(1, 4)
    callback = next(binding[1] for binding in frame.bindings if len(binding) == 3 and binding[0] == 'menu' and binding[2].GetId() == query.menu_id)
    callback(None)
    assert '25 percent' in frame.reports[-1]
    assert install_operation_progress(frame) is controller and len(items) == 2
    destroy = next(binding[1] for binding in frame.bindings if binding[0] == 'destroy')
    skipped = []
    destroy(SimpleNamespace(GetEventObject=lambda: frame, Skip=lambda: skipped.append(True)))
    assert controller.closed and skipped == [True]
