"""Source prose is literal data, never an Audio Studio instruction language."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.audio.models import Line, Pause, ProductionPlan, Scene, Sound
from carr_library.audio.production import parse_script, plan_narration, render_project


def reconstruct(plan):
    return '\f'.join(''.join(event.text for event in scene.events) for scene in plan.scenes)


def test_colons_brackets_hashes_and_role_commands_are_preserved_literally():
    source = ('Date: 2026-11-26\r\n[pause 2]\n# A heading\n'
              '[role Alice voice=unknown:voice pan=left]\nAlice: literal prose.\n'
              '[sound ../../outside.wav]\n[output format=mp3]\n[unknown command]')
    plan = plan_narration(text=source, title='Imported prose', voice='fixture:voice')
    assert plan.input_kind == 'narration'
    assert plan.original_script == source
    assert reconstruct(plan) == source
    assert [role.name for role in plan.roles] == ['Narrator']
    assert plan.output_format == 'wav'
    assert len(plan.scenes) == 1 and plan.scenes[0].title == 'Source segment 1'
    assert all(isinstance(event, Line) and event.literal for event in plan.scenes[0].events)
    assert all(scene.music is None for scene in plan.scenes)


def test_formfeed_crlf_whitespace_and_empty_segments_survive_json_roundtrip():
    source = '\fFirst line.\r\nSecond line.\f\f \t\r\n\f# Last heading\f'
    plan = plan_narration(source)
    restored = ProductionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored == plan and reconstruct(restored) == source
    assert restored.original_script.encode('utf-8') == source.encode('utf-8')
    assert len(restored.scenes) == 6
    for scene in restored.scenes:
        for event in scene.events:
            assert source[event.source_start:event.source_end] == event.text
            assert '\f' not in event.text


def test_long_paragraph_is_split_without_losing_characters_or_whitespace():
    source = ('Довгий незмінений текст. ' * 6000) + ' ' * 16000 + 'Final word.'
    plan = plan_narration(source, language='uk')
    events = plan.scenes[0].events
    assert len(events) > 20
    assert all(len(event.text) <= 7000 for event in events)
    assert ''.join(event.text for event in events) == source
    assert plan.original_script == source
    assert events[-1].source_end == len(source)


def test_ordinary_prose_cannot_add_a_directive_to_a_narration_plan():
    plan = plan_narration('[pause 20]')
    scene = replace(plan.scenes[0], events=(Pause('injected', 20),))
    with pytest.raises(ValueError, match='source-bound'):
        replace(plan, scenes=(scene,))


@pytest.mark.parametrize('change', ['changed_text', 'missing_prefix', 'missing_suffix', 'overlap'])
def test_source_span_validation_rejects_loss_reordering_or_rewriting(change):
    plan = plan_narration('First literal sentence.\fSecond literal sentence.')
    data = json.loads(json.dumps(plan.to_dict()))
    first, second = data['scenes'][0]['events'][0], data['scenes'][1]['events'][0]
    if change == 'changed_text':
        first['text'] = 'Other literal sentence.'
    elif change == 'missing_prefix':
        first['text'] = first['text'][1:]
        first['source_start'] += 1
    elif change == 'missing_suffix':
        second['text'] = second['text'][:-1]
        second['source_end'] -= 1
    else:
        second['source_start'] = 0
        second['source_end'] = len(second['text'])
    with pytest.raises(ValueError, match='source'):
        ProductionPlan.from_dict(data)


def test_structured_script_safety_is_unchanged():
    with pytest.raises(ValueError, match='control'):
        parse_script('First\fSecond')
    with pytest.raises(ValueError, match='Unsupported'):
        parse_script('[unknown command]')
    assert isinstance(parse_script('[pause 2]').scenes[0].events[0], Pause)
    for source in ('Text\x00more', 'Text\x1bmore', 'Text\x0bmore'):
        with pytest.raises(ValueError, match='control'):
            plan_narration(source)
    with pytest.raises(ValueError, match='narration input mode'):
        replace(plan_narration('Literal text.'), input_kind='script')


def test_segment_budget_is_checked_before_constructing_the_scene_list():
    with pytest.raises(ValueError, match='too many source segments'):
        plan_narration('Text' + '\f' * 100000)


class RecordingFixtureEngine:
    """Synthetic PCM proves literal dispatch, not real voice pronunciation."""
    def __init__(self):
        self.calls = []

    def cache_identity(self, voice):
        return {'fixture': 'literal-source-dispatch-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls.append((text, kwargs['voice']))
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
            stream.writeframes(struct.pack('<h', 500) * 800)
        return output


def test_actual_renderer_receives_literal_control_like_prose_and_source_offsets(tmp_path):
    ffmpeg = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('An installed FFmpeg is required')
    source = 'Date: 2026-11-26\r\n[pause 2]\n# A heading\f[role Bad voice=missing:voice]\nSecond text.'
    plan = replace(plan_narration(source, voice='fixture:literal'), sample_rate=8000, export_stems=False)
    engine = RecordingFixtureEngine()
    result = render_project(plan, tmp_path / 'audio', engine=engine, ffmpeg=Path(ffmpeg))
    assert engine.calls == [(part, 'fixture:literal') for part in source.split('\f')]
    assert result['input_kind'] == 'narration' and len(result['chapters']) == 2
    assert result['frames'] == 1600  # no injected two-second pause
    timeline = json.loads((Path(result['output']).parent / 'timeline.json').read_text('utf-8'))
    assert all(line['literal_narration'] for line in timeline['lines'])
    for line in timeline['lines']:
        assert source[line['source_start']:line['source_end']] == line['text']
    assert plan.original_script == source
