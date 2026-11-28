"""Actual local files exercise project/draft publication and import boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from types import SimpleNamespace

import pytest

from carr_library.audio.narration import cue_derivative, set_cue_derivative
from carr_library.audio.projects import Cue, Music, ProjectStore, Settings, atomic_json, directory, local_path, parse_script, read_json
from carr_library.audio.voices import Voice
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.audio_studio import StudioController


@pytest.fixture
def registry():
    return SimpleNamespace(list_voices=lambda: (Voice('flite:kal','flite','Kal',('en',),(),'', ''),))


def linked(path, target):
    try:
        path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('This platform requires permission to create directory links')


def test_linked_ancestor_is_rejected_before_creating_any_directory(tmp_path):
    outside=tmp_path/'outside'
    outside.mkdir()
    alias=tmp_path/'alias'
    linked(alias,outside)
    for operation in (lambda:ProjectStore(alias/'new-library'), lambda:directory(alias/'another'/'nested')):
        with pytest.raises(ValueError,match='symbolic links|reparse'):
            operation()
    assert not tuple(outside.iterdir())


def test_swapped_project_parent_cannot_create_outside_the_library(tmp_path):
    store=ProjectStore(tmp_path/'library')
    outside=tmp_path/'outside'
    outside.mkdir()
    store.projects.rmdir()
    linked(store.projects,outside)
    with pytest.raises(ValueError,match='symbolic links|reparse'):
        store.create(parse_script('Narrator: Safe source.',['flite:kal']))
    assert not tuple(outside.iterdir())


@pytest.mark.parametrize('path',['//server/share/audio','\\\\server\\share\\audio','https://example.invalid/audio','../outside','bad\x00path'])
def test_nonlocal_or_traversal_paths_are_rejected(path):
    with pytest.raises(ValueError):
        local_path(path)


def test_export_and_atomic_new_file_never_replace_an_existing_destination(tmp_path):
    store=ProjectStore(tmp_path/'library')
    project=store.create(parse_script('Narrator: Original.',['flite:kal']))
    target=tmp_path/'existing.json'
    target.write_bytes(b'preserve this existing file')
    with pytest.raises(FileExistsError):
        store.export(project.id,target)
    with pytest.raises(FileExistsError):
        atomic_json(target,{'new':'data'},replace_existing=False)
    assert target.read_bytes()==b'preserve this existing file'


def test_two_windows_keep_independent_drafts_when_one_becomes_stale(tmp_path,registry):
    first=StudioController(tmp_path/'library',registry)
    original=first.save('Original','Narrator: Original source.')
    other=StudioController(first.store.root,registry,original.id)
    first.save_draft('First','@incomplete first window','uk','flac')
    other.save_draft('Second','@incomplete second window','sk','mp3')
    assert {row['id'] for row in first.store.project_drafts(original.id)}=={first.draft_id,other.draft_id}
    newer=other.save('Committed elsewhere','Narrator: Newer saved source.')
    first.save_draft('First updated','\0still invalid and unsaved','uk','flac')
    row,=first.store.project_drafts(original.id)
    assert row['id']==first.draft_id and row['script']=='\0still invalid and unsaved'
    assert row['base_revision']==original.revision and row['base_fingerprint']==original.fingerprint
    with pytest.raises(ValueError,match='another window'):
        first.save('Stale attempted commit','Narrator: Keep as a draft.')
    assert first.store.load(original.id)==newer
    assert first.store.project_drafts(original.id)[0]==row


def test_recovery_uses_a_fresh_window_and_never_starts_an_engine(tmp_path,registry,monkeypatch):
    first=StudioController(tmp_path/'library',registry)
    first.save_draft('Unsaved','@invalid but recoverable','uk','wav')
    def forbidden(*args,**kwargs):
        pytest.fail('Recovery attempted to start an engine')
    monkeypatch.setattr('subprocess.Popen',forbidden)
    recovered=StudioController(first.store.root,registry,draft_id=first.draft_id)
    assert recovered.draft_id!=first.draft_id
    assert recovered.recovery_selected and recovered.recovery_draft=='@invalid but recoverable'
    recovered.save_draft('Recovered edits','@still invalid','uk','wav')
    assert {row['id'] for row in first.store.new_drafts()}=={first.draft_id,recovered.draft_id}
    recovered.save('Valid project','Narrator: Valid source.')
    assert [row['id'] for row in first.store.new_drafts()]==[first.draft_id]


def test_saved_project_recovery_requires_selection_and_preserves_stale_base(tmp_path,registry):
    first=StudioController(tmp_path/'library',registry)
    original=first.save('Original','Narrator: Original.')
    first.save_draft('Draft','Narrator: Draft words.','en','wav')
    other=StudioController(first.store.root,registry,original.id)
    assert not other.recovery_selected
    newer=other.save('Newer','Narrator: Newer committed words.')
    opened=StudioController(first.store.root,registry,original.id)
    assert not opened.recovery_selected and opened.project==newer
    draft,=opened.recovery_candidates
    opened.recover_draft(draft)
    opened.save_draft('Recovered','Narrator: Recovered draft.','en','wav')
    assert opened.base_revision==original.revision
    assert opened.base_fingerprint==original.fingerprint
    with pytest.raises(ValueError,match='older project revision'):
        opened.save('Recovered','Narrator: Recovered draft.')
    assert first.store.load(original.id)==newer
    assert len(first.store.project_drafts(original.id))==2


def test_draft_corruption_cannot_overwrite_previous_data(tmp_path,registry):
    controller=StudioController(tmp_path/'library',registry)
    controller.save_draft('Title','@pending','en','wav')
    path=controller.store.studio/'drafts'/(controller.draft_id+'.json')
    payload=read_json(path)
    payload['script']='tampered without matching envelope hash'
    path.write_text(json.dumps(payload),encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='checksum'):
        controller.save_draft('Title','replacement','en','wav')
    with pytest.raises(ValueError,match='checksum'):
        controller.store.new_drafts()
    assert path.read_bytes()==before


def test_commit_cleanup_failure_does_not_poison_next_autosave(tmp_path,registry,monkeypatch):
    controller=StudioController(tmp_path/'library',registry)
    controller.save_draft('Title','Narrator: Saved source.','en','wav')
    previous_id=controller.draft_id
    discard=controller.store.discard_new_draft
    def denied(*args,**kwargs):
        raise OSError('simulated cleanup failure')
    monkeypatch.setattr(controller.store,'discard_new_draft',denied)
    committed=controller.save('Title','Narrator: Saved source.')
    assert controller.store.load(committed.id)==committed
    assert 'committed' in controller.cleanup_warning
    assert controller.store.new_drafts()[0]['id']==previous_id
    assert controller.draft_id!=previous_id
    monkeypatch.setattr(controller.store,'discard_new_draft',discard)
    controller.save_draft('Next edit','@still unfinished after the commit','uk','flac')
    current,=controller.store.project_drafts(committed.id)
    assert current['id']==controller.draft_id
    assert current['base_fingerprint']==committed.fingerprint
    assert current['script']=='@still unfinished after the commit'
    assert controller.store.new_drafts()[0]['id']==previous_id
    assert controller.store.load(committed.id)==committed


def test_corrupt_legacy_draft_does_not_make_a_commit_falsely_fail(tmp_path):
    store=ProjectStore(tmp_path/'library')
    original=store.create(parse_script('Narrator: Original.',['flite:kal']))
    legacy=store.path(original.id)/'draft.json'
    legacy.write_bytes(b'not valid json; preserve for diagnosis')
    newer=parse_script('Narrator: Committed successfully.',['flite:kal'],previous=original)
    assert store.save(newer)==newer
    assert store.load(original.id)==newer
    assert legacy.read_bytes()==b'not valid json; preserve for diagnosis'


def test_legacy_single_draft_remains_explicitly_recoverable(tmp_path,registry):
    store=ProjectStore(tmp_path/'library')
    original=store.create(parse_script('Narrator: Original.',['flite:kal']))
    store.save_draft(original.id,'Narrator: Legacy draft.')
    opened=StudioController(store.root,registry,original.id)
    legacy,=opened.recovery_candidates
    assert not opened.recovery_selected and legacy['version']==2
    opened.recover_draft(legacy)
    opened.save_draft('Recovered','Narrator: Legacy draft.','en','wav')
    opened.save('Recovered','Narrator: Legacy draft.')
    assert (store.path(original.id)/'draft.json').is_file()
    assert store.project_drafts(original.id)==(legacy,)


def test_full_library_backup_roundtrip_preserves_both_windows_and_exact_source(tmp_path,registry):
    from carr_library.application import LibraryApp
    app=LibraryApp(tmp_path/'library')
    first=StudioController(app.root,registry)
    original=first.save('Original','Narrator: Original source.')
    other=StudioController(app.root,registry,original.id)
    first.save_draft('First','\0unfinished text','uk','wav')
    other.save_draft('Second','@invalid but retained','sk','flac')
    before=first.store.project_drafts(original.id)
    backup=tmp_path/'backup'
    checksum=create_backup(app,backup)
    restored_root=restore_backup(backup,tmp_path/'restored',checksum)
    restored=ProjectStore(restored_root)
    assert restored.load(original.id)==original
    assert restored.project_drafts(original.id)==before
    assert first.store.project_drafts(original.id)==before


def test_narration_export_import_rebinds_only_project_identity(tmp_path):
    store=ProjectStore(tmp_path/'library')
    script='Narrator: Read this original source.'
    source={'workspace':'w','edition':'e','revision':'r','kind':'text','index':1,
            'text_sha256':sha256(script.encode()).hexdigest()}
    original=store.create(parse_script(script,['flite:kal'],directives=False,source=source))
    cue=original.scenes[0].cues[0]
    edited=cue_derivative(original,cue.id).apply(0,8,'Speaker:').apply(9,13,'Pronounce').undo()
    updated=set_cue_derivative(original,cue.id,edited)
    store.save(updated)
    exported=tmp_path/'export.json'
    store.export(updated.id,exported)
    imported=store.import_project(exported)
    copy=cue_derivative(imported,cue.id)
    assert imported.id!=updated.id and imported.revision==1
    assert imported.source==source and imported.script==script and imported.scenes==updated.scenes
    assert copy==replace(edited,source_id=f'{imported.id}:{cue.id}')
    assert copy.source_text==edited.source_text and copy.undo_stack==edited.undo_stack and copy.redo_stack==edited.redo_stack
    assert store.load(updated.id)==updated and store.load(imported.id)==imported


def test_unchanged_script_metadata_save_keeps_custom_scene_cues_music_and_narration(tmp_path,registry):
    store=ProjectStore(tmp_path/'library')
    original=parse_script('Narrator: Original source.',['flite:kal'])
    scene=replace(original.scenes[0],music=Music(engine='fluidsynth',mood='calm',seed=9),
        cues=(*original.scenes[0].cues,Cue('authored-pause','pause',seconds=4),
              Cue('authored-asset','asset',asset='a'*64,gain_db=-6)))
    original=store.create(replace(original,scenes=(scene,)))
    cue=original.scenes[0].cues[0]
    derivative=cue_derivative(original,cue.id).apply(0,8,'Read')
    original=store.save(set_cue_derivative(original,cue.id,derivative))
    controller=StudioController(store.root,registry,original.id)
    controller.voices=()  # Editing metadata does not need an installed engine.
    changed=controller.save('Changed title',original.script,settings=Settings(format='flac'))
    assert changed.title=='Changed title' and changed.settings.format=='flac'
    assert changed.scenes==original.scenes and changed.roles==original.roles
    assert changed.narration==original.narration and changed.script==original.script
    assert changed.source==original.source and changed.revision==original.revision+1
