"""Edited Studio audio stays attached to the versioned source performance."""
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.studio.project import Cue, Project, Scene, parse_script
from carr_library.audio import AudioCancelled, AudioRuntime
from carr_library.durable_state import encode
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.render import ProductionService


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


def test_clip_only_production_does_not_discover_or_require_any_voice(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    def forbidden(*args, **kwargs):
        raise AssertionError('A clip-only project must not start voice discovery.')
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)
    service = ProductionService(tmp_path / 'library/state/studio', runtime_factory=AudioRuntime)
    editor = ClipEditor(tmp_path / 'library', service=service)
    project = editor.create()
    project = editor.add(project, project.scenes[0].id, audio(tmp_path / 'clip.wav'), duration=.25)
    output = tmp_path / 'outputs'
    output.mkdir()
    job = service.submit(project, output)
    result = service.run(job)
    assert result['status'] == 'complete' and result['result']['clips']
    assert service._voices is None


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
    assert cue.path == 'inputs/' + sha256(original).hexdigest() + '.audio'
    retained = editor.service.root / cue.path
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
    assert not list(editor.service.inputs.glob('*.audio'))
    assert not list(editor.service.inputs.glob('.pending-*'))


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
    assert not list(editor.service.inputs.glob('*.audio'))
    assert not list(editor.service.inputs.glob('.pending-*'))


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
    assert not list(editor.service.inputs.glob('*.audio'))
    assert not list(editor.service.inputs.glob('.pending-*'))


def test_add_does_not_authorize_neighbouring_inputs(editor, tmp_path):
    project = editor.create()
    source = audio(tmp_path / 'selected.wav')
    neighbour = audio(tmp_path / 'not-selected.wav')
    editor.add(project, project.scenes[0].id, source)
    with pytest.raises(ValueError, match='Portable Studio clips'):
        editor.service._cue_source(Cue('unselected', str(neighbour)))
    with pytest.raises(ValueError, match='explicitly selected'):
        editor.service._cue_source(Cue('unselected', str(neighbour)), schema=1)


def test_move_is_one_revision_and_keeps_clip_identity_and_original(editor, tmp_path):
    first = editor.create()
    first = replace(first, revision=2, scenes=(*first.scenes,
                    Scene('scene-2', 'Destination', (), minimum_duration=1)))
    editor.service.projects.save(first, expected_revision=1)
    project = editor.add(first, first.scenes[0].id, audio(tmp_path / 'selected.wav'),
                         trim_start=.5, duration=.75, start=.2, pan=-1)
    cue = project.scenes[0].cues[0]
    original = (editor.service.root / cue.path).read_bytes()
    moved = editor.move(project, project.scenes[0].id, cue.id, 'scene-2')
    assert moved.revision == project.revision + 1
    assert not moved.scenes[0].cues and moved.scenes[1].cues == (cue,)
    assert (editor.service.root / cue.path).read_bytes() == original
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
    assert (editor.service.root / clip.path).parent == editor.service.inputs
    assert (editor.service.root / clip.path).read_bytes() == Path(saved['path']).read_bytes()
    preview = editor.preview(project, project.scenes[0].id, clip.id)
    channels, rate = pcm(preview)
    assert len(channels[0]) == round(.5 * rate) and rms(channels[0]) > 100

