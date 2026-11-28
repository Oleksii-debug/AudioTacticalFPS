"""Keyboard view controller contracts without pretending to run native NVDA."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.studio.project import Music, Project, ProjectStore, parse_script
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


@pytest.mark.parametrize('kwargs', [dict(mood='unknown mood'), dict(seed=2**31), dict(seed=True)])
def test_music_plan_rejects_values_not_supported_by_runtime(kwargs):
    with pytest.raises(ValueError):
        Music(**kwargs)


@pytest.mark.parametrize('change', ['script', 'title', 'noop'])
def test_script_controller_preserves_exact_saved_markers_and_noop_revision(tmp_path, change):
    ui = controller(tmp_path)
    try:
        original = ui.save('[Scene: Arrival]\nNarrator: Original words.', 'Marked project', ('voice-a',))
        marked = ui.service.projects.add_marker(original, scene_id=original.scenes[0].id,
            position=.5, label='Keep this position', marker_id='marker-note')
        ui.project = marked
        text = marked.original_script.replace('Original words.', 'Revised words.') if change == 'script' else marked.original_script
        title = 'Revised title' if change == 'title' else marked.title
        saved = ui.save(text, title, ('voice-a',))
        assert saved.id == marked.id and saved.schema == 4
        assert saved.markers == marked.markers
        assert saved.revision == marked.revision + (change != 'noop')
        assert ui.service.projects.load(saved.id) == saved
        assert saved.scenes[0].id == marked.scenes[0].id
        if change == 'noop':
            assert saved.to_dict() == marked.to_dict() and saved.fingerprint == marked.fingerprint
        else:
            history = ui.service.projects.directory(saved.id) / 'revisions'
            assert any(Project.from_dict(json.loads(path.read_text(encoding='utf-8'))) == marked
                       for path in history.glob('*.json'))
    finally:
        ui.service.close()


@pytest.mark.parametrize('change', ['rename-marked-scene', 'remove-marked-scene', 'reuse-removed-scene-id'])
def test_script_controller_refuses_marker_retargeting_and_retains_invalid_draft(tmp_path, change):
    ui = controller(tmp_path)
    try:
        text = '[Scene: Arrival]\nNarrator: First words.\n[Scene: Outro]\nNarrator: Last words.'
        original = ui.save(text, 'Marked project', ('voice-a',))
        selected = original.scenes[1] if change == 'remove-marked-scene' else original.scenes[0]
        marked = ui.service.projects.add_marker(original, scene_id=selected.id,
            position=.5, label='Bound scene position', marker_id='marker-note')
        ui.project = marked
        if change == 'rename-marked-scene':
            edited = text.replace('[Scene: Arrival]', '[Scene: Another scene]')
        elif change == 'remove-marked-scene':
            edited = '[Scene: Arrival]\nNarrator: First words.'
        else:
            edited = '[Scene: Outro]\nNarrator: Last words.'
        with pytest.raises(ValueError, match='named markers'):
            ui.save(edited, marked.title, ('voice-a',))
        assert ui.project == marked
        assert ui.service.projects.load(marked.id) == marked
        assert json.loads(ui.draft.read_text(encoding='utf-8'))['text'] == edited
        assert marked.markers[0].scene_id == selected.id
    finally:
        ui.service.close()


@pytest.mark.parametrize('change', ['remove-first', 'reorder'])
def test_duplicate_scene_titles_never_retarget_existing_markers(tmp_path, change):
    ui = controller(tmp_path)
    try:
        text = '[Scene: Same]\nNarrator: First scene words.\n[Scene: Same]\nNarrator: Second scene words.'
        original = ui.save(text, 'Repeated scene titles', ('voice-a',))
        marked = ui.service.projects.add_marker(original, scene_id=original.scenes[0].id,
            position=.5, label='First scene only', marker_id='marker-note')
        ui.project = marked
        edited = ('[Scene: Same]\nNarrator: Second scene words.' if change == 'remove-first' else
                  '[Scene: Same]\nNarrator: Second scene words.\n[Scene: Same]\nNarrator: First scene words.')
        with pytest.raises(ValueError, match='[Nn]amed markers'):
            ui.save(edited, marked.title, ('voice-a',))
        assert ui.project == marked and ui.service.projects.load(marked.id) == marked
        assert json.loads(ui.draft.read_text(encoding='utf-8'))['text'] == edited
        assert marked.scenes[0].lines[0].text == 'First scene words.'
    finally:
        ui.service.close()


@pytest.mark.parametrize('change', ['title', 'noop'])
def test_duplicate_scene_titles_allow_unchanged_script_and_project_title_edits(tmp_path, change):
    ui = controller(tmp_path)
    try:
        text = '[Scene: Same]\nNarrator: First scene words.\n[Scene: Same]\nNarrator: Second scene words.'
        original = ui.save(text, 'Repeated scene titles', ('voice-a',))
        marked = ui.service.projects.add_marker(original, scene_id=original.scenes[0].id,
            position=.5, label='First scene only', marker_id='marker-note')
        ui.project = marked
        title = 'Reviewed project title' if change == 'title' else marked.title
        saved = ui.save(text, title, ('voice-a',))
        assert saved.markers == marked.markers and saved.scenes == marked.scenes
        assert saved.revision == marked.revision + (change == 'title')
        assert ui.service.projects.load(saved.id) == saved
        if change == 'noop':
            assert saved.fingerprint == marked.fingerprint
    finally:
        ui.service.close()
