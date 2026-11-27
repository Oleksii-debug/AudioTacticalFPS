"""Real PCM headers with modeled device protocols; no acoustic qualification."""
from hashlib import sha256
import math
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import player as module
from carr_library.speech.player import WavePlayer
from carr_library.ui.reader_playback import ReaderPlaybackControls


class Clock:
    def __init__(self):
        self.value = 100.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


class Process:
    def __init__(self, argv):
        self.argv, self.returncode, self.pid = tuple(argv), None, 12345
    def poll(self):
        return self.returncode


class Native:
    """Independent MCI protocol model; the installed probe uses actual WinMM."""
    def __init__(self, clock, duration):
        self.clock, self.duration = clock, duration
        self.commands = []
        self.mode, self.offset, self.started, self.end = 'stopped', 0, clock(), duration
    def position(self):
        if self.mode == 'playing':
            value = min(self.end, self.offset + self.clock() - self.started)
            if value >= self.end:
                self.offset, self.mode = self.end, 'stopped'
            return value
        return self.offset
    def send(self, command):
        self.commands.append(command)
        fields = command.split()
        operation = fields[0]
        if operation == 'play':
            self.offset = int(fields[fields.index('from') + 1]) / 1000
            self.end = int(fields[fields.index('to') + 1]) / 1000 if 'to' in fields else self.duration
            self.started, self.mode = self.clock(), 'playing'
        elif operation == 'status':
            position = self.position()
            return self.mode if fields[-1] == 'mode' else str(round(position * 1000))
        elif operation == 'pause':
            self.offset, self.mode = self.position(), 'paused'
        elif operation == 'resume':
            self.started, self.mode = self.clock(), 'playing'
        elif operation in ('stop', 'close'):
            self.offset, self.mode = self.position(), 'stopped'
        return ''


@pytest.fixture(params=('mci', 'ffplay'))
def transport(tmp_path, monkeypatch, request):
    source = tmp_path / 'controlled PCM.wav'
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 16000)
    original = sha256(source.read_bytes()).hexdigest()
    clock, processes, signals = Clock(), [], []
    platform = SimpleNamespace(name='nt' if request.param == 'mci' else 'posix',
                               kill=lambda pid, signal: signals.append((pid, signal)))
    monkeypatch.setattr(module, 'os', platform)
    def popen(argv, **options):
        assert options['shell'] is False and options['start_new_session'] is True
        process = Process(argv)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    monkeypatch.setattr(module, 'stop_process', lambda process: setattr(process, 'returncode', 0))
    player = WavePlayer(clock=clock)
    player._executable = 'purpose-written-process-fixture'
    native = Native(clock, 2.0)
    monkeypatch.setattr(player, '_mci', native.send)
    def starts():
        return len([command for command in native.commands if command.startswith('play ')]) \
            if request.param == 'mci' else len(processes)
    def complete():
        clock.advance(3)
        if request.param == 'ffplay' and processes:
            processes[-1].returncode = 0
    value = SimpleNamespace(player=player, clock=clock, source=source, native=native,
                            processes=processes, starts=starts, complete=complete,
                            backend=request.param, signals=signals)
    yield value
    player.close()
    assert sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.parametrize(('a', 'b'), [
    (-.1, 1), (0, 2.1), (1, 1), (1.5, 1), (True, 1), (0, False),
    (float('nan'), 1), (0, float('inf')), ('0', 1), (None, 1),
])
def test_invalid_ab_points_preserve_opened_pcm_and_do_not_start_repeat(transport, a, b):
    player = transport.player
    player.play(transport.source)
    count = transport.starts()
    with pytest.raises(ValueError):
        player.set_ab_loop(a, b)
    assert player.duration_seconds == 2.0 and player.ab_loop is None
    assert not player.repeat_enabled and transport.starts() == count


def test_points_alone_do_not_enable_repeat_and_native_endpoint_bounds_playback(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.25, .75)
    assert not player.repeat_enabled and transport.starts() == 1
    player.enable_repeat()
    assert player.repeat_enabled and transport.starts() == 2
    if transport.backend == 'mci':
        assert transport.native.commands[-1].endswith('from 250 to 750')
    else:
        argv = transport.processes[-1].argv
        assert argv[argv.index('-ss') + 1] == '0.25'
        assert float(argv[argv.index('-t') + 1]) == .5
    transport.complete()
    count = transport.starts()
    assert player.poll(restart=False) == 'repeat_due'
    assert transport.starts() == count
    assert player.restart_repeat()
    assert player.repeat_count == 1 and transport.starts() == count + 1


