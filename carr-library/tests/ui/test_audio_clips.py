"""Current Studio clip controls, stale-result boundary and portable backup."""
from dataclasses import replace
from pathlib import Path
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.backup import input_assets
from carr_library.audio.clips import ClipEditor
from carr_library.audio.production import parse_script
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.ui.audio_clips import clip_values, FIELDS
from carr_library.ui.audio_studio import StudioController


def values(**changes):
    data = {key: default for key, _, default in FIELDS}
    data.update(changes)
    return data


def test_parameter_form_keeps_explicit_zero_and_optional_duration():
    value = clip_values(values(start='0', duration='', gain_db='-6', pan='1'),
                        track='microphone', title='Мікрофон')
    assert value['start'] == 0 and value['duration'] is None
    assert value['gain_db'] == -6 and value['pan'] == 1
    assert value['track'] == 'microphone' and value['title'] == 'Мікрофон'


@pytest.mark.parametrize('change', [
    {'pan': 'nan'}, {'duration': '0'}, {'start': '-1'}, {'gain_db': '13'},
    {'fade_in': '1', 'fade_out': '1', 'duration': '1'},
    {'trim_start': 'not a number'}], ids=['nan', 'empty-selection', 'negative-start', 'gain',
                                       'overlapping-fades', 'invalid-number'])
def test_invalid_native_form_values_are_not_silently_clamped(change):
    with pytest.raises(ValueError):
        clip_values(values(**change), track='sfx', title='')


def test_clip_input_references_survive_complete_backup_restore_without_reparsing_narration(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Clips')
    studio = StudioController(private_root=app.root)
    studio.new('[clip retained.data id=c1 duration=.1]\n[duration .2]', input_kind='script')
    studio.save_in_library()
    assets = studio.output_dir / 'assets'
    assets.mkdir(exist_ok=True)
    source = assets / 'retained.data'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 1000) * 1600)
    original = source.read_bytes()
    found = input_assets(studio.project_path, studio.output_dir)
    assert found['audio/assets/retained.data'] == source
    archive = tmp_path / 'clips.zip'
    manifest_sha = create_backup(app, archive)
    verify_backup(archive, manifest_sha)
    destination = tmp_path / 'restored'
    restore_backup(archive, destination, manifest_sha)
    retained = list(destination.glob('audio-projects/*/audio/assets/retained.data'))
    assert len(retained) == 1 and retained[0].read_bytes() == original
    restored_project = next(destination.glob('audio-projects/*/studio/project.json'))
    restored = StudioController(private_root=destination)
    restored.open(restored_project)
    assert restored.plan().schema_version == 2
    assert restored.plan().scenes[0].clips[0].asset == 'retained.data'
    again = tmp_path / 'again.zip'
    again_sha = create_backup(LibraryApp(destination), again)
    verify_backup(again, again_sha)


def test_late_clip_result_cannot_change_replaced_script_or_project_folder(tmp_path):
    root = tmp_path / 'audio'
    root.mkdir()
    studio = StudioController(private_root=tmp_path / 'library')
    studio.new('[duration 1]', input_kind='script')
    studio.configure(output_dir=root)
    editor = studio.clip_editor(studio.clip_snapshot())
    result = studio.prepare_clip_edit(editor, {'action': 'duration', 'scene_id': 'scene-000001',
                                               'minimum_duration': 2})
    studio.edit_script('[duration 3]')
    before = studio.script
    with pytest.raises(ValueError, match='changed'):
        studio.accept_clip_edit(result)
    assert studio.script == before
