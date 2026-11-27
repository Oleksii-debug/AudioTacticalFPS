"""Repeat, sleep and skip contracts on actual PCM with a controlled MCI device.

The command adapter below exercises production WavePlayer state transitions.
Actual Windows playback is separately required by windows_reader_player_smoke.
"""
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import player as implementation


@pytest.fixture
def pcm(tmp_path):
    path = tmp_path / 'Повтор — source.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x01\x00' * 24000)
    return path


class Mci:
    def __init__(self):
        self.commands, self.mode, self.position = [], 'stopped', 0

    def __call__(self, command):
        self.commands.append(command)
        words = command.split()
        if words[0] == 'status':
            return self.mode if words[-1] == 'mode' else str(self.position)
        if words[0] == 'play':
            self.position = int(words[words.index('from') + 1])
            self.mode = 'playing'
        elif words[0] == 'resume':
            self.mode = 'playing'
        elif words[0] == 'pause':
            self.mode = 'paused'
        elif words[0] == 'seek':
            self.position = int(words[words.index('to') + 1])
            self.mode = 'stopped'
        elif words[0] in ('stop', 'close'):
            self.mode = 'stopped'
        return ''

    @property
    def plays(self):
        return [command for command in self.commands if command.startswith(('play ', 'resume '))]

    def end(self, seconds):
        self.position, self.mode = round(seconds * 1000), 'stopped'


@pytest.fixture
def transport(pcm, monkeypatch):
    player = implementation.WavePlayer()
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    device = Mci()
    player._mci = device
    player.play(pcm)
    yield player, device
    player.close()


def test_ab_native_endpoint_is_explicit_and_poll_never_starts_audio(transport, pcm):
    player, device = transport
    original = pcm.read_bytes()
    player.set_ab_loop(.25, .75)
    assert device.plays[-1].endswith('from 250 to 750')
    before = list(device.plays)
    device.end(.75)
    assert player.poll() == 'repeat-pending'
    assert player.poll() == 'repeat-pending' and device.plays == before
    assert player.repeat_if_ready() is True
    assert device.plays[-1].endswith('from 250 to 750')
    assert len(device.plays) == len(before) + 1
    assert player.ab_loop == (.25, .75)
    assert pcm.read_bytes() == original


@pytest.mark.parametrize('action', ['stop', 'pause', 'clear', 'close'])
def test_user_action_at_b_suppresses_a_pending_repeat(transport, action):
    player, device = transport
    player.set_ab_loop(.25, .75)
    device.end(.75)
    assert player.repeat_pending
    before = len(device.plays)
    {'stop': player.stop, 'pause': player.pause,
     'clear': player.clear_ab_loop, 'close': player.close}[action]()
    assert player.poll() is None and player.repeat_if_ready() is False
    assert len(device.plays) == before
    if action == 'pause':
        assert player.state == 'paused' and player.position_seconds == .25
        assert player.resume() is True
        assert device.plays[-1].endswith('from 250 to 750')


def test_stopped_configuration_and_skips_do_not_autoplay(transport):
    player, device = transport
    player.stop()
    before = list(device.plays)
    player.set_ab_loop(.2, 1.2)
    player.set_skip_intervals(.1, .4)
    assert player.skip_intervals == (.1, .4)
    assert player.skip_forward() == pytest.approx(.6)
    assert player.skip_backward() == pytest.approx(.5)
    assert player.state == 'stopped' and device.plays == before
    player.seek(.7, start_if_stopped=False)
    assert player.state == 'stopped' and device.plays == before
    player.clear_ab_loop()
    assert player.ab_loop is None and device.plays == before
    assert player.play_current() is True
    assert device.plays[-1].endswith('from 700')
    assert player.play_current() is False


def test_paused_skip_and_ab_changes_remain_silent_until_resume(transport):
    player, device = transport
    device.position = 500
    assert player.pause()
    before = list(device.plays)
    player.set_ab_loop(.3, 1.0)
    player.set_skip_intervals(.1, .2)
    player.skip_forward()
    assert player.state == 'paused' and player.position_seconds == .7
    assert device.plays == before
    player.resume()
    assert device.plays[-1].endswith('from 700 to 1000')


@pytest.mark.parametrize('first,last', [
    (-.1, .3), (.3, .3), (.4, .3), (0, 3.1), (0, .049),
    (True, 2), (0, float('nan')), (0, float('inf')), (0, 10 ** 400),
])
def test_invalid_loop_preserves_current_playback_and_settings(transport, first, last):
    player, device = transport
    player.set_ab_loop(.2, .8)
    before = list(device.commands)
    with pytest.raises(ValueError):
        player.set_ab_loop(first, last)
    assert device.commands == before and player.ab_loop == (.2, .8)


def test_decimal_minimum_loop_is_accepted_without_float_subtraction_loss(transport):
    player, _ = transport
    assert player.set_ab_loop(.2, .25) == (.2, .25)


@pytest.mark.parametrize('backward,forward', [
    (0, 1), (.09, 1), (1, 601), (True, 1), (1, float('nan')), (10 ** 400, 1),
])
def test_invalid_skip_settings_do_not_change_previous_values(transport, backward, forward):
    player, device = transport
    before = list(device.commands)
    with pytest.raises(ValueError):
        player.set_skip_intervals(backward, forward)
    assert player.skip_intervals == (10, 30) and device.commands == before


