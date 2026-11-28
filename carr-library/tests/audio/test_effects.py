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
