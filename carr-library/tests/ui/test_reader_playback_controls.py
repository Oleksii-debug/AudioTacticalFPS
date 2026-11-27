"""Real reader workers and PCM with explicit controllable native-output boundaries."""
from types import SimpleNamespace

import pytest

from carr_library.audio.capture_types import AudioBudget
from carr_library.audio.playback_processing import PlaybackControls
from carr_library.audio.runtime import resolve_ffmpeg
from tests.ui.test_live_reading import window as live_window, drain as drain_live
from tests.ui.test_media_overlay import window as overlay_window, drain as drain_overlay


def test_live_play_and_seek_use_controlled_pcm_and_original_coordinates(live_window):
    view, registry, broker, queue, messages, selected, disposed = live_window
    session = view.session
    session.ffmpeg = resolve_ffmpeg()
    assert session.ffmpeg is not None, 'Actual selected FFmpeg is required; no skip.'
    session.playback_controls = PlaybackControls(speed=2, pitch_semitones=4, volume=.6, pan=.2)
    session.playback_budget = AudioBudget(threads=1, max_output_bytes=1024**2, reserve_disk_bytes=0)
    original = session.render_next()
    controlled = session._prepared_playback
    view.play()
    drain_live(view, queue)
    assert view.player.calls == [controlled.audio] and controlled.audio != original.path
    assert view.playing and session._playback_reader is not None and controlled._reader is not None
    seeks = []
    view.player.seek = seeks.append
    view.seek_seconds = SimpleNamespace(GetValue=lambda: '.06')
    view.seek()
    assert seeks == [.03] and 'estimated_after_time_stretch' in messages[-1]
    view.player.refuse_stop = True
    view.close()
    assert not view.closed and controlled.audio.exists() and controlled._reader is not None
    view.player.refuse_stop = False
    view.close()
    drain_live(view, queue, predicate=lambda item: item.closed)
    assert not controlled.audio.exists() and not original.path.exists()
    assert session.cursor == 0 and broker.snapshot()['leases'] == []


def test_recorded_controls_stay_pinned_through_deferred_native_action_and_close(overlay_window):
    view, queue, messages, disposed, root = overlay_window
    callbacks = []
    def defer(begin, **kwargs):
        callbacks.append(begin)
        return SimpleNamespace(close=lambda: callbacks.clear())
    view.progress = SimpleNamespace(media_action=defer)
    view.playback_speed.value = '2'
    view.playback_pitch.value = '6'
    view.playback_volume.value = '.4'
    view.playback_pan.value = '-.5'
    original = view.session.document.audio
    view.play()
    assert not view.playback_speed.enabled
    drain_overlay(view, queue)
    controlled = view.session._prepared_playback
    assert controlled is not None and not view.player.calls and len(callbacks) == 1
    assert view.session._reader is not None and controlled._reader is not None
    assert controlled.audio_duration == .5 and original.exists()
    callbacks[0]()
    assert view.player.calls[0][0] == controlled.audio and not view.playback_pitch.enabled
    view.player.frames = 3000
    view.where()
    assert '0.750' in messages[-1] and 'estimated_after_time_stretch' in messages[-1]
    view.player.stop_failure = True
    view.close()
    assert not view.closed and controlled._reader is not None
    view.player.stop_failure = False
    view.close()
    drain_overlay(view, queue, lambda item: item.closed)
    assert not controlled.audio.exists() and not original.exists()


def test_invalid_reader_control_never_starts_processing_or_output(overlay_window):
    view, queue, messages, disposed, root = overlay_window
    view.playback_speed.value = 'nan'
    view.play()
    assert view.future is None and not view.player.calls
    assert view.session._reader is None and view.session._prepared_playback is None
    assert messages and not disposed


@pytest.mark.parametrize('reason', ['revoked-source', 'invalid-derivative'])
def test_paused_live_resume_rechecks_source_and_controlled_pcm_without_losing_stop_owner(live_window, reason):
    view, registry, broker, queue, messages, selected, disposed = live_window
    session = view.session
    session.ffmpeg = resolve_ffmpeg()
    assert session.ffmpeg is not None, 'Actual selected FFmpeg is required; no skip.'
    session.playback_controls = PlaybackControls(speed=2)
    session.playback_budget = AudioBudget(threads=1, max_output_bytes=1024**2, reserve_disk_bytes=0)
    original = session.render_next()
    view.play()
    drain_live(view, queue)
    controlled = session._prepared_playback
    resumed = []
    view.player.state = 'paused'
    view.player.resume = lambda: resumed.append(True)
    if reason == 'revoked-source':
        def denied():
            raise PermissionError('Source authorization revoked while paused.')
        view.guard = denied
    else:
        controlled.invalid = True
    view.pause()
    assert resumed == [] and messages
    assert session._playback_reader is not None and controlled._reader is not None
    assert controlled.audio.exists() and original.path.exists() and session.cursor == 0
    view.close()
    drain_live(view, queue, predicate=lambda item: item.closed)
    assert not controlled.audio.exists() and not original.path.exists()
