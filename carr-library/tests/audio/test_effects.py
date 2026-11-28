import math

import pytest

from carr_library.audio.effects import Effect, PRESETS, compile_effects, effected_duration


@pytest.mark.parametrize('kind,parameters', [
    ('amovie', {}), ('gain', {'gain_db': '0;amovie=http://example.invalid'}),
    ('gain', {'gain_db': math.nan}), ('gain', {'unknown': 1}),
    ('gain', {'gain_db': 10 ** 400}),
    ('equalizer', {'q': -1}), ('delay', {'delay_ms': 10000000}),
    ('compressor', {'ratio': True}), ('declip', {'plugin': '/tmp/native.dll'}),
])
def test_untrusted_effects_cannot_become_filter_code(kind, parameters):
    with pytest.raises(ValueError):
        Effect(kind, parameters)


def test_presets_are_typed_and_supported():
    for effects in PRESETS.values():
        assert isinstance(compile_effects(effects), str)
    with pytest.raises(ValueError):
        compile_effects(['volume=1'])
    with pytest.raises(ValueError):
        compile_effects((Effect('equalizer', {'frequency_hz': 12000}),), sample_rate=16000)


def test_disabled_effects_preserve_data_and_duration():
    effect = Effect('tempo', {'factor': 2}, enabled=False)
    assert compile_effects((effect,)) == ''
    assert effected_duration(10, (effect,)) == 10
    assert effected_duration(10, (Effect('tempo', {'factor': 2}),)) == 5
    assert effected_duration(1, (Effect('delay', {'delay_ms': 200}),)) == pytest.approx(1.8)


def test_parameter_input_is_copied_and_duplicate_names_rejected():
    parameters = {'gain_db': -10}
    effect = Effect('gain', parameters)
    parameters['gain_db'] = 20
    assert compile_effects((effect,)) == 'volume=-10dB'
    with pytest.raises(ValueError):
        Effect('gain', [('gain_db', -1), ('gain_db', -2)])


# These converter controls reuse the current Studio DSP, retaining its finite
# numeric schema instead of accepting a second filter language.
@pytest.mark.parametrize('kind,parameters,expected', [
    ('lowshelf', {'frequency_hz': 200, 'gain_db': 6, 'q': 1},
     'bass=f=200:t=q:w=1:g=6'),
    ('highshelf', {'frequency_hz': 5000, 'gain_db': -6, 'q': 1},
     'treble=f=5000:t=q:w=1:g=-6'),
    ('notch', {'frequency_hz': 50, 'q': 10},
     'bandreject=f=50:t=q:w=10'),
    ('chorus', {'delay_ms': 25, 'depth_ms': 4, 'speed_hz': 1, 'mix': .4},
     'chorus=0.8:0.8:25:0.4:1:4'),
])
def test_added_converter_controls_use_existing_typed_studio_filters(kind, parameters, expected):
    from carr_library.audio.dsp import effect_filter
    from carr_library.audio.models import Effect as StudioEffect

    effect = Effect(kind, parameters)
    studio_parameters = {('frequency' if key == 'frequency_hz' else key): value
                         for key, value in parameters.items()}
    assert compile_effects((effect,), sample_rate=48000) == expected
    assert effect_filter(StudioEffect(kind, studio_parameters), 48000) == expected
    assert compile_effects((Effect(kind, parameters, enabled=False),), sample_rate=48000) == ''


def test_graphic_equalizer_has_ten_fixed_saved_bands_and_a_bounded_compiled_chain():
    from carr_library.audio.dsp import effect_controls, effect_filter
    from carr_library.audio.models import Effect as StudioEffect

    names = ('band_31_5_db', 'band_63_db', 'band_125_db', 'band_250_db',
             'band_500_db', 'band_1000_db', 'band_2000_db', 'band_4000_db',
             'band_8000_db', 'band_16000_db')
    controls = effect_controls()['graphic_equalizer']
    assert tuple(controls) == names
    assert all(value == {'default': 0, 'minimum': -24, 'maximum': 24, 'unit': 'dB'}
               for value in controls.values())
    assert compile_effects((Effect('graphic_equalizer'),), sample_rate=8000) == ''
    assert effect_filter(StudioEffect('graphic_equalizer'), 8000) == ''
    parameters = dict.fromkeys(names, 6)
    effect = Effect('graphic_equalizer', parameters)
    expected = effect_filter(StudioEffect('graphic_equalizer', parameters), 48000)
    assert expected.count('equalizer=') == 10
    assert 'equalizer=f=31.5:t=q:w=1.41421356237:g=6' in expected
    assert 'equalizer=f=16000:t=q:w=1.41421356237:g=6' in expected
    compiled = compile_effects((effect,) * 24, sample_rate=48000)
    assert compiled.count('equalizer=') == 240 and len(compiled) < 24000
    with pytest.raises(ValueError, match='24'):
        compile_effects((effect,) * 25, sample_rate=48000)
    parameters['band_1000_db'] = -24
    assert dict(effect.parameters)['band_1000_db'] == 6


@pytest.mark.parametrize('kind,parameters', [
    ('lowshelf', {'gain_db': 25}),
    ('highshelf', {'q': 0}),
    ('notch', {'frequency_hz': '50;amovie=outside'}),
    ('chorus', {'delay_ms': 101}),
    ('chorus', {'depth_ms': 11}),
    ('chorus', {'speed_hz': 0}),
    ('chorus', {'mix': True}),
    ('graphic_equalizer', {'band_31_5_db': -25}),
    ('graphic_equalizer', {'band_16000_db': math.nan}),
    ('graphic_equalizer', {'band_1000_db': math.inf}),
    ('graphic_equalizer', {'band_1000_db': True}),
    ('graphic_equalizer', {'band_1000_db': '0,amovie=outside'}),
    ('graphic_equalizer', {'frequency_hz': 1000}),
    ('graphic_equalizer', {'filter': 'equalizer=f=1000'}),
])
def test_new_converter_controls_reject_invalid_values_even_when_bypassed(kind, parameters):
    with pytest.raises(ValueError):
        Effect(kind, parameters, enabled=False)


@pytest.mark.parametrize('rate,band', [
    (8000, 'band_4000_db'), (16000, 'band_8000_db'), (32000, 'band_16000_db'),
])
@pytest.mark.parametrize('enabled', [True, False])
def test_graphic_equalizer_refuses_active_nyquist_bands_without_silent_resampling(rate, band, enabled):
    from carr_library.audio.dsp import effect_filter
    from carr_library.audio.models import Effect as StudioEffect

    with pytest.raises(ValueError, match='half the selected sample rate'):
        compile_effects((Effect('graphic_equalizer', {band: 1}, enabled=enabled),),
                        sample_rate=rate)
    with pytest.raises(ValueError, match='half the selected sample rate'):
        effect_filter(StudioEffect('graphic_equalizer', {band: 1}, enabled=enabled), rate)
    assert compile_effects((Effect('graphic_equalizer', {band: 0}, enabled=enabled),),
                           sample_rate=rate) == ''


@pytest.mark.parametrize('enabled', [True, False])
def test_chorus_duration_accounts_for_the_native_eof_drain_before_later_tempo(enabled):
    chorus = Effect('chorus', {'delay_ms': 25, 'depth_ms': 4}, enabled=enabled)
    expected = 1.029 if enabled else 1
    assert effected_duration(1, (chorus,)) == pytest.approx(expected)
    assert effected_duration(1, (chorus, Effect('tempo', {'factor': 2}))) == pytest.approx(expected / 2)
