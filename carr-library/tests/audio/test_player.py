"""Real RIFF/RF64 preflight; native device calls are explicit unit-test doubles."""
from dataclasses import replace
from types import SimpleNamespace
import math
import struct
import wave

import pytest

import carr_library.audio.player as player_module
import carr_library.audio.playback as native_module
from carr_library.audio.playback import PCMPlayer, PlaybackStatus
from carr_library.audio.ffmpeg import wav_info
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
    class NativeDouble(PCMPlayer):
        # Facade routing only; the real worker and native ABI have separate tests.
        def play(self, selected, *, start_seconds=0, end_seconds=None, device_id=None):
            info = wav_info(selected)
            self._update(state='playing', path=str(selected), total_frames=info.frames,
                         position_frames=self._frame(start_seconds, info.rate, info.frames),
                         sample_rate=info.rate, channels=info.channels, device_id=device_id)
        def pause(self):
            self._update(state='paused')
        def resume(self):
            self._update(state='playing')
        def seek(self, seconds):
            self._update(position_frames=self._frame(seconds, 8000, 8000))
    monkeypatch.setattr(native_module, 'PCMPlayer', NativeDouble)
    player = WavePlayer()
    player.play(path, device_id=2)
    assert player.path == path and player.status().device_id == 2
    assert player.backend == 'Windows WinMM waveOut' and not player.position_is_approximate
    assert player.duration_seconds == 1 and player.state == 'playing'
    assert player.pause() and player.state == 'paused'
    player.seek(.5)
    assert player.state == 'paused' and player.position_seconds == .5
    assert player.resume() and player.state == 'playing'
    player.stop()
    assert player.state == 'stopped' and player.position_seconds == 0
    player.close()
    assert player.state == 'closed'


