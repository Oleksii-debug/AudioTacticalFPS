"""Real PCM transforms through the live reader; no device or voice-quality claim."""
from pathlib import Path
from threading import Event

import pytest

from carr_library.audio.capture_types import AudioBudget
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.jobs import sha256_file
from carr_library.audio.playback_processing import PlaybackControls
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader import live_speech as module
from carr_library.reader.live_speech import SpeechSession, SpeechSelection, SpeechSettings
from tests.reader.test_live_speech import PcmEngine


@pytest.fixture
def session(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    engine = PcmEngine()
    binary = resolve_ffmpeg()
    assert binary is not None, 'Actual selected FFmpeg is required; no skip.'
    broker = ResourceBroker.for_app(root,
        limits=ResourceLimits(cpu_slots=1, memory_bytes=64 * 1024**2))
    value = SpeechSession(root, 'First. Second!', SpeechSettings('fixture:voice', 'en'),
        SpeechSelection(0, 14), registry=engine, broker=broker, temporary_parent=tmp_path,
        playback_controls=PlaybackControls(speed=2, pitch_semitones=5, volume=.4, pan=-.5),
        playback_budget=AudioBudget(threads=1, max_output_bytes=1024**2, reserve_disk_bytes=0),
        ffmpeg=binary)
    yield value, engine, broker
    value.close()


def test_actual_controlled_utterance_keeps_original_bytes_exact_range_and_completion_cursor(session):
    value, engine, broker = session
    excerpt = value.render_next()
    prepared = value._prepared_playback
    assert prepared is not None and prepared.audio != excerpt.path
    assert prepared.source_sha256 == excerpt.sha256 == sha256_file(excerpt.path)
    assert wav_info(prepared.audio).frames == round(excerpt.frames / 2)
    assert engine.calls[0][0] == 'First.' and value.cursor == 0
    assert engine.calls[0][1]['pitch'] == 0 and engine.calls[0][1]['rate'] == 0
    assert value.playback_path == prepared.audio
    assert value.source_time_accuracy == 'estimated_after_time_stretch'
    assert value.source_to_audio(.1) == .05 and value.audio_to_source(.05) == .1
    assert broker.snapshot()['leases'] == []
    value.pin_playback()
    value.check_playback()
    assert value._playback_reader is not None and prepared._reader is not None
    paths = excerpt.path, prepared.audio
    value.release(completed=True)
    assert value.cursor == 6 and value.current is None
    assert all(not path.exists() for path in paths)
    assert value._prepared_playback is None
    second = value.render_next()
    assert second.selection == SpeechSelection(7, 14)
    assert value.cursor == 6 and engine.calls[-1][0] == 'Second!'


def test_original_playback_does_not_require_or_resolve_an_optional_processor(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Original controls must not prepare a derivative.')
    monkeypatch.setattr(module, 'prepare_playback', forbidden)
    value = SpeechSession(tmp_path, 'First.', SpeechSettings('fixture:voice', 'en'),
        SpeechSelection(0, 6), registry=PcmEngine())
    try:
        excerpt = value.render_next()
        assert value.playback_path == excerpt.path and value.playback_duration == .1
        assert value.source_to_audio(.025) == .025
        value.pin_playback()
        value.check_playback()
    finally:
        value.close()


def test_controlled_pcm_cleanup_failure_retains_exact_owner_and_never_advances_cursor(session, monkeypatch):
    value, engine, broker = session
    excerpt = value.render_next()
    value.pin_playback()
    prepared = value._prepared_playback
    cleanup = prepared._temporary.cleanup
    def refuse():
        raise OSError('Temporary controlled PCM remains owned.')
    monkeypatch.setattr(prepared._temporary, 'cleanup', refuse)
    with pytest.raises(OSError, match='remains owned'):
        value.release(completed=True)
    assert value.cursor == 0 and value.current is excerpt
    assert value._prepared_playback is prepared and prepared.audio.exists()
    monkeypatch.setattr(prepared._temporary, 'cleanup', cleanup)
    value.close()
    assert not prepared.audio.exists() and not excerpt.path.exists()
    assert value.cursor == 0 and broker.snapshot()['leases'] == []


def test_validation_failure_is_sticky_and_a_retry_cannot_acknowledge_changed_speech(session, monkeypatch):
    value, engine, broker = session
    excerpt = value.render_next()
    value.pin_playback()
    prepared = value._prepared_playback
    release = prepared.release
    def changed():
        release()
        raise ValueError('Controlled PCM changed after playback.')
    monkeypatch.setattr(prepared, 'release', changed)
    with pytest.raises(ValueError, match='changed'):
        value.release(completed=True)
    assert value.invalid_audio and value.cursor == 0
    with pytest.raises(ValueError, match='cannot advance'):
        value.release(completed=True)
    value.close()
    assert value.cursor == 0 and not excerpt.path.exists()


def test_cancellation_after_real_derivative_preparation_releases_both_pcm_owners(session, monkeypatch):
    value, engine, broker = session
    prepare = module.prepare_playback
    outputs = []
    def cancel_after(*args, **kwargs):
        result = prepare(*args, **kwargs)
        outputs.append(result.audio)
        value.cancel.set()
        return result
    monkeypatch.setattr(module, 'prepare_playback', cancel_after)
    with pytest.raises(InterruptedError):
        value.render_next()
    assert outputs and all(not path.exists() for path in outputs)
    assert value.current is None and value._prepared_playback is None
    assert list(value.directory.iterdir()) == [] and broker.snapshot()['leases'] == []


def test_independent_pcm_byte_allowance_refuses_before_publishing_an_utterance(session):
    value, engine, broker = session
    value.playback_budget = AudioBudget(threads=1, max_output_bytes=4096, reserve_disk_bytes=0)
    with pytest.raises(ValueError, match='allowance'):
        value.render_next()
    assert value.current is None and value.cursor == 0
    assert list(value.directory.iterdir()) == [] and broker.snapshot()['leases'] == []
