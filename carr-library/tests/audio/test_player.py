"""Real RIFF/RF64 preflight; native device calls are explicit unit-test doubles."""
from types import SimpleNamespace
import math
import struct
import shutil
import time
import wave

import pytest

import carr_library.audio.player as player_module
from carr_library.audio.player import WavePlayer


def recording(path, *, rf64=False):
    data = b'\x00\x00' * 8000
    if rf64:
        path.write_bytes(b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE' +
            b'ds64' + struct.pack('<IQQQI', 28, len(data) + 72, len(data), 8000, 0) +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16) +
            b'data' + struct.pack('<I', 0xffffffff) + data)
    else:
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(data)
    return path


@pytest.mark.parametrize('rf64', [False, True])
def test_pcm_preflight_pause_resume_seek_stop_and_close(tmp_path, monkeypatch, rf64):
    path = recording(tmp_path / 'recording.wav', rf64=rf64)
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    state = {'mode': 'stopped', 'position': 0}
    commands = []
    def mci(self, command):
        commands.append(command)
        if command.startswith('status'):
            return state['mode'] if command.endswith('mode') else str(state['position'])
        if command.startswith('play'):
            state.update(mode='playing', position=int(command.rsplit(' ', 1)[1]))
        elif command.startswith('pause'):
            state['mode'] = 'paused'
        elif command.startswith('resume'):
            state['mode'] = 'playing'
        elif command.startswith('stop'):
            state.update(mode='stopped', position=0)
        return ''
    monkeypatch.setattr(WavePlayer, '_mci', mci)
    player = WavePlayer()
    player.play(path)
    assert player.duration_seconds == 1 and player.state == 'playing'
    assert player.pause() and player.state == 'paused'
    player.seek(.5)
    assert player.state == 'paused' and player.position_seconds == .5
    assert player.resume() and player.state == 'playing'
    player.stop()
    assert player.state == 'stopped' and player.position_seconds == 0
    player.close()
    assert not player._open
    assert any(command.startswith('close ') for command in commands)


