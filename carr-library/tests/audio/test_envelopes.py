"""Actual PCM and hostile numeric inputs for typed Studio envelopes."""
from array import array
from dataclasses import replace
from hashlib import sha256
import math
import shutil
import sys
import wave

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioRuntime, Effect, Track
from carr_library.audio.dsp import effect_controls, effect_filter
from carr_library.audio.envelopes import (
    ENVELOPE_KINDS, envelope_effect, read_envelope, validate_envelope_scope,
)
from carr_library.audio.effect_presets import default_effect

RATE = 24000


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        rate, channels, frames = source.getframerate(), source.getnchannels(), source.getnframes()
        data = array('h')
        data.frombytes(source.readframes(frames))
    if sys.byteorder != 'little':
        data.byteswap()
    return tuple(tuple(data[channel::channels]) for channel in range(channels)), rate, frames


def tone(path, *, stereo=False, seconds=2):
    values = array('h')
    for frame in range(round(seconds * RATE)):
        values.append(round(2500 * math.sin(2 * math.pi * 317 * frame / RATE)))
        if stereo:
            values.append(round(1900 * math.sin(2 * math.pi * 733 * frame / RATE)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((2 if stereo else 1, 2, RATE, 0, 'NONE', 'uncompressed'))
        output.writeframes(values.tobytes())
    return path


@pytest.fixture
def runtime(tmp_path):
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        pytest.skip('Actual pinned FFmpeg and ffprobe are required for PCM acceptance.')
    return AudioRuntime(tmp_path / 'output', input_roots=(tmp_path,),
                        budget=AudioBudget(timeout=30, reserve_disk_bytes=0))


@pytest.mark.parametrize('kind', sorted(ENVELOPE_KINDS))
def test_default_points_use_existing_typed_numeric_wire(kind):
    effect = default_effect(kind, RATE)
    assert len(effect.parameters) == 18
    assert set(effect_controls()[kind]) == set(effect.parameters)
    assert read_envelope(effect, RATE).frames == (0, RATE)
    assert Effect(effect.kind, effect.parameters, enabled=effect.enabled).to_dict() == effect.to_dict()
    assert 'aeval=' in effect_filter(effect, RATE)
    with pytest.raises(TypeError):
        effect.parameters['value_1'] = 999


@pytest.mark.parametrize('parameters', [
    {'point_count': 1}, {'point_count': 9}, {'point_count': 2.25},
    {'point_count': True}, {'interpolation': .25}, {'interpolation': 2},
    {'time_1': .1}, {'time_2': 0}, {'time_2': -1},
    {'time_2': 1e-12}, {'time_2': float('nan')}, {'time_2': float('inf')},
    {'value_1': -121}, {'value_2': 25}, {'value_8': float('nan')},
    {'value_2': '0;amovie=/private'}, {'filter': 'anull'}, {'time_8': True},
    {'point_count': 3, 'time_2': 2, 'time_3': 1},
])
@pytest.mark.parametrize('enabled', [False, True])
def test_invalid_active_and_bypassed_controls_cannot_compile(parameters, enabled):
    with pytest.raises(ValueError):
        effect_filter(Effect('gain_envelope', parameters, enabled=enabled), RATE)


@pytest.mark.parametrize('kind, bad', [('pan_envelope', 1.01), ('pan_envelope', -1.01),
                                      ('width_envelope', -.01), ('width_envelope', 2.01)])
def test_spatial_point_ranges_are_not_clamped_or_silently_changed(kind, bad):
    with pytest.raises(ValueError):
        read_envelope(Effect(kind, value_1=bad), RATE)


def test_linear_and_hold_reference_values_and_exact_point_boundaries():
    points = ((0, -12), (.5, 0), (1, -6))
    linear = read_envelope(envelope_effect('gain_envelope', points), RATE)
    hold = read_envelope(envelope_effect('gain_envelope', points, interpolation='hold'), RATE)
    assert [linear.at(x) for x in (0, 6000, 12000, 18000, 24000, 36000)] == [-12, -6, 0, -3, -6, -6]
    assert [hold.at(x) for x in (0, 11999, 12000, 23999, 24000, 36000)] == [-12, -12, 0, 0, -6, -6]


def test_output_sample_quantization_refuses_collapsed_distinct_times():
    effect = Effect('gain_envelope', time_2=1 / 48000)
    assert read_envelope(effect, 48000).frames == (0, 1)
    with pytest.raises(ValueError, match='one output sample'):
        read_envelope(effect, 8000)


@pytest.mark.parametrize('routing,pan', [('stereo', 0), ('left', 0), ('right', 0), ('pan', .2)])
def test_pan_curve_cannot_silently_override_stereo_route_or_fixed_placement(routing, pan):
    with pytest.raises(ValueError, match='fixed pan zero'):
        validate_envelope_scope((Effect('pan_envelope'),), routing=routing, pan=pan)


def test_width_curve_requires_stereo_and_duplicate_owners_are_rejected():
    with pytest.raises(ValueError, match='stereo'):
        validate_envelope_scope((Effect('width_envelope'),), routing='pan')
    with pytest.raises(ValueError, match='at most one'):
        validate_envelope_scope((Effect('gain_envelope'), Effect('gain_envelope')))
    validate_envelope_scope((Effect('pan_envelope', enabled=False),), routing='stereo')


def test_actual_linear_gain_is_sample_evaluated_and_preserves_frames(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    original = sha256(source.read_bytes()).hexdigest()
    base = runtime.root / 'base.wav'
    rendered = runtime.root / 'curve.wav'
    runtime.mix((Track(source),), base, sample_rate=RATE, duck_music=False)
    effect = envelope_effect('gain_envelope', ((0, -18), (1, 0)))
    runtime.mix((Track(source, effects=(effect,)),), rendered, sample_rate=RATE, duck_music=False)
    before, rate, frames = pcm(base)
    after, after_rate, after_frames = pcm(rendered)
    assert (after_rate, after_frames) == (rate, frames) == (RATE, RATE * 2)
    # Independent closed-form sample expectation; no production evaluator is used.
    for frame in (1213, 6013, 12013, 18013, 23983, 30013, 42013):
        multiplier = 10 ** ((-18 + 18 * min(frame / RATE, 1)) / 20)
        for channel in (0, 1):
            assert abs(after[channel][frame] - before[channel][frame] * multiplier) <= 3
    assert sha256(source.read_bytes()).hexdigest() == original


def test_actual_hold_gain_changes_at_sample_point_and_holds_last_value(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    base, changed = runtime.root / 'base.wav', runtime.root / 'hold.wav'
    runtime.mix((Track(source),), base, duck_music=False)
    effect = envelope_effect('gain_envelope', ((0, 0), (.5, -12), (1, -6)), interpolation='hold')
    runtime.mix((Track(source, effects=(effect,)),), changed, duck_music=False)
    raw, _, _ = pcm(base)
    result, rate, frames = pcm(changed)
    assert rate == RATE and frames == RATE * 2
    for frame, gain in ((11999, 0), (12000, -12), (12013, -12), (23999, -12), (24000, -6), (36013, -6)):
        assert abs(result[0][frame] - raw[0][frame] * 10 ** (gain / 20)) <= 3


def test_actual_pan_moves_mono_between_isolated_channels_and_equal_power_centre(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    effect = envelope_effect('pan_envelope', ((0, -1), (.5, 1), (1, 0)), interpolation='hold')
    rendered = runtime.root / 'pan.wav'
    runtime.mix((Track(source, effects=(effect,)),), rendered, duck_music=False)
    data, rate, frames = pcm(rendered)
    assert (rate, frames) == (RATE, RATE * 2)
    assert max(abs(x) for x in data[1][2400:9600]) == 0
    assert max(abs(x) for x in data[0][14400:21600]) == 0
    assert max(abs(x) for x in data[0][2400:9600]) > 2000
    assert max(abs(x) for x in data[1][14400:21600]) > 2000
    assert data[0][26400:45600] == data[1][26400:45600]
    assert 1700 < max(abs(x) for x in data[0][26400:45600]) < 1900


def test_actual_stereo_width_keeps_mid_side_math_and_duration(runtime, tmp_path):
    source = tone(tmp_path / 'stereo.wav', stereo=True)
    original = source.read_bytes()
    base, target = runtime.root / 'base.wav', runtime.root / 'width.wav'
    runtime.mix((Track(source, routing='stereo'),), base, duck_music=False)
    effect = envelope_effect('width_envelope', ((0, 0), (.5, 1), (1, 2)), interpolation='hold')
    runtime.mix((Track(source, routing='stereo'),), target, effects=(effect,), duck_music=False)
    dry, _, _ = pcm(base)
    wet, rate, frames = pcm(target)
    assert (rate, frames) == (RATE, RATE * 2)
    for frame, width in ((3613, 0), (18013, 1), (30013, 2), (42013, 2)):
        mid = (dry[0][frame] + dry[1][frame]) / 2
        side = (dry[0][frame] - dry[1][frame]) / 2
        assert abs(wet[0][frame] - (mid + width * side)) <= 3
        assert abs(wet[1][frame] - (mid - width * side)) <= 3
    assert source.read_bytes() == original


def test_actual_curve_bypass_keeps_pcm_and_cancellation_never_publishes(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    base, target = runtime.root / 'base.wav', runtime.root / 'bypassed.wav'
    runtime.mix((Track(source),), base)
    runtime.mix((Track(source, effects=(Effect('gain_envelope', value_1=-30, enabled=False),)),), target)
    assert pcm(base) == pcm(target)
    refused = runtime.root / 'cancelled.wav'
    with pytest.raises(AudioCancelled):
        runtime.mix((Track(source, effects=(Effect('gain_envelope'),)),), refused, cancelled=lambda: True)
    assert not refused.exists()


def test_actual_loudnorm_then_curve_reestablishes_declared_sample_clock(runtime, tmp_path):
    source = tone(tmp_path / 'original.wav')
    normal = Effect('loudness', target_lufs=-30, peak_db=-3, range_lu=7)
    curve = envelope_effect('gain_envelope', ((0, 0), (.5, -12)), interpolation='hold')
    baseline, output = runtime.root / 'normal.wav', runtime.root / 'normal-curve.wav'
    runtime.mix((Track(source, effects=(normal,)),), baseline, duck_music=False)
    runtime.mix((Track(source, effects=(normal, curve)),), output, duck_music=False)
    dry, _, _ = pcm(baseline)
    wet, rate, frames = pcm(output)
    assert (rate, frames) == (RATE, RATE * 2)
    for sample, gain in ((3013, 0), (11999, 0), (12000, -12), (12013, -12), (18013, -12)):
        for channel in (0, 1):
            assert abs(wet[channel][sample] - dry[channel][sample] * 10 ** (gain / 20)) <= 3


@pytest.mark.parametrize('kind,stereo,channels', [
    ('pan_envelope', True, 2), ('pan_envelope', False, 1),
    ('width_envelope', True, 1), ('width_envelope', False, 2),
])
def test_actual_convert_refuses_unreviewed_spatial_downmix(runtime, tmp_path, kind, stereo, channels):
    source = tone(tmp_path / 'source.wav', stereo=stereo)
    original = source.read_bytes()
    output = runtime.root / 'refused.wav'
    with pytest.raises(ValueError, match='require'):
        runtime.convert(source, output, effects=(Effect(kind),), channels=channels)
    assert not output.exists() and source.read_bytes() == original


def test_actual_explicit_mono_to_stereo_convert_retains_pan_curve(runtime, tmp_path):
    source = tone(tmp_path / 'mono.wav')
    effect = envelope_effect('pan_envelope', ((0, -1), (.5, 1)), interpolation='hold')
    output = runtime.root / 'pan-copy.wav'
    runtime.convert(source, output, effects=(effect,), channels=2)
    values, rate, frames = pcm(output)
    assert (rate, frames) == (RATE, RATE * 2)
    assert max(abs(v) for v in values[1][2400:9600]) == 0
    assert max(abs(v) for v in values[0][14400:21600]) == 0
    assert max(abs(v) for v in values[0][2400:9600]) > 2000
    assert max(abs(v) for v in values[1][14400:21600]) > 2000


def test_actual_convert_rejects_duplicate_curve_owners_before_publication(runtime, tmp_path):
    source = tone(tmp_path / 'source.wav')
    output = runtime.root / 'duplicate.wav'
    with pytest.raises(ValueError, match='at most one'):
        runtime.convert(source, output, effects=(Effect('gain_envelope'), Effect('gain_envelope')))
    assert not output.exists()
