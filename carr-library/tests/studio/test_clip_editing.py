"""Edited Studio audio stays attached to the versioned source performance."""
from dataclasses import replace
from types import SimpleNamespace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.studio.project import Cue, Project, Scene, parse_script
from carr_library.ui.studio_dialog import StudioController
from carr_library.audio import AudioCancelled, AudioRuntime
from carr_library.durable_state import encode
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.render import ProductionService


def configured(tmp_path):
    controller = StudioController(SimpleNamespace(root=tmp_path))
    project = parse_script('[Scene: Archive]\nNarrator: Original words.',
                           voice_ids=('chosen-voice',), mode='authored')
    scene = replace(project.scenes[0],
                    cues=(Cue('recorded-1', str(tmp_path / 'selected.wav'), start=.5),),
                    lines=(replace(project.scenes[0].lines[0], source_ref='retained-source-1'),))
    project = replace(project, scenes=(scene,), sample_rate=48000)
    controller.service.projects.save(project)
    controller.project = project
    return controller, project


def test_loaded_project_save_preserves_cues_source_bindings_mode_and_sample_rate(tmp_path):
    controller, project = configured(tmp_path)
    assert controller.save(project.original_script, project.title, ('other-default',)) == project
    renamed = controller.save(project.original_script, 'New title', ('other-default',))
    assert renamed.scenes == project.scenes and renamed.roles == project.roles
    assert renamed.mode == 'authored' and renamed.sample_rate == 48000
    assert renamed.revision == 2


def test_script_structure_change_cannot_silently_discard_attached_audio(tmp_path):
    controller, project = configured(tmp_path)
    with pytest.raises(ValueError, match='scene.*audio|audio.*scene'):
        controller.save('[Scene: Different scene]\nNarrator: New words.', project.title, ('chosen-voice',))
    assert controller.service.projects.load(project.id) == project
    assert controller.project == project


def audio(path):
    # A known silent prefix and suffix make actual source trimming observable.
    values = [round(10000 * math.sin(2 * math.pi * 400 * index / 24000))
              if 12000 <= index < 30000 else 0 for index in range(48000)]
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(struct.pack('<' + 'h' * len(values), *values))
    return path


@pytest.fixture
def editor(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual clip editing requires installed FFmpeg and ffprobe.')
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]),
                                runtime_factory=AudioRuntime)
    return ClipEditor(tmp_path / 'library', service=service)


def test_schema_one_roundtrip_preserves_original_fingerprint_and_wire_fields():
    value = {'id': 'legacy', 'title': 'Original', 'roles': [], 'schema': 1, 'revision': 1,
        'original_script': 'Immutable words.', 'mode': 'faithful', 'sample_rate': 24000,
        'scenes': [{'id': 'scene', 'title': 'Scene', 'lines': [], 'music': None,
            'minimum_duration': 0, 'cues': [{'id': 'cue', 'path': '/selected.wav',
                'start': 0, 'gain_db': -6, 'pan': 0, 'role': 'sfx'}]}]}
    project = Project.from_dict(value)
    assert project.fingerprint == sha256(encode(value)).hexdigest()
    assert json.loads(encode(project.to_dict())) == value
    with pytest.raises(ValueError, match='schema 2'):
        replace(project, scenes=(replace(project.scenes[0],
            cues=(replace(project.scenes[0].cues[0], trim_start=.1),)),))


def test_edit_retains_exact_bytes_and_noop_keeps_revision(editor, tmp_path):
    selected = audio(tmp_path / 'Selected original.wav')
    original = selected.read_bytes()
    first = editor.create()
    second = editor.add(first, first.scenes[0].id, selected)
    cue = second.scenes[0].cues[0]
    retained = Path(cue.path)
    assert retained == editor.service.inputs / (sha256(original).hexdigest() + '.audio')
    assert retained.read_bytes() == selected.read_bytes() == original
    third = editor.edit(second, second.scenes[0].id, cue.id,
                        trim_start=.5, duration=.75, start=.25, pan=-1, fade_in=.2, fade_out=.2)
    assert third.revision == 3 and third.schema == 2
    assert editor.edit(third, third.scenes[0].id, cue.id, pan=-1) == third
    assert Project.from_dict(json.loads(encode(third.to_dict()))) == third
    assert retained.read_bytes() == selected.read_bytes() == original
    with pytest.raises(ValueError, match='changed'):
        editor.remove(second, second.scenes[0].id, cue.id)
    removed = editor.remove(third, third.scenes[0].id, cue.id)
    assert not removed.scenes[0].cues and retained.read_bytes() == original


