"""Bounded numeric gain, mono-pan and stereo-width envelopes.

Eight explicit points are compiled by this module; no saved expression, command
or filter text is accepted. Time is quantized to the selected output sample
rate, and interpolation is evaluated once per PCM sample by FFmpeg's aeval.
The existing Effect wire shape, project history and backup contracts are used.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Effect, finite, integer

MAX_POINTS = 8
ENVELOPE_KINDS = frozenset(('gain_envelope', 'pan_envelope', 'width_envelope'))
_VALUES = {
    'gain_envelope': (0, -120, 24, 'dB'),
    'pan_envelope': (0, -1, 1, 'left -1, centre 0, right 1'),
    'width_envelope': (1, 0, 2, 'mono 0, original 1, wide 2'),
}
ENVELOPE_NOTICE = (
    'Up to eight points, relative to the selected audio. Point 1 starts at zero. '
    'Point count selects the active points. Interpolation: 0 linear, 1 hold. '
    'Times are seconds, quantized to output samples. The final value is held; '
    'the curve never extends the audio. Gain values are decibels. '
    'Pan places a mono clip or speaking role and requires fixed pan zero; '
    'stereo width preserves stereo channels and is available on scene/master buses. '
    'A speaking-role curve starts again for each rendered line.'
)


def control_definitions():
    result = {}
    for kind, (default, low, high, unit) in _VALUES.items():
        controls = {'point_count': (2, 2, MAX_POINTS, 'integer points'),
                    'interpolation': (0, 0, 1, '0 linear, 1 hold')}
        for index in range(1, MAX_POINTS + 1):
            controls[f'time_{index}'] = (index - 1, 0, 86400, 'seconds')
            controls[f'value_{index}'] = (default, low, high, unit)
        result[kind] = controls
    return result


@dataclass(frozen=True)
class Envelope:
    kind: str
    frames: tuple[int, ...]
    values: tuple[float, ...]
    interpolation: int
    sample_rate: int

    def at(self, frame):
        """Reference evaluator for independently measured output checks."""
        integer(frame, 'Envelope sample', 0, 192000 * 86400 * 2)
        for index, end in enumerate(self.frames[1:], 1):
            if frame < end:
                first = self.values[index - 1]
                if self.interpolation:
                    return first
                return first + (self.values[index] - first) * (
                    (frame - self.frames[index - 1]) /
                    (end - self.frames[index - 1]))
        return self.values[-1]


def read_envelope(effect, sample_rate=24000):
    integer(sample_rate, 'Envelope sample rate', 8000, 192000)
    if not isinstance(effect, Effect) or effect.kind not in ENVELOPE_KINDS:
        raise ValueError('Choose a typed gain, pan or stereo-width envelope.')
    definitions = control_definitions()[effect.kind]
    if set(effect.parameters) - set(definitions):
        raise ValueError('An envelope accepts only its bounded numeric point controls.')
    values = {name: finite(effect.parameters.get(name, spec[0]), name, spec[1], spec[2])
              for name, spec in definitions.items()}
    count, mode = values['point_count'], values['interpolation']
    if int(count) != count or int(mode) != mode:
        raise ValueError('Envelope point count and interpolation must be integers.')
    count, mode = int(count), int(mode)
    times = tuple(values[f'time_{index}'] for index in range(1, count + 1))
    points = tuple(values[f'value_{index}'] for index in range(1, count + 1))
    if times[0] != 0 or any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError('Envelope points must start at zero and increase strictly in time.')
    frames = tuple(round(value * sample_rate) for value in times)
    if any(right <= left for left, right in zip(frames, frames[1:])):
        raise ValueError('Envelope points must be at least one output sample apart.')
    return Envelope(effect.kind, frames, points, mode, sample_rate)


def envelope_effect(kind, points, *, interpolation='linear', enabled=True, sample_rate=24000):
    """Construct the existing immutable Effect from explicit (seconds, value) pairs."""
    if kind not in ENVELOPE_KINDS or interpolation not in ('linear', 'hold'):
        raise ValueError('Choose a supported envelope kind and interpolation.')
    if not isinstance(points, (tuple, list)) or not 2 <= len(points) <= MAX_POINTS:
        raise ValueError('An envelope needs two to eight explicit points.')
    controls = {'point_count': len(points), 'interpolation': int(interpolation == 'hold')}
    for index, point in enumerate(points, 1):
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise ValueError('Every envelope point must contain time and value.')
        controls[f'time_{index}'], controls[f'value_{index}'] = point
    effect = Effect(kind, controls, enabled=enabled)
    read_envelope(effect, sample_rate)
    return effect


def _number(value):
    return format(float(value), '.17g')


def _expression(curve):
    expression = _number(curve.values[-1])
    for index in range(len(curve.frames) - 2, -1, -1):
        start, end = curve.frames[index:index + 2]
        first, last = curve.values[index:index + 2]
        value = _number(first)
        if not curve.interpolation and first != last:
            value = f'({value}+({_number(last - first)})*(n-{start})/{end - start})'
        expression = f'if(lt(n,{end}),{value},{expression})'
    return expression


def envelope_filter(effect, sample_rate=24000):
    """Compile trusted syntax from finite numbers, including bypass validation."""
    curve = read_envelope(effect, sample_rate)
    if not effect.enabled:
        return ''
    value = _expression(curve)
    if effect.kind == 'gain_envelope':
        expressions, prefix, layout = f'val(ch)*pow(10,({value})/20)', '', 'same'
    elif effect.kind == 'pan_envelope':
        # Match existing mono placement and its equal-power law. Runtime.mix
        # omits the fixed pan when this explicit curve owns placement.
        left = f'val(0)*sqrt(max(0,(1-({value}))/2))'
        right = f'val(0)*sqrt(max(0,(1+({value}))/2))'
        expressions, prefix, layout = left + '|' + right, 'aformat=channel_layouts=mono,', 'stereo'
    else:
        middle, side = '(val(0)+val(1))/2', '(val(0)-val(1))/2'
        expressions = f'{middle}+({value})*{side}|{middle}-({value})*{side}'
        prefix, layout = 'aformat=channel_layouts=stereo,', 'stereo'
    # Quotes belong to libavfilter's parser, not a shell. All contents above
    # are fixed operators and validated numbers; arbitrary expressions have
    # no transport field.
    # A preceding dynamic loudnorm or pitch stage can change its working
    # rate. Re-establish the declared sample clock before evaluating n.
    return f'aresample={sample_rate},' + prefix + "aeval=exprs='" + expressions + "':c=" + layout


def active_envelope(effects, kind):
    return any(effect.enabled and effect.kind == kind for effect in effects)


def validate_envelope_scope(effects, *, routing='stereo', pan=0):
    """Reject conflicting fixed/automated placement before native execution."""
    if routing not in ('pan', 'left', 'right', 'stereo'):
        raise ValueError('Invalid envelope routing scope.')
    counts = {kind: sum(effect.enabled and effect.kind == kind for effect in effects)
              for kind in ENVELOPE_KINDS}
    if any(count > 1 for count in counts.values()):
        raise ValueError('Use at most one enabled envelope of each kind in a processing chain.')
    if counts['pan_envelope'] and (routing != 'pan' or pan != 0):
        raise ValueError('A pan envelope needs mono pan routing and fixed pan zero.')
    if counts['width_envelope'] and routing != 'stereo':
        raise ValueError('A stereo-width envelope needs a stereo scene/master bus or stereo track.')
