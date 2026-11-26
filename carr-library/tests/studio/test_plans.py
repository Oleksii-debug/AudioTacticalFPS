from dataclasses import replace
import json

import pytest

from carr_library.studio import Budget, Line, MusicCue, PlanError, Project, Role, Scene, load_project, narration_project
from carr_library.studio.models import split_text
from carr_library.studio.script import dialogue_project


def test_roundtrip_preserves_multilingual_source_and_no_ten_minute_ceiling():
    text = 'Українська — слово.\r\nSlovenčina: ľščťžýáíé.\nEnglish exact source.'
    project = Project('10 hours', (Role('reader', 'espeak:uk'),),
                      (Scene('long', 'Long', (Line('words', 'reader', text),), duration=36000),),
                      budget=Budget(disk_bytes=80 * 1024**3))
    assert Project.from_dict(project.to_dict()) == project
    assert project.scenes[0].lines[0].text == text
    assert len(project.revision) == 64
    assert replace(project, title='Revised title').revision != project.revision


@pytest.mark.parametrize('text', ['A ' * 8000, '漢字' * 7000, '\r\n  Alpha\t word.  Beta!\n' * 200,
                                 'Українська e\u0301 Straße. ' * 600])
def test_bounded_chunks_cover_every_original_codepoint_without_edits(text):
    chunks = tuple(split_text(text, 128))
    assert ''.join(c[2] for c in chunks) == text
    cursor = 0
    for start, end, chunk in chunks:
        assert start == cursor and chunk == text[start:end] and 0 < len(chunk) <= 128
        cursor = end
    assert cursor == len(text)


@pytest.mark.parametrize('change', [dict(sample_rate=True), dict(channels=True), dict(schema_version=True),
                                  dict(sample_rate=12345), dict(channels=8), dict(budget='no')])
def test_invalid_plan_numbers_and_types_are_rejected(change):
    project = narration_project('Exact text.', 'test:voice')
    with pytest.raises((PlanError, TypeError)):
        replace(project, **change)


def test_unassigned_speaker_and_duplicate_roles_never_guess_a_voice():
    with pytest.raises(PlanError, match='Unknown role'):
        Project('X', (Role('a', 'voice'),), (Scene('s', 'S', (Line('l', 'b', 'Private words.'),)),))
    with pytest.raises(PlanError, match='Assign a voice'):
        dialogue_project('Unknown: words', {'Narrator': Role('n', 'voice')})
    with pytest.raises(PlanError, match='distinct'):
        dialogue_project('Reader: words', {'Reader': Role('a', 'voice'), 'reader': Role('b', 'voice')})


def test_dialogue_creates_roles_chapters_and_continuations_without_running_text():
    script = '## Introduction\nNarrator: This is a script.\nAnother sentence.\nLeft: Left words.\n## Ending\nRight: Right words.'
    roles = {'Narrator': Role('n', 'voice'), 'Left': Role('l', 'voice', pan=-1), 'Right': Role('r', 'voice', pan=1)}
    result = dialogue_project(script, roles)
    assert [s.title for s in result.scenes] == ['Introduction', 'Ending']
    assert result.scenes[0].lines[0].text == 'This is a script.\nAnother sentence.'
    assert result.scenes[1].lines[0].role == 'r'
    assert result.source_reference['kind'] == 'user-authored-dialogue'
    assert len(result.source_reference['script_sha256']) == 64


def test_json_rejects_duplicates_unknown_fields_and_nonfinite_values(tmp_path):
    path = tmp_path / 'script.json'
    path.write_text('{"title":"one","title":"two"}')
    with pytest.raises(PlanError, match='Duplicate'):
        load_project(path)
    value = narration_project('Text', 'voice').to_dict()
    path.write_text(json.dumps({**value, 'execute_shell': 'whoami'}))
    with pytest.raises(PlanError):
        load_project(path)
    value['roles'][0]['rate'] = float('nan')
    path.write_text(json.dumps(value))
    with pytest.raises(PlanError):
        load_project(path)


def test_musicgen_is_included_with_real_chunk_limit_but_unlimited_final_length():
    cue = MusicCue('score', 'Quiet instrumental', engine='musicgen', duration=36000)
    project = Project('Long music', (), (Scene('music', 'Music', music=(cue,)),))
    assert Project.from_dict(project.to_dict()) == project
    assert cue.duration == 36000 and cue.chunk_seconds == 30
    with pytest.raises(ValueError, match='chunk duration'):
        replace(cue, chunk_seconds=60)


def test_channel_routing_is_distinct_from_panning():
    assert Role('r', 'voice', routing='right').routing == 'right'
    with pytest.raises(PlanError, match='either pan'):
        Role('r', 'voice', routing='right', pan=-1)
