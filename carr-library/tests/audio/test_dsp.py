"""Real FFmpeg measurements, including the source and publication boundaries."""
from array import array
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import shutil
import sys
import time
import wave

import pytest

from carr_library.audio import (AudioBudget, AudioCancelled, AudioError, AudioRuntime,
                                Effect, Track, effect_controls)


pytestmark = pytest.mark.skipif(not (shutil.which('ffmpeg') and shutil.which('ffprobe')),
                                reason='Actual local FFmpeg and ffprobe are required.')
RATE = 24000


def write_wave(path, values, rate=RATE, channels=1):
    pcm = array('h', (max(-32768, min(32767, round(value * 32767))) for value in values))
    if sys.byteorder != 'little':
        pcm.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm.tobytes())
    return path


def tone(path, *, frequency=440, duration=1, amplitude=.2, rate=RATE):
    return write_wave(path, (amplitude * math.sin(2 * math.pi * frequency * n / rate)
                             for n in range(round(duration * rate))), rate=rate)


def samples(path):
    with wave.open(str(path), 'rb') as stream:
        channels, rate = stream.getnchannels(), stream.getframerate()
        data = array('h')
        data.frombytes(stream.readframes(stream.getnframes()))
        if sys.byteorder != 'little':
            data.byteswap()
    return [[value / 32768 for value in data[channel::channels]] for channel in range(channels)], rate


def rms(values):
    return math.sqrt(math.fsum(value * value for value in values) / len(values))


def amplitude(values, frequency, rate=RATE):
    real = math.fsum(value * math.cos(2 * math.pi * frequency * n / rate) for n, value in enumerate(values))
    imag = math.fsum(value * math.sin(2 * math.pi * frequency * n / rate) for n, value in enumerate(values))
    return 2 * math.hypot(real, imag) / len(values)


@pytest.fixture
def runtime(tmp_path):
    return AudioRuntime(tmp_path)


def test_actual_pan_timing_frames_and_source_unchanged(runtime, tmp_path):
    first = tone(tmp_path / 'лівий.wav', frequency=440)
    second = tone(tmp_path / "правий's.wav", frequency=880)
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)]
    info = runtime.mix([Track(first, pan=-1), Track(second, start=1.5, routing='right')],
                       'mix.wav', duration=3)
    (left, right), rate = samples(info.path)
    assert rate == RATE and info.frames == 3 * RATE and info.channels == 2
    assert rms(left[:RATE]) > .1 and rms(right[:RATE]) == 0
    assert rms(left[round(1.1 * RATE):round(1.4 * RATE)]) == 0
    assert rms(right[round(1.6 * RATE):round(2.4 * RATE)]) > .1
    assert rms(left[round(1.6 * RATE):round(2.4 * RATE)]) == 0
    assert max(map(abs, left + right)) <= .951
    assert before == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)]
    assert info.sha256 == hashlib.sha256(info.path.read_bytes()).hexdigest()


