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
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Cue, Line, Project, Role, Scene
from carr_library.studio.render import ProductionService
from carr_library.ui.studio_dialog import StudioController


def legacy(schema):
    cue = {'id': 'cue', 'path': '/selected.wav', 'start': 0, 'gain_db': -6,
           'pan': 0, 'role': 'sfx'}
    if schema == 2:
        cue['path'] = 'inputs/' + '0' * 64 + '.audio'
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
    retained = editor.retain(source)
    value['scenes'][0]['cues'][0]['path'] = 'inputs/' + retained.name
    before = service.projects.save(Project.from_dict(value))
    args = {'scene_id': 'scene', 'clip_id': 'cue', 'role_id': 'voice'}
    changed = editor.set_effects(before, scope, (Effect('gain', gain_db=-6),), **args)
    assert changed.schema == 3 and changed.revision == 2
    with pytest.raises(ValueError, match='changed since'):
        editor.set_effects(before, scope, (), **args)
    historical = service.projects.directory(before.id) / 'revisions'
    old_raw = historical / ('00000001-' + before.fingerprint + '.json')
    assert old_raw.read_bytes() == encode(before.to_dict())
    # Clearing and restoring this reviewed chain use the same explicit CAS save;
    # this does not claim a separate project-wide Undo/Redo command.
    undone = editor.set_effects(changed, scope, (), **args)
    assert undone.revision == 3 and undone.schema == 3
    assert not undone.effects and not undone.roles[0].effects
    assert not undone.scenes[0].effects and not undone.scenes[0].cues[0].effects
    redone = editor.set_effects(undone, scope, (Effect('gain', gain_db=-6),), **args)
    assert redone.revision == 4
    assert replace(redone, revision=changed.revision) == changed
    assert len(list(historical.glob('*.json'))) == 3
    assert old_raw.read_bytes() == encode(before.to_dict())
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


@pytest.mark.parametrize('same_path', [False, True])
def test_actual_clip_scene_master_processing_preview_and_backup_restore(tmp_path, monkeypatch, same_path):
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
    shutil.rmtree(output)
    shutil.rmtree(app.root)
    destination = restore_backup(backup, app.root if same_path else tmp_path / 'restored', receipt)
    recovered = ProductionService(destination / 'state/studio', voices=VoiceRegistry([]), runtime_factory=AudioRuntime)
    try:
        restored = recovered.projects.load(project.id)
        assert restored.effects == project.effects
        assert restored.scenes[0].effects == project.scenes[0].effects
        assert restored.scenes[0].cues[0].effects == project.scenes[0].cues[0].effects
        assert restored.scenes[0].cues[0].path == 'inputs/' + sha256(original).hexdigest() + '.audio'
        assert (recovered.root / restored.scenes[0].cues[0].path).read_bytes() == original
        next_output = tmp_path / 'restored outputs'
        next_output.mkdir()
        old_open, old_stat, old_lstat = Path.open, Path.stat, Path.lstat
        def denied(method):
            def check(path, *args, **kwargs):
                if Path(path).absolute().is_relative_to(output.absolute()):
                    pytest.fail('Restored schema3 job inspected its old output before new approval.')
                return method(path, *args, **kwargs)
            return check
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', denied(old_open))
            patch.setattr(Path, 'stat', denied(old_stat))
            patch.setattr(Path, 'lstat', denied(old_lstat))
            assert recovered.get(rendered['id'])['recovery']['requires_output_directory']
            with pytest.raises(ValueError, match='restored|output|origin'):
                recovered.run(rendered['id'])
        recovered.relocate_restored_job(rendered['id'], next_output)
        result = recovered.run(rendered['id'])
        assert pcm(Path(result['destination']) / 'master.wav')[0] == expected_pcm
    finally:
        recovered.close()


def test_actual_speech_cache_survives_role_and_master_effect_edits(tmp_path, monkeypatch):
    require_audio()
    class ToneVoices:
        """Real PCM fixture, not speech/acoustic or native SAPI qualification."""
        def voices(self):
            return (SimpleNamespace(id='synthetic-tone', name='Controlled tone',
                                    language='en', engine='synthetic fixture'),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            assert voice_id == 'synthetic-tone' and not cancelled()
            write_tone(destination)
        def close(self):
            pass
    voices = ToneVoices()
    selected = 'synthetic-tone'
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

def test_book_plan_legacy_role_wire_stays_stable_and_effects_need_separate_review(tmp_path):
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
    assert plan.fingerprint == sha256(encode({**serialized, 'role': old_role})).hexdigest()
    with pytest.raises(ValueError, match='separate reviewed effect contract'):
        replace(plan, role=replace(role, effects=(Effect('gain', gain_db=-3),)))
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    editor = ClipEditor(app.root, service=service)
    project = service.projects.save(plan.project(1))
    path = service.projects.directory(project.id) / 'project.json'
    before = path.read_bytes()
    with pytest.raises(ValueError, match='book-audio plan'):
        editor.set_effects(project, 'master', (Effect('gain', gain_db=-6),))
    assert path.read_bytes() == before and not service.jobs()
    service.close()


def test_empty_effect_review_does_not_promote_or_create_a_revision(tmp_path):
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]))
    try:
        editor = ClipEditor(tmp_path / 'library', service=service)
        project = editor.create('Unprocessed portable project')
        path = service.projects.directory(project.id) / 'project.json'
        before = path.read_bytes()
        assert editor.set_effects(project, 'master', ()) == project
        assert path.read_bytes() == before and project.schema == 2
        assert list((path.parent / 'revisions').glob('*.json')) == []
    finally:
        service.close()


