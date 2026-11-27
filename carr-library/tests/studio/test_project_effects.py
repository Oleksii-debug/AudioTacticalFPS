"""Schema compatibility and measured Studio track/scene/master effects."""
from array import array
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, Effect
from carr_library.durable_state import encode
from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Cue, Line, Project, Role, Scene
from carr_library.studio.render import ProductionService
from carr_library.ui.studio_dialog import StudioController


def legacy(schema):
    cue = {'id': 'cue', 'path': '/selected.wav', 'start': 0, 'gain_db': -6,
           'pan': 0, 'role': 'sfx'}
    if schema == 2:
        cue.update(trim_start=.25, duration=.5, fade_in=.1, fade_out=.1, title='Retained')
    return {'id': 'legacy', 'title': 'Original', 'schema': schema, 'revision': 1,
        'original_script': 'Narrator: Immutable words.', 'mode': 'faithful', 'sample_rate': 24000,
        'roles': [{'id': 'voice', 'name': 'Narrator', 'voice_id': 'local-voice', 'language': 'en',
                   'pan': 0, 'gain_db': -3, 'rate': 0, 'volume': 100}],
        'scenes': [{'id': 'scene', 'title': 'Scene', 'music': None, 'minimum_duration': 0,
            'lines': [{'id': 'line', 'role': 'voice', 'text': 'Immutable words.',
                       'pause_after': .3, 'source_ref': ''}], 'cues': [cue]}]}


@pytest.mark.parametrize('schema', [1, 2])
def test_legacy_wire_bytes_and_fingerprints_remain_exact(schema):
    value = legacy(schema)
    raw = encode(value)
    project = Project.from_dict(json.loads(raw))
    assert encode(project.to_dict()) == raw
    assert project.fingerprint == sha256(raw).hexdigest()
    assert project.effects == project.roles[0].effects == project.scenes[0].effects == ()
    assert project.scenes[0].cues[0].effects == ()
    with pytest.raises(ValueError, match='schema 3'):
        replace(project, effects=(Effect('gain', gain_db=-6),))


def enriched():
    original = Project.from_dict(legacy(2))
    return replace(original, schema=3, effects=(Effect('gain', gain_db=-4),),
        roles=(replace(original.roles[0], effects=(Effect('compressor', ratio=2),)),),
        scenes=(replace(original.scenes[0], effects=(Effect('highpass', frequency=80),),
            cues=(replace(original.scenes[0].cues[0], effects=(Effect('gain', gain_db=-9, enabled=False),)),)),))


def test_schema_three_roundtrip_preserves_typed_order_and_all_bypass_values():
    project = enriched()
    raw = encode(project.to_dict())
    restored = Project.from_dict(json.loads(raw))
    assert restored == project and restored.fingerprint == project.fingerprint
    assert restored.scenes[0].cues[0].effects[0].enabled is False
    assert restored.scenes[0].cues[0].effects[0].parameters['gain_db'] == -9
    with pytest.raises(TypeError):
        restored.effects[0].parameters['gain_db'] = 100


@pytest.mark.parametrize('scope', ['master', 'role', 'scene', 'clip'])
def test_saved_effect_edits_share_project_cas_and_history(tmp_path, scope):
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]),
                                input_roots=(tmp_path,))
    editor = ClipEditor(tmp_path / 'library', service=service)
    require_audio()
    source = write_tone(tmp_path / 'selected.wav')
    source_bytes = source.read_bytes()
    value = legacy(2)
    value['scenes'][0]['cues'][0]['path'] = str(source)
    before = service.projects.save(Project.from_dict(value))
    args = {'scene_id': 'scene', 'clip_id': 'cue', 'role_id': 'voice'}
    changed = editor.set_effects(before, scope, (Effect('gain', gain_db=-6),), **args)
    assert changed.schema == 3 and changed.revision == 2
    with pytest.raises(ValueError, match='changed since'):
        editor.set_effects(before, scope, (), **args)
    undone = editor.history(changed)
    assert undone.revision == 3 and undone.schema == 3
    assert not undone.effects and not undone.roles[0].effects
    assert not undone.scenes[0].effects and not undone.scenes[0].cues[0].effects
    redone = editor.history(undone, forward=True)
    assert redone.revision == 4
    assert replace(redone, revision=changed.revision) == changed
    assert source.read_bytes() == source_bytes
    service.close()


@pytest.mark.parametrize('scope', ['master', 'role', 'scene', 'clip'])
def test_timeline_rejects_enabled_tempo_before_saving_or_reading_audio(tmp_path, scope):
    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]))
    editor = ClipEditor(tmp_path, service=service)
    before = service.projects.save(Project.from_dict(legacy(2)))
    with pytest.raises(ValueError, match='preserve duration'):
        editor.set_effects(before, scope, (Effect('tempo', factor=2),),
                           scene_id='scene', clip_id='cue', role_id='voice')
    assert service.projects.load(before.id) == before
    assert not list(service.cache.iterdir())
    service.close()