@pytest.mark.parametrize('transport', ['ffplay'], indirect=True)
def test_ffplay_waits_for_actual_child_completion_not_estimated_position(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, .4)
    player.enable_repeat()
    transport.clock.advance(2)
    assert player.position_seconds == .4
    # Simulate slow device startup: elapsed wall time is not EOF evidence.
    assert player.poll(restart=False) is None
    assert player.repeat_count == 0
    transport.processes[-1].returncode = 0
    assert player.poll(restart=False) == 'repeat_due'


def test_paused_range_change_never_opens_or_briefly_plays_a_device(transport):
    player = transport.player
    player.play(transport.source)
    transport.clock.advance(.2)
    assert player.pause()
    starts = transport.starts()
    player.set_ab_loop(.3, .8)
    player.enable_repeat()
    assert player.state == 'paused' and transport.starts() == starts
    assert player.position_seconds == .3
    assert player.poll() is None and not player.restart_repeat()
    assert player.resume()
    assert player.state == 'playing' and transport.starts() == starts + 1
    assert player.position_seconds == .3


def test_sleep_counts_paused_monotonic_time_and_never_rearms_on_resume(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, 1)
    player.enable_repeat()
    player.set_sleep_timer(.5)
    transport.clock.advance(.2)
    player.pause()
    position, starts = player.position_seconds, transport.starts()
    transport.clock.advance(.31)
    assert player.poll() == 'sleep'
    assert player.state == 'paused'
    assert player.position_seconds == pytest.approx(position)
    assert player.ab_loop is None and not player.repeat_enabled
    assert player.sleep_remaining_seconds is None and transport.starts() == starts
    transport.clock.advance(20)
    assert player.poll() is None and transport.starts() == starts
    assert player.resume()
    assert player.sleep_remaining_seconds is None and not player.repeat_enabled
    if transport.backend == 'mci':
        assert ' to ' not in transport.native.commands[-1]
    else:
        assert '-t' not in transport.processes[-1].argv


def test_sleep_expiry_takes_priority_over_a_pending_repeat(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, .2)
    player.enable_repeat()
    player.set_sleep_timer(.1)
    transport.complete()
    starts = transport.starts()
    assert player.poll(restart=False) == 'sleep'
    assert not player.restart_repeat()
    assert player.repeat_count == 0 and transport.starts() == starts


@pytest.mark.parametrize('seconds', [False, True, 0, -1, '1', float('nan'), float('inf')])
def test_sleep_timer_requires_explicit_positive_finite_interval(transport, seconds):
    player = transport.player
    player.play(transport.source)
    with pytest.raises(ValueError):
        player.set_sleep_timer(seconds)
    assert player.sleep_remaining_seconds is None


@pytest.mark.parametrize('operation', ['stop', 'close', 'new_audio'])
def test_session_bound_controls_disarm_on_existing_lifecycle_commands(transport, operation):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, .8)
    player.enable_repeat()
    player.set_sleep_timer(5)
    previous = player.session_generation
    if operation == 'new_audio':
        player.play(transport.source)
    else:
        getattr(player, operation)()
    assert player.session_generation > previous
    assert player.ab_loop is None and not player.repeat_enabled
    assert player.sleep_remaining_seconds is None
    assert player.poll() is None


def test_failed_device_stop_still_disarms_automatic_actions(transport, monkeypatch):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, .8)
    player.enable_repeat()
    player.set_sleep_timer(5)
    original = player._halt
    monkeypatch.setattr(player, '_halt', lambda: (_ for _ in ()).throw(RuntimeError('device failure')))
    try:
        with pytest.raises(RuntimeError, match='device failure'):
            player.stop()
        assert player.ab_loop is None and not player.repeat_enabled
        assert player.sleep_remaining_seconds is None
    finally:
        monkeypatch.setattr(player, '_halt', original)


def test_repeat_seek_respects_range_and_keeps_sleep_deadline(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.2, 1.5)
    player.enable_repeat()
    player.set_sleep_timer(10)
    transport.clock.advance(1)
    with pytest.raises(ValueError, match='outside'):
        player.seek(.1)
    player.seek(.5)
    assert player.sleep_remaining_seconds == pytest.approx(9)
    assert player.repeat_enabled and player.ab_loop == (.2, 1.5)


