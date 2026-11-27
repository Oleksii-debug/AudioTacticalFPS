"""Actual reader transforms retain original interval coordinates and cleanup owners."""
from pathlib import Path
from threading import Event

import pytest

from carr_library.audio.jobs import sha256_file
from carr_library.audio.playback_processing import PlaybackControls
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations import ResourceBroker
from carr_library.reader.media_overlay import OverlaySession, ReadingBudget
from carr_library.reader.publication_state import PublicationPositions
from tests.ui.test_media_overlay import Player, make_document


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    document = make_document(tmp_path)
    player = Player()
    session = OverlaySession(document, player)
    binary = resolve_ffmpeg()
    assert binary is not None, 'Actual selected FFmpeg is required; no skip.'
    broker = ResourceBroker.for_app(root)
    def prepare(controls=PlaybackControls(speed=2, pitch_semitones=3, volume=.5, pan=.25), **kwargs):
        return session.prepare_controls(controls, library_root=root, ffmpeg=binary,
            broker=broker, budget=kwargs.pop('budget',
                ReadingBudget(max_bytes=1024**2, reserve_bytes=0)), **kwargs)
    yield session, player, root, broker, prepare
    session.close()


def test_recorded_reader_maps_observed_frames_to_original_intervals_and_saves_original_time(prepared):
    session, player, root, broker, prepare = prepared
    original = session.document.audio.read_bytes()
    session.seek(.5)
    prepare()
    controlled = session._prepared_playback
    session.play(device_id=7)
    assert player.calls == [(controlled.audio, .25, 7)]
    player.frames = 3000  # .375 seconds of the two-times-speed derivative.
    assert session.position == .75 and session.index == 1
    assert session.source_time_accuracy == 'estimated_after_time_stretch'
    session.pause()
    session.play(device_id=7)
    assert len(player.calls) == 1
    session.stop()
    assert controlled._reader is None and session._reader is None
    session.remember(PublicationPositions(root))
    assert PublicationPositions(root).load(session.document) == .75
    assert session.document.audio.read_bytes() == original
    assert broker.snapshot()['leases'] == []


def test_changed_controls_require_native_stop_and_reuse_verifies_cached_derivative(prepared):
    session, player, root, broker, prepare = prepared
    prepare()
    first = session._prepared_playback
    session.play()
    with pytest.raises(ValueError, match='Stop'):
        prepare(PlaybackControls(speed=.5))
    assert session._prepared_playback is first and first._reader is not None
    session.stop()
    prepare()
    assert session._prepared_playback is first and first._reader is not None
    session.stop()
    prepare(PlaybackControls(speed=.5))
    second = session._prepared_playback
    assert second is not first and not first.audio.exists()
    assert second.audio_duration == 2 and second.source_duration == 1
    session.stop()
    prepare(PlaybackControls())
    assert session._prepared_playback is None and not second.audio.exists()
    session.play()
    assert player.calls[-1][0] == session.document.audio


def test_failed_native_stop_retains_both_verified_readers_and_controlled_pcm(prepared):
    session, player, root, broker, prepare = prepared
    prepare()
    controlled = session._prepared_playback
    session.play()
    player.stop_failure = True
    try:
        with pytest.raises(RuntimeError, match='still owns'):
            session.stop()
        assert session._reader is not None and controlled._reader is not None
        assert session.document.audio.exists() and controlled.audio.exists()
        with pytest.raises(ValueError, match='Stop'):
            session.remember(PublicationPositions(root))
    finally:
        player.stop_failure = False
    session.stop()
    session.close()
    assert not controlled.audio.exists() and not session.document.audio.exists()


def test_combined_budget_counts_original_and_derivative_and_releases_after_refusal(prepared, monkeypatch):
    session, player, root, broker, prepare = prepared
    # Large enough source metadata is selected through the real PCM producer,
    # avoiding a pretend disk-usage receipt or a weakened format bound.
    from carr_library.audio.ffmpeg import silence
    from carr_library.audio.jobs import file_identity, regular_file
    from dataclasses import replace
    silence(session.document.audio, 300000, sample_rate=8000)
    session.document.audio_identity = file_identity(regular_file(session.document.audio))
    session.document.audio_sha256 = sha256_file(session.document.audio)
    session.document.frames, session.document.duration = 300000, 37.5
    with pytest.raises(ValueError, match='allowance'):
        prepare(PlaybackControls(speed=.25))
    assert session._prepared_playback is None and not player.calls
    session.stop()
    assert session._reader is None and broker.snapshot()['leases'] == []


def test_replacing_publication_discards_controlled_pcm_without_closing_shared_player(prepared):
    session, player, root, broker, prepare = prepared
    prepare()
    controlled = session._prepared_playback
    session.stop()
    session.dispose_document()
    assert not controlled.audio.exists() and not session.document.audio.exists()
    assert player.close_calls == 0
    assert session._prepared_playback is None and session._reader is None


def test_cancelled_real_transform_never_opens_player_and_keeps_cleanup_reachable(prepared):
    session, player, root, broker, prepare = prepared
    stop = Event()
    def report(message):
        if message.startswith('Preparing independent'):
            stop.set()
    with pytest.raises(InterruptedError):
        prepare(cancelled=stop.is_set, progress=report)
    assert stop.is_set() and session._prepared_playback is None and not player.calls
    session.stop()
    assert session._reader is None and broker.snapshot()['leases'] == []
