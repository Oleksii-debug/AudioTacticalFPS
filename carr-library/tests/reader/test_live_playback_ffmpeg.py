"""Actual configured FFmpeg transforms synthetic PCM in the current reader.

The full source/installed gate must provide its pinned CARR_FFMPEG executable.
This verifies byte identity, measured duration/frequency and cursor ownership;
it is not a listening, TTS quality or audio-device result.
"""
from array import array
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import sys
import wave

import pytest

from carr_library.operations import ResourceBroker, ResourceLimits
from carr_library.reader.live_playback import PlaybackSettings, discover_playback_runtime
from carr_library.reader.live_speech import SpeechOperation, SpeechSelection, SpeechSession, SpeechSettings, text_hash


def engine_type():
    path = Path(__file__).with_name('test_live_playback.py')
    spec = importlib.util.spec_from_file_location('_carr_live_playback_pcm_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ToneEngine


def measured_frequency(path):
    with wave.open(str(path), 'rb') as stream:
        assert stream.getsampwidth() == 2 and stream.getnchannels() == 2
        rate = stream.getframerate()
        samples = array('h', stream.readframes(stream.getnframes()))
    if sys.byteorder != 'little':
        samples.byteswap()
    first_channel = samples[::2]
    # Omit bounded filter edges and count actual rising crossings.
    trimmed = first_channel[rate // 10:-(rate // 10)]
    assert len(trimmed) > rate // 2
    crossings = sum(left <= 0 < right for left, right in zip(trimmed, trimmed[1:]))
    return crossings * rate / (len(trimmed) - 1)


@pytest.mark.parametrize('tempo,pitch', [(2, 0), (1, 12), (.5, -12)])
def test_actual_verified_ffmpeg_prepares_independent_tempo_and_pitch(
        tmp_path, record_property, tempo, pitch):
    selected = os.environ.get('CARR_FFMPEG')
    assert selected and Path(selected).is_absolute(), 'This required gate must supply its pinned CARR_FFMPEG.'
    runtime = discover_playback_runtime(selected)
    assert runtime is not None
    root = tmp_path / 'library'
    root.mkdir()
    engine = engine_type()()
    broker = ResourceBroker.for_app(root,
        limits=ResourceLimits(cpu_slots=1, memory_bytes=128 * 1024**2))
    settings = PlaybackSettings(tempo, pitch)
    operation = SpeechOperation(playback=settings, playback_runtime=runtime)
    session = SpeechSession(root, 'Exact source.', SpeechSettings('fixture:voice', 'en'),
        SpeechSelection(0, 13), registry=engine, broker=broker, operation=operation)
    try:
        excerpt = session.render_next()
        duration = excerpt.frames / excerpt.sample_rate
        frequency = measured_frequency(excerpt.path)
        expected_duration = 2 / tempo
        expected_frequency = 440 * 2 ** (pitch / 12)
        assert abs(duration - expected_duration) < max(.1, expected_duration * .08)
        assert abs(frequency - expected_frequency) < expected_frequency * .03
        assert excerpt.synthesis_frames == 16000 and excerpt.synthesis_sample_rate == 8000
        assert excerpt.playback == settings and excerpt.text_sha256 == text_hash('Exact source.')
        assert excerpt.sha256 == sha256(excerpt.path.read_bytes()).hexdigest()
        assert excerpt.path.read_bytes() != engine.bytes[0]
        assert list(session.directory.iterdir()) == [excerpt.path]
        assert session.cursor == 0 and broker.snapshot()['leases'] == []
        session.pin_playback()
        session.check_playback()
        session.release(completed=True)
        assert session.cursor == 13 and session.text == 'Exact source.'
        assert not list(session.directory.iterdir())
        record_property('processor_sha256', runtime.sha256)
        record_property('prepared_tempo', tempo)
        record_property('prepared_pitch_semitones', pitch)
        record_property('measured_pcm_seconds', duration)
        record_property('measured_tone_hz', frequency)
    finally:
        session.close()
        operation.close()
