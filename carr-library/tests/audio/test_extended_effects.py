"""Exercise the sealed FFmpeg signal path, not simulated filter output."""
from array import array
import hashlib
import json
from pathlib import Path
import math
import sys
import wave

import pytest

from carr_library.audio.ffmpeg import (effect_filters, measure_loudness, measure_peak,
                                       process_voice, wav_info)
from carr_library.audio.models import Effect, ProductionPlan, effect_controls
from carr_library.audio.production import parse_script
from carr_library.audio.runtime import find_ffmpeg


@pytest.fixture
def ffmpeg():
    selected = find_ffmpeg()
    assert selected is not None, 'The required signal checks need the configured local FFmpeg runtime.'
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
                        '[output format=wav]\nNarrator: Exact text.')
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



class SyntheticToneEngine:
    """Deterministic PCM fixture; no speech or voice-quality qualification."""
    def __init__(self):
        self.calls = []

    def cache_identity(self, voice):
        return {'fixture': 'extended-effect-tone-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        return tone(output, rate=24000, seconds=2)


def _assert_measured_production(result):
    directory = Path(result['manifest']).parent
    timeline = json.loads((directory / 'timeline.json').read_text(encoding='utf-8'))
    master = wav_info(result['output'])
    assert master.rate == 48000 and master.channels == 2
    position = 0
    for scene, chapter in zip(timeline['scenes'], timeline['chapters']):
        local = 0
        for event in scene['events']:
            assert event['start_frame'] == local and event['frames'] > 0
            local += event['frames']
        assert chapter['start_frame'] == position
        position += local
        assert chapter['end_frame'] == position
    assert position == master.frames
    assert len(timeline['scenes']) == len(timeline['chapters'])
    assert result['stems']
    for path in result['stems']:
        assert wav_info(path).frames == master.frames
    return master


def test_new_effects_preserve_real_timeline_stems_and_content_addressed_resume(tmp_path, ffmpeg):
    from carr_library.audio.production import render_project

    script = ('[role Alice voice=fixture:voice pan=left]\n'
              '[effect Alice tempo factor=2]\n[effect Alice pitch semitones=12]\n'
              '[output sample_rate=48000]\nAlice: Exact first words.\n'
              '[pause 0.25]\n# Second\nAlice: Exact second words.')
    plan = parse_script(script)
    engine = SyntheticToneEngine()
    root = tmp_path / 'Проєкт effects'
    first = render_project(plan, root, engine=engine, ffmpeg=ffmpeg)
    info = _assert_measured_production(first)
    assert 2 < info.seconds < 2.5
    first_bytes = Path(first['output']).read_bytes()
    assert plan.original_script == script
    assert plan.source_sha256 == hashlib.sha256(script.encode('utf-8')).hexdigest()
    assert engine.calls == ['Exact first words.', 'Exact second words.']

    repeated = render_project(plan, root, engine=engine, ffmpeg=ffmpeg)
    assert repeated['run_key'] == first['run_key'] and repeated['output'] == first['output']
    assert Path(repeated['output']).read_bytes() == first_bytes
    assert len(engine.calls) == 2

    changed = parse_script(script.replace('tempo factor=2', 'tempo factor=1'))
    second = render_project(changed, root, engine=engine, ffmpeg=ffmpeg)
    assert second['run_key'] != first['run_key']
    assert _assert_measured_production(second).frames > info.frames
    assert len(engine.calls) == 2  # Accepted original speech remains reusable.
    assert Path(first['output']).read_bytes() == first_bytes


def test_cancel_after_processed_effect_preserves_accepted_chunk_for_resume(tmp_path, ffmpeg):
    from carr_library.audio.jobs import RenderCancelled
    from carr_library.audio.production import render_project

    plan = parse_script('[effect Narrator pitch semitones=2]\n'
                        '[output sample_rate=48000]\nLiteral sentence in an explicit script.')
    engine = SyntheticToneEngine()
    root = tmp_path / 'cancelled effects'
    state = {'steps': 0, 'cancelled': False}

    def progress(message):
        if message == 'Completed and saved an audio step.':
            state['steps'] += 1
            if state['steps'] == 2:
                state['cancelled'] = True

    with pytest.raises(RenderCancelled):
        render_project(plan, root, engine=engine, ffmpeg=ffmpeg,
                       cancelled=lambda: state['cancelled'], on_status=progress)
    assert len(engine.calls) == 1
    journal = json.loads((root / 'render-journal.json').read_text(encoding='utf-8'))
    assert journal['state'] == 'cancelled'
    assert not list((root / 'renders').glob('*/complete.json'))

    state['cancelled'] = False
    result = render_project(plan, root, engine=engine, ffmpeg=ffmpeg)
    assert len(engine.calls) == 1
    _assert_measured_production(result)


@pytest.mark.parametrize('effect', [
    Effect('reverb', (('mix', 0),)),
    Effect('delay', (('mix', 0),)),
    Effect('delay', (('decay', 0),)),
])
def test_actual_zero_echo_controls_preserve_the_dry_pcm(tmp_path, ffmpeg, effect):
    source = tone(tmp_path / 'original.wav', rate=48000)
    before = hashlib.sha256(source.read_bytes()).digest()
    dry = process_voice(source, tmp_path / 'dry.wav', ffmpeg=ffmpeg)
    selected = process_voice(source, tmp_path / 'zero-echo.wav', ffmpeg=ffmpeg,
                             effects=(effect,))
    assert wav_info(selected).frames == wav_info(dry).frames
    assert samples(selected, 0) == samples(dry, 0)
    assert samples(selected, 1) == samples(dry, 1)
    assert hashlib.sha256(source.read_bytes()).digest() == before


@pytest.mark.parametrize('effect', [
    Effect('reverb', (('decay', .01),)),
    Effect('delay', (('mix', 1e-8),)),
])
def test_actual_small_positive_echo_coefficients_are_not_serialized_as_zero(tmp_path, ffmpeg, effect):
    source = tone(tmp_path / 'original.wav', rate=48000)
    before = hashlib.sha256(source.read_bytes()).digest()
    selected = process_voice(source, tmp_path / 'quiet-echo.wav', ffmpeg=ffmpeg,
                             effects=(effect,))
    original, result = wav_info(source), wav_info(selected)
    assert result.channels == 2 and result.rate == 48000
    assert result.frames > original.frames
    assert 0 < measure_peak(selected)['peak'] < .951
    assert hashlib.sha256(source.read_bytes()).digest() == before


@pytest.mark.parametrize('effect', [
    Effect('reverb', (('mix', 1e-300),)),
    Effect('delay', (('mix', 1e-300),)),
    Effect('delay', (('decay', 1e-300),)),
])
def test_unrepresentable_positive_echo_is_rejected_before_starting_ffmpeg(tmp_path, monkeypatch, effect):
    from carr_library.audio import ffmpeg as module

    source = tone(tmp_path / 'original.wav')
    before = source.read_bytes()
    output = tmp_path / 'never-published.wav'
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('An unrepresentable echo must fail before native execution.')

    monkeypatch.setattr(module, 'run_ffmpeg', forbidden)
    with pytest.raises(ValueError, match='reflection gain is too small'):
        process_voice(source, output, ffmpeg='not-invoked', effects=(effect,))
    assert not calls and not output.exists() and source.read_bytes() == before
