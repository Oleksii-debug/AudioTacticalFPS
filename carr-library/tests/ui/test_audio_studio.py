"""Recovery and source integrity at the native studio controller boundary."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.projects import Settings,parse_script
from carr_library.audio.voices import Voice
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import StudioController,prepare_reading_studio


@pytest.fixture
def registry():
    return SimpleNamespace(list_voices=lambda:(Voice('flite:kal','flite','Kal',('en',),(),'', ''),))


def test_unsaved_invalid_script_and_metadata_survive_restart(tmp_path,registry):
    first=StudioController(tmp_path/'data',registry)
    first.save_draft('New title','@not yet valid','uk','flac')
    drafts=first.store.new_drafts()
    assert len(drafts)==1 and drafts[0]['id']==first.draft_id
    reopened=StudioController(tmp_path/'data',registry,draft_id=first.draft_id)
    assert reopened.recovery_draft=='@not yet valid'
    assert reopened.recovery_metadata['title']=='New title'
    assert reopened.recovery_metadata['language']=='uk'
    assert reopened.recovery_metadata['format']=='flac'
    with pytest.raises(ValueError,match='Unknown script directive'):
        reopened.save('New title',reopened.recovery_draft,'uk')
    assert len(first.store.new_drafts())==1
    project=reopened.save('New title','Narrator: Valid text.','en',settings=Settings(format='flac'))
    assert project.settings.format=='flac'
    assert reopened.draft_id != first.draft_id
    assert [row['id'] for row in first.store.new_drafts()] == [first.draft_id]


def test_existing_draft_keeps_unsaved_metadata_and_preserves_stale_writer(tmp_path,registry):
    first=StudioController(tmp_path/'data',registry)
    project=first.save('Original','Narrator: Saved source.','en')
    first.save_draft('Changed title','Narrator: Changed draft.','sk','mp3')
    reopened=StudioController(tmp_path/'data',registry,project.id)
    assert reopened.recovery_draft=='Narrator: Changed draft.'
    assert reopened.recovery_metadata['language']=='sk'
    reopened.save('Changed title',reopened.recovery_draft,'sk',settings=Settings(format='mp3'))
    newer=reopened.project
    reopened.save_draft('Second window','Other unsaved text','en','wav')
    first.save_draft('Stale','Retained old-base draft','en','wav')
    rows={row['id']:row for row in first.store.project_drafts(project.id)}
    assert set(rows)=={first.draft_id,reopened.draft_id}
    assert rows[first.draft_id]['script']=='Retained old-base draft'
    assert rows[first.draft_id]['base_revision']==project.revision
    assert rows[first.draft_id]['base_fingerprint']==project.fingerprint
    assert rows[reopened.draft_id]['base_revision']==newer.revision
    assert first.store.load(project.id)==newer
    assert not (first.store.path(project.id)/'draft.json').exists()


def test_reading_copy_cannot_reinterpret_or_mutate_source(tmp_path,registry):
    app=LibraryApp(tmp_path/'data')
    workspace=app.create_workspace('Source scope')
    raw='@pause 999\nNarrator: These are original source words.'
    imported=app.add_text(workspace,'Untrusted source',raw)
    page=ReadingDesk(app,workspace,imported['edition']).page(1)
    controller=prepare_reading_studio(app.root,page,registry)
    project=controller.save('Reading copy',page.raw_text,'en')
    assert not controller.directives and project.source['text_sha256']==page.location.text_sha256
    assert all(cue.kind=='speech' for scene in project.scenes for cue in scene.cues)
    assert project.scenes[0].cues[0].text=='@pause 999'
    with pytest.raises(ValueError,match='exact original source text'):
        controller.save('Reading copy','Changed original','en')
    assert ReadingDesk(app,workspace,imported['edition']).page(1).raw_text==raw
    with pytest.raises(ValueError,match='exact source page'):
        prepare_reading_studio(app.root,replace(page,raw_text='tampered'),registry)


def test_recovered_source_draft_keeps_source_boundary(tmp_path,registry):
    app=LibraryApp(tmp_path/'data')
    workspace=app.create_workspace('Source scope')
    imported=app.add_text(workspace,'Source','Unchanged original source.')
    page=ReadingDesk(app,workspace,imported['edition']).page(1)
    initial=prepare_reading_studio(app.root,page,registry)
    initial.save_draft('Recovered narration',page.raw_text,'en','wav')
    recovered=StudioController(app.root,registry,draft_id=initial.draft_id)
    assert recovered.source==initial.source and not recovered.directives
    saved=recovered.save('Recovered narration',recovered.recovery_draft,'en')
    assert saved.script==page.raw_text and saved.source==initial.source