@pytest.mark.parametrize('settings', [
    {'trim_start': 2}, {'trim_start': 1.9, 'duration': .2}, {'duration': 0},
    {'start': -1}, {'pan': 1.1}, {'gain_db': float('nan')}, {'fade_in': 3},
    {'duration': .5, 'fade_out': .6}, {'role': 'shell'}, {'start': True},
])
def test_invalid_clip_cannot_publish_a_project_revision(editor, tmp_path, settings):
    project = editor.create()
    source = audio(tmp_path / 'selected.wav')
    with pytest.raises(ValueError):
        editor.add(project, project.scenes[0].id, source, **settings)
    assert editor.service.projects.load(project.id) == project
    assert not list(editor.service.inputs.glob('.pending-*'))


def test_receipt_binding_rejects_changed_recording(editor, tmp_path):
    project = editor.create()
    selected = audio(tmp_path / 'recorded.wav')
    with pytest.raises(ValueError, match='receipt'):
        editor.add(project, project.scenes[0].id, selected, expected_sha256='0' * 64)
    assert editor.service.projects.load(project.id) == project
    assert not list(editor.service.inputs.iterdir())


def test_cancel_after_copy_starts_keeps_project_and_original(editor, tmp_path):
    project = editor.create()
    selected = audio(tmp_path / 'selected.wav')
    original = selected.read_bytes()
    stopped = False
    def progress(message):
        nonlocal stopped
        stopped = True
    with pytest.raises((AudioCancelled, InterruptedError)):
        editor.add(project, project.scenes[0].id, selected, cancelled=lambda: stopped, progress=progress)
    assert selected.read_bytes() == original and editor.service.projects.load(project.id) == project
    assert not list(editor.service.inputs.iterdir())


def test_source_change_during_retention_is_not_admitted(editor, tmp_path):
    project = editor.create()
    selected = audio(tmp_path / 'selected.wav')
    def mutate(message):
        with selected.open('r+b') as stream:
            stream.seek(1000)
            stream.write(b'changed!')
    with pytest.raises(ValueError, match='changed'):
        editor.add(project, project.scenes[0].id, selected, progress=mutate)
    assert editor.service.projects.load(project.id) == project
    assert not list(editor.service.inputs.iterdir())


def test_add_does_not_authorize_neighbouring_inputs(editor, tmp_path):
    project = editor.create()
    source = audio(tmp_path / 'selected.wav')
    neighbour = audio(tmp_path / 'not-selected.wav')
    editor.add(project, project.scenes[0].id, source)
    with pytest.raises(ValueError, match='explicitly selected'):
        editor.service._cue_source(Cue('unselected', str(neighbour)))


def test_move_is_one_revision_and_keeps_clip_identity_and_original(editor, tmp_path):
    first = editor.create()
    first = replace(first, revision=2, scenes=(*first.scenes,
                    Scene('scene-2', 'Destination', (), minimum_duration=1)))
    editor.service.projects.save(first, expected_revision=1)
    project = editor.add(first, first.scenes[0].id, audio(tmp_path / 'selected.wav'),
                         trim_start=.5, duration=.75, start=.2, pan=-1)
    cue = project.scenes[0].cues[0]
    original = Path(cue.path).read_bytes()
    moved = editor.move(project, project.scenes[0].id, cue.id, 'scene-2')
    assert moved.revision == project.revision + 1
    assert not moved.scenes[0].cues and moved.scenes[1].cues == (cue,)
    assert Path(cue.path).read_bytes() == original
    assert editor.move(moved, 'scene-2', cue.id, 'scene-2') == moved


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        channels, rate, count = stream.getnchannels(), stream.getframerate(), stream.getnframes()
        values = struct.unpack('<' + 'h' * (count * channels), stream.readframes(count))
    return tuple(values[index::channels] for index in range(channels)), rate


def rms(values):
    return math.sqrt(sum(value * value for value in values) / max(1, len(values)))


