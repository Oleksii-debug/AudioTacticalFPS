"""Real spoken-line revisions, approval identities and portable library recovery."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup
from carr_library.speech.reading import PronunciationRule
from carr_library.studio.project import Cue, Project, parse_script
from carr_library.studio.pronunciation import StudioPronunciations, SpokenLineDraft
from carr_library.ui.pronunciation_dialog import ReadingSnapshot, preview_pronunciations, apply_pronunciation_preview
from carr_library.ui.studio_dialog import StudioController


SCRIPT = '[Scene: Original scene]\nNarrator: Dr. One carries 12 kg.\nReader: Dr. Two waits.\n[Scene: Closing]\nNarrator: Original ending.'


def configured(tmp_path, *, clips=True):
    library = LibraryApp(tmp_path / 'library')
    library.create_workspace('Synthetic spoken-line acceptance')
    editor = StudioPronunciations(library.root)
    project = parse_script(SCRIPT, title='Original performance', voice_ids=('fixture:one', 'fixture:two'), mode='authored')
    scene = project.scenes[0]
    line = replace(scene.lines[0], pause_after=.7, source_ref='retained-original-location')
    cues = (Cue('saved-clip', str(tmp_path / 'retained.audio'), start=.4, trim_start=.2,
                duration=.5, fade_in=.1, fade_out=.1, role='microphone', title='Saved original clip'),) if clips else ()
    scene = replace(scene, lines=(line, *scene.lines[1:]), cues=cues)
    project = replace(project, roles=tuple(replace(role, language='en') for role in project.roles),
                      scenes=(scene, *project.scenes[1:]), sample_rate=48000, schema=2)
    editor.projects.save(project)
    draft = editor.open_line(project, scene.id, line.id)
    return library, editor, project, draft


def apply_rules(draft):
    snapshot = ReadingSnapshot.capture(draft.document, (0, 0), language=draft.role.language,
                                       voice_id=draft.role.voice_id)
    preview = preview_pronunciations(snapshot, (
        PronunciationRule('Dr.', 'Doctor', voice_id='fixture:one'),
        PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True, language='en')),
        language=draft.role.language)
    apply_pronunciation_preview(draft.document, preview, snapshot)


def test_apply_is_a_local_draft_then_one_saved_revision_with_exact_history(tmp_path):
    library, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    assert draft.document.text == 'Doctor One carries 12 kilograms.' and draft.dirty
    assert editor.projects.load(project.id) == project
    changed = editor.save_line(draft)
    assert changed.before == project and changed.after.revision == project.revision + 1
    expected_line = replace(project.scenes[0].lines[0], text=draft.document.text)
    expected_scene = replace(project.scenes[0], lines=(expected_line, *project.scenes[0].lines[1:]))
    assert changed.after == replace(project, revision=2, scenes=(expected_scene, *project.scenes[1:]))
    history = editor.projects.directory(project.id) / 'revisions'
    files = list(history.iterdir())
    assert len(files) == 1 and Project.from_dict(read_json(files[0])) == project
    assert changed.after.original_script == SCRIPT
    assert changed.after.scenes[0].lines[0].source_ref == draft.line.source_ref
    assert changed.after.scenes[0].cues == project.scenes[0].cues
    assert draft.document.source_text == draft.line.text


def test_undo_saved_line_is_another_revision_and_preserves_the_other_performance(tmp_path):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    changed = editor.save_line(draft)
    undone = editor.undo_line(changed)
    assert undone.after == replace(project, revision=3)
    assert editor.projects.load(project.id) == undone.after
    previous = {Project.from_dict(read_json(path)).fingerprint
                for path in (editor.projects.directory(project.id) / 'revisions').iterdir()}
    assert previous == {project.fingerprint, changed.after.fingerprint}


def test_reader_undo_before_save_is_a_noop_project_save(tmp_path):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    assert draft.document.undo() and not draft.dirty
    unchanged = editor.save_line(draft)
    assert unchanged.before == unchanged.after == project
    assert not (editor.projects.directory(project.id) / 'revisions').exists()
    with pytest.raises(ValueError, match='no saved'):
        editor.undo_line(unchanged)


@pytest.mark.parametrize('change', ['new_revision', 'same_revision', 'role', 'other_line'])
def test_stale_complete_project_identity_blocks_save_without_partial_history(tmp_path, change):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    if change == 'new_revision':
        newer = replace(project, title='Changed title', revision=2)
        editor.projects.save(newer, expected_revision=1)
    elif change == 'role':
        newer = replace(project, roles=(replace(project.roles[0], voice_id='different-voice'), *project.roles[1:]))
    elif change == 'other_line':
        scene = project.scenes[0]
        newer = replace(project, scenes=(replace(scene, lines=(scene.lines[0],
            replace(scene.lines[1], text='Other changed line.'))), *project.scenes[1:]))
    else:
        newer = replace(project, title='Same revision, different content')
    if change != 'new_revision':
        atomic_json(editor.projects.directory(project.id) / 'project.json', newer.to_dict(), replace=True)
    with pytest.raises(ValueError, match='changed'):
        editor.save_line(draft)
    assert editor.projects.load(project.id) == newer
    if change != 'new_revision':
        assert not (editor.projects.directory(project.id) / 'revisions').exists()


def test_same_revision_change_at_the_save_boundary_is_rejected_under_existing_lock(tmp_path, monkeypatch):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    original_save = editor.projects.save
    outsider = replace(project, title='Changed after preliminary review')
    def replaced_before_store(candidate, **kwargs):
        atomic_json(editor.projects.directory(project.id) / 'project.json', outsider.to_dict(), replace=True)
        return original_save(candidate, **kwargs)
    monkeypatch.setattr(editor.projects, 'save', replaced_before_store)
    with pytest.raises(ValueError, match='content changed'):
        editor.save_line(draft)
    assert editor.projects.load(project.id) == outsider
    assert not (editor.projects.directory(project.id) / 'revisions').exists()


def test_old_project_store_callers_keep_revision_contract_and_fingerprint_is_additive(tmp_path):
    _, editor, project, _ = configured(tmp_path)
    second = replace(project, revision=2, title='Ordinary saved edit')
    assert editor.projects.save(second, expected_revision=1) == second
    with pytest.raises(ValueError, match='changed'):
        editor.projects.save(replace(second, revision=3), expected_revision=1)
    with pytest.raises(ValueError, match='content changed'):
        editor.projects.save(replace(second, revision=3), expected_fingerprint=project.fingerprint)
    with pytest.raises(ValueError, match='fingerprint'):
        editor.projects.save(replace(second, revision=3), expected_fingerprint='not-a-digest')
    assert editor.projects.load(project.id) == second


def test_later_edit_blocks_saved_undo_instead_of_overwriting_other_work(tmp_path):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    changed = editor.save_line(draft)
    newer = replace(changed.after, revision=3, title='Subsequent title change')
    editor.projects.save(newer, expected_revision=2)
    with pytest.raises(ValueError, match='changed'):
        editor.undo_line(changed)
    assert editor.projects.load(project.id) == newer


@pytest.mark.parametrize('failure', ['cancelled', 'guard', 'wrong_source', 'too_long'])
def test_invalid_or_cancelled_publication_keeps_the_original_revision(tmp_path, failure):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    options = {}
    if failure == 'cancelled':
        options['cancelled'] = lambda: True
    elif failure == 'guard':
        def guard(): raise ValueError('Library changed.')
        options['guard'] = guard
    elif failure == 'wrong_source':
        from carr_library.speech import ReadingDocument
        draft = replace(draft, document=ReadingDocument('Different line source'))
    else:
        draft.document.set_text('x' * 100001)
    with pytest.raises((ValueError, InterruptedError)):
        editor.save_line(draft, **options)
    assert editor.projects.load(project.id) == project
    assert not (editor.projects.directory(project.id) / 'revisions').exists()


def test_cancel_after_atomic_save_keeps_the_committed_change(tmp_path, monkeypatch):
    _, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    cancelled = Event()
    original_save = editor.projects.save
    def committed(candidate, **kwargs):
        result = original_save(candidate, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(editor.projects, 'save', committed)
    result = editor.save_line(draft, cancelled=cancelled.is_set)
    assert result.after.revision == 2 and editor.projects.load(project.id) == result.after


def test_ordinary_studio_save_keeps_the_saved_spoken_line_and_source_script(tmp_path):
    library, editor, project, draft = configured(tmp_path)
    apply_rules(draft)
    changed = editor.save_line(draft)
    controller = StudioController(library)
    controller.project = changed.after
    assert controller.save(SCRIPT, project.title, ('other-default',)) == changed.after
    assert controller.project.scenes[0].lines[0].text == 'Doctor One carries 12 kilograms.'
    assert controller.project.original_script == SCRIPT


def test_backup_delete_restore_retains_edited_lines_exact_history_and_original_asset(tmp_path):
    library, editor, project, draft = configured(tmp_path, clips=False)
    workspace = library.create_workspace('Original source')
    source = library.add_text(workspace, 'Immutable original', SCRIPT)
    original_path = library.assets.resolve(source['asset'])
    source_hash = sha256(original_path.read_bytes()).hexdigest()
    apply_rules(draft)
    changed = editor.save_line(draft)
    manifest = library.backup(tmp_path / 'backup')
    shutil.rmtree(library.root)
    restored = tmp_path / 'restored'
    restore_backup(tmp_path / 'backup', restored, expected_manifest_sha256=manifest)
    reopened = StudioPronunciations(restored)
    assert reopened.projects.load(project.id) == changed.after
    assert Project.from_dict(read_json(next((reopened.projects.directory(project.id) / 'revisions').iterdir()))) == project
    recovered_library = LibraryApp(restored)
    assert sha256(recovered_library.assets.resolve(source['asset']).read_bytes()).hexdigest() == source_hash
    assert reopened.open_line(changed.after, draft.scene_id, draft.line_id).document.text == draft.document.text


def test_project_list_is_cancellable_and_bounded(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    assert editor.list_projects() == (project,)
    with pytest.raises(InterruptedError):
        editor.list_projects(cancelled=lambda: True)
    editor.projects.save(replace(project, id='second-project'))
    monkeypatch.setattr('carr_library.studio.pronunciation.MAX_PROJECTS', 1)
    with pytest.raises(ValueError, match='budget'):
        editor.list_projects()


def test_project_list_aggregate_budget_rejects_multiple_projects_before_any_load(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    second = replace(project, id='second-project')
    editor.projects.save(second)
    total = sum((editor.projects.directory(item.id) / 'project.json').stat().st_size for item in (project, second))
    monkeypatch.setattr('carr_library.studio.pronunciation.MAX_PROJECT_LIST_BYTES', total - 1)
    def forbidden_load(identity):
        pytest.fail('A project was loaded before the complete catalog budget was admitted.')
    monkeypatch.setattr(editor.projects, 'load', forbidden_load)
    with pytest.raises(ValueError, match='aggregate review byte budget'):
        editor.list_projects()


def test_project_growth_after_catalog_preflight_cannot_bypass_aggregate_budget(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    path = editor.projects.directory(project.id) / 'project.json'
    monkeypatch.setattr('carr_library.studio.pronunciation.MAX_PROJECT_LIST_BYTES', path.stat().st_size + 50)
    load = editor.projects.load
    changed = replace(project, original_script=project.original_script + ('expanded ' * 100))
    def grew(identity):
        atomic_json(path, changed.to_dict(), replace=True)
        return load(identity)
    monkeypatch.setattr(editor.projects, 'load', grew)
    with pytest.raises(ValueError, match='grew beyond.*aggregate'):
        editor.list_projects()
    assert load(project.id) == changed


def test_project_list_rejects_link_or_junction_before_external_load(tmp_path, monkeypatch):
    _, editor, _, _ = configured(tmp_path)
    target = tmp_path / 'external'; target.mkdir()
    link = editor.projects.root / 'linked-project'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    loaded = []
    monkeypatch.setattr(editor.projects, 'load', lambda identity: loaded.append(identity))
    with pytest.raises(ValueError, match='link|regular'):
        editor.list_projects()
    assert loaded == []


def test_project_inventory_counts_ignored_entries_before_loading(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    before = (editor.projects.directory(project.id) / 'project.json').read_bytes()
    for index in range(4):
        (editor.projects.root / f'ordinary-{index}.txt').write_text('Unrelated retained entry')
    monkeypatch.setattr('carr_library.studio.pronunciation.MAX_PROJECT_ENTRIES', 3)
    monkeypatch.setattr(editor.projects, 'load', lambda identity: pytest.fail('Oversized inventory admitted a project.'))
    with pytest.raises(ValueError, match='total entry budget'):
        editor.list_projects()
    assert (editor.projects.directory(project.id) / 'project.json').read_bytes() == before
    assert len(list(editor.projects.root.iterdir())) == 5


def test_project_inventory_propagates_unreadable_scan_before_any_load(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    import carr_library.studio.pronunciation as implementation
    actual = os.scandir
    def unreadable(path):
        if Path(path) == editor.projects.root:
            raise PermissionError('Synthetic unreadable project inventory')
        return actual(path)
    monkeypatch.setattr(implementation.os, 'scandir', unreadable)
    monkeypatch.setattr(editor.projects, 'load', lambda identity: pytest.fail('Unreadable inventory admitted a project.'))
    with pytest.raises(PermissionError, match='unreadable'):
        editor.list_projects()
    assert Project.from_dict(read_json(editor.projects.directory(project.id) / 'project.json')) == project


def test_project_inventory_cancellation_is_checked_while_enumerating(tmp_path, monkeypatch):
    _, editor, project, _ = configured(tmp_path)
    for index in range(5):
        (editor.projects.root / f'ordinary-{index}.txt').write_text('Retained')
    calls = []
    def stopped():
        calls.append(True)
        return len(calls) == 3
    monkeypatch.setattr(editor.projects, 'load', lambda identity: pytest.fail('Cancelled inventory admitted a project.'))
    with pytest.raises(InterruptedError):
        editor.list_projects(cancelled=stopped)
    assert len(calls) == 3
    assert len(list(editor.projects.root.iterdir())) == 6
    assert Project.from_dict(read_json(editor.projects.directory(project.id) / 'project.json')) == project

