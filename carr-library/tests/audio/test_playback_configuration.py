"""Real local files and explicit device doubles for borrowing one player."""
from dataclasses import replace
import os
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.player import WavePlayer
from carr_library.audio.playback import PlaybackStatus


def wav(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
        stream.writeframes(b'\x10\x00' * 8000)
    return path


class Device:
    def __init__(self):
        self.snapshot = PlaybackStatus()
        self.plays = []
        self.stops = 0
        self.refuse_stop = False

    def status(self):
        return self.snapshot

    def play(self, path, *, start_seconds=0, end_seconds=None, device_id=None):
        info = wav_info(path)
        self.plays.append((path, start_seconds, end_seconds, device_id))
        self.snapshot = PlaybackStatus(state='playing', path=str(path),
            position_frames=round(start_seconds * info.rate), total_frames=info.frames,
            sample_rate=info.rate, channels=info.channels, device_id=device_id,
            position_accuracy='completed_buffers')

    def stop(self):
        self.stops += 1
        if self.refuse_stop:
            raise RuntimeError('Exact owned device has not stopped.')
        self.snapshot = replace(self.snapshot, state='stopped', position_frames=0)

    def pause(self):
        self.snapshot = replace(self.snapshot, state='paused')

    def resume(self):
        self.snapshot = replace(self.snapshot, state='playing')


def player(tmp_path):
    clock = [100.]
    value = WavePlayer(clock=lambda: clock[0])
    value.native = Device()
    a, b = wav(tmp_path / 'first.wav'), wav(tmp_path / 'Україна — second.wav')
    value.set_playlist([a, b])
    value.play_index(1, device_id=2)
    value.set_segments([(0, 'Start'), (.5, 'Second half')])
    value.set_ab_loop(.2, .8)
    value.set_sleep_timer(60)
    return value, clock, (a, b)


def test_borrowed_player_restores_queue_selection_loop_segments_and_exact_deadline_without_play(tmp_path):
    value, clock, paths = player(tmp_path)
    snapshot = value.capture_configuration()
    assert value.status().state == 'stopped'
    assert len(snapshot.queue) == 2 and all(len(row[3]) == 64 for row in snapshot.queue)
    value.play(wav(tmp_path / 'derivative.wav'), device_id=3)
    before = len(value.native.plays)
    clock[0] += 17
    value.restore_configuration(snapshot)
    assert len(value.native.plays) == before
    assert value.playlist == paths and value.playlist_index == 1 and value.path == paths[1]
    assert value.ab_loop == (.2, .8) and value.segments == ((0., 'Start'), (.5, 'Second half'))
    assert value.sleep_remaining_seconds == 43 and value._device_id == 2
    assert value.status().state == 'stopped'
    assert value.poll() is None


def test_return_does_not_extend_an_expired_original_sleep_deadline(tmp_path):
    value, clock, paths = player(tmp_path)
    snapshot = value.capture_configuration()
    clock[0] = 161
    value.restore_configuration(snapshot)
    assert value.sleep_remaining_seconds is None
    assert value.playlist == paths


@pytest.mark.parametrize('member', [0, 1], ids=['queued', 'selected'])
def test_changed_original_bytes_refuse_restore_even_when_size_and_mtime_are_restored(tmp_path, member):
    value, clock, paths = player(tmp_path)
    snapshot = value.capture_configuration()
    info = paths[member].stat()
    offset = wav_info(paths[member]).data_offset
    with paths[member].open('r+b') as stream:
        stream.seek(offset)
        stream.write(b'\x20\x00')
    os.utime(paths[member], ns=(info.st_atime_ns, info.st_mtime_ns))
    value.set_playlist([])
    before = len(value.native.plays)
    with pytest.raises(ValueError, match='changed'):
        value.restore_configuration(snapshot)
    assert value.playlist == () and len(value.native.plays) == before


def test_restore_requires_original_player_and_validated_navigation(tmp_path):
    value, clock, paths = player(tmp_path)
    snapshot = value.capture_configuration()
    with pytest.raises(ValueError, match='another player'):
        WavePlayer().restore_configuration(snapshot)
    for fields in ({'index': True}, {'loop': (0, 3)}, {'device': True},
                   {'segments': ((.5, 'One'), (.4, 'Backwards'))},
                   {'current': ()}, {'sleep_deadline': float('inf')}):
        with pytest.raises(ValueError):
            value.restore_configuration(replace(snapshot, **fields))


def test_unreleased_device_stops_snapshot_before_file_work_or_queue_change(tmp_path, monkeypatch):
    import carr_library.audio.player as module
    value, clock, paths = player(tmp_path)
    value.native.refuse_stop = True
    monkeypatch.setattr(module, '_snapshot_item', lambda *a, **k: pytest.fail('No snapshot before quiescence.'))
    with pytest.raises(RuntimeError, match='owned device'):
        value.capture_configuration()
    assert value.playlist == paths and value.path == paths[1]


def test_configuration_hashing_obeys_the_callers_actual_cancellation(tmp_path):
    value, clock, paths = player(tmp_path)
    called = []
    def cancelled():
        called.append(True)
        raise InterruptedError('Snapshot check cancelled.')
    with pytest.raises(InterruptedError, match='Snapshot check'):
        value.capture_configuration(cancelled=cancelled)
    assert called and value.playlist == paths
