"""Actual bounded PCM worker with a synthetic driver, not device acceptance."""
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path
from runpy import run_path

import pytest

from carr_library.audio.player import WavePlayer
from carr_library.audio.playback import PlaybackStatus
from carr_library.audio.playback_resume import PlaybackOwner, prepare_audio
from carr_library.audio.transport import PlaybackTransport
_helpers = run_path(str(Path(__file__).with_name('test_playback.py')))
source, player_fixture, wait = (_helpers[name] for name in ('source', 'player_fixture', 'wait'))


@pytest.fixture
def transport(tmp_path):
    now = [10.0]
    player = WavePlayer(clock=lambda: now[0])
    player.native, devices = player_fixture(buffer_ms=20, buffer_count=3)
    muted = {}
    progress = SimpleNamespace(prepare_media=lambda **kw: muted.update({kw['owner']: True}) or True,
                               set_media_state=lambda **kw: muted.pop(kw['owner'], None))
    current = [True]
    def guard():
        if not current[0]:
            raise ValueError('Old scope')
    owner = PlaybackOwner('project', project='a' * 32, revision='b' * 64)
    prepared = prepare_audio(source(tmp_path / 'audio.wav'), owner=owner)
    value = PlaybackTransport(tmp_path, player=player, guard=guard, progress=progress, clock=lambda: now[0])
    value.load(prepared)
    yield value, devices, muted, now, current
    player.close()


def test_pause_seek_stop_preserves_measured_resume_and_never_resets_it(transport):
    control, devices, muted, _, _ = transport
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing')
    assert muted
    control.seek(.4)
    wait(control.player.native, lambda s: s.state == 'playing' and s.position_frames >= 3200)
    control.pause_resume()
    wait(control.player.native, lambda s: s.state == 'paused')
    control.poll()
    assert control.store.position(control.prepared.owner, control.prepared.audio) == 3200
    control.stop()
    control.stop()
    assert not muted
    assert control.store.position(control.prepared.owner, control.prepared.audio) == 3200
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing' and s.position_frames >= 3200)
    assert all(device.maximum_queued <= 3 * 20 * 8000 * 4 // 1000 for device in devices)


def test_paused_poll_does_not_rewrite_resume_on_every_timer_tick(transport, monkeypatch):
    control, _, _, _, _ = transport
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing')
    control.pause_resume()
    wait(control.player.native, lambda s: s.state == 'paused')
    saves = Mock(wraps=control.store.remember)
    monkeypatch.setattr(control.store, 'remember', saves)
    for _ in range(20):
        control.poll()
    assert saves.call_count == 1


def test_ab_and_sleep_use_one_native_player_and_release_media_after_stop(transport):
    control, _, muted, now, _ = transport
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing')
    control.loop(.1, .4)
    assert control.player.ab_loop == (.1, .4)
    control.sleep(1)
    now[0] += 2
    assert control.poll().startswith('Sleep timer stopped')
    assert control.player.state == 'stopped' and not muted


def test_scope_drift_stops_and_withdraws_recording(transport):
    control, _, muted, _, current = transport
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing')
    current[0] = False
    with pytest.raises(ValueError, match='Old scope'):
        control.poll()
    assert control.prepared is None and control.player.state == 'stopped' and not muted


def test_failed_stop_never_releases_its_progress_suppression(transport, monkeypatch):
    control, _, muted, _, _ = transport
    control.play()
    wait(control.player.native, lambda s: s.state == 'playing')
    original = control.player.native.stop
    monkeypatch.setattr(control.player.native, 'stop', Mock(side_effect=RuntimeError('still active')))
    with pytest.raises(RuntimeError):
        control.stop()
    assert muted
    monkeypatch.setattr(control.player.native, 'stop', original)
    control.close()
    assert not muted


def test_external_playhead_is_never_saved_as_measured_position(transport):
    control, _, _, _, _ = transport
    info = control.prepared.audio
    control.checkpoint(PlaybackStatus(state='playing', path=str(control.prepared.path),
        position_frames=2000, total_frames=info.frames, sample_rate=info.rate,
        channels=info.channels, position_accuracy='external_player'))
    assert control.store.position(control.prepared.owner, info) is None


def test_media_must_acknowledge_quiet_before_actual_player_start(transport, monkeypatch):
    control, _, muted, _, _ = transport
    monkeypatch.setattr(control.progress, 'prepare_media', lambda **kw: False)
    with pytest.raises(ValueError, match='has not started'):
        control.play()
    assert control.player.native.status().state == 'stopped' and not muted


def test_restarting_prepared_audio_rechecks_current_source_authority(transport):
    control, _, muted, _, _ = transport
    def revoked():
        raise ValueError('The selected source was revoked')
    control.load(control.prepared, authorize=revoked)
    with pytest.raises(ValueError, match='revoked'):
        control.play()
    assert control.player.native.status().state == 'stopped' and not muted
