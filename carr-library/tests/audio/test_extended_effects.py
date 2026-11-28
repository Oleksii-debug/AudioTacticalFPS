"""Exercise the sealed FFmpeg signal path, not simulated filter output."""
from array import array
import hashlib
import math
import sys
import wave

import pytest

from carr_library.audio.ffmpeg import (effect_filters, measure_loudness, measure_peak,
                                       process_voice, run_ffmpeg, transcode, wav_info)
from carr_library.audio.models import Effect, ProductionPlan, effect_controls
from carr_library.audio.production import parse_script
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    selected = resolve_ffmpeg()
    if selected is None:
        pytest.skip('These signal checks require the pinned local FFmpeg runtime.')
    return selected


def tone(path, *, rate=24000, seconds=2, frequencies=(440,), amplitude=5000):
    values = array('h', (round(sum(amplitude * math.sin(2 * math.pi * f * i / rate)
                                  for f in frequencies))
                         for i in range(round(rate * seconds))))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, len(values), 'NONE', 'PCM'))
        stream.writeframes(values.tobytes())
    return path


def samples(path, channel=0):
    with wave.open(str(path), 'rb') as stream:
        values = array('h', stream.readframes(stream.getnframes()))
        rate, channels = stream.getframerate(), stream.getnchannels()
    if sys.byteorder != 'little':
        values.byteswap()
    return values[channel::channels], rate


def component(path, frequency, start=.3, end=1.3):
    values, rate = samples(path)
    selected = values[round(start * rate):round(end * rate)]
    assert selected
    real = sum(v * math.cos(2 * math.pi * frequency * i / rate) for i, v in enumerate(selected))
    imag = sum(v * math.sin(2 * math.pi * frequency * i / rate) for i, v in enumerate(selected))
    return 2 * math.hypot(real, imag) / len(selected)


def test_existing_effect_graphs_keep_their_cache_compatible_defaults():
    assert effect_filters(tuple(Effect(kind) for kind in (
        'highpass', 'lowpass', 'equalizer', 'gate', 'compressor', 'delay', 'denoise'))) == [
        'highpass=f=80.00000000',
        'lowpass=f=12000.00000000',
        'equalizer=f=1000.00000000:t=q:w=1.00000000:g=0.00000000',
        'agate=threshold=0.0056234133:attack=10.00000000:release=200.00000000',
        'acompressor=threshold=0.1000000000:ratio=3.00000000:attack=20.00000000:release=250.00000000',
        'aecho=0.8:0.9:250.00000000:0.25000000',
        'afftdn=nr=10.00000000',
    ]


@pytest.mark.parametrize('kind,parameter,value', [
    ('pitch', 'semitones', float('nan')),
    ('tempo', 'factor', .1),
    ('limiter', 'ceiling_db', 1),
    ('width', 'factor', True),
    ('chorus', 'speed_hz', float('inf')),
    ('reverb', 'room_ms', '1;amovie=private.txt'),
    ('compressor', 'mix', -1),
    ('deesser', 'frequency', 2),
])
def test_unsafe_or_out_of_range_controls_are_rejected_even_when_bypassed(kind, parameter, value):
    with pytest.raises(ValueError):
        Effect(kind, ((parameter, value),), enabled=False)


def test_effect_descriptions_are_copies_and_typed_plans_round_trip():
    controls = effect_controls()
    controls['pitch']['semitones']['default'] = 99
    assert effect_controls()['pitch']['semitones']['default'] == 0
    plan = parse_script('[effect Narrator pitch semitones=3]\n'
                        '[effect Narrator tempo factor=1.2]\n'
                        '[effect Narrator limiter ceiling_db=-3]\n'
                        '[output format=ogg]\nNarrator: Exact text.')
    restored = ProductionPlan.from_dict(plan.to_dict())
    assert restored == plan
    assert restored.roles[0].effects[0] == Effect('pitch', (('semitones', 3.0),))


@pytest.mark.parametrize('kind', ['lowshelf', 'highshelf', 'notch'])
def test_new_frequency_controls_refuse_nyquist_and_above(kind):
    with pytest.raises(ValueError, match='half'):
        effect_filters((Effect(kind, (('frequency', 4000),)),), sample_rate=8000)


