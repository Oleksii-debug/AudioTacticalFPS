"""Sample-aligned, non-destructive edits of one retained Studio clip.

No media is opened here. ClipEditor supplies an inspected selection length.
Stateful clip DSP must be rendered explicitly before splitting; scene/master
effects continue to process the assembled scene once through the existing graph.
"""
from dataclasses import replace
import math
from uuid import uuid4

from ..audio.envelopes import ENVELOPE_KINDS, envelope_effect, read_envelope
from .project import Cue


def sample_index(seconds, rate, label):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 31_536_000:
        raise ValueError(f'{label} must be a finite nonnegative number of seconds.')
    if type(rate) is not int or not 8000 <= rate <= 192000:
        raise ValueError('A supported output sample rate is required.')
    result = round(seconds * rate)
    if result > 31_536_000 * rate:
        raise ValueError(f'{label} exceeds the supported timeline.')
    return result


def aligned_index(seconds, rate, label):
    result = sample_index(seconds, rate, label)
    position = seconds * rate
    tolerance = max(1e-7, 4 * math.ulp(position))
    if not math.isclose(position, result, rel_tol=0, abs_tol=tolerance):
        raise ValueError(f'{label} must lie on the original sample grid. '
                         'Use Convert at the project sample rate before editing.')
    return result


def _selection(cue, frames, rate):
    frames = _length(frames)
    aligned_index(cue.trim_start, rate, 'Original selection start')
    if cue.duration is not None and aligned_index(cue.duration, rate, 'Selected duration') != frames:
        raise ValueError('Inspected clip duration differs from its selected sample count.')
    if sample_index(cue.start + frames / rate, rate, 'Scene end') != sample_index(cue.start, rate, 'Scene start') + frames:
        raise ValueError('The fractional scene boundary changes its rounded duration. '
                         'Move this clip to a sample-aligned scene start before editing.')
    return frames


def _length(frames):
    if type(frames) is not int or frames < 1:
        raise ValueError('The inspected selection needs at least one output sample.')
    return frames


def _editable(cue):
    if not isinstance(cue, Cue):
        raise ValueError('Select a typed Studio clip.')
    if any(effect.enabled and effect.kind not in ENVELOPE_KINDS and effect.kind != 'gain' for effect in cue.effects):
        raise ValueError('Render stateful clip effects before splitting or joining. '
                         'Scene and master effects may remain enabled.')
    if any(effect.enabled and effect.kind == 'width_envelope' for effect in cue.effects):
        raise ValueError('Stereo-width curves belong to a stereo bus, not a mono clip.')


def shifted_curve(effect, offset, rate):
    """Keep the exact discrete linear/hold function after a new clip start."""
    curve = read_envelope(effect, rate)
    if offset == 0:
        return effect
    points = [(0, curve.at(offset))]
    points.extend(((frame - offset) / rate, value)
                  for frame, value in zip(curve.frames, curve.values) if frame > offset)
    if len(points) == 1:
        points.append((1 / rate, points[0][1]))
    return envelope_effect(effect.kind, points,
                           interpolation='hold' if curve.interpolation else 'linear',
                           enabled=effect.enabled, sample_rate=rate)


def _boundary(cue, frame, frames, rate):
    # A split inside a fade would restart or reshape its existing gain ramp.
    # Edge and exactly completed fades can be retained without approximation.
    if 0 < frame < frames:
        fade_in = sample_index(cue.fade_in, rate, 'Fade in')
        fade_out = sample_index(cue.fade_out, rate, 'Fade out')
        if frame < fade_in or frame > frames - fade_out:
            raise ValueError('The edit boundary is inside an existing fade. '
                             'Move the boundary outside the fade or render the clip first.')


def _piece(cue, start, end, frames, rate, *, identity):
    _boundary(cue, start, frames, rate)
    _boundary(cue, end, frames, rate)
    effects = tuple(shifted_curve(effect, start, rate)
                    if effect.enabled and effect.kind in ENVELOPE_KINDS else effect for effect in cue.effects)
    return replace(cue, id=identity,
        start=(sample_index(cue.start, rate, 'Scene start') + start) / rate,
        trim_start=(aligned_index(cue.trim_start, rate, 'Original selection start') + start) / rate,
        duration=(end - start) / rate,
        fade_in=cue.fade_in if start == 0 else 0,
        fade_out=cue.fade_out if end == frames else 0, effects=effects)


def split_clip(cue, seconds, *, frames, sample_rate, new_id=None):
    """Split at a clip-relative position; neither source nor scene time changes."""
    _editable(cue)
    frames = _selection(cue, frames, sample_rate)
    cut = sample_index(seconds, sample_rate, 'Split position')
    if not 0 < cut < frames:
        raise ValueError('Split strictly inside the selected clip, leaving at least one sample on each side.')
    right_id = new_id or uuid4().hex
    if right_id == cue.id:
        raise ValueError('The second clip needs a different stable identity.')
    return (_piece(cue, 0, cut, frames, sample_rate, identity=cue.id),
            _piece(cue, cut, frames, frames, sample_rate, identity=right_id))