def test_legacy_absolute_clip_cannot_be_silently_promoted_by_effect_edit(tmp_path, monkeypatch):
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]))
    try:
        editor = ClipEditor(tmp_path / 'library', service=service)
        selected = tmp_path / 'external-never-opened.wav'
        selected.write_bytes(b'These bytes are not a migration approval.')
        value = legacy(1)
        value['scenes'][0]['cues'][0]['path'] = str(selected)
        project = service.projects.save(Project.from_dict(value))
        current = service.projects.directory(project.id) / 'project.json'
        before = current.read_bytes()
        actual_open = Path.open
        def protected(path, *args, **kwargs):
            if path == selected:
                pytest.fail('An effect review implicitly opened a legacy external cue.')
            return actual_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', protected)
        with pytest.raises(ValueError, match='legacy project'):
            editor.set_effects(project, 'master', (Effect('gain', gain_db=-3),))
        assert current.read_bytes() == before and not list(service.inputs.iterdir())
    finally:
        service.close()


def _actual_clip(tmp_path):
    require_audio()
    root = tmp_path / 'library'
    service = ProductionService(root / 'state/studio', voices=VoiceRegistry([]),
                                runtime_factory=AudioRuntime)
    editor = ClipEditor(root, service=service)
    source = write_tone(tmp_path / 'selected original.wav')
    project = editor.create('Actual retained clip')
    project = editor.add(project, project.scenes[0].id, source, gain_db=0, pan=-1)
    scene, cue = project.scenes[0], project.scenes[0].cues[0]
    project = editor.set_effects(project, 'clip', (Effect('gain', gain_db=-6),),
                                 scene_id=scene.id, clip_id=cue.id)
    return service, editor, project, source


@pytest.mark.parametrize('change', ['missing', 'tampered'])
def test_effect_job_refuses_changed_managed_bytes_before_codec_or_output(tmp_path, monkeypatch, change):
    service, editor, project, source = _actual_clip(tmp_path)
    try:
        output = tmp_path / 'outputs'
        output.mkdir()
        identity = service.submit(project, output)
        selected = service.root / project.scenes[0].cues[0].path
        if change == 'missing':
            selected.unlink()
        else:
            raw = selected.read_bytes()
            selected.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        monkeypatch.setattr(service, '_runtime',
            lambda: pytest.fail('A rejected retained source launched or inspected a codec.'))
        with pytest.raises((ValueError, FileNotFoundError)):
            service.run(identity)
        assert list(output.iterdir()) == []
        assert service.projects.load(project.id) == project
        assert source.is_file()
    finally:
        service.close()


def test_master_processing_must_admit_its_extra_pcm_and_chapter_copies(tmp_path, monkeypatch):
    require_audio()
    from carr_library.operations import ResourceLimits, ResourceUnavailable
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]),
                                runtime_factory=AudioRuntime)
    try:
        editor = ClipEditor(tmp_path / 'library', service=service)
        project = editor.create('Explicit silence with master processing')
        output = tmp_path / 'outputs'
        output.mkdir()
        plain_job = service.submit(project, output)
        plain, _ = service._resource_plan(project, service.get(plain_job), service._runtime())
        processed = editor.set_effects(project, 'master', (Effect('gain', gain_db=-3),))
        processed_job = service.submit(processed, output)
        changed, _ = service._resource_plan(processed, service.get(processed_job), service._runtime())
        assert changed.total_disk_bytes - plain.total_disk_bytes == 2 * project.sample_rate * 2 * 2
        service.broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0,
            disk_bytes=(plain.total_disk_bytes + changed.total_disk_bytes) // 2))
        monkeypatch.setattr(AudioRuntime, 'mix',
            lambda *args, **kwargs: pytest.fail('Unadmitted master processing started.'))
        with pytest.raises(ResourceUnavailable):
            service.run(processed_job)
        assert list(output.iterdir()) == [] and service.broker.snapshot()['leases'] == []
    finally:
        service.close()