def test_output_rate_revalidates_even_bypassed_filter_frequencies():
    with pytest.raises(ValueError, match='half'):
        replace(enriched(), sample_rate=16000,
                effects=(Effect('lowpass', frequency=10000, enabled=False),))


def test_script_edits_preserve_effects_and_do_not_discard_a_processed_role(tmp_path):
    controller = StudioController(SimpleNamespace(root=tmp_path))
    original = controller.save('[Scene: Scene]\nNarrator: Original words.', 'Project', ('voice',))
    modified = replace(original, schema=3, revision=2, effects=(Effect('gain', gain_db=-1),),
        roles=(replace(original.roles[0], effects=(Effect('highpass', frequency=80),)),),
        scenes=(replace(original.scenes[0], effects=(Effect('compressor', ratio=2),)),))
    controller.service.projects.save(modified, expected_revision=original.revision)
    controller.project = modified
    saved = controller.save('[Scene: Scene]\nNarrator: Revised words.', 'Project', ('other',))
    assert saved.effects == modified.effects and saved.roles == modified.roles
    assert saved.scenes[0].effects == modified.scenes[0].effects
    assert saved.revision == 3 and saved.schema == 3
    with pytest.raises(ValueError, match='scene|role'):
        controller.save('[Scene: Other]\n[Pause: 1]', 'Project', ('other',))
    assert controller.service.projects.load(saved.id) == saved
    controller.service.close()