class Control:
    focused = None
    def __init__(self, *args, label='', value='', **kwargs):
        self.label, self.value, self.enabled, self.handlers = label, value, True, {}
    def SetName(self, value): self.name = value
    def Bind(self, event, handler, *args): self.handlers[event] = handler
    def Add(self, *args, **kwargs): pass
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def Enable(self, value=True): self.enabled = value
    def SetFocus(self): Control.focused = self
    def Start(self, interval): self.interval, self.running = interval, True
    def Stop(self): self.running = False


@pytest.fixture
def controls(transport):
    player = transport.player
    player.play(transport.source)
    pending, reports = [], []
    state = SimpleNamespace(alive=True, cancelled=False, allowed=True, chooser=None)
    class Entry(Control):
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self):
            if callable(state.chooser):
                state.chooser()
            return 1
        def GetValue(self): return state.answer
    wx = SimpleNamespace(BoxSizer=Control, Button=Control, CheckBox=Control,
        TextCtrl=Control, Timer=Control, TextEntryDialog=Entry, ID_OK=1,
        HORIZONTAL=1, ALL=2, ALIGN_CENTER_VERTICAL=4, EXPAND=8, TE_READONLY=16,
        EVT_BUTTON='button', EVT_CHECKBOX='checkbox', EVT_TIMER='timer')
    def guard():
        if not state.allowed:
            raise ValueError('Source scope changed')
    def start(action, *, on_error):
        pending.append((action, on_error))
    view = ReaderPlaybackControls(Control(), Control(), player, start=start, guard=guard,
        cancelled=lambda: state.cancelled, alive=lambda: state.alive,
        report=lambda message, focus=False: reports.append((message, focus)), wx=wx)
    def finish():
        action, failed = pending.pop(0)
        try:
            action()
        except Exception as exc:
            failed(exc)
    value = SimpleNamespace(view=view, player=player, pending=pending, finish=finish,
                            state=state, reports=reports, transport=transport)
    yield value
    view.close()


def test_native_named_controls_start_only_after_quiet_admission_and_explicit_enable(controls):
    c = controls
    assert c.view.timer.interval == 100
    c.state.answer = '.1, .7'
    c.view.times.handlers['button'](None)
    assert c.player.ab_loop is None and len(c.pending) == 1
    c.finish()
    assert c.player.ab_loop == (.1, .7) and not c.player.repeat_enabled
    c.view.repeat.SetValue(True)
    c.view.repeat.handlers['checkbox'](None)
    assert not c.player.repeat_enabled
    c.finish()
    assert c.player.repeat_enabled
    assert c.view.repeat.name == 'Enable A/B repeat'


@pytest.mark.parametrize('boundary', ['stop', 'close', 'new_audio', 'scope', 'cancel'])
def test_queued_repeat_cannot_restart_after_lifecycle_boundary(controls, boundary):
    c = controls
    c.player.set_ab_loop(.1, .7)
    c.player.enable_repeat()
    c.transport.complete()
    c.view.poll()
    assert c.pending
    starts = c.transport.starts()
    if boundary == 'stop':
        c.player.stop()
    elif boundary == 'close':
        c.view.close()
    elif boundary == 'new_audio':
        c.player.play(c.transport.source)
        starts += 1
    elif boundary == 'scope':
        c.state.allowed = False
        c.view.poll()
    else:
        c.state.cancelled = True
        c.view.poll()
    c.finish()
    assert c.transport.starts() == starts
    assert not c.player.repeat_enabled and c.player.sleep_remaining_seconds is None


def test_expired_sleep_invalidates_an_already_admitted_repeat_callback(controls):
    c = controls
    c.player.set_ab_loop(.1, .7)
    c.player.enable_repeat()
    c.player.set_sleep_timer(10)
    c.transport.complete()
    c.view.poll()
    assert c.pending
    starts = c.transport.starts()
    c.transport.clock.advance(8)
    c.view.poll()
    c.finish()
    assert c.transport.starts() == starts
    assert not c.player.repeat_enabled
    assert any('Sleep timer expired' in text and not focus for text, focus in c.reports)


@pytest.mark.parametrize('boundary', ['scope', 'cancel'])
def test_modal_time_entry_rechecks_scope_and_cancel_before_applying(controls, boundary):
    c = controls
    c.state.answer = '.1, .8'
    c.state.chooser = lambda: setattr(c.state, 'allowed' if boundary == 'scope' else 'cancelled',
                                     False if boundary == 'scope' else True)
    c.view.set_times()
    assert c.player.ab_loop is None and c.pending == []