def test_stop_at_actual_master_processing_preserves_original_and_prior_render(tmp_path):
    import threading
    from carr_library.audio import AudioCancelled
    service, editor, project, source = _actual_clip(tmp_path)
    try:
        output = tmp_path / 'outputs'
        output.mkdir()
        old = service.run(service.submit(project, output))
        old_root = Path(old['destination'])
        old_bytes = {path.relative_to(old_root): path.read_bytes()
                     for path in old_root.rglob('*') if path.is_file()}
        original = source.read_bytes()
        project = editor.set_effects(project, 'master', (Effect('gain', gain_db=-6),))
        identity = service.submit(project, output)
        stop = threading.Event()
        notices = []
        def progress(message):
            notices.append(message)
            if message == 'master effects: rendering local audio':
                stop.set()
        with pytest.raises((AudioCancelled, InterruptedError)):
            service.run(identity, cancelled=stop.is_set, progress=progress)
        assert stop.is_set() and 'master effects: rendering local audio' in notices
        assert not Path(service.get(identity)['destination']).exists()
        assert source.read_bytes() == original
        assert all((old_root / path).read_bytes() == raw for path, raw in old_bytes.items())
        stop.clear()
        result = service.run(identity, cancelled=stop.is_set)
        assert result['status'] == 'complete'
        assert .497 < rms(Path(result['destination']) / 'master.wav') / rms(old_root / 'master.wav') < .505
        assert service.broker.snapshot()['leases'] == []
    finally:
        service.close()


def test_waiting_clip_preview_cancels_before_source_inspection_or_decoder(tmp_path, monkeypatch):
    from threading import Event, Thread
    from carr_library.operations import ResourceCancelled, ResourceLimits, ResourceRequest
    service, editor, project, source = _actual_clip(tmp_path)
    stopped, waiting, errors = Event(), Event(), []
    service.broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    actual_reason = service.broker._reason
    def reason(*args):
        answer = actual_reason(*args)
        if answer == 'cpu_slots':
            waiting.set()
        return answer
    monkeypatch.setattr(service.broker, '_reason', reason)
    monkeypatch.setattr(AudioRuntime, 'inspect',
        lambda *args, **kwargs: pytest.fail('Waiting preview inspected an audio source.'))
    def work():
        try:
            editor.preview(project, project.scenes[0].id, project.scenes[0].cues[0].id,
                           cancelled=stopped.is_set)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    try:
        with service.broker.reserve(ResourceRequest(cpu_slots=2),
                                    operation_id='occupied-preview-cpu', timeout=0):
            worker.start()
            assert waiting.wait(10)
            stopped.set()
            worker.join(10)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], ResourceCancelled)
            assert len(service.broker.snapshot()['leases']) == 1
        assert service.broker.snapshot()['leases'] == []
        assert not list(service.cache.glob('clip-preview-*'))
    finally:
        stopped.set()
        if worker.ident is not None:
            worker.join(15)
        service.close()


def test_unreaped_clip_preview_child_retains_real_admission_until_death(tmp_path, monkeypatch):
    import subprocess
    import time
    from threading import Event, Thread
    from carr_library.audio.cleanup import CleanupOwner
    from carr_library.audio.models import AudioCleanupPending
    from carr_library.operations import ResourceLimits, ResourceRequest, ResourceTimeout
    service, editor, project, source = _actual_clip(tmp_path)
    shared_runtime = service._runtime()
    original_budget = shared_runtime.budget
    monkeypatch.setattr(service, '_runtime', lambda: shared_runtime)
    service.broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    ready, release = tmp_path / 'child-ready', tmp_path / 'release-child'
    code = ("import sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_bytes(b'ready'); release=Path(sys.argv[2]); "
            "exec('while not release.exists():\\n time.sleep(.01)')")
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(ready), str(release)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = CleanupOwner(lambda: child.poll() is not None)
    pending = AudioCleanupPending('Controlled actual preview cleanup', cleanup_owner=owner)
    entered, errors = Event(), []
    def interrupted(*args, **kwargs):
        assert ready.is_file() and child.poll() is None
        entered.set()
        raise pending
    monkeypatch.setattr(AudioRuntime, 'mix', interrupted)
    def work():
        try:
            editor.preview(project, project.scenes[0].id, project.scenes[0].cues[0].id)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.is_file() and child.poll() is None
        worker.start()
        assert entered.wait(15) and worker.is_alive()
        assert not owner.close()
        with pytest.raises(ResourceTimeout):
            with service.broker.reserve(ResourceRequest(cpu_slots=2),
                                        operation_id='unreaped-preview-competitor', timeout=0):
                pytest.fail('A live preview child released admission.')
        release.write_bytes(b'release')
        worker.join(15)
        assert not worker.is_alive() and errors == [pending]
        assert owner.quiescent and child.poll() == 0
        assert shared_runtime.budget == original_budget
        assert service.broker.snapshot()['leases'] == []
        assert not list(service.cache.glob('clip-preview-*'))
    finally:
        release.write_bytes(b'release')
        child.wait(timeout=10)
        owner.close()
        if worker.ident is not None:
            worker.join(15)
        service.close()
