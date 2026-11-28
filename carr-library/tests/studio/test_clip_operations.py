"""Independent geometry, curve continuity and refusal contracts for saved clips."""
from dataclasses import replace
import math
import pytest

from carr_library.audio import Effect
from carr_library.audio.envelopes import envelope_effect, read_envelope
from carr_library.studio.clip_operations import cut_clip, join_clips, shifted_curve, split_clip
from carr_library.studio.project import Cue

RATE = 24000


def clip(**settings):
    return Cue('original', 'inputs/' + 'a' * 64 + '.audio', start=3, trim_start=1,
               duration=2, **settings)


def test_split_retains_original_identity_bytes_and_exact_scene_grid():
    original = clip(title='Reviewed take', gain_db=-4, pan=-1, role='microphone')
    first, second = split_clip(original, .625, frames=48000, sample_rate=RATE, new_id='second')
    assert first.id == original.id and second.id == 'second'
    assert first.path == second.path == original.path
    assert (first.trim_start, first.start, first.duration) == (1, 3, .625)
    assert (second.trim_start, second.start, second.duration) == (1.625, 3.625, 1.375)
    assert original.duration == 2 and original.trim_start == 1
    assert first.title == second.title == 'Reviewed take'
    assert join_clips(first, second, first_frames=15000, second_frames=33000, sample_rate=RATE) == original


@pytest.mark.parametrize('seconds', [-1, 0, 2, 3, float('nan'), float('inf'), True, 1e-12, 1e100])
def test_split_invalid_boundary_cannot_create_a_piece(seconds):
    with pytest.raises(ValueError):
        split_clip(clip(), seconds, frames=48000, sample_rate=RATE)


@pytest.mark.parametrize('seconds', [.25, 1.75])
def test_split_inside_existing_fade_is_refused_without_reshaping_it(seconds):
    with pytest.raises(ValueError, match='inside an existing fade'):
        split_clip(clip(fade_in=.5, fade_out=.5), seconds, frames=48000, sample_rate=RATE)


@pytest.mark.parametrize('seconds', [.5, 1.5])
def test_completed_outer_fades_survive_split_and_rejoin(seconds):
    original = clip(fade_in=.5, fade_out=.5)
    first, second = split_clip(original, seconds, frames=48000, sample_rate=RATE)
    assert (first.fade_in, first.fade_out, second.fade_in, second.fade_out) == (.5, 0, 0, .5)
    assert join_clips(first, second, first_frames=round(seconds * RATE),
        second_frames=round((2 - seconds) * RATE), sample_rate=RATE) == original


@pytest.mark.parametrize('mode', ['linear', 'hold'])
@pytest.mark.parametrize('offset', [.125, .75, 1.75, 2.5])
def test_curve_shift_matches_independent_piecewise_function_at_every_sample(mode, offset):
    effect = envelope_effect('gain_envelope', ((0, -12), (1, 0), (2, -6)), interpolation=mode)
    changed = read_envelope(shifted_curve(effect, round(offset * RATE), RATE), RATE)
    for index in range(RATE):
        time = offset + index / RATE
        if mode == 'hold':
            expected = -12 if time < 1 else 0 if time < 2 else -6
        else:
            expected = -12 + 12 * time if time < 1 else -6 * (time - 1) if time < 2 else -6
        assert math.isclose(changed.at(index), expected, abs_tol=1e-10)


@pytest.mark.parametrize('mode', ['linear', 'hold'])
def test_pan_curve_split_rejoins_without_resetting_original_motion(mode):
    effect = envelope_effect('pan_envelope', ((0, -1), (.75, .5), (1.5, 1)), interpolation=mode)
    original = clip(effects=(effect,))
    first, second = split_clip(original, .5, frames=48000, sample_rate=RATE)
    assert read_envelope(second.effects[0], RATE).values[0] == (-1 if mode == 'hold' else 0)
    assert join_clips(first, second, first_frames=12000, second_frames=36000, sample_rate=RATE) == original
    changed = replace(second, effects=(envelope_effect('pan_envelope', ((0, 0), (1, -1))),))
    with pytest.raises(ValueError, match='does not continue'):
        join_clips(first, changed, first_frames=12000, second_frames=36000, sample_rate=RATE)