@pytest.mark.parametrize('position', [-1, math.nan, math.inf, 2, True])
def test_invalid_seek_rejected_before_device_call(tmp_path, monkeypatch, position):
    path = recording(tmp_path / 'recording.wav')
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(PCMPlayer, 'play', lambda *args, **kwargs: pytest.fail('No native call before validation.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path, start_seconds=position)


def test_truncated_wav_fails_before_playback(tmp_path, monkeypatch):
    path = recording(tmp_path / 'recording.wav')
    path.write_bytes(path.read_bytes()[:-1])
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(PCMPlayer, 'play', lambda *args, **kwargs: pytest.fail('No device call for damaged PCM.'))
    with pytest.raises(ValueError):
        WavePlayer().play(path)


def test_native_cleanup_failure_remains_visible_without_losing_handle_owner(monkeypatch):
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    owner = player.native
    def disconnected(*args):
        raise RuntimeError('Device disconnected')
    monkeypatch.setattr(owner, 'close', disconnected)
    with pytest.raises(RuntimeError, match='Device disconnected'):
        player.close()
    assert player.native is owner


def test_invalid_new_source_keeps_existing_controller_state(tmp_path, monkeypatch):
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    player = WavePlayer()
    player.native._update(state='playing', position_frames=4000, total_frames=8000, sample_rate=8000)
    damaged = recording(tmp_path / 'damaged.wav')
    damaged.write_bytes(damaged.read_bytes()[:-1])
    with pytest.raises(ValueError):
        player.play(damaged)
    assert player.state == 'playing' and player.position_seconds == .5


@pytest.fixture
def native_player(monkeypatch):
    """Controlled facade boundary; actual worker/endpoints are tested separately."""
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    state = {'mode': 'stopped', 'position': 0, 'clock': 0.0, 'plays': []}
    class NativeDouble(PCMPlayer):
        def play(self, selected, *, start_seconds=0, end_seconds=None, device_id=None):
            info = wav_info(selected)
            state['plays'].append({'path': selected, 'start': start_seconds, 'end': end_seconds, 'device_id': device_id})
            state.update(mode='playing', position=round(start_seconds * 1000))
            self._update(state='playing', path=str(selected), total_frames=info.frames,
                         position_frames=round(start_seconds * info.rate), sample_rate=info.rate,
                         channels=info.channels, device_id=device_id)
        def status(self):
            value = super().status()
            return replace(value, state=state['mode'], position_frames=round(state['position'] * value.sample_rate / 1000))
        def pause(self):
            state['mode'] = 'paused'
        def resume(self):
            state['mode'] = 'playing'
        def stop(self, **kwargs):
            state.update(mode='stopped', position=0)
            super().stop(**kwargs)
        def close(self):
            super().close()
            state['mode'] = 'closed'
    monkeypatch.setattr(native_module, 'PCMPlayer', NativeDouble)
    return WavePlayer(clock=lambda: state['clock']), state


def test_bounded_playlist_does_not_autoplay_and_advances_only_after_natural_end(tmp_path, native_player):
    player, state = native_player
    first, second = recording(tmp_path/'first.wav'), recording(tmp_path/'second.wav', rf64=True)
    player.set_playlist([first, second])
    assert player.state == 'stopped' and not state['plays']
    player.play_index(0)
    state.update(mode='completed', position=1000)
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
    assert not state['plays']


def test_ab_repeat_uses_native_endpoints_and_preserves_pause(tmp_path, native_player):
    player, state = native_player
    player.play(recording(tmp_path/'range.wav'))
    player.set_ab_loop(.2, .7)
    assert state['plays'][-1]['start'] == .2 and state['plays'][-1]['end'] == .7
    assert player.pause()
    player.seek(.4)
    assert player.state == 'paused' and player.ab_loop == (.2, .7)
    player.resume()
    state.update(mode='completed', position=700)
    assert player.poll() == 'repeat' and state['position'] == 200
    player.clear_ab_loop()
    assert player.ab_loop is None and state['plays'][-1]['start'] == .2 and state['plays'][-1]['end'] is None
    player.stop()
    assert player.poll() is None


@pytest.mark.parametrize('bounds', [(True, .8), (-1,.5), (.5,.5), (.9,1.1), (math.nan,.9)])
def test_invalid_repeat_cannot_change_current_range(tmp_path, native_player, bounds):
    player, state = native_player
    player.play(recording(tmp_path/'range.wav'))
    player.set_ab_loop(.2, .7)
    before = len(state['plays'])
    with pytest.raises(ValueError):
        player.set_ab_loop(*bounds)
    assert player.ab_loop == (.2,.7) and len(state['plays']) == before


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


def test_output_selection_survives_playlist_repeat_and_segment_navigation(tmp_path, native_player):
    player, state = native_player
    first, second = recording(tmp_path/'selected.wav'), recording(tmp_path/'next.wav')
    player.play(first, device_id=7)
    player.enqueue([second])
    player.set_ab_loop(.2, .7)
    player.set_segments([(0, 'Opening'), (.4, 'Middle')])
    player.seek_segment(1)
    assert all(row['device_id'] == 7 for row in state['plays'])
    player.play_index(1, device_id=3)
    assert state['plays'][-1]['device_id'] == 3
    assert player.ab_loop is None and player.segments == ()
    player.previous_track()
    assert state['plays'][-1]['device_id'] == 3


def test_empty_playlist_replacement_releases_device_without_closing_controller(tmp_path, native_player):
    player, state = native_player
    path = recording(tmp_path/'reopen.wav')
    player.play(path)
    player.set_playlist([])
    assert player.path is None and player.status().path is None and player.duration_seconds == 0
    player.play(path)
    assert player.state == 'playing'
    player.close()


@pytest.mark.parametrize('position', [0, .199, .7, 1])
def test_seek_cannot_play_outside_an_active_repeat(tmp_path, native_player, position):
    player, state = native_player
    player.play(recording(tmp_path / 'range.wav'), device_id=7)
    player.set_ab_loop(.2, .7)
    player.pause()
    before = list(state['plays'])
    with pytest.raises(ValueError, match='active A–B range'):
        player.seek(position)
    assert state['plays'] == before and player.state == 'paused'
    assert player.ab_loop == (.2, .7) and player.status().device_id == 7


def test_pause_between_native_range_end_and_poll_holds_the_next_repeat(tmp_path, native_player):
    player, state = native_player
    player.play(recording(tmp_path / 'range.wav'), device_id=7)
    player.set_ab_loop(.2, .7)
    state.update(mode='completed', position=700)
    assert player.pause() and player.state == 'paused'
    assert state['plays'][-1] == {'path': player.path, 'start': .2, 'end': .7, 'device_id': 7}
    before = len(state['plays'])
    assert player.poll() is None and len(state['plays']) == before


@pytest.mark.parametrize('end', [.7, 1])
def test_clear_repeat_at_native_end_preserves_remaining_recording_before_queue(tmp_path, native_player, end):
    player, state = native_player
    first, second = recording(tmp_path / 'first.wav'), recording(tmp_path / 'second.wav')
    player.set_playlist([first, second])
    player.play_index(0, device_id=7)
    player.set_ab_loop(.2, end)
    state.update(mode='completed', position=round(end * 1000))
    player.clear_ab_loop()
    assert player.ab_loop is None
    if end < 1:
        assert player.poll() is None and player.playlist_index == 0
        assert state['plays'][-1] == {'path': first, 'start': end, 'end': None, 'device_id': 7}
        state.update(mode='completed', position=1000)
    assert player.poll() == 'next' and player.path == second and player.playlist_index == 1