def test_actual_trim_position_pan_fades_and_complete_clip_stems(editor, tmp_path):
    selected = audio(tmp_path / 'selected.wav')
    original = selected.read_bytes()
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, selected, trim_start=.5, duration=.75,
                         start=.25, pan=-1, gain_db=0, fade_in=.2, fade_out=.2)
    project = replace(project, revision=project.revision + 1,
        scenes=(*project.scenes, Scene('scene-2', 'Second scene', (), minimum_duration=.6)))
    editor.service.projects.save(project, expected_revision=project.revision - 1)
    project = editor.add(project, 'scene-2', selected, role='microphone', trim_start=.5,
                         duration=.4, start=.2, pan=1, gain_db=0)
    output = tmp_path / 'outputs'
    output.mkdir()
    result = editor.service.run(editor.service.submit(project, output))
    directory = Path(result['destination'])
    (left, right), rate = pcm(directory / 'master.wav')
    assert len(left) == len(right) == round(1.6 * rate)
    assert max(map(abs, left[:round(.25 * rate)])) == 0
    assert rms(left[round(.5 * rate):round(.65 * rate)]) > 5000
    assert rms(left[round(.25 * rate):round(.28 * rate)]) < 1000
    assert rms(left[round(.97 * rate):round(1 * rate)]) < 1000
    assert max(map(abs, right[:round(1.2 * rate)])) == 0
    assert rms(right[round(1.25 * rate):round(1.5 * rate)]) > 5000
    assert max(map(abs, left[round(1 * rate):])) == 0
    for role, channel, start, end in [('sfx', 0, .25, 1), ('microphone', 1, 1.2, 1.6)]:
        channels, stem_rate = pcm(directory / 'stems/clips' / (role + '.wav'))
        assert stem_rate == rate and len(channels[0]) == len(left)
        assert max(map(abs, channels[1 - channel])) == 0
        assert rms(channels[channel][round((start + .2) * rate):round((end - .1) * rate)]) > 4000
    clips = result['result']['clips']
    assert [item['start'] for item in clips] == [.25, 1.2]
    assert [item['end'] for item in clips] == [1, 1.6]
    assert not result['result']['lines']  # Recorded audio never invents a transcript.
    assert selected.read_bytes() == original
    preview = editor.preview(project, project.scenes[0].id, project.scenes[0].cues[0].id)
    channels, rate = pcm(preview)
    assert len(channels[0]) == round(.75 * rate) and max(map(abs, channels[1])) == 0