def test_eight_point_shift_stays_in_existing_wire_bound_and_bypass_is_unchanged():
    curve = envelope_effect('gain_envelope', tuple((index, -index) for index in range(8)))
    disabled = envelope_effect('pan_envelope', ((0, 0), (1, 1)), enabled=False)
    original = clip(effects=(curve, disabled, Effect('gain', gain_db=-2)))
    first, second = split_clip(original, .75, frames=48000, sample_rate=RATE)
    assert len(read_envelope(second.effects[0], RATE).frames) == 8
    assert second.effects[1:] == original.effects[1:]
    assert join_clips(first, second, first_frames=18000, second_frames=30000, sample_rate=RATE) == original


@pytest.mark.parametrize('kind', ['compressor', 'reverb', 'loudness', 'equalizer'])
def test_stateful_clip_processing_requires_explicit_render_before_edit(kind):
    original = clip(effects=(Effect(kind),))
    with pytest.raises(ValueError, match='Render stateful'):
        split_clip(original, 1, frames=48000, sample_rate=RATE)
    with pytest.raises(ValueError, match='Render stateful'):
        cut_clip(original, .5, 1, frames=48000, sample_rate=RATE)


@pytest.mark.parametrize('start,end,expected', [
    (0, .5, ((3.5, 1.5, 1.5),)),
    (1.5, 2, ((3, 1, 1.5),)),
    (.5, 1.5, ((3, 1, .5), (4.5, 2.5, .5))),
    (0, 2, ()),
])
def test_cut_leaves_original_scene_time_and_source_ranges(start, end, expected):
    original = clip()
    pieces = cut_clip(original, start, end, frames=48000, sample_rate=RATE, new_id='tail')
    assert tuple((item.start, item.trim_start, item.duration) for item in pieces) == expected
    assert all(item.path == original.path for item in pieces)
    assert len({item.id for item in pieces}) == len(pieces)
    if pieces:
        assert pieces[0].id == original.id
    assert original.duration == 2


@pytest.mark.parametrize('start,end', [(-1, 1), (1, 1), (2, 1), (0, 3), (True, 1), (0, float('nan')), (0, 1e-12)])
def test_invalid_cut_is_refused(start, end):
    with pytest.raises(ValueError):
        cut_clip(clip(), start, end, frames=48000, sample_rate=RATE)


@pytest.mark.parametrize('settings', [
    {'path': 'inputs/' + 'b' * 64 + '.audio'},
    {'start': 4.1}, {'trim_start': 2.1}, {'fade_in': .1},
    {'gain_db': -2}, {'pan': .5}, {'role': 'music'}, {'id': 'original'},
])
def test_join_cannot_discard_different_content_routing_or_internal_fade(settings):
    first, second = split_clip(clip(), 1, frames=48000, sample_rate=RATE, new_id='second')
    changed = replace(second, **settings)
    with pytest.raises(ValueError):
        join_clips(first, changed, first_frames=24000, second_frames=24000, sample_rate=RATE)


def test_join_keeps_external_fades_and_refuses_changed_bypassed_controls():
    original = clip(effects=(Effect('gain', gain_db=-3, enabled=False),))
    first, second = split_clip(original, 1, frames=48000, sample_rate=RATE)
    changed = replace(second, effects=(Effect('gain', gain_db=-6, enabled=False),))
    with pytest.raises(ValueError, match='settings differ'):
        join_clips(first, changed, first_frames=24000, second_frames=24000, sample_rate=RATE)


@pytest.mark.parametrize('frames,rate,identity', [(0, RATE, 'tail'), (True, RATE, 'tail'), (48000, True, 'tail'), (48000, RATE, 'original')])
def test_invalid_inspected_geometry_or_new_identity_is_refused(frames, rate, identity):
    with pytest.raises(ValueError):
        split_clip(clip(), 1, frames=frames, sample_rate=rate, new_id=identity)

@pytest.mark.parametrize('half_sample', [.5, 1.5, 2.5])
def test_split_and_prefix_cut_use_one_captured_integer_scene_origin(half_sample):
    original = replace(clip(), start=half_sample / RATE)
    first, second = split_clip(original, 1 / RATE, frames=48000, sample_rate=RATE)
    expected = round(half_sample)
    assert round(first.start * RATE) == expected
    assert round(second.start * RATE) == expected + 1
    remaining, = cut_clip(original, 0, 1 / RATE, frames=48000, sample_rate=RATE)
    assert round(remaining.start * RATE) == expected + 1


@pytest.mark.parametrize('field', ['trim_start', 'duration'])
def test_unaligned_source_selection_requires_explicit_conversion(field):
    original = replace(clip(), **{field: (1 if field == 'trim_start' else 2) + .5 / RATE})
    with pytest.raises(ValueError, match='original sample grid'):
        split_clip(original, 1, frames=48000, sample_rate=RATE)