def cut_clip(cue, start, end, *, frames, sample_rate, new_id=None):
    """Remove one clip-relative interval and leave its time as silence."""
    _editable(cue)
    frames = _selection(cue, frames, sample_rate)
    first = sample_index(start, sample_rate, 'Cut start')
    last = sample_index(end, sample_rate, 'Cut end')
    if not 0 <= first < last <= frames:
        raise ValueError('Choose a nonempty cut interval entirely inside the selected clip.')
    result = []
    if first:
        result.append(_piece(cue, 0, first, frames, sample_rate, identity=cue.id))
    if last < frames:
        identity = (new_id or uuid4().hex) if result else cue.id
        if result and identity == cue.id:
            raise ValueError('The second clip needs a different stable identity.')
        result.append(_piece(cue, last, frames, frames, sample_rate, identity=identity))
    return tuple(result)


def _curve_continues(first, second, offset, count, rate):
    left, right = read_envelope(first, rate), read_envelope(second, rate)
    if left.kind != right.kind:
        return False
    # Two linear/hold functions are equal on each discrete affine interval
    # when both interval ends and every discontinuity agree.
    samples = {0, count - 1}
    for frame in (*right.frames, *(item - offset for item in left.frames)):
        for value in (frame - 1, frame, frame + 1):
            if 0 <= value < count:
                samples.add(value)
    return all(math.isclose(left.at(offset + value), right.at(value),
                            rel_tol=1e-12, abs_tol=1e-10) for value in samples)


def join_clips(first, second, *, first_frames, second_frames, sample_rate):
    """Rejoin adjacent selections of the same original without another render."""
    _editable(first)
    _editable(second)
    first_frames = _selection(first, first_frames, sample_rate)
    second_frames = _selection(second, second_frames, sample_rate)
    if first.id == second.id or first.path != second.path:
        raise ValueError('Join two different adjacent clips from the same retained original.')
    if any(getattr(first, name) != getattr(second, name) for name in ('gain_db', 'pan', 'role')):
        raise ValueError('Adjacent clips must have the same gain, pan and track role.')
    if (sample_index(first.start, sample_rate, 'First scene start') + first_frames
            != sample_index(second.start, sample_rate, 'Second scene start')
            or sample_index(first.trim_start, sample_rate, 'First original start') + first_frames
            != sample_index(second.trim_start, sample_rate, 'Second original start')):
        raise ValueError('Clips must be contiguous in both the scene and retained original.')
    if first.fade_out or second.fade_in:
        raise ValueError('A fade at the internal join cannot be discarded. Render it first.')
    if len(first.effects) != len(second.effects):
        raise ValueError('Clip effects differ at the join.')
    for left, right in zip(first.effects, second.effects):
        if left.kind != right.kind or left.enabled != right.enabled:
            raise ValueError('Clip effects differ at the join.')
        if left.enabled and left.kind in ENVELOPE_KINDS:
            if not _curve_continues(left, right, first_frames, second_frames, sample_rate):
                raise ValueError('The second curve does not continue the first clip exactly.')
        elif left != right:
            raise ValueError('Clip effect settings differ at the join.')
    return replace(first, start=sample_index(first.start, sample_rate, 'Scene start') / sample_rate,
                   duration=(first_frames + second_frames) / sample_rate, fade_out=second.fade_out)


def crossfade_clips(first, second, seconds, *, first_frames, second_frames, sample_rate):
    """Overlap two contiguous saved clips with complementary existing fades.

    The caller retains the old scene extent. No original selection, stable ID,
    routing or effect is changed; only the second scene start moves earlier.
    """
    _editable(first)
    _editable(second)
    first_frames = _selection(first, first_frames, sample_rate)
    second_frames = _selection(second, second_frames, sample_rate)
    if first.id == second.id:
        raise ValueError('Choose two different saved clips in one scene.')
    first_start = aligned_index(first.start, sample_rate, 'First scene start')
    second_start = aligned_index(second.start, sample_rate, 'Second scene start')
    if first_start + first_frames != second_start:
        raise ValueError('Crossfade requires two contiguous clips in scene order. '
                         'Review any gap or existing overlap before editing.')
    overlap = aligned_index(seconds, sample_rate, 'Crossfade duration')
    if not 0 < overlap <= min(first_frames, second_frames):
        raise ValueError('Crossfade must contain at least one sample and fit both selected clips.')
    if first.fade_out or second.fade_in:
        raise ValueError('Crossfade cannot replace an existing fade at the selected boundary.')
    if (aligned_index(first.fade_in, sample_rate, 'First fade in') > first_frames - overlap
            or aligned_index(second.fade_out, sample_rate, 'Second fade out') > second_frames - overlap):
        raise ValueError('Crossfade would overlap an existing outer fade. Review the saved fades first.')
    duration = overlap / sample_rate
    return (replace(first, fade_out=duration),
            replace(second, start=(second_start - overlap) / sample_rate, fade_in=duration))