def test_edited_clip_backup_survives_deletion_and_same_job_resume(editor, tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    selected = audio(tmp_path / 'selected.wav')
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, selected,
                         role='microphone', trim_start=.5, duration=.5, pan=1, fade_in=.1)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    identity = editor.service.submit(project, outputs, operation_id='edited-clip-recovery')
    completed = editor.service.run(identity)
    master = Path(completed['destination'], 'master.wav').read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    selected.unlink()
    shutil.rmtree(app.root)
    shutil.rmtree(outputs)
    root = restore_backup(backup, tmp_path / 'restored', digest)
    restored = ProductionService(root / 'state/studio', voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    recovered = restored.projects.load(project.id)
    cue = recovered.scenes[0].cues[0]
    assert (recovered.schema, cue.trim_start, cue.duration, cue.fade_in, cue.role) == (2, .5, .5, .1, 'microphone')
    assert restored.get(identity)['status'] == 'waiting-input'
    outputs.mkdir()
    restored.relocate_restored_job(identity, outputs)
    final = restored.run(identity)
    assert Path(final['destination'], 'master.wav').read_bytes() == master


def test_actual_recording_receipt_becomes_an_editable_managed_clip(editor, tmp_path, monkeypatch):
    from carr_library.audio import recording
    from carr_library.audio.recording import RecordingOptions, RecordingService, parse_devices
    service = RecordingService(tmp_path / 'library', runtime=AudioRuntime(tmp_path / 'capture-runtime'))
    inventory = (b'[dshow] "Synthetic recorder" (audio)\n'
                 b'[dshow] Alternative name "@device_cm_{123}\\wave_{456}"\n')
    device = parse_devices(inventory)[0]
    service._devices = {device.id: device}
    service._engine_hash = recording._digest(shutil.which('ffmpeg'))
    # Test-only fixed synthetic input; production cannot accept lavfi or argv.
    monkeypatch.setattr(service, '_input_arguments', lambda selected: [
        '-re', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=24000'])
    saved = service.capture(device.id, title='Synthetic microphone fixture',
        options=RecordingOptions(max_seconds=1, sample_rate=24000))
    reviewed = service.get(saved['id'])
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, reviewed['path'],
        expected_sha256=reviewed['audio']['sha256'], role='microphone', title=reviewed['title'],
        trim_start=.2, duration=.5, fade_out=.1)
    clip = project.scenes[0].cues[0]
    assert clip.title == saved['title'] and clip.role == 'microphone'
    assert Path(clip.path).parent == editor.service.inputs
    assert Path(clip.path).read_bytes() == Path(saved['path']).read_bytes()
    preview = editor.preview(project, project.scenes[0].id, clip.id)
    channels, rate = pcm(preview)
    assert len(channels[0]) == round(.5 * rate) and rms(channels[0]) > 100


def test_split_and_join_preserve_actual_pcm_timeline_and_original(editor, tmp_path):
    selected = audio(tmp_path / 'split original.wav')
    original = selected.read_bytes()
    project = editor.create('Exact cuts')
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, selected, trim_start=.5, duration=1, start=.25, pan=-1, gain_db=0)
    cue = project.scenes[0].cues[0]

    def rendered(value, name):
        output = tmp_path / name
        output.mkdir()
        result = editor.service.run(editor.service.submit(value, output))
        with wave.open(str(Path(result['destination']) / 'master.wav'), 'rb') as stream:
            count = stream.getnframes()
            return stream.getframerate(), stream.getnchannels(), count, sha256(stream.readframes(count)).hexdigest()

    before = rendered(project, 'whole')
    divided = editor.split(project, scene_id, cue.id, .5)
    left, right = divided.scenes[0].cues
    assert (left.id, left.duration, right.start, right.trim_start, right.duration) == (cue.id, .5, .75, 1, .5)
    assert left.path == right.path == cue.path and left.id != right.id
    assert rendered(divided, 'split') == before
    joined = editor.join(divided, scene_id, right.id, left.id)
    assert joined.scenes[0].cues == (cue,)
    assert rendered(joined, 'joined') == before
    assert selected.read_bytes() == Path(cue.path).read_bytes() == original


def test_cut_paste_and_history_publish_revisions_without_losing_originals(editor, tmp_path):
    selected = audio(tmp_path / 'clipboard source.wav')
    project = editor.create()
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, selected, trim_start=.5, duration=.5, pan=1, role='microphone')
    original = project.scenes[0].cues[0]
    copied = editor.copy(project, scene_id, original.id)
    assert copied == original and editor.service.projects.load(project.id) == project
    cut = editor.cut(project, scene_id, original.id)
    assert not cut.scenes[0].cues and Path(original.path).exists()
    pasted = editor.paste(cut, scene_id, start=.75)
    pasted_cue, = pasted.scenes[0].cues
    assert pasted_cue.id != original.id and pasted_cue.path == original.path and pasted_cue.start == .75
    undone_paste = editor.history(pasted)
    assert not undone_paste.scenes[0].cues
    undone_cut = editor.history(undone_paste)
    assert undone_cut.scenes == project.scenes and undone_cut.original_script == project.original_script
    redone_cut = editor.history(undone_cut, forward=True)
    redone_paste = editor.history(redone_cut, forward=True)
    assert redone_paste.scenes == pasted.scenes
    assert redone_paste.revision == pasted.revision + 4
    assert Path(original.path).read_bytes() == selected.read_bytes()
    versions = tuple((editor.service.projects.directory(project.id) / 'revisions').glob('*.json'))
    assert len(versions) == redone_paste.revision - 1


@pytest.mark.parametrize('position', [0, -1, 1, 2, True, float('nan'), .1, .9])
def test_invalid_or_fade_crossing_split_never_changes_the_project(editor, tmp_path, position):
    project = editor.create()
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, audio(tmp_path / 'source.wav'),
                         duration=1, fade_in=.2, fade_out=.2)
    cue = project.scenes[0].cues[0]
    with pytest.raises((ValueError, TypeError)):
        editor.split(project, scene_id, cue.id, position)
    assert editor.service.projects.load(project.id) == project