@pytest.mark.parametrize('kind', [
    'gain', 'lowshelf', 'highshelf', 'notch', 'limiter', 'declick', 'declip',
    'deesser', 'reverb', 'chorus', 'tempo', 'pitch', 'width', 'loudness',
])
def test_each_added_effect_runs_through_actual_ffmpeg(tmp_path, ffmpeg, kind):
    source = tone(tmp_path / 'source.wav', rate=48000)
    before = hashlib.sha256(source.read_bytes()).digest()
    output = process_voice(source, tmp_path / (kind + '.wav'), ffmpeg=ffmpeg,
                           sample_rate=48000, effects=(Effect(kind),))
    info = wav_info(output)
    assert info.channels == 2 and info.rate == 48000 and info.seconds > 1.5
    assert 0 < measure_peak(output)['peak'] <= .951
    assert hashlib.sha256(source.read_bytes()).digest() == before


def test_actual_tempo_changes_duration_without_transposing(tmp_path, ffmpeg):
    source = tone(tmp_path / 'source.wav', seconds=3)
    output = process_voice(source, tmp_path / 'faster.wav', ffmpeg=ffmpeg,
                           sample_rate=24000, effects=(Effect('tempo', (('factor', 2),)),))
    assert 1.4 < wav_info(output).seconds < 1.6
    assert component(output, 440) > component(output, 880) * 10


@pytest.mark.parametrize('semitones,frequency', [(12, 880), (-12, 220)])
def test_actual_pitch_uses_input_rate_and_preserves_duration(tmp_path, ffmpeg, semitones, frequency):
    # A different selected output rate must never be interpreted as pitch.
    source = tone(tmp_path / 'source.wav', rate=24000, seconds=3)
    output = process_voice(source, tmp_path / 'shifted.wav', ffmpeg=ffmpeg,
                           sample_rate=48000, effects=(Effect('pitch', (('semitones', semitones),)),))
    assert 2.8 < wav_info(output).seconds < 3.2
    assert component(output, frequency) > component(output, 440) * 10


def test_actual_notch_removes_one_frequency_and_preserves_the_other(tmp_path, ffmpeg):
    source = tone(tmp_path / 'source.wav', frequencies=(500, 2000), amplitude=2500)
    output = process_voice(source, tmp_path / 'notch.wav', ffmpeg=ffmpeg,
                           sample_rate=24000,
                           effects=(Effect('notch', (('frequency', 500), ('q', 5))),))
    assert component(output, 500) < component(output, 2000) * .1


def test_actual_width_is_applied_to_stereo_placement(tmp_path, ffmpeg):
    source = tone(tmp_path / 'source.wav')
    flat = process_voice(source, tmp_path / 'left.wav', ffmpeg=ffmpeg, pan=-1)
    narrowed = process_voice(source, tmp_path / 'mono-center.wav', ffmpeg=ffmpeg, pan=-1,
                             effects=(Effect('width', (('factor', 0),)),))
    assert max(abs(v) for v in samples(flat, 1)[0]) == 0
    left, right = samples(narrowed, 0)[0], samples(narrowed, 1)[0]
    assert left == right and max(abs(v) for v in right) > 1000


def test_actual_selectable_loudness_and_limiter_are_measured(tmp_path, ffmpeg):
    source = tone(tmp_path / 'source.wav', rate=48000, seconds=4, amplitude=22000)
    output = process_voice(source, tmp_path / 'normalized.wav', ffmpeg=ffmpeg,
                           effects=(Effect('loudness', (('target_lufs', -24), ('peak_db', -3))),))
    measured = measure_loudness(output, ffmpeg=ffmpeg)
    assert abs(measured['integrated_lufs'] - (-24)) < 1
    limited = process_voice(source, tmp_path / 'limited.wav', ffmpeg=ffmpeg,
                            effects=(Effect('limiter', (('ceiling_db', -12),)),))
    assert measure_peak(limited)['peak'] <= 10 ** (-12 / 20) + .001


@pytest.mark.parametrize('format,rate', [('ogg', 48000), ('aac', 48000), ('wma', 48000), ('wma', 96000)])
def test_added_formats_encode_and_decode_real_audio_without_overwriting(tmp_path, ffmpeg, format, rate):
    source = tone(tmp_path / 'source.wav', rate=rate)
    before = hashlib.sha256(source.read_bytes()).digest()
    output = transcode(source, tmp_path / ('audio.' + format), ffmpeg=ffmpeg, format=format)
    decoded = tmp_path / 'decoded.wav'
    run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-i', str(output),
                       '-vn', '-c:a', 'pcm_s16le', '-ac', '2',
                       '-f', 'wav', '-n', str(decoded)])
    assert wav_info(decoded).rate == 48000
    assert 1.8 < wav_info(decoded).seconds < 2.2
    assert measure_peak(decoded)['peak'] > .01
    assert hashlib.sha256(source.read_bytes()).digest() == before
    original_output = output.read_bytes()
    with pytest.raises(FileExistsError):
        transcode(source, output, ffmpeg=ffmpeg, format=format)
    assert output.read_bytes() == original_output


