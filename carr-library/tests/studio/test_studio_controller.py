"""Keyboard view controller contracts without pretending to run native NVDA."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.studio.project import Cue, Music, Project, ProjectStore, parse_script
from carr_library.ui.studio_dialog import StudioController


def controller(tmp_path):
    return StudioController(SimpleNamespace(root=tmp_path))


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


def test_opened_authored_project_preserves_custom_cues_rate_and_source_refs_on_save(tmp_path):
    ui = controller(tmp_path)
    source = parse_script('Alice: Keep this exact performance.', mode='authored', voice_ids=('voice-a',))
    line = replace(source.scenes[0].lines[0], pause_after=1.75, source_ref='authenticated-position-7')
    scene = replace(source.scenes[0], lines=(line,), cues=(Cue('cue-one', str(tmp_path / 'ambience.wav')),))
    additional = replace(source.roles[0], id='another-role', name='ALICE', voice_id='voice-b')
    imported = replace(source, roles=(*source.roles, additional), sample_rate=48000, scenes=(scene,))
    ui.service.projects.save(imported)
    ui.project = imported
    assert ui.save(imported.original_script, imported.title, ('voice-a',)) == imported
    renamed = ui.save(imported.original_script, 'Renamed performance', ('voice-a',))
    assert renamed.mode == 'authored' and renamed.sample_rate == 48000
    assert renamed.scenes == imported.scenes
    assert renamed.revision == imported.revision + 1
    assert ui.service.projects.load(imported.id) == renamed


def test_edit_cannot_silently_reparse_an_imported_custom_scene(tmp_path):
    ui = controller(tmp_path)
    original = parse_script('An original line.', voice_ids=('voice-a',))
    scene = replace(original.scenes[0], minimum_duration=77)
    imported = replace(original, scenes=(scene,))
    ui.service.projects.save(imported)
    ui.project = imported
    with pytest.raises(ValueError, match='custom scenes'):
        ui.save('An edited line.', imported.title, ('voice-a',))
    assert ui.service.projects.load(imported.id) == imported
    assert json.loads(ui.draft.read_text())['text'] == 'An edited line.'


def test_controller_cas_rejects_an_older_job_snapshot_without_overwriting_newer_project(tmp_path):
    ui = controller(tmp_path)
    first = ui.save('An original line.', 'Original title', ('voice-a',))
    newer = replace(first, title='Newer title', revision=2)
    ui.service.projects.save(newer, expected_revision=1)
    with pytest.raises(ValueError, match='changed'):
        ui.save(first.original_script, 'Older job title', ('voice-a',))
    assert ui.service.projects.load(first.id) == newer


def test_first_save_binds_recovery_to_saved_project_and_preserves_export_choices(tmp_path):
    ui = controller(tmp_path)
    saved = ui.save('A recoverable narration.', 'Recovery', ('voice-a',), format='flac', stems=False)
    recovered = json.loads(ui.draft.read_text())
    assert recovered['project_id'] == saved.id
    assert recovered['format'] == 'flac' and recovered['stems'] is False
    assert recovered['text'] == saved.original_script


@pytest.mark.parametrize('kwargs', [dict(mood='unknown mood'), dict(seed=2**31), dict(seed=True)])
def test_music_plan_rejects_values_not_supported_by_runtime(kwargs):
    with pytest.raises(ValueError):
        Music(**kwargs)