@pytest.mark.parametrize('change', [{'start': .6}, {'trim_start': .6}, {'pan': 1},
                                     {'gain_db': -3}, {'fade_in': .1}, {'role': 'music'}])
def test_join_rejects_noncontiguous_or_different_processing(editor, tmp_path, change):
    project = editor.create()
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, audio(tmp_path / 'source.wav'), duration=1)
    project = editor.split(project, scene_id, project.scenes[0].cues[0].id, .5)
    left, right = project.scenes[0].cues
    changed = editor.edit(project, scene_id, right.id, **change)
    with pytest.raises(ValueError, match='adjacent cuts'):
        editor.join(changed, scene_id, left.id, right.id)
    assert editor.service.projects.load(project.id) == changed


@pytest.mark.parametrize('operation', ['copy', 'cut', 'paste', 'split', 'history'])
def test_cancelled_clip_commands_do_not_publish_another_revision(editor, tmp_path, operation):
    project = editor.create()
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, audio(tmp_path / 'source.wav'), duration=1)
    cue = project.scenes[0].cues[0]
    editor.copy(project, scene_id, cue.id)
    calls = {
        'copy': lambda: editor.copy(project, scene_id, cue.id, cancelled=lambda: True),
        'cut': lambda: editor.cut(project, scene_id, cue.id, cancelled=lambda: True),
        'paste': lambda: editor.paste(project, scene_id, cancelled=lambda: True),
        'split': lambda: editor.split(project, scene_id, cue.id, .5, cancelled=lambda: True),
        'history': lambda: editor.history(project, cancelled=lambda: True),
    }
    with pytest.raises(AudioCancelled):
        calls[operation]()
    assert editor.service.projects.load(project.id) == project
    assert not list(editor.service.inputs.glob('.pending-*'))


def test_paste_rechecks_copied_media_before_publication(editor, tmp_path):
    project = editor.create()
    scene_id = project.scenes[0].id
    project = editor.add(project, scene_id, audio(tmp_path / 'source.wav'))
    cue = project.scenes[0].cues[0]
    editor.copy(project, scene_id, cue.id)
    Path(cue.path).write_bytes(b'changed after copy')
    with pytest.raises(ValueError, match='content identity'):
        editor.paste(project, scene_id)
    assert editor.service.projects.load(project.id) == project


def test_clip_publication_rejects_same_revision_content_race(editor, tmp_path, monkeypatch):
    project = editor.create()
    scene = project.scenes[0]
    real_save = editor.service.projects.save
    changed = replace(project, title='Concurrent change at the same revision')
    path = editor.service.projects.directory(project.id) / 'project.json'

    def competing_save(candidate, **kwargs):
        path.write_bytes(encode(changed.to_dict()))
        return real_save(candidate, **kwargs)

    monkeypatch.setattr(editor.service.projects, 'save', competing_save)
    with pytest.raises(ValueError, match='content changed'):
        editor.scene_duration(project, scene.id, 2)
    assert editor.service.projects.load(project.id) == changed


def test_undo_refuses_external_edits_and_new_edit_discards_redo(editor, tmp_path):
    project = editor.create()
    scene_id = project.scenes[0].id
    first = editor.scene_duration(project, scene_id, 2)
    second = editor.scene_duration(first, scene_id, 3)
    undone = editor.history(second)
    third = editor.scene_duration(undone, scene_id, 4)
    with pytest.raises(ValueError, match='redo'):
        editor.history(third, forward=True)
    external = replace(third, revision=third.revision + 1, title='External edit')
    editor.service.projects.save(external, expected_revision=third.revision)
    with pytest.raises(ValueError, match='outside this editing history'):
        editor.history(external)
    assert editor.service.projects.load(project.id) == external


def test_editor_close_preserves_a_borrowed_service_and_closes_its_own(tmp_path, editor):
    editor.close()
    assert not editor.service._closed.is_set()
    owned = ClipEditor(tmp_path / 'owned')
    owned.close()
    assert owned.service._closed.is_set()
    with pytest.raises(ValueError, match='closed'):
        owned.create()