def test_editor_appends_after_existing_effects_before_the_role_speaks():
    from carr_library.audio.production import add_role_effect

    script = '[role Alice pan=left]\r\n[effect Alice highpass frequency=80]\r\nAlice: Exact text.\r\n'
    updated = add_role_effect(script, 'Alice', Effect('pitch', (('semitones', 2),)))
    assert updated == ('[role Alice pan=left]\r\n[effect Alice highpass frequency=80]\r\n'
                       '[effect Alice pitch semitones=2]\r\nAlice: Exact text.\r\n')
    alice = next(role for role in parse_script(updated).roles if role.name == 'Alice')
    assert [effect.kind for effect in alice.effects] == ['highpass', 'pitch']
    assert alice.pan == -1


def test_editor_materializes_implicit_role_without_changing_its_voice_or_words():
    from carr_library.audio.production import add_role_effect

    script = 'Olena: Точні слова, без змін.\n'
    updated = add_role_effect(script, 'Olena', Effect('limiter'), voice='local:test', language='uk')
    before = parse_script(script, voice='local:test', language='uk')
    after = parse_script(updated, voice='local:test', language='uk')
    assert updated.endswith(script)
    assert before.scenes == after.scenes
    old_role = next(role for role in before.roles if role.name == 'Olena')
    new_role = next(role for role in after.roles if role.name == 'Olena')
    assert new_role.voice == old_role.voice == 'local:test'
    assert new_role.language == old_role.language == 'uk'


def test_editor_handles_quoted_role_and_keeps_order_after_redefinition():
    from carr_library.audio.production import add_role_effect

    script = '[role "First voice" pan=left]\n[effect "First voice" highpass]\n' \
             '[role "First voice" pan=right]\nFirst voice: Spoken.'
    updated = add_role_effect(script, 'First voice', Effect('gain', (('gain_db', -3),)))
    role = next(role for role in parse_script(updated).roles if role.name == 'First voice')
    assert role.pan == 1 and [effect.kind for effect in role.effects] == ['highpass', 'gain']
    assert updated.endswith('First voice: Spoken.')


def test_editor_refuses_unknown_role_and_chain_overflow():
    from carr_library.audio.production import add_role_effect

    with pytest.raises(ValueError, match='current production plan'):
        add_role_effect('Narrator: Words.', 'Unknown', Effect('gain'))
    script = '[effect Narrator gain]\n' * 16 + 'Narrator: Words.'
    with pytest.raises(ValueError, match='maximum 16'):
        add_role_effect(script, 'Narrator', Effect('gain'))


def test_codec_rate_constraint_is_visible_before_rendering():
    from carr_library.ui.audio_studio import plan_description

    plan = parse_script('[output format=wma sample_rate=96000]\nNarrator: Exact words.')
    description = plan_description(plan)
    assert '48,000 Hz' in description and '96000 Hz' in description


def test_script_bypass_and_order_change_reach_actual_signal_path(tmp_path, ffmpeg):
    from carr_library.audio.effect_editing import RoleEffectEditor

    source = tone(tmp_path / 'source.wav', rate=48000, amplitude=2500)
    before = hashlib.sha256(source.read_bytes()).digest()
    script = ('[effect Narrator gain gain_db=12]\n'
              '[effect Narrator limiter ceiling_db=-18]\nExact source.')
    editor = RoleEffectEditor(script)
    enabled = parse_script(editor.script()).roles[0].effects
    limited = process_voice(source, tmp_path / 'limited-first.wav', ffmpeg=ffmpeg, effects=enabled)
    editor.move('Narrator', 1, 0)
    reordered = process_voice(source, tmp_path / 'gain-last.wav', ffmpeg=ffmpeg,
                              effects=parse_script(editor.script()).roles[0].effects)
    assert measure_peak(reordered)['peak'] > measure_peak(limited)['peak'] * 2
    editor.bypass('Narrator', 0)
    editor.bypass('Narrator', 1)
    bypassed = process_voice(source, tmp_path / 'bypassed.wav', ffmpeg=ffmpeg,
                             effects=parse_script(editor.script()).roles[0].effects)
    dry = process_voice(source, tmp_path / 'dry.wav', ffmpeg=ffmpeg)
    assert samples(bypassed) == samples(dry)
    assert hashlib.sha256(source.read_bytes()).digest() == before
