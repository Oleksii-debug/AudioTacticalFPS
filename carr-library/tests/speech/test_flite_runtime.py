"""Six required real Flite cases, qualified on the Ubuntu FFmpeg/libflite job.

Missing binaries, filters or voices are failures. Windows uses test_runtime.py
for the shared boundaries and native SAPI; it does not claim Flite availability.
"""
from hashlib import sha256
import math
import shutil
import struct
import wave

import pytest

from carr_library.speech import FliteProvider, SpeechSettings, VoiceRegistry


@pytest.fixture(scope='module')
def flite():
    executable = shutil.which('ffmpeg')
    assert executable, 'Required Flite acceptance needs the installed FFmpeg executable.'
    registry = VoiceRegistry([FliteProvider(executable)])
    required = {'flite:awb', 'flite:rms', 'flite:slt', 'flite:kal16'}
    available = {voice.id for voice in registry.voices()}
    assert required <= available, 'Required FFmpeg Flite voices are missing: ' + ', '.join(sorted(required - available))
    return registry


def test_actual_local_pcm_speech_and_three_distinct_voices(flite, tmp_path):
    outputs = []
    text = 'The door opened. A quiet narrator described the empty room.'
    for voice_id in ('flite:awb', 'flite:rms', 'flite:slt'):
        assert voice_id in {v.id for v in flite.voices()}, f'Required Flite voice is missing: {voice_id}'
        result = flite.synthesize(text, voice_id, tmp_path / (voice_id.replace(':', '_') + ' голос.wav'))
        assert result.frames > 22050
        assert result.sample_rate == 22050
        assert result.channels == 1
        assert result.duration_seconds == result.frames / 22050
        assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
        assert result.text_sha256 == sha256(text.encode()).hexdigest()
        with wave.open(str(result.path), 'rb') as audio:
            values = struct.unpack('<' + 'h' * audio.getnframes(), audio.readframes(audio.getnframes()))
        assert max(map(abs, values)) > 100
        assert math.sqrt(sum(x*x for x in values) / len(values)) > 10
        outputs.append(result.sha256)
    assert len(set(outputs)) == 3


def test_actual_volume_reduction_and_source_invariance(flite, tmp_path):
    text = '<voice>Speak literally</voice>; $(no command) | ; colon: and quote " stay data.'
    before = text.encode('utf-8')
    high = flite.synthesize(text, 'flite:slt', tmp_path / 'high.wav')
    low = flite.synthesize(text, 'flite:slt', tmp_path / 'low.wav', settings=SpeechSettings(volume=25))
    def rms(path):
        with wave.open(str(path), 'rb') as audio:
            data = audio.readframes(audio.getnframes())
        samples = struct.unpack('<' + 'h' * (len(data) // 2), data)
        return math.sqrt(sum(x*x for x in samples) / len(samples))
    assert high.frames == low.frames
    assert .23 < rms(low.path) / rms(high.path) < .27
    assert text.encode('utf-8') == before


def test_no_overwrite_existing_audio(flite, tmp_path):
    output = tmp_path / 'keep.wav'
    output.write_bytes(b'original recording')
    with pytest.raises(FileExistsError):
        flite.synthesize('Test', 'flite:slt', output)
    assert output.read_bytes() == b'original recording'


def test_cancel_synthesis_leaves_no_partial_audio(flite, tmp_path):
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 5
    with pytest.raises(InterruptedError):
        flite.synthesize('Cancellation test. ' * 500, 'flite:slt', tmp_path / 'cancelled.wav', cancelled=cancelled)
    assert not list(tmp_path.iterdir())


def test_actual_chunked_synthesis_and_budget(flite, tmp_path):
    text = 'A sentence about a synthetic book. ' * 100
    result = flite.synthesize(text, 'flite:kal16', tmp_path / 'chunks.wav')
    assert result.chunks >= 2
    assert result.duration_seconds > 100
    with pytest.raises(ValueError, match='budget'):
        flite.synthesize('This has more than a few milliseconds of audio.', 'flite:slt',
                         tmp_path / 'small.wav', settings=SpeechSettings(max_output_bytes=4096))
    assert not (tmp_path / 'small.wav').exists()


def test_unsupported_native_controls_do_not_silently_apply(flite, tmp_path):
    with pytest.raises(ValueError, match='pitch'):
        flite.synthesize('Text', 'flite:slt', tmp_path / 'pitch.wav', settings=SpeechSettings(pitch=50))
    with pytest.raises(ValueError, match='rate'):
        flite.synthesize('Text', 'flite:slt', tmp_path / 'rate.wav', settings=SpeechSettings(rate=3))
    with pytest.raises(ValueError, match='voice'):
        flite.synthesize('Text', 'not-an-installed-voice', tmp_path / 'bad.wav')
    assert not list(tmp_path.iterdir())