def write_tone(path):
    values = array('h', (round(8000 * math.sin(2 * math.pi * 440 * n / 24000))
                         for n in range(24000)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(values.tobytes())
    return path


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        shape = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        raw = stream.readframes(stream.getnframes())
    values = array('h')
    values.frombytes(raw)
    if sys.byteorder != 'little':
        values.byteswap()
    return raw, values, shape


def rms(path):
    _, values, _ = pcm(path)
    return math.sqrt(math.fsum(value * value for value in values) / len(values))


def require_audio():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg and ffprobe are required.'


def test_actual_clip_scene_master_processing_preview_and_backup_restore(tmp_path):
    require_audio()
    from carr_library.library_backup import create_backup, restore_backup
    app = LibraryApp(tmp_path / 'library')
    voices = VoiceRegistry([])
    service = ProductionService(app.root / 'state/studio', voices=voices, runtime_factory=AudioRuntime)
    editor = ClipEditor(app.root, service=service)
    source = write_tone(tmp_path / 'Original — незмінний.wav')
    original = source.read_bytes()
    output = tmp_path / 'outputs'
    output.mkdir()
    project = editor.create('Measured effects')
    project = editor.add(project, project.scenes[0].id, source, gain_db=0, pan=-1)
    before = service.run(service.submit(project, output))
    scene_id, clip_id = project.scenes[0].id, project.scenes[0].cues[0].id
    project = editor.set_effects(project, 'clip', (Effect('gain', gain_db=-6),),
                                 scene_id=scene_id, clip_id=clip_id)
    wet = editor.preview(project, scene_id, clip_id)
    dry = editor.preview(project, scene_id, clip_id, dry=True)
    assert .497 < rms(wet) / rms(dry) < .505
    with pytest.raises(ValueError, match='Bypass'):
        editor.split(project, scene_id, clip_id, .5)
    project = editor.set_effects(project, 'scene', (Effect('gain', gain_db=-6),), scene_id=scene_id)
    project = editor.set_effects(project, 'master', (Effect('gain', gain_db=-6),))
    rendered = service.run(service.submit(project, output))
    master = Path(rendered['destination']) / 'master.wav'
    baseline = Path(before['destination']) / 'master.wav'
    assert .122 < rms(master) / rms(baseline) < .130
    stem = Path(rendered['destination']) / 'stems/clips/sfx.wav'
    assert .497 < rms(stem) / rms(baseline) < .505
    chapter, = (Path(rendered['destination']) / 'chapters').glob('*.wav')
    assert pcm(chapter)[0] == pcm(master)[0]
    assert pcm(master)[2] == pcm(baseline)[2]
    assert rendered['result']['effects']['master'] == [effect.to_dict() for effect in project.effects]
    assert source.read_bytes() == original

    backup = tmp_path / 'backup'
    receipt = create_backup(app, backup)
    expected_pcm = pcm(master)[0]
    service.close()
    voices.close()
    source.unlink()
    shutil.rmtree(app.root)
    destination = restore_backup(backup, tmp_path / 'restored', receipt)
    recovered = ProductionService(destination / 'state/studio', voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    try:
        restored = recovered.projects.load(project.id)
        assert restored.effects == project.effects
        assert restored.scenes[0].effects == project.scenes[0].effects
        assert restored.scenes[0].cues[0].effects == project.scenes[0].cues[0].effects
        assert Path(restored.scenes[0].cues[0].path).is_relative_to(destination)
        next_output = tmp_path / 'restored outputs'
        next_output.mkdir()
        result = recovered.run(recovered.submit(restored, next_output))
        assert pcm(Path(result['destination']) / 'master.wav')[0] == expected_pcm
    finally:
        recovered.close()


def test_actual_speech_cache_survives_role_and_master_effect_edits(tmp_path, monkeypatch):
    require_audio()
    voices = VoiceRegistry([NativeSapiProvider()] if os.name == 'nt'
                           else [FliteProvider(shutil.which('ffmpeg'))])
    available = voices.voices()
    assert available, 'An actual installed SAPI/Flite voice is required.'
    selected = available[0].id if os.name == 'nt' else 'flite:awb'
    assert selected in {voice.id for voice in available}
    calls = []
    actual_synthesis = voices.synthesize
    def synthesize(*args, **kwargs):
        calls.append(args[0])
        return actual_synthesis(*args, **kwargs)
    monkeypatch.setattr(voices, 'synthesize', synthesize)
    service = ProductionService(tmp_path / 'library/state/studio', voices=voices, runtime_factory=AudioRuntime)
    output = tmp_path / 'output'
    output.mkdir()
    try:
        project = service.create('[Scene: One]\nNarrator: These are the first words.\n'
            '[Scene: Two]\nNarrator: These are the final words.', voice_ids=(selected,))
        original = service.run(service.submit(project, output))
        assert len(calls) == 2
        changed = replace(project, schema=3, revision=2, effects=(Effect('gain', gain_db=-6),),
                          roles=(replace(project.roles[0], effects=(Effect('gain', gain_db=-6),)),))
        service.projects.save(changed, expected_revision=project.revision,
                              expected_fingerprint=project.fingerprint)
        result = service.run(service.submit(changed, output))
        assert len(calls) == 2, 'Effect edits must not repeat accepted voice synthesis.'
        speech_nodes = [row for row in result['nodes'].values() if row['kind'] == 'speech']
        assert len(speech_nodes) == 2 and all(row['reused'] for row in speech_nodes)
        assert result['result']['lines'] == original['result']['lines']
        assert result['result']['chapters'] == original['result']['chapters']
        master = Path(result['destination']) / 'master.wav'
        baseline = Path(original['destination']) / 'master.wav'
        assert .245 < rms(master) / rms(baseline) < .257
        joined = b''.join(pcm(path)[0] for path in sorted(
            (Path(result['destination']) / 'chapters').glob('*.wav')))
        assert joined == pcm(master)[0]
        assert (Path(result['destination']) / 'script.vtt').read_bytes() == (
            Path(original['destination']) / 'script.vtt').read_bytes()
        serialized = json.loads((Path(result['destination']) / 'project.json').read_bytes())
        assert serialized['original_script'] == project.original_script
        assert result['result']['source_sha256'] == original['result']['source_sha256']
    finally:
        service.close()
        voices.close()

def test_book_plan_legacy_role_wire_stays_stable_and_typed_effects_roundtrip(tmp_path):
    from carr_library.studio.book_audio import BookPlan, SplitOptions, capture_plan
    app = LibraryApp(tmp_path / 'book library')
    workspace = app.create_workspace('Books')
    imported = app.add_text(workspace, 'Source', 'These are exact retained source words.')
    role = Role('narrator', 'Narrator', 'review-only-local-id', 'en')
    plan = capture_plan(app, workspace, imported['edition'], options=SplitOptions(), role=role)
    old_role = {'id': role.id, 'name': role.name, 'voice_id': role.voice_id,
                'language': role.language, 'pan': role.pan, 'gain_db': role.gain_db,
                'rate': role.rate, 'volume': role.volume}
    serialized = plan.to_dict()
    assert serialized['role'] == old_role and 'effects' not in serialized['role']
    assert BookPlan.from_dict(json.loads(encode(serialized))).fingerprint == plan.fingerprint
    processed = replace(plan, role=replace(role, effects=(Effect('gain', gain_db=-3),)))
    reconstructed = BookPlan.from_dict(json.loads(encode(processed.to_dict())))
    assert reconstructed == processed and reconstructed.fingerprint == processed.fingerprint
    assert reconstructed.project(1).schema == 3
    assert reconstructed.project(1).roles[0].effects == processed.role.effects
    assert reconstructed.project(1).original_script == plan.project(1).original_script