def test_failed_quiet_admission_disarms_without_retrying_automatically(controls):
    c = controls
    c.player.set_ab_loop(.1, .7)
    c.player.enable_repeat()
    c.transport.complete()
    c.view.poll()
    _, failed = c.pending.pop(0)
    failed(RuntimeError('Progress audio did not become quiet'))
    c.view.poll()
    assert c.pending == []
    assert not c.player.repeat_enabled
    assert c.reports and c.reports[-1][1] is False


def test_status_updates_do_not_take_focus_and_user_query_is_explicit(controls):
    c = controls
    sentinel = object()
    Control.focused = sentinel
    c.player.set_sleep_timer(10)
    c.view.poll()
    assert Control.focused is sentinel
    c.view.query.handlers['button'](None)
    assert Control.focused is c.view.status
    assert 'Playback playing' in c.view.status.GetValue()


@pytest.mark.parametrize('transport', ['mci'], indirect=True)
def test_mci_endpoint_is_quantized_once_without_float_rounding_drift(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.2, 1.0019)
    player.enable_repeat()
    assert transport.native.commands[-1].endswith('from 200 to 1001')
    assert player._end_seconds == 1.001


@pytest.mark.parametrize('transport', ['mci'], indirect=True)
def test_mci_rejects_interval_below_device_resolution_without_rearming(transport):
    player = transport.player
    player.play(transport.source)
    with pytest.raises(ValueError, match='resolution'):
        player.set_ab_loop(.1001, .1009)
    assert player.ab_loop is None and not player.repeat_enabled
    assert transport.starts() == 1


def test_scope_failure_stops_device_once_and_cannot_retry_automatic_restart(controls, monkeypatch):
    c = controls
    c.player.set_ab_loop(.1, .7)
    c.player.enable_repeat()
    c.player.set_sleep_timer(20)
    calls = []
    stop = c.player.stop
    def tracked():
        calls.append('stop')
        stop()
    monkeypatch.setattr(c.player, 'stop', tracked)
    c.state.allowed = False
    c.view.poll()
    c.view.poll()
    assert calls == ['stop']
    assert c.player.state == 'stopped' and c.player.ab_loop is None
    assert c.player.sleep_remaining_seconds is None and not c.pending
    assert len(c.reports) == 1 and c.reports[0][1] is False


def test_pause_at_completed_b_wins_over_already_queued_quiet_restart(controls):
    c = controls
    c.player.set_ab_loop(.1, .7)
    c.player.enable_repeat()
    c.player.set_sleep_timer(10)
    c.transport.complete()
    c.view.poll()
    assert c.pending and c.player.state == 'stopped'
    starts, remaining = c.transport.starts(), c.player.sleep_remaining_seconds
    assert c.player.pause()
    assert c.player.state == 'paused' and c.player.position_seconds == .1
    assert c.transport.starts() == starts
    assert c.player.repeat_enabled and c.player.sleep_remaining_seconds == remaining
    c.finish()
    assert c.player.state == 'paused' and c.transport.starts() == starts
    assert c.player.resume()
    assert c.transport.starts() == starts + 1 and c.player.repeat_enabled


def test_sleep_at_completed_b_preserves_explicit_resume_beyond_interval(transport):
    player = transport.player
    player.play(transport.source)
    player.set_ab_loop(.1, .7)
    player.enable_repeat()
    player.set_sleep_timer(1)
    transport.complete()
    starts = transport.starts()
    assert player.poll(restart=False) == 'sleep'
    assert player.state == 'paused' and player.position_seconds == .7
    assert not player.repeat_enabled and player.sleep_remaining_seconds is None
    assert transport.starts() == starts
    assert player.resume()
    assert player.state == 'playing' and player._end_seconds == player.duration_seconds
    assert transport.starts() == starts + 1


def test_sleep_after_actual_full_eof_does_not_claim_a_resumable_pause(transport):
    player = transport.player
    player.play(transport.source)
    player.set_sleep_timer(1)
    transport.complete()
    starts = transport.starts()
    assert player.poll(restart=False) == 'sleep'
    assert player.state == 'stopped' and not player.resume()
    assert transport.starts() == starts and player.sleep_remaining_seconds is None