@pytest.mark.parametrize('state', ['playing', 'paused', 'at-b'])
def test_sleep_expires_on_monotonic_time_and_has_priority_over_repeat(transport, monkeypatch, state):
    player, device = transport
    now = [100.0]
    monkeypatch.setattr(implementation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    player.set_ab_loop(.2, .8)
    player.set_sleep_timer(2)
    if state == 'paused':
        player.pause()
    elif state == 'at-b':
        device.end(.8)
    before = list(device.plays)
    now[0] = 101
    assert player.sleep_remaining_seconds == 1
    now[0] = 102
    assert player.poll() == 'sleep'
    assert player.state == 'stopped' and player.sleep_remaining_seconds is None
    assert not player.repeat_if_ready() and device.plays == before
    assert player.poll() is None


def test_expired_sleep_cannot_resume_before_next_ui_tick(transport, monkeypatch):
    player, device = transport
    now = [50.0]
    monkeypatch.setattr(implementation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    player.pause()
    player.set_sleep_timer(.5)
    before = list(device.plays)
    now[0] = 51
    assert player.resume() is False
    assert player.state == 'stopped' and device.plays == before


@pytest.mark.parametrize('action', ['repeat', 'resume', 'play'])
@pytest.mark.parametrize('mutation', ['changed-bytes', 'same-bytes-replacement'])
def test_changed_recording_is_not_reopened_by_session_controls(transport, pcm, action, mutation):
    player, device = transport
    player.set_ab_loop(.2, .8)
    if action == 'repeat':
        device.end(.8)
    elif action == 'resume':
        player.pause()
    else:
        player.stop()
    before = list(device.plays)
    if mutation == 'changed-bytes':
        with pcm.open('ab') as stream:
            stream.write(b'changed local recording')
    else:
        replacement = pcm.with_name('replacement.wav')
        replacement.write_bytes(pcm.read_bytes())
        replacement.replace(pcm)
    changed = pcm.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        {'repeat': player.repeat_if_ready, 'resume': player.resume,
         'play': player.play_current}[action]()
    assert device.plays == before and pcm.read_bytes() == changed
    assert not player.repeat_pending


def test_new_explicit_recording_resets_repeat_and_sleep_but_retains_skip_preferences(transport, pcm, tmp_path):
    player, _ = transport
    player.set_ab_loop(.2, .8)
    player.set_sleep_timer(60)
    player.set_skip_intervals(2, 4)
    other = tmp_path / 'explicit next.wav'
    other.write_bytes(pcm.read_bytes())
    player.play(other)
    assert player.path == other and player.ab_loop is None
    assert player.sleep_remaining_seconds is None and player.skip_intervals == (2, 4)


def test_posix_paused_seek_does_not_spawn_or_signal_a_new_player_before_resume(pcm, monkeypatch):
    player = implementation.WavePlayer()
    signals, launches, stopped = [], [], []
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(
        name='posix', kill=lambda *args: signals.append(args)))
    player._executable = '/selected/ffplay'
    def launch(args, **options):
        process = SimpleNamespace(pid=100 + len(launches), returncode=None, poll=lambda: None)
        launches.append((args, options, process))
        return process
    monkeypatch.setattr(implementation.subprocess, 'Popen', launch)
    monkeypatch.setattr(implementation, 'stop_process', lambda process: stopped.append(process))
    player.play(pcm)
    first = launches[0][2]
    player.pause()
    player.seek(.5)
    assert len(launches) == 1 and stopped == [first]
    assert len(signals) == 1 and player.state == 'paused'
    player.resume()
    assert len(launches) == 2
    assert launches[-1][0][launches[-1][0].index('-ss') + 1] == '0.5'
    assert len(signals) == 1
    player.close()


def test_ffplay_repeat_range_is_bounded_and_progress_estimate_stays_before_b(pcm, monkeypatch):
    player = implementation.WavePlayer()
    player.position_is_approximate = True
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='posix'))
    now = [10.0]
    monkeypatch.setattr(implementation, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    player._executable = '/selected/ffplay'
    launched, closed = [], []
    child = SimpleNamespace(returncode=None)
    child.poll = lambda: child.returncode
    monkeypatch.setattr(implementation.subprocess, 'Popen',
        lambda args, **kwargs: launched.append((args, kwargs)) or child)
    monkeypatch.setattr(implementation, 'stop_process', lambda process: closed.append(process))
    player.play(pcm, start_seconds=.2, end_seconds=.8)
    args, options = launched[-1]
    assert float(args[args.index('-t') + 1]) == pytest.approx(.6)
    assert options['shell'] is False and options['start_new_session'] is True
    now[0] = 20
    assert player.position_seconds == .8 and player.position_is_approximate
    child.returncode = 1
    assert player.state == 'error' and player.poll() is None
    player.close()


def test_native_stop_before_b_is_not_mistaken_for_completed_repeat(transport):
    player, device = transport
    player.set_ab_loop(.2, .8)
    before = list(device.plays)
    device.end(.5)
    assert player.poll() is None and player.repeat_if_ready() is False
    assert device.plays == before and player.state == 'stopped'