def test_fractional_scene_end_that_changes_rounding_is_explicitly_refused():
    original = replace(clip(), start=.5 / RATE, duration=3 / RATE)
    with pytest.raises(ValueError, match='rounded duration'):
        split_clip(original, 1 / RATE, frames=3, sample_rate=RATE)


def _crossfade_pair():
    first = clip(title='First retained selection', gain_db=-3, pan=-1, role='microphone')
    second = Cue('following', 'inputs/' + 'b' * 64 + '.audio', start=5,
        trim_start=2, duration=3, gain_db=-7, pan=1, role='sfx',
        title='Second retained selection')
    return first, second


@pytest.mark.parametrize('seconds,outer', [(1 / RATE, .25), (.5, .25), (1.75, .25), (2, 0)])
def test_crossfade_keeps_both_original_selections_and_outer_ramps(seconds, outer):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    curve = envelope_effect('gain_envelope', ((0, -6), (1, 0)))
    bypass = Effect('reverb', enabled=False)
    first = replace(first, fade_in=outer, effects=(curve, bypass))
    second = replace(second, fade_out=outer)
    left, right = crossfade_clips(first, second, seconds,
        first_frames=48000, second_frames=72000, sample_rate=RATE)
    assert replace(left, fade_out=0) == first
    assert replace(right, start=5, fade_in=0) == second
    assert (left.fade_out, right.fade_in) == (seconds, seconds)
    assert round(right.start * RATE) == 5 * RATE - round(seconds * RATE)
    assert left.effects == first.effects and right.effects == second.effects
    assert first.fade_out == second.fade_in == 0 and second.start == 5


@pytest.mark.parametrize('seconds', [-1, 0, True, float('nan'), float('inf'),
    1e-12, 1.5 / RATE, 2 + 1 / RATE, 1e100])
def test_crossfade_rejects_invalid_or_unaligned_overlap(seconds):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    with pytest.raises(ValueError):
        crossfade_clips(first, second, seconds,
            first_frames=48000, second_frames=72000, sample_rate=RATE)


@pytest.mark.parametrize('change', [
    'same-id', 'gap', 'overlap', 'reverse', 'unaligned-left', 'unaligned-right'])
def test_crossfade_requires_distinct_contiguous_sample_aligned_scene_pair(change):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    if change == 'same-id':
        second = replace(second, id=first.id)
    elif change == 'gap':
        second = replace(second, start=5 + 1 / RATE)
    elif change == 'overlap':
        second = replace(second, start=5 - 1 / RATE)
    elif change == 'reverse':
        first, second = replace(second, duration=2), replace(first, duration=3)
    elif change == 'unaligned-left':
        first = replace(first, start=3 + .5 / RATE)
    else:
        second = replace(second, start=5 + .5 / RATE)
    with pytest.raises(ValueError):
        crossfade_clips(first, second, .5,
            first_frames=48000, second_frames=72000, sample_rate=RATE)


@pytest.mark.parametrize('change', ['left-boundary', 'right-boundary', 'left-outer', 'right-outer'])
def test_crossfade_never_replaces_boundary_or_overlapping_outer_fades(change):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    if change == 'left-boundary':
        first = replace(first, fade_out=.1)
    elif change == 'right-boundary':
        second = replace(second, fade_in=.1)
    elif change == 'left-outer':
        first = replace(first, fade_in=1.75)
    else:
        second = replace(second, fade_out=2.75)
    with pytest.raises(ValueError, match='fade'):
        crossfade_clips(first, second, .5,
            first_frames=48000, second_frames=72000, sample_rate=RATE)


@pytest.mark.parametrize('kind', ['compressor', 'reverb', 'equalizer', 'loudness'])
def test_crossfade_retains_existing_editable_effect_boundary(kind):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    second = replace(second, effects=(Effect(kind),))
    with pytest.raises(ValueError, match='Render stateful'):
        crossfade_clips(first, second, .5,
            first_frames=48000, second_frames=72000, sample_rate=RATE)


@pytest.mark.parametrize('first_frames,second_frames', [
    (0, 72000), (True, 72000), (47999, 72000), (48000, 71999)])
def test_crossfade_requires_exact_inspected_selected_lengths(first_frames, second_frames):
    from carr_library.studio.clip_operations import crossfade_clips
    first, second = _crossfade_pair()
    with pytest.raises(ValueError):
        crossfade_clips(first, second, .5,
            first_frames=first_frames, second_frames=second_frames, sample_rate=RATE)
