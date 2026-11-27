"""Speech boundaries plus actual installed Flite and Windows SAPI synthesis."""
from hashlib import sha256
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import time
import wave

import pytest

from carr_library.speech import (FliteProvider, LocalHelperProvider, PiperProvider,
    SpeechSettings, Voice, VoiceRegistry, split_utterances)
from carr_library.speech._io import new_output, run_process


@pytest.fixture(scope='module')
def flite():
    executable = shutil.which('ffmpeg')
    if not executable:
        pytest.skip('Local FFmpeg is not installed; native SAPI is tested separately on Windows.')
    registry = VoiceRegistry([FliteProvider(executable)])
    if not registry.voices():
        pytest.skip('The installed FFmpeg does not include the optional Flite filter.')
    return registry


def test_actual_local_pcm_speech_and_three_distinct_voices(flite, tmp_path):
    outputs = []
    text = 'The door opened. A quiet narrator described the empty room.'
    for voice_id in ('flite:awb', 'flite:rms', 'flite:slt'):
        if voice_id not in {v.id for v in flite.voices()}:
            pytest.skip(f'This installed build lacks {voice_id}.')
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


def test_split_retains_every_unicode_character_and_bounds_calls():
    text = ('Довгий український текст. A Slovak sentence: Dobrý deň.\n' * 200) + '尾声'
    chunks = list(split_utterances(text))
    assert ''.join(chunks) == text
    assert all(len(chunk) <= 2400 for chunk in chunks)


@pytest.mark.parametrize('settings', [dict(rate=True), dict(rate=11), dict(volume=-1),
    dict(pitch=100), dict(punctuation=1), dict(timeout_seconds=float('nan')), dict(max_output_bytes=2**32)])
def test_invalid_settings_fail_early(settings):
    with pytest.raises(ValueError):
        SpeechSettings(**settings)


def test_worker_timeout_and_output_budget_are_real_process_checks(tmp_path):
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_process([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(30)'], timeout=.12)
    assert time.monotonic() - started < 3
    with pytest.raises(ValueError, match='diagnostic-output'):
        run_process([sys.executable, '-I', '-S', '-c', 'import sys; sys.stdout.write("x" * 2000100)'])


def test_atomic_output_publication_detects_concurrent_destination(tmp_path):
    target = tmp_path / 'result.wav'
    with pytest.raises(FileExistsError):
        with new_output(target) as staged:
            staged.write_bytes(b'our output')
            target.write_bytes(b'other output')
    assert target.read_bytes() == b'other output'
    assert len(list(tmp_path.iterdir())) == 1


def test_helper_requires_opt_in_and_exact_binary_hash(tmp_path):
    executable = tmp_path / 'voice-helper.exe'
    executable.write_bytes(b'not executable, never run')
    with pytest.raises(ValueError, match='Explicitly enable'):
        LocalHelperProvider(executable, sha256=sha256(executable.read_bytes()).hexdigest(), engine_name='dordiai-voices')
    with pytest.raises(ValueError, match='hash verification'):
        LocalHelperProvider(executable, sha256='0'*64, engine_name='dordiai-voices', enabled=True)


def test_piper_requires_hashed_existing_model_and_config(tmp_path):
    model = tmp_path / 'voice.onnx'
    config = tmp_path / 'voice.onnx.json'
    model.write_bytes(b'not a real neural model; never executed')
    config.write_bytes(b'{}')
    row = {'path': str(model), 'sha256': sha256(model.read_bytes()).hexdigest(),
           'config_sha256': sha256(config.read_bytes()).hexdigest(), 'name': 'Test manifest',
           'language': 'uk', 'license': 'Synthetic fixture, no voice model bundled'}
    provider = PiperProvider(sys.executable, [row])
    assert len(provider.voices()) == 1
    config.write_bytes(b'{"modified":true}')
    with pytest.raises(ValueError, match='hash verification'):
        PiperProvider(sys.executable, [row])


@pytest.mark.skipif(os.name != 'nt', reason='Actual SAPI requires native Windows.')
def test_native_windows_sapi_voice_and_pcm_output(tmp_path):
    registry = VoiceRegistry.discover(include_flite=False)
    voices = [voice for voice in registry.voices() if voice.id.startswith('sapi5-')]
    assert voices, 'Windows must expose at least one installed SAPI voice: ' + '; '.join(registry.notices)
    result = registry.synthesize('A real Windows source-preserving speech test. <voice>Literal text.</voice>',
                                 voices[0].id, tmp_path / 'native-голос.wav', settings=SpeechSettings(volume=50))
    assert result.frames > result.sample_rate
    assert result.path.is_file()
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()


@pytest.mark.parametrize('change', ['none', 'audio', 'voice', 'cancel'])
def test_pcm_receipt_attests_flushed_output(tmp_path, monkeypatch, change):
    """Exercise real no-clobber publication with a synthetic local PCM provider."""
    asset = tmp_path / 'voice.fixture'
    asset.write_bytes(b'synthetic voice identity')
    output = tmp_path / 'result.wav'
    stopped = False
    flushed = False

    class SyntheticVoice:
        name = 'Synthetic PCM fixture'
        executable = asset

        def voices(self):
            return (Voice('fixture:voice', 'Fixture', self.name, 'uk'),)

        def render(self, text, voice, destination, settings, cancelled):
            with wave.open(str(destination), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(22050)
                stream.writeframes(b'\x01\x00' * 32)

    actual_fsync = os.fsync

    def flush(fd):
        nonlocal stopped, flushed
        actual_fsync(fd)
        staged = list(tmp_path.glob('.speech-*/output.wav'))
        if not staged or flushed:
            return
        opened, candidate = os.fstat(fd), staged[0].stat()
        if (opened.st_dev, opened.st_ino) != (candidate.st_dev, candidate.st_ino):
            return
        flushed = True
        if change == 'audio':
            os.lseek(fd, -1, os.SEEK_END)
            os.write(fd, b'\x7f')
            actual_fsync(fd)
        elif change == 'voice':
            asset.write_bytes(b'changed voice identity')
        elif change == 'cancel':
            stopped = True

    monkeypatch.setattr(os, 'fsync', flush)
    registry = VoiceRegistry([SyntheticVoice()])
    try:
        text = 'Дослівний текст — Dobrý deň.'
        def render():
            return registry.synthesize(text, 'fixture:voice', output,
                                       cancelled=lambda: stopped)
        if change == 'none':
            result = render()
            assert result.sha256 == sha256(output.read_bytes()).hexdigest()
            assert result.text_sha256 == sha256(text.encode('utf-8')).hexdigest()
        elif change == 'cancel':
            with pytest.raises(InterruptedError):
                render()
        else:
            with pytest.raises(ValueError, match='receipt' if change == 'audio' else 'voice assets changed'):
                render()
        assert flushed
        assert output.exists() == (change == 'none')
        assert not list(tmp_path.glob('.speech-*'))
    finally:
        registry.close()
