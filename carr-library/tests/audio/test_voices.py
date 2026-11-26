"""Actual local speech plus rejection, cancellation and provenance checks."""
from array import array
from hashlib import sha256
import math
import os
from pathlib import Path
import time
import wave

import pytest

from carr_library.audio.voices import MAX_TEXT_CHARS, VoiceRegistry


@pytest.fixture(scope='module')
def registry():
    registry = VoiceRegistry()
    voices = registry.list_voices()
    assert voices, 'This required speech suite needs actual installed SAPI5, Flite or eSpeak NG voices.'
    return registry


@pytest.fixture(scope='module')
def voice(registry):
    voices = registry.list_voices()
    return next((voice for voice in voices if any(lang.startswith('en') for lang in voice.languages)), voices[0])


def rms(path):
    with wave.open(str(path), 'rb') as wav:
        samples = array('h', wav.readframes(wav.getnframes()))
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def test_real_speech_is_pcm_with_exact_source_and_file_provenance(registry, voice, tmp_path):
    original = 'The library reads a calm sentence. The original text remains unchanged.'
    result = registry.synthesize(original, tmp_path / 'speech.wav', voice_id=voice.id,
                                 language=voice.languages[0])
    assert result.path.is_file()
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert result.text_sha256 == sha256(original.encode()).hexdigest()
    assert result.duration_seconds == result.frames / result.sample_rate
    assert result.duration_seconds > 0.5 and result.channels == 1
    assert result.timing == 'utterance'
    assert rms(result.path) > 100
    assert original == 'The library reads a calm sentence. The original text remains unchanged.'


def test_real_voice_rate_and_volume_change_audio(registry, voice, tmp_path):
    text = 'A deliberately longer speech sample makes the timing comparison stable across local engines.'
    slow = registry.synthesize(text, tmp_path / 'slow.wav', voice_id=voice.id, rate=0.7)
    fast = registry.synthesize(text, tmp_path / 'fast.wav', voice_id=voice.id, rate=1.5)
    quiet = registry.synthesize(text, tmp_path / 'quiet.wav', voice_id=voice.id, rate=0.7, volume=0.35)
    assert slow.duration_seconds > fast.duration_seconds * 1.1
    assert quiet.duration_seconds == pytest.approx(slow.duration_seconds, rel=0.02)
    assert rms(quiet.path) < rms(slow.path) * 0.7
    assert slow.sha256 != fast.sha256 != quiet.sha256


def test_real_text_is_never_a_shell_or_filter_program(registry, voice, tmp_path):
    sentinel = tmp_path / 'must-not-exist'
    text = f"A quote '; $(touch {sentinel}); [a]; anullsrc. <speak>ordinary text</speak>."
    output = tmp_path / "Speech with spaces and apostrophe's.wav"
    result = registry.synthesize(text, output, voice_id=voice.id)
    assert result.frames > 0 and not sentinel.exists()
    assert result.text_sha256 == sha256(text.encode()).hexdigest()


def test_runtime_identity_is_repeatable_and_includes_actual_runtime_hashes(registry):
    first = registry.fingerprint()
    identity = registry.runtime_identity()
    assert len(first) == 64 and first == registry.fingerprint()
    assert identity['voices'] and identity['executables']
    assert all(len(row['executable_sha256']) == 64 for row in identity['executables'])
    assert len(identity['sapi_helper_sha256']) == 64
    assert 'transitive' not in identity['fingerprint_scope']


def test_no_unavailable_voice_is_misrepresented_as_working():
    empty = VoiceRegistry({'ffmpeg': False, 'espeak_ng': False, 'include_sapi': False})
    assert empty.list_voices() == ()
    statuses = {row['engine']: row for row in empty.engine_status()}
    assert statuses['dordiai-voices']['status'] == 'unavailable'
    assert statuses['sapi4']['status'] == 'unavailable'
    assert not any(row['status'] == 'available' for row in statuses.values())


@pytest.mark.parametrize('config', [{'endpoint': 'https://example.invalid'},
                                  {'ffmpeg': 'https://example.invalid/engine'},
                                  {'espeak_ng': r'\\server\voice.exe'},
                                  {'include_sapi': 'yes'}])
def test_remote_or_ambiguous_engine_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        VoiceRegistry(config)


def test_existing_output_and_symlink_are_not_overwritten(registry, voice, tmp_path):
    output = tmp_path / 'existing.wav'
    output.write_bytes(b'unchanged user file')
    with pytest.raises(FileExistsError):
        registry.synthesize('Test', output, voice_id=voice.id)
    assert output.read_bytes() == b'unchanged user file'
    if os.name != 'nt':
        link = tmp_path / 'link.wav'
        link.symlink_to(output)
        with pytest.raises(FileExistsError):
            registry.synthesize('Test', link, voice_id=voice.id)
        assert output.read_bytes() == b'unchanged user file'


def test_unsupported_language_is_not_silently_read_in_english(registry, voice, tmp_path):
    with pytest.raises(ValueError, match='language'):
        registry.synthesize('Text', tmp_path / 'speech.wav', voice_id=voice.id, language='zz')
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('extra', [{'rate': 0}, {'rate': float('nan')}, {'rate': True},
                                  {'volume': -1}, {'volume': 2}, {'timeout_seconds': 0}])
def test_invalid_controls_cannot_launch_the_engine(registry, voice, tmp_path, extra):
    with pytest.raises(ValueError):
        registry.synthesize('Text', tmp_path / 'speech.wav', voice_id=voice.id, **extra)
    assert not list(tmp_path.iterdir())


def test_oversize_utterance_is_recoverable_not_a_project_duration_cap(registry, voice, tmp_path):
    with pytest.raises(ValueError, match='chunks'):
        registry.synthesize('x' * (MAX_TEXT_CHARS + 1), tmp_path / 'speech.wav', voice_id=voice.id)
    result = registry.synthesize('A valid smaller chunk.', tmp_path / 'speech.wav', voice_id=voice.id)
    assert result.frames > 0


def test_cancellation_before_launch_publishes_nothing(registry, voice, tmp_path):
    with pytest.raises(InterruptedError):
        registry.synthesize('Cancelled speech', tmp_path / 'speech.wav', voice_id=voice.id,
                            cancelled=lambda: True)
    assert not list(tmp_path.iterdir())


def test_real_in_progress_engine_is_terminated_without_partial_publication(registry, voice, tmp_path):
    start = time.monotonic()
    with pytest.raises(InterruptedError):
        registry.synthesize('This is a continuing sentence in a deliberately long utterance. ' * 400,
            tmp_path / 'speech.wav', voice_id=voice.id,
            cancelled=lambda: time.monotonic() - start > 0.06)
    assert time.monotonic() - start < 4
    assert not list(tmp_path.iterdir())


def test_missing_voice_does_not_fall_back_or_create_audio(registry, tmp_path):
    with pytest.raises(ValueError, match='installed voice'):
        registry.synthesize('Text', tmp_path / 'speech.wav', voice_id='dordiai-voices:invented')
    assert not list(tmp_path.iterdir())