@pytest.mark.parametrize('position', [-1, math.nan, math.inf, 2, True])
def test_invalid_seek_rejected_before_device_call(tmp_path, monkeypatch, position):
    path = recording(tmp_path / 'recording.wav')
    monkeypatch.setattr(WavePlayer, '_mci', lambda *args: pytest.fail('No native call before validation.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path, start_seconds=position)


def test_truncated_wav_fails_before_playback(tmp_path, monkeypatch):
    path = recording(tmp_path / 'recording.wav')
    path.write_bytes(path.read_bytes()[:-1])
    monkeypatch.setattr(WavePlayer, '_mci', lambda *args: pytest.fail('No device call for damaged PCM.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path)


def test_removed_windows_device_cannot_block_studio_cleanup(monkeypatch):
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    player._open = True
    def disconnected(*args):
        raise RuntimeError('Device disconnected')
    monkeypatch.setattr(player, '_mci', disconnected)
    player.close()
    assert not player._open and player.state == 'stopped'


@pytest.fixture
def native_player(monkeypatch):
    """Controlled MCI protocol double; no physical audio-device claim."""
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    state = {'mode': 'stopped', 'position': 0, 'clock': 0.0, 'commands': []}
    def mci(self, command):
        state['commands'].append(command)
        parts = command.split()
        if command.startswith('status'):
            return state['mode'] if command.endswith('mode') else str(state['position'])
        if command.startswith('play'):
            state.update(mode='playing', position=int(parts[parts.index('from')+1]))
        elif command.startswith('pause'):
            state['mode'] = 'paused'
        elif command.startswith('resume'):
            state['mode'] = 'playing'
        elif command.startswith('stop'):
            state.update(mode='stopped', position=0)
        return ''
    monkeypatch.setattr(WavePlayer, '_mci', mci)
    return WavePlayer(clock=lambda: state['clock']), state


def test_bounded_playlist_does_not_autoplay_and_advances_only_after_natural_end(tmp_path, native_player):
    player, state = native_player
    first, second = recording(tmp_path/'first.wav'), recording(tmp_path/'second.wav', rf64=True)
    player.set_playlist([first, second])
    assert player.state == 'stopped' and not any(c.startswith('open ') for c in state['commands'])
    player.play_index(0)
    state.update(mode='stopped', position=1000)
    assert player.poll() == 'next' and player.path == second and player.playlist_index == 1
    player.stop()
    assert player.poll() is None and player.state == 'stopped'
    assert player.previous_track() and player.path == first
    player.close()


def test_playlist_replacement_is_atomic_and_detects_changed_recording(tmp_path, native_player):
    player, state = native_player
    first = recording(tmp_path/'first.wav')
    player.set_playlist([first])
    for selection in ([first] * 257, [first, tmp_path/'missing.wav'], str(first)):
        with pytest.raises((ValueError, FileNotFoundError)):
            player.set_playlist(selection)
        assert player.playlist == (first,)
    with first.open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError):
        player.play_index(0)
    assert player.state == 'stopped'
    assert not any(c.startswith('open ') for c in state['commands'])


def test_ab_repeat_uses_native_endpoints_and_preserves_pause(tmp_path, native_player):
    player, state = native_player
    player.play(recording(tmp_path/'range.wav'))
    player.set_ab_loop(.2, .7)
    assert state['commands'][-1].endswith('from 200 to 700')
    assert player.pause()
    player.seek(.4)
    assert player.state == 'paused' and player.ab_loop == (.2, .7)
    player.resume()
    state.update(mode='stopped', position=700)
    assert player.poll() == 'repeat' and state['position'] == 200
    player.clear_ab_loop()
    assert player.ab_loop is None and state['commands'][-1].endswith('from 200')
    player.stop()
    assert player.poll() is None


@pytest.mark.parametrize('bounds', [(True, .8), (-1,.5), (.5,.5), (.9,1.1), (math.nan,.9)])
def test_invalid_repeat_cannot_change_current_range(tmp_path, native_player, bounds):
    player, state = native_player
    player.play(recording(tmp_path/'range.wav'))
    player.set_ab_loop(.2, .7)
    before = len(state['commands'])
    with pytest.raises(ValueError):
        player.set_ab_loop(*bounds)
    assert player.ab_loop == (.2,.7) and len(state['commands']) == before


def test_sleep_timer_has_priority_over_repeat_and_queue_and_runs_while_paused(tmp_path, native_player):
    player, state = native_player
    first, second = recording(tmp_path/'first.wav'), recording(tmp_path/'second.wav')
    player.set_playlist([first,second]); player.play_index(0)
    player.set_ab_loop(.2,.7); player.set_sleep_timer(5)
    player.pause()
    state['clock'] = 5
    assert player.poll() == 'sleep' and player.state == 'stopped'
    assert player.playlist_index == 0 and player.sleep_remaining_seconds is None
    assert player.poll() is None
    player.play_index(1)
    player.set_sleep_timer(10)
    player.close()
    assert player.sleep_remaining_seconds is None and player.poll() is None


def test_segments_preserve_pause_and_invalid_replacement_keeps_current_map(tmp_path, native_player):
    player, state = native_player
    player.play(recording(tmp_path/'chapters.wav'))
    player.set_segments([(0,'Opening'),(.4,'Middle'),(.8,'Ending')])
    player.pause()
    assert player.next_segment() == 'Middle' and player.state == 'paused'
    assert player.next_segment() == 'Ending'
    assert player.previous_segment() == 'Middle'
    before = player.segments
    for value in ([(.2,'A'),(.2,'Duplicate')],[(1,'Outside')],[(0,'')],[(True,'Bool')]):
        with pytest.raises(ValueError):
            player.set_segments(value)
        assert player.segments == before


def test_actual_ffplay_headless_decode_and_queue_completion(tmp_path, monkeypatch):
    if player_module.os.name == 'nt' or not shutil.which('ffplay'):
        pytest.skip('Actual ffplay dummy-device probe requires an installed POSIX ffplay.')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    paths = []
    for name in ('first.wav','second.wav'):
        path = tmp_path/name
        with wave.open(str(path), 'wb') as output:
            output.setparams((1,2,8000,0,'NONE','not compressed'))
            output.writeframes(b'\x10\x00' * 1600)
        paths.append(path)
    player = WavePlayer()
    try:
        player.set_playlist(paths); player.play_index(0)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            player.poll()
            assert player.state != 'error', 'The installed ffplay could not decode through SDL dummy audio.'
            if player.playlist_index == 1 and player.state == 'stopped':
                break
            time.sleep(.02)
        else:
            pytest.fail('Headless actual playback did not complete the two-item queue within its budget.')
    finally:
        player.close()
