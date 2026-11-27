"""Check frame-exact scheduling without claiming unavailable neural inference."""
from dataclasses import replace

import pytest

from carr_library.audio import MusicSpec
from carr_library.studio.project import Project, parse_script
from carr_library.studio.render import music_segments


@pytest.mark.parametrize('duration', [1.01, 30, 30.0001, 60.005, 3600.1, 10800, 36000.25])
def test_long_scores_have_bounded_independent_chunks_and_exact_total_frames(duration):
    rate = 24000
    spec = MusicSpec(duration=duration, engine='musicgen', seed=2**31 - 1)
    segments = list(music_segments(spec, rate, 30))
    assert sum(round(item.duration * rate) for item in segments) == round(duration * rate)
    assert all(.01 <= item.duration <= 30 for item in segments)
    assert len({item.seed for item in segments}) == len(segments)
    assert all(item.mood == spec.mood and item.tempo == spec.tempo for item in segments)


def test_unchanged_prefix_survives_extension_for_checkpoint_reuse():
    spec = MusicSpec(duration=120, engine='ace-step', seed=17, prompt='quiet cello')
    before = list(music_segments(spec, 24000, 30))
    after = list(music_segments(replace(spec, duration=180), 24000, 30))
    assert before == after[:len(before)]
    assert all(part.prompt == spec.prompt for part in after)


def test_studio_neural_prompt_preserves_project_roundtrip():
    script = '[Music: suspense; engine=ace-step]\n[Опис музики: Тихе тремоло.]\nРепліка.'
    project = parse_script(script)
    assert Project.from_dict(project.to_dict()).scenes[0].music.prompt == 'Тихе тремоло.'
    ordinary = parse_script('[Music: calm]\nРепліка.')
    assert 'prompt' not in ordinary.to_dict()['scenes'][0]['music']
    with pytest.raises(ValueError, match='local neural engine'):
        parse_script('[Music: calm]\n[Music prompt: Strings.]\nA line.')