def test_music_is_actually_ducked_under_speech(runtime, tmp_path):
    music = tone(tmp_path / 'bed.wav', frequency=220, duration=4, amplitude=.18)
    voice = tone(tmp_path / 'voice.wav', frequency=880, duration=1, amplitude=.5)
    tracks = [Track(music, role='music', pan=-1), Track(voice, start=1, pan=1)]
    duck = runtime.mix(tracks, 'duck.wav')
    dry = runtime.mix(tracks, 'dry.wav', duck_music=False)
    wet_values, _ = samples(duck.path)
    dry_values, _ = samples(dry.path)
    interval = slice(round(1.3 * RATE), round(1.8 * RATE))
    assert rms(wet_values[0][interval]) < rms(dry_values[0][interval]) * .45
    assert abs(rms(wet_values[1][interval]) - rms(dry_values[1][interval])) < .002
    assert rms(wet_values[0][:RATE // 2]) > .1


def test_equalizer_measures_frequency_selective_gain(runtime, tmp_path):
    source = write_wave(tmp_path / 'two-tones.wav',
        (.08 * math.sin(2 * math.pi * 1000 * n / RATE) +
         .08 * math.sin(2 * math.pi * 4000 * n / RATE) for n in range(2 * RATE)))
    rendered = runtime.convert(source, 'eq.wav', channels=1,
        effects=(Effect('equalizer', frequency=1000, gain_db=6, q=3),))
    original, _ = samples(source)
    changed, _ = samples(rendered.path)
    measured_gain = 20 * math.log10(amplitude(changed[0][RATE:], 1000) / amplitude(original[0][RATE:], 1000))
    other_gain = 20 * math.log10(amplitude(changed[0][RATE:], 4000) / amplitude(original[0][RATE:], 4000))
    assert 5.8 < measured_gain < 6.2
    assert abs(other_gain) < .2


def test_gate_reduces_quiet_tone_without_muting_loud_tone(runtime, tmp_path):
    source = write_wave(tmp_path / 'levels.wav',
        ((.003 if n < RATE else .4) * math.sin(2 * math.pi * 440 * n / RATE)
         for n in range(2 * RATE)))
    result = runtime.convert(source, 'gate.wav', channels=1,
        effects=(Effect('gate', threshold_db=-30, ratio=8, attack_ms=1, release_ms=20),))
    before, _ = samples(source)
    after, _ = samples(result.path)
    assert rms(after[0][RATE // 4:RATE // 2]) < rms(before[0][RATE // 4:RATE // 2]) * .05
    assert rms(after[0][RATE + RATE // 2:]) > rms(before[0][RATE + RATE // 2:]) * .9


def test_compression_reduces_dynamic_range_and_limiter_prevents_clipping(runtime, tmp_path):
    source = write_wave(tmp_path / 'dynamics.wav',
        ((.03 if n < RATE else .9) * math.sin(2 * math.pi * 400 * n / RATE)
         for n in range(2 * RATE)))
    result = runtime.convert(source, 'compressed.wav', channels=1,
        effects=(Effect('compressor', threshold_db=-24, ratio=8, attack_ms=1, release_ms=30),))
    before, _ = samples(source)
    after, _ = samples(result.path)
    low, high = slice(RATE // 2, RATE), slice(RATE + RATE // 2, 2 * RATE)
    assert rms(after[0][high]) / rms(after[0][low]) < (rms(before[0][high]) / rms(before[0][low])) * .3
    limited = runtime.mix([Track(source, gain_db=24)], 'limited.wav')
    values, _ = samples(limited.path)
    assert max(abs(value) for channel in values for value in channel) <= .951


@pytest.mark.parametrize('effect,expected_sample', [(Effect('delay', time_ms=200, mix=.5), 4800),
                                                  (Effect('reverb', room_ms=50, mix=.5), 1200)])
def test_echo_and_room_reflections_have_measured_impulse_timing(runtime, tmp_path, effect, expected_sample):
    values = [0.] * RATE
    values[100] = .6
    source = write_wave(tmp_path / 'impulse.wav', values)
    result = runtime.convert(source, 'reflected.wav', channels=1, effects=(effect,))
    after, _ = samples(result.path)
    assert abs(after[0][100]) > .1
    assert abs(after[0][100 + expected_sample]) > .03
    assert rms(after[0][200:expected_sample]) < .0001


def test_time_stretch_preserves_pitch_and_pitch_control_preserves_duration(runtime, tmp_path):
    source = tone(tmp_path / 'pitched.wav', frequency=440, duration=2)
    stretched = runtime.convert(source, 'fast.wav', channels=1, effects=(Effect('tempo', factor=2),))
    shifted = runtime.convert(source, 'octave.wav', channels=1, effects=(Effect('pitch', semitones=12),))
    fast, _ = samples(stretched.path)
    octave, _ = samples(shifted.path)
    assert stretched.frames == RATE and shifted.frames == 2 * RATE
    assert amplitude(fast[0][RATE // 4:3 * RATE // 4], 440) > .1
    assert amplitude(octave[0][RATE // 2:RATE], 880) > .1
    assert amplitude(octave[0][RATE // 2:RATE], 440) < .01


def test_true_peak_loudness_meter_and_measured_normalization(runtime, tmp_path):
    source = tone(tmp_path / 'quiet.wav', duration=4, amplitude=.025)
    before = runtime.measure(source)
    result = runtime.convert(source, 'normalized.wav', channels=1,
        effects=(Effect('loudness', target_lufs=-18, peak_db=-1),))
    after = runtime.measure_loudness(result.path)
    assert before.integrated_lufs < -30
    assert -18.6 < after.integrated_lufs < -17.4
    assert after.true_peak_dbfs < -.9
    assert not after.silent


def test_silence_measurement_is_not_fabricated_loudness(runtime):
    result = runtime.silence('silence.wav', duration=1)
    measured = runtime.measure(result.path)
    assert measured.silent
    assert measured.integrated_lufs is None and measured.true_peak_dbfs is None


@pytest.mark.parametrize('effect', [Effect('gain', gain_db='1;amovie=/private'), Effect('unknown'),
    Effect('gate', hold_ms=100), Effect('gain', gain_db=float('nan')),
    Effect('gain', gain_db=float('inf')), Effect('gain', gain_db=True),
    Effect('equalizer', frequency=16000), Effect('delay', feedback=.8),
    Effect('reverb', parameters={'ir_file': '/private.wav'}),
    Effect('gain', gain_db=99, enabled=False)])
def test_invalid_controls_fail_before_output(runtime, tmp_path, effect):
    source = tone(tmp_path / 'source.wav')
    with pytest.raises(ValueError):
        runtime.convert(source, 'never.wav', effects=(effect,))
    assert not (tmp_path / 'never.wav').exists()


def test_unicode_quotes_and_many_scene_join_preserve_exact_frames(runtime, tmp_path):
    a = tone(tmp_path / "сцена 'один'.wav", duration=.125)
    b = tone(tmp_path / 'сцена два.wav', frequency=880, duration=.125)
    before = [path.read_bytes() for path in (a, b)]
    result = runtime.concatenate([a, b] * 36, 'joined.wav')
    assert result.frames == RATE * 9
    assert result.duration == 9
    assert [path.read_bytes() for path in (a, b)] == before
    values, _ = samples(result.path)
    assert amplitude(values[0][:RATE // 8], 440) > .1
    assert amplitude(values[0][RATE // 8:RATE // 4], 880) > .1


def test_incompatible_scene_rates_are_rejected(runtime, tmp_path):
    a = tone(tmp_path / 'a.wav', rate=24000)
    b = tone(tmp_path / 'b.wav', rate=16000)
    with pytest.raises(ValueError, match='identical PCM'):
        runtime.concatenate([a, b], 'never.wav')


def test_no_overwrite_and_input_or_output_escape(runtime, tmp_path):
    source = tone(tmp_path / 'source.wav')
    with pytest.raises(FileExistsError):
        runtime.convert(source, source)
    with pytest.raises(ValueError, match='outside'):
        runtime.convert(source, tmp_path.parent / 'escaped.wav')
    outside = tmp_path.parent / 'outside.wav'
    tone(outside)
    with pytest.raises(ValueError, match='outside'):
        runtime.inspect(outside)
    with pytest.raises(ValueError, match='local'):
        runtime.inspect('https://example.invalid/voice.wav')
    linked = tmp_path / 'linked.wav'
    try:
        linked.symlink_to(outside)
    except (OSError, NotImplementedError):
        return  # Windows test accounts may lack the optional symlink privilege.
    with pytest.raises(ValueError):
        runtime.inspect(linked)


def test_untrusted_playlist_cannot_select_external_inputs(runtime, tmp_path):
    playlist = tmp_path / 'fake.wav'
    playlist.write_text("ffconcat version 1.0\nfile 'http://127.0.0.1:9/private'\n", encoding='utf-8')
    with pytest.raises(AudioError):
        runtime.inspect(playlist)


def test_ancestor_links_and_unc_roots_are_rejected(tmp_path):
    for network in ('//server/share', '\\\\server\\share'):
        with pytest.raises(ValueError, match='local'):
            AudioRuntime(network)
    actual, linked = tmp_path / 'actual', tmp_path / 'linked'
    actual.mkdir()
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ValueError, match='links'):
        AudioRuntime(linked / 'output')
    with pytest.raises(ValueError, match='links'):
        AudioRuntime(actual, input_roots=(linked,))


def test_oversized_finite_duration_is_refused_by_resource_budget(runtime):
    with pytest.raises(AudioError, match='budget'):
        runtime.silence('never.wav', duration=1e300)


def test_cancellation_during_actual_render_removes_partial_output(runtime, tmp_path):
    # Use the progress stream of a long real FFmpeg render to trigger cancel.
    runtime.capabilities()
    state = {'cancelled': False}
    def progress(seconds):
        state['cancelled'] = True
    with pytest.raises(AudioCancelled):
        runtime.silence('cancelled.wav', duration=7200,
            cancelled=lambda: state['cancelled'], progress=progress)
    assert state['cancelled']
    assert not (tmp_path / 'cancelled.wav').exists()
    assert not list(tmp_path.glob('.audio-*'))


def test_disk_allowance_is_checked_before_render(runtime, tmp_path):
    runtime.budget = replace(runtime.budget, max_output_bytes=100000)
    with pytest.raises(AudioError, match='budget'):
        runtime.silence('too-large.wav', duration=10)
    assert not (tmp_path / 'too-large.wav').exists()


def test_trim_does_not_invent_samples_and_final_stereo_is_preserved(runtime, tmp_path):
    source = tone(tmp_path / 'tone.wav')
    with pytest.raises(ValueError, match='exceeds'):
        runtime.convert(source, 'bad.wav', duration=2)
    original = runtime.mix([Track(source, pan=-1)], 'left.wav')
    final = runtime.mix([Track(original.path, routing='stereo')], 'preserved.wav')
    (left, right), _ = samples(final.path)
    assert rms(left) > .1 and rms(right) == 0


def test_effect_bypass_preserves_signal_and_controls_have_units(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    result = runtime.convert(source, 'bypass.wav', channels=1,
        effects=(Effect('gain', gain_db=-60, enabled=False),))
    before, _ = samples(source)
    after, _ = samples(result.path)
    assert before == after
    assert effect_controls()['compressor']['attack_ms']['unit'] == 'ms'


@pytest.mark.parametrize('extension,rate', [('.flac',24000),('.mp3',24000),('.ogg',24000),
    ('.opus',48000),('.m4a',24000),('.m4b',24000),('.aiff',24000),('.wma',44100)])
def test_actual_installed_codecs(runtime, tmp_path, extension, rate):
    if extension not in runtime.capabilities()['formats']:
        pytest.skip('This local FFmpeg build does not contain the optional codec.')
    source = tone(tmp_path / 'source.wav', duration=2)
    result = runtime.convert(source, 'converted' + extension, sample_rate=rate)
    assert result.sample_rate == rate and result.channels == 2
    assert abs(result.duration - 2) < .15
    assert result.size > 1000
