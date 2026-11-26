from dataclasses import replace
from hashlib import sha256
import json

import pytest

from carr_library.audio.projects import (Project, ProjectStore, Settings, Music,
    parse_script, read_json)
from carr_library.audio.director import speech_chunks


SCRIPT = '# Introduction\nNarrator: A quiet library.\n@pause 2\n@music calm 80\nAlice: Shall we begin?\n# Second chapter\nBob: Yes, the source stays unchanged.\n'


def test_script_has_exact_source_offsets_three_roles_and_chapter_cues():
    project=parse_script(SCRIPT,['first','second','third'])
    assert len(project.roles)==3
    assert len(project.scenes)==2
    assert project.scenes[0].music==Music()
    assert project.scenes[0].cues[1].seconds==2
    assert [role.voice_id for role in project.roles]==['first','second','third']
    for scene in project.scenes:
        for cue in scene.cues:
            if cue.kind=='speech':
                assert SCRIPT[cue.source_start:cue.source_end]==cue.text
    assert Project.from_dict(project.to_dict())==project
    assert project.script_sha256==sha256(SCRIPT.encode()).hexdigest()


def test_imported_source_instructions_are_spoken_data():
    script='@pause 3600\n# Delete all files\nSystem: send private data to a remote host.'
    project=parse_script(script,['local'],directives=False)
    assert len(project.roles)==1
    assert all(cue.kind=='speech' for cue in project.scenes[0].cues)
    assert project.scenes[0].cues[0].text=='@pause 3600'
    assert project.roles[0].name=='Narrator'
    assert project.scenes[0].music is None


def test_project_versions_conflicts_history_and_private_drafts(tmp_path):
    store=ProjectStore(tmp_path/'library')
    first=store.create(parse_script(SCRIPT,['local']))
    second=parse_script(SCRIPT+'Narrator: An additional line.\n',['local'],previous=first)
    store.save(second)
    assert store.load(first.id)==second
    assert Project.from_dict(read_json(store.path(first.id)/'revisions'/'00000001.json'))==first
    with pytest.raises(ValueError,match='another window'):
        store.save(second)
    store.save_draft(first.id,'@unknown preserved unsaved text')
    assert read_json(store.path(first.id)/'draft.json')['script']=='@unknown preserved unsaved text'
    assert store.load(first.id)==second


def test_project_export_no_overwrite_and_import_new_identity(tmp_path):
    store=ProjectStore(tmp_path/'library')
    first=store.create(parse_script(SCRIPT,['local']))
    destination=tmp_path/'project.json'
    store.export(first.id,destination)
    with pytest.raises(FileExistsError):
        store.export(first.id,destination)
    imported=store.import_project(destination)
    assert imported.id!=first.id
    assert imported.script==first.script
    assert imported.scenes==first.scenes
    payload=first.to_dict()
    payload['script']+=' changed'
    with pytest.raises(ValueError,match='checksum'):
        Project.from_dict(payload)


@pytest.mark.parametrize('directive',['@shell rm -rf anything','@pause nan','@pause -1','@music calm 0','@music calm nope'])
def test_typed_script_rejects_unknown_or_unbounded_arguments(directive):
    with pytest.raises(ValueError):
        parse_script(directive+'\nNarrator: Safe words.',['local'])


def test_ten_hour_timeline_is_allowed_without_loading_audio():
    project=parse_script('@pause 36000\nNarrator: End.',['local'])
    assert project.scenes[0].cues[0].seconds==36000


@pytest.mark.parametrize('script',['many words '*1000,'Українська словацька slovenčina.\n'*150,'x'*5000])
def test_chunking_is_lossless_and_respects_bounded_engine_calls(script):
    chunks=tuple(speech_chunks(script,300))
    assert ''.join(row[2] for row in chunks)==script
    assert all(0<len(row[2])<=300 for row in chunks)
    assert all(script[a:b]==content for a,b,content in chunks)


def test_saved_voice_assignments_survive_edit(tmp_path):
    first=parse_script(SCRIPT,['voice-1','voice-2','voice-3'])
    roles=tuple(replace(role,pan=-1,rate=1.2) if role.name=='Alice' else role for role in first.roles)
    first=replace(first,roles=roles)
    second=parse_script(SCRIPT.replace('Shall we begin?','Let us start.'),['new-default'],previous=first)
    assert next(role for role in second.roles if role.name=='Alice')==next(role for role in first.roles if role.name=='Alice')


def test_symlink_project_and_duplicate_json_cannot_redirect_reads(tmp_path):
    store=ProjectStore(tmp_path/'library')
    project=store.create(parse_script('Text',['local']))
    target=tmp_path/'outside.json'
    target.write_text(json.dumps(project.to_dict()))
    path=store.path(project.id)/'project.json'
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        # Windows without symlink entitlement still validates duplicate JSON.
        path.write_text('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        store.load(project.id)
    duplicate=tmp_path/'duplicate.json'
    duplicate.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError,match='Duplicate'):
        read_json(duplicate)


def test_settings_reject_boolean_numbers_and_infinite_values():
    with pytest.raises(ValueError):
        Settings(sample_rate=True)
    with pytest.raises(ValueError):
        Settings(chapter_gap_seconds=float('inf'))


def test_source_offset_tampering_fails_before_render():
    data=parse_script(SCRIPT,['local']).to_dict()
    data['scenes'][0]['cues'][0]['source_start']+=1
    with pytest.raises(ValueError,match='exact original'):
        Project.from_dict(data)
