"""Keyboard view controller contracts without pretending to run native NVDA."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.studio.project import Music, Project, ProjectStore, parse_script
from carr_library.studio.pronunciation import StudioPronunciations
from carr_library.ui.studio_dialog import StudioController, _playback_ranges


def controller(tmp_path):
    return StudioController(SimpleNamespace(root=tmp_path))


def test_playback_ranges_follow_completed_timeline_and_selected_scene():
    result = {
        'duration': 5,
        'chapters': [{'id': 'first', 'title': 'Introduction', 'start': 0, 'end': 2},
                     {'id': 'second', 'title': 'Друга сцена', 'start': 2, 'end': 5}],
        'lines': [{'scene': 'first', 'line': 'line-1', 'text': 'First', 'start': 0.1, 'end': 1.9},
                  {'scene': 'second', 'line': 'line-1', 'text': 'Українська репліка', 'start': 2.5, 'end': 4}],
    }
    assert _playback_ranges(result) == [
        ('first', '1. Introduction', 0, 2), ('second', '2. Друга сцена', 2, 5),
    ]
    assert _playback_ranges(result, scene_id='second') == [('line-1', '1. Українська репліка', 2.5, 4)]
    assert _playback_ranges(result, scene_id='missing') == []
    result['lines'][1]['end'] = 5.0000000001
    assert _playback_ranges(result, scene_id='second')[0][-1] == 5
    result['lines'][1]['end'] = 6
    with pytest.raises(ValueError, match='invalid playback timing'):
        _playback_ranges(result, scene_id='second')


def test_per_line_delivery_round_trips_after_reopening(tmp_path):
    ui = controller(tmp_path)
    original = 'Narrator: Репліка.\n[Подача: rate=-2; volume=75; pitch=41; gain=-8; pause=0.6]\nДругий рядок.'
    project = ui.save(original, 'Delivery', ('voice-a',))
    project = ui.service.projects.load(project.id)
    line, plain_line = project.scenes[0].lines
    assert project.original_script == original
    assert (line.text, line.gain_db, line.pause_after) == ('Репліка.', -8, .6)
    settings = line.speech_settings(rate=3, volume=60)
    assert (settings.rate, settings.volume, settings.pitch) == (-2, 75, 41)
    fallback = plain_line.speech_settings(rate=3, volume=60)
    assert (fallback.rate, fallback.volume, fallback.pitch) == (3, 60, None)
    assert set(project.to_dict()['scenes'][0]['lines'][1]) == {
        'id', 'role', 'text', 'source_ref', 'pause_after',
    }


@pytest.mark.parametrize('direction', [
    'rate=11', 'rate=1.5', 'volume=-1', 'pitch=100', 'gain=nan',
    'pause=-0.1', 'rate=1; rate=2', 'path=audio.wav',
])
def test_invalid_line_delivery_leaves_existing_project_unchanged(tmp_path, direction):
    ui = controller(tmp_path)
    project = ui.save('Narrator: Original.', 'Delivery', ('voice-a',))
    with pytest.raises(ValueError):
        ui.save(f'Narrator: New voice.\n[Delivery: {direction}]', 'Delivery', ('voice-a',))
    assert ui.service.projects.load(project.id) == project


def test_plain_prose_colons_are_not_silently_removed_in_faithful_mode():
    original = 'Date: 2026-11-26\nRatio: 2:1\nhttps://example.invalid/reference\nA plain sentence.'
    project = parse_script(original, mode='faithful')
    assert '\n'.join(line.text for scene in project.scenes for line in scene.lines) == original
    assert project.original_script == original


def test_save_reopen_revision_and_noop_preserve_project_identity(tmp_path):
    ui = controller(tmp_path)
    first = ui.save('A plain narration.', 'A book', ('voice-a',))
    again = ui.save('A plain narration.', 'A book', ('voice-a',))
    assert again == first and again.revision == 1
    edited = ui.save('A changed narration.', 'A book', ('voice-a',))
    assert edited.id == first.id and edited.revision == 2
    assert ui.service.projects.load(first.id) == edited
    historical = list(ui.service.projects.directory(first.id).joinpath('revisions').glob('*.json'))
    assert len(historical) == 1
    assert Project.from_dict(json.loads(historical[0].read_text())) == first


def test_role_custom_voice_pan_gain_rate_volume_and_language_survive_save(tmp_path):
    ui = controller(tmp_path)
    first = ui.save('Narrator: An unchanged line.', 'Voice overrides', ('voice-a',))
    role = replace(first.roles[0], voice_id='voice-b', pan=-.6, gain_db=-11, rate=-2, volume=47, language='uk')
    altered = replace(first, roles=(role,), revision=2)
    ui.service.projects.save(altered, expected_revision=1)
    ui.project = altered
    saved = ui.save(first.original_script, first.title, ('voice-a',))
    assert saved.roles[0] == role
    assert saved.revision == 2


def test_invalid_script_draft_survives_without_replacing_valid_project(tmp_path):
    ui = controller(tmp_path)
    valid = ui.save('A valid sentence.', 'Valid project', ('voice-a',))
    invalid = '[Execute: rm -rf /]\nThis is untrusted text.'
    with pytest.raises(ValueError, match='direction'):
        ui.save(invalid, 'Working draft', ('voice-a',))
    draft = json.loads(ui.draft.read_text())
    assert draft['text'] == invalid
    assert ui.service.projects.load(valid.id) == valid
    assert ui.project == valid


def test_project_edit_detects_stale_revision(tmp_path):
    store = ProjectStore(tmp_path)
    original = parse_script('Original')
    store.save(original)
    revised = replace(original, title='New title', revision=2)
    store.save(revised, expected_revision=1)
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(revised, title='Stale title', revision=3), expected_revision=1)
    assert store.load(original.id) == revised


@pytest.mark.parametrize('kwargs', [dict(mood='unknown mood'), dict(seed=2**31), dict(seed=True)])
def test_music_plan_rejects_values_not_supported_by_runtime(kwargs):
    with pytest.raises(ValueError):
        Music(**kwargs)


@pytest.mark.parametrize('duplicate', [False, True])
def test_studio_reparse_keeps_an_independently_saved_spoken_edit(tmp_path, duplicate):
    ui = controller(tmp_path)
    passage = 'Narrator: A word with two readings.\n[Pronunciation: First reading.]'
    script = passage + '\nAn independent line.'
    first = ui.save(script, 'Book', ('fixture:voice',))
    service = StudioPronunciations(tmp_path)
    scene = first.scenes[0]
    draft = service.open_line(first, scene.id, scene.lines[0].id)
    draft.document.set_text('Reviewed spoken version.', label='Pronunciation review')
    ui.project = service.save_line(draft).after
    if duplicate:
        with pytest.raises(ValueError, match='unique'):
            ui.save(script + '\n' + passage, first.title, ('fixture:voice',))
        assert service.projects.load(first.id) == ui.project
    else:
        updated = 'A new introductory line.\n' + script
        saved = ui.save(updated, first.title, ('fixture:voice',))
        line = service.projects.load(first.id).scenes[0].lines[1]
        assert saved.original_script == updated
        assert line.text == 'A word with two readings.'
        assert line.spoken_text == 'Reviewed spoken version.'
        explicit = updated.replace('First reading.', 'New direct override.')
        saved = ui.save(explicit, first.title, ('fixture:voice',))
        assert saved.scenes[0].lines[1].spoken_text == 'New direct override.'
