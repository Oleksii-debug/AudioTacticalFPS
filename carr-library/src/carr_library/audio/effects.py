"""Small typed, offline FFmpeg effect vocabulary.

No filter text, expression, file name or plugin supplied by a script is evaluated.
The application stores this declarative representation; FFmpeg supplies the DSP.
"""
from dataclasses import dataclass
import math
from typing import Mapping

from .dsp import effect_controls as _studio_controls, effect_filter as _studio_filter
from .models import Effect as _StudioEffect


def number(value, name, minimum, maximum):
    if (type(value) not in (int, float) or not minimum <= value <= maximum
            or not math.isfinite(value)):
        raise ValueError(f'{name} must be a finite number from {minimum} to {maximum}.')
    return float(value)


def _fmt(value):
    return format(float(value), '.12g')


@dataclass(frozen=True)
class Effect:
    kind: str
    parameters: tuple = ()
    enabled: bool = True

    def __post_init__(self):
        if not isinstance(self.kind, str) or self.kind not in EFFECTS:
            raise ValueError('Unsupported audio effect.')
        if type(self.enabled) is not bool:
            raise ValueError('Effect enabled must be true or false.')
        values = self.parameters
        if isinstance(values, Mapping):
            values = tuple(sorted(values.items()))
        if not isinstance(values, (tuple, list)) or len(values) > 12:
            raise ValueError('Effect parameters must be a small named mapping.')
        try:
            pairs = tuple((key, value) for key, value in values)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid effect parameters.') from exc
        if any(not isinstance(key, str) for key, _ in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ValueError('Duplicate or invalid effect parameter.')
        object.__setattr__(self, 'parameters', pairs)
        _validated(self)


# parameter: (default, minimum, maximum). Bounds also bound native DSP work.
EFFECTS = {
    'gain': {'gain_db': (0, -48, 24)},
    'highpass': {'frequency_hz': (80, 10, 22000)},
    'lowpass': {'frequency_hz': (12000, 10, 22000)},
    'equalizer': {'frequency_hz': (1000, 10, 22000), 'gain_db': (0, -24, 24), 'q': (1, .1, 10)},
    'gate': {'threshold_db': (-45, -80, 0), 'range_db': (-60, -96, 0),
             'ratio': (4, 1, 100), 'attack_ms': (10, .01, 9000), 'release_ms': (200, .01, 9000)},
    'compressor': {'threshold_db': (-18, -60, 0), 'ratio': (3, 1, 20),
                   'attack_ms': (10, .01, 2000), 'release_ms': (150, .01, 9000),
                   'makeup_db': (0, 0, 24), 'knee_db': (6, 0, 18)},
    'limiter': {'ceiling_db': (-1, -20, -.05), 'attack_ms': (5, .1, 80), 'release_ms': (50, 1, 8000)},
    'denoise': {'reduction_db': (12, .01, 97), 'noise_floor_db': (-50, -80, -20)},
    'deesser': {'intensity': (.5, 0, 1), 'amount': (.5, 0, 1)},
    'declick': {},
    'declip': {},
    'delay': {'delay_ms': (180, 1, 2000), 'feedback': (.3, 0, .8), 'wet': (.2, 0, .8)},
    # A measured multi-tap echo preset, explicitly not a convolution room model.
    'room_echo': {'room_ms': (65, 10, 200), 'decay': (.4, .01, .8), 'wet': (.15, 0, .7)},
    'tempo': {'factor': (1, .5, 2)},
    'pitch': {'semitones': (0, -12, 12)},
    'stereo_width': {'width': (1, 0, 2)},
    'loudness': {'integrated_lufs': (-18, -36, -9), 'true_peak_dbfs': (-1.5, -9, -1), 'range_lu': (11, 1, 20)},
}


# Reuse the reviewed Studio DSP vocabulary for these additional converter
# controls. Existing kinds keep their exact defaults and compiled graphs.
_SHARED_EFFECT_KINDS = ('lowshelf', 'highshelf', 'notch', 'chorus', 'graphic_equalizer')
for _kind in _SHARED_EFFECT_KINDS:
    EFFECTS[_kind] = {
        ('frequency_hz' if name == 'frequency' else name):
            (definition['default'], definition['minimum'], definition['maximum'])
        for name, definition in _studio_controls()[_kind].items()
    }


def _validated(effect):
    supplied = dict(effect.parameters)
    allowed = EFFECTS[effect.kind]
    if set(supplied) - set(allowed):
        raise ValueError(f'Unsupported parameter for {effect.kind}.')
    return {key: number(supplied.get(key, bounds[0]), key, bounds[1], bounds[2])
            for key, bounds in allowed.items()}


def compile_effects(effects, *, sample_rate=24000):
    """Return only filters built from the reviewed numeric vocabulary."""
    if not isinstance(effects, (tuple, list)) or len(effects) > 24:
        raise ValueError('An effect chain can contain at most 24 stages.')
    sample_rate = int(number(sample_rate, 'sample rate', 8000, 192000))
    parts = []
    for effect in effects:
        if not isinstance(effect, Effect):
            raise ValueError('Expected typed Effect values, not filter text.')
        p = _validated(effect)
        if effect.kind in _SHARED_EFFECT_KINDS:
            controls = {('frequency' if name == 'frequency_hz' else name): value
                        for name, value in p.items()}
            compiled = _studio_filter(_StudioEffect(effect.kind, controls, enabled=effect.enabled),
                                      sample_rate)
            if compiled:
                parts.append(compiled)
            continue
        if not effect.enabled:
            continue
        kind = effect.kind
        if 'frequency_hz' in p and p['frequency_hz'] >= sample_rate / 2:
            raise ValueError('Effect frequency must be below half the sample rate.')
        if kind == 'gain':
            parts.append(f"volume={_fmt(p['gain_db'])}dB")
        elif kind in ('highpass', 'lowpass'):
            parts.append(f"{kind}=f={_fmt(p['frequency_hz'])}:p=2")
        elif kind == 'equalizer':
            parts.append(f"equalizer=f={_fmt(p['frequency_hz'])}:t=q:w={_fmt(p['q'])}:g={_fmt(p['gain_db'])}")
        elif kind == 'gate':
            parts.append('agate=' + ':'.join((
                f"threshold={_fmt(10 ** (p['threshold_db']/20))}",
                f"range={_fmt(10 ** (p['range_db']/20))}", f"ratio={_fmt(p['ratio'])}",
                f"attack={_fmt(p['attack_ms'])}", f"release={_fmt(p['release_ms'])}")))
        elif kind == 'compressor':
            parts.append('acompressor=' + ':'.join((
                f"threshold={_fmt(10 ** (p['threshold_db']/20))}", f"ratio={_fmt(p['ratio'])}",
                f"attack={_fmt(p['attack_ms'])}", f"release={_fmt(p['release_ms'])}",
                f"makeup={_fmt(10 ** (p['makeup_db']/20))}", f"knee={_fmt(10 ** (p['knee_db']/20))}")))
        elif kind == 'limiter':
            parts.append(f"alimiter=limit={_fmt(10 ** (p['ceiling_db']/20))}:level=0:"
                         f"attack={_fmt(p['attack_ms'])}:release={_fmt(p['release_ms'])}:latency=1")
        elif kind == 'denoise':
            parts.append(f"afftdn=nr={_fmt(p['reduction_db'])}:nf={_fmt(p['noise_floor_db'])}")
        elif kind == 'deesser':
            parts.append(f"deesser=i={_fmt(p['intensity'])}:m={_fmt(p['amount'])}")
        elif kind in ('declick', 'declip'):
            parts.append('adeclick' if kind == 'declick' else 'adeclip')
        elif kind in ('delay', 'room_echo'):
            delay = p['delay_ms'] if kind == 'delay' else p['room_ms']
            decay = p['feedback'] if kind == 'delay' else p['decay']
            if p['wet']:
                delays = '|'.join(_fmt(delay * (i + 1)) for i in range(4))
                gains = '|'.join(_fmt(p['wet'] * decay ** i) for i in range(4))
                parts.append(f'aecho=1:1:{delays}:{gains}')
        elif kind == 'tempo':
            parts.append(f"atempo={_fmt(p['factor'])}")
        elif kind == 'pitch':
            factor = 2 ** (p['semitones'] / 12)
            parts.append(f'asetrate={round(sample_rate*factor)},aresample={sample_rate},atempo={_fmt(1/factor)}')
        elif kind == 'stereo_width':
            parts.append(f"extrastereo=m={_fmt(p['width'])}")
        elif kind == 'loudness':
            parts.append(f"loudnorm=I={_fmt(p['integrated_lufs'])}:TP={_fmt(p['true_peak_dbfs'])}:LRA={_fmt(p['range_lu'])}")
    return ','.join(parts)


def effected_duration(duration, effects):
    """Conservative timeline duration, including deliberate echo tails."""
    value = float(duration)
    for effect in effects:
        if not isinstance(effect, Effect):
            raise ValueError('Expected typed effects.')
        p = _validated(effect)
        if not effect.enabled:
            continue
        if effect.kind == 'tempo':
            value /= p['factor']
        elif effect.kind in ('delay', 'room_echo') and p['wet']:
            value += 4 * (p.get('delay_ms', p.get('room_ms', 0))) / 1000
        elif effect.kind == 'chorus':
            # FFmpeg drains at most delay+depth after its input reaches EOF.
            value += (p['delay_ms'] + p['depth_ms']) / 1000
    return value


PRESETS = {
    'dry': (),
    'narration': (Effect('highpass', {'frequency_hz': 70}),
                  Effect('compressor', {'threshold_db': -20, 'ratio': 2.5}),
                  Effect('limiter', {'ceiling_db': -1.5})),
    'podcast': (Effect('highpass', {'frequency_hz': 80}),
                Effect('gate', {'threshold_db': -55, 'range_db': -30}),
                Effect('compressor', {'threshold_db': -18, 'ratio': 3}),
                Effect('limiter', {'ceiling_db': -1.5})),
    'small_room_echo': (Effect('room_echo'),),
}
