"""Schema4 markers through real rendering, retained inputs and passive backup boundaries."""
from array import array
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, Effect
from carr_library.backup_producers import _studio_job
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import VoiceRegistry
from carr_library.state_backup import restore_state, snapshot_state
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Cue, Project, Role, Scene, project_effects
from carr_library.studio.render import ProductionService


def tone(path, frames=24000):
    values = array('h', (round(6000 * math.sin(2 * math.pi * 330 * n / 24000))
                         for n in range(frames)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(values.tobytes())
    return path


def pcm(path):
    with wave.open(str(path), 'rb') as stream:
        shape = stream.getframerate(), stream.getnchannels(), stream.getnframes()
        return shape, stream.readframes(stream.getnframes())


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    try:
        selected = tone(tmp_path / 'selected original.wav', frames=2400)
        original = selected.read_bytes()
        retained = service.inputs / (sha256(original).hexdigest() + '.audio')
        retained.write_bytes(original)
        base = Project('marked', 'Passive marker fixture', (Role('role', 'Unused narrator'),),
            (Scene('scene', 'One', (), cues=(Cue('cue', 'inputs/' + retained.name,
                effects=(Effect('gain', gain_db=-3),)),),
                effects=(Effect('highpass', frequency=80),)),),
            schema=3, effects=(Effect('gain', gain_db=-6),))
        service.projects.save(base)
        project = service.projects.add_marker(base, scene_id='scene', position=10,
            label='Reviewed navigation point', marker_id='marker')
        output = tmp_path / 'old selected output'
        output.mkdir()
        identity = service.submit(project, output)
        job = service.get(identity)
        # This interrupted receipt is deliberately synthetic and passive. The
        # last test separately renders actual PCM with the real audio runtime.
        metadata = {'schema': 1, 'project': project.id, 'revision': project.revision,
            'source_sha256': sha256(project.original_script.encode()).hexdigest(),
            'chapters': [{'id': 'scene', 'title': 'One', 'start': 0, 'end': .1}],
            'lines': [], 'duration': .1, 'sample_rate': 24000, 'channels': 2,
            'mode': 'faithful', 'local_only': True, 'loudness': {},
            'music_kind': 'No music', 'timing_notice': 'Synthetic passive marker fixture',
            'effects': project_effects(project)}
        job['status'], job['result'] = 'failed', metadata
        service._save(job)
        job_root = service.jobs_root / identity
        atomic_json(job_root / 'project.json', project.to_dict())
        atomic_json(job_root / 'timeline.json', metadata)
        _studio_job(job, identity)
        return SimpleNamespace(app=app, base=base, project=project, selected=selected,
            original=original, identity=identity, job=job, output=output)
    finally:
        service.close()


def snapshot(prepared, folder, route):
    if route == 'library':
        create_backup(prepared.app, folder)
        return folder, json.loads((folder / 'manifest.json').read_bytes()), route
    folder.mkdir()
    return folder, snapshot_state(prepared.app.root / 'state', folder), route


def entries(saved):
    return saved[1]['sidecars'] if saved[2] == 'library' else saved[1]


def stored_name(saved, relative):
    return ('state/' if saved[2] == 'library' else '') + relative


def put_record(saved, relative, value):
    raw = value if isinstance(value, bytes) else encode(value)
    name = stored_name(saved, relative)
    target = saved[0] / name
    target.write_bytes(raw)
    rows = entries(saved)
    rows[:] = [row for row in rows if row['filename'] != name]
    rows.append({'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})


def restore(saved, root):
    folder, manifest, route = saved
    if route == 'library':
        raw = encode(manifest)
        (folder / 'manifest.json').write_bytes(raw)
        return restore_backup(folder, root, sha256(raw).hexdigest())
    restore_state(folder, root / 'state', manifest)
    return root


def forbidden(*args, **kwargs):
    pytest.fail('A passive marker operation started audio, synthesis or a child process.')


@pytest.mark.parametrize('route', ['library', 'state'])
def test_marker_backup_preserves_current_history_inputs_and_passive_output_boundary(
        prepared, tmp_path, monkeypatch, route):
    item = prepared
    relative = 'studio/projects/' + item.project.id + '/project.json'
    historical = ('studio/projects/' + item.base.id + '/revisions/'
                  + f'{item.base.revision:08d}-{item.base.fingerprint}.json')
    expected = (item.app.root / 'state' / relative).read_bytes()
    history = (item.app.root / 'state' / historical).read_bytes()
    actual_open = Path.open
    def opened(path, *args, **kwargs):
        if Path(path).absolute().is_relative_to(item.output.absolute()):
            pytest.fail('Passive marker backup opened a recorded output destination.')
        return actual_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(AudioRuntime, '__init__', forbidden)
        patch.setattr(ProductionService, 'run', forbidden)
        patch.setattr(subprocess, 'Popen', forbidden)
        patch.setattr(Path, 'open', opened)
        saved = snapshot(item, tmp_path / 'backup', route)
        assert not any('.local-origin.json' in row['filename'] for row in entries(saved))
        item.selected.unlink()
        shutil.rmtree(item.app.root)
        root = restore(saved, tmp_path / 'restored')
    assert (root / 'state' / relative).read_bytes() == expected
    assert (root / 'state' / historical).read_bytes() == history == encode(item.base.to_dict())
    assert 'markers' not in json.loads(history)
    assert not (root / 'state/studio/.local-origin.json').exists()
    service = ProductionService(root / 'state/studio', voices=VoiceRegistry([]))
    try:
        assert service.projects.load(item.project.id) == item.project
        assert (service.root / item.project.scenes[0].cues[0].path).read_bytes() == item.original
        assert service.get(item.identity)['recovery']['requires_output_directory'] is True
    finally:
        service.close()


@pytest.mark.parametrize('route', ['library', 'state'])
@pytest.mark.parametrize('damage', [
    'marker-scene', 'marker-grid', 'marker-duplicate', 'job-marker',
    'exported-marker', 'history-marker', 'effects', 'missing-origin',
    'missing-input', 'changed-input',
])
def test_resigned_marker_backup_still_requires_typed_identity_effects_and_retained_sources(
        prepared, tmp_path, route, damage):
    item = prepared
    saved = snapshot(item, tmp_path / 'backup', route)
    current_name = 'studio/projects/' + item.project.id + '/project.json'
    job_name = 'studio/jobs/' + item.identity + '/job.json'
    if damage.startswith('marker-'):
        value = json.loads(encode(item.project.to_dict()))
        if damage == 'marker-scene':
            value['markers'][0]['scene_id'] = 'not-a-current-scene'
        elif damage == 'marker-grid':
            value['markers'][0]['position'] = .5 / item.project.sample_rate
        else:
            value['markers'].append(deepcopy(value['markers'][0]))
        put_record(saved, current_name, value)
    elif damage == 'job-marker':
        value = deepcopy(item.job)
        value['project']['markers'][0]['label'] = 'Unreviewed plan mutation'
        put_record(saved, job_name, value)
    elif damage == 'exported-marker':
        value = json.loads(encode(item.project.to_dict()))
        value['markers'][0]['label'] = 'Different exported marker'
        put_record(saved, 'studio/jobs/' + item.identity + '/project.json', value)
    elif damage == 'history-marker':
        value = json.loads(encode(item.base.to_dict()))
        value['markers'] = json.loads(encode(item.project.to_dict()))['markers']
        relative = ('studio/projects/' + item.base.id + '/revisions/'
                    + f'{item.base.revision:08d}-{item.base.fingerprint}.json')
        put_record(saved, relative, value)
    elif damage == 'effects':
        value = deepcopy(item.job)
        value['result']['effects']['master'][0]['parameters']['gain_db'] = -12
        put_record(saved, job_name, value)
    elif damage == 'missing-origin':
        value = deepcopy(item.job)
        value.pop('origin')
        put_record(saved, job_name, value)
    else:
        relative = 'studio/' + item.project.scenes[0].cues[0].path
        if damage == 'changed-input':
            put_record(saved, relative, item.original[:-1] + bytes([item.original[-1] ^ 1]))
        else:
            name = stored_name(saved, relative)
            (saved[0] / name).unlink()
            rows = entries(saved)
            rows[:] = [row for row in rows if row['filename'] != name]
    with pytest.raises((ValueError, FileNotFoundError)):
        restore(saved, tmp_path / 'unaccepted')
    assert item.selected.read_bytes() == item.original
    assert (item.app.root / 'state' / current_name).read_bytes() == encode(item.project.to_dict())


@pytest.mark.parametrize('route', ['library', 'state'])
def test_restored_marker_job_needs_fresh_output_before_any_recorded_path_or_runtime_access(
        prepared, tmp_path, monkeypatch, route):
    item = prepared
    saved = snapshot(item, tmp_path / 'backup', route)
    root = restore(saved, tmp_path / 'restored')
    service = ProductionService(root / 'state/studio', voices=VoiceRegistry([]))
    path = service.jobs_root / item.identity / 'job.json'
    original = path.read_bytes()
    try:
        actual_open, actual_stat, actual_lstat = Path.open, Path.stat, Path.lstat
        def guarded(method):
            def check(path, *args, **kwargs):
                absolute = Path(path).absolute()
                if absolute == item.selected.absolute() or absolute.is_relative_to(item.output.absolute()):
                    pytest.fail('A restored marker job touched an old selected path.')
                return method(path, *args, **kwargs)
            return check
        with monkeypatch.context() as patch:
            patch.setattr(service, '_runtime', forbidden)
            patch.setattr(Path, 'open', guarded(actual_open))
            patch.setattr(Path, 'stat', guarded(actual_stat))
            patch.setattr(Path, 'lstat', guarded(actual_lstat))
            assert service.get(item.identity)['recovery']['requires_output_directory'] is True
            with pytest.raises(ValueError, match='restored|output|origin'):
                service.run(item.identity)
            with pytest.raises(ValueError, match='restored|output|origin'):
                service.submit(item.project, item.output, operation_id=item.identity)
            assert path.read_bytes() == original
            fresh = tmp_path / 'new approved output'
            fresh.mkdir()
            relocated = service.relocate_restored_job(item.identity, fresh)
            _studio_job(relocated, item.identity)
            assert relocated['status'] == 'pending'
            assert relocated['recovery']['requires_output_directory'] is False
            assert relocated['recovery']['previous'][0]['destination'] == item.job['destination']
            assert relocated['recovery']['previous'][0]['result'] == item.job['result']
            assert Project.from_dict(relocated['project']).markers == item.project.markers
            assert not Path(relocated['destination']).exists()
        assert item.selected.read_bytes() == item.original
    finally:
        service.close()


@pytest.mark.parametrize('scope', ['master', 'role', 'scene', 'clip'])
def test_effect_edits_keep_marker_schema_identity_and_the_existing_history(prepared, monkeypatch, scope):
    item = prepared
    service = ProductionService(item.app.root / 'state/studio', voices=VoiceRegistry([]))
    try:
        editor = ClipEditor(item.app.root, service=service)
        monkeypatch.setattr(service, '_runtime', forbidden)
        before = service.projects.load(item.project.id)
        changed = editor.set_effects(before, scope, (Effect('gain', gain_db=-9),),
            scene_id='scene', clip_id='cue', role_id='role')
        assert changed.schema == 4 and changed.markers == before.markers
        assert changed.revision == before.revision + 1
        assert service.projects.load(changed.id) == changed
        path = service.projects.directory(before.id) / 'revisions' / (
            f'{before.revision:08d}-{before.fingerprint}.json')
        assert path.read_bytes() == encode(before.to_dict())
        assert (service.root / changed.scenes[0].cues[0].path).read_bytes() == item.original
        assert item.selected.read_bytes() == item.original
    finally:
        service.close()


@pytest.mark.parametrize('damage', ['changed-bytes', 'cancelled'])
def test_schema_four_source_resolution_keeps_fresh_hash_and_original_cancellation(
        prepared, monkeypatch, damage):
    item = prepared
    service = ProductionService(item.app.root / 'state/studio', voices=VoiceRegistry([]))
    try:
        monkeypatch.setattr(service, '_runtime', forbidden)
        cue = item.project.scenes[0].cues[0]
        source = service.root / cue.path
        stop = InterruptedError('Original marker source stop')
        if damage == 'changed-bytes':
            source.write_bytes(item.original[:-1] + bytes([item.original[-1] ^ 1]))
            with pytest.raises(ValueError, match='content identity'):
                service._cue_source(cue, schema=4)
        else:
            def cancelled():
                raise stop
            with pytest.raises(InterruptedError) as caught:
                service._cue_source(cue, schema=4, cancelled=cancelled)
            assert caught.value is stop
            assert source.read_bytes() == item.original
        assert service.projects.load(item.project.id) == item.project
        assert item.selected.read_bytes() == item.original
    finally:
        service.close()


def test_actual_marker_only_render_reuses_speech_and_preserves_pcm_timing_and_restored_effects(
        tmp_path, monkeypatch):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg and ffprobe are required.'
    class ToneVoices:
        """Synthetic PCM fixture; no speech model or acoustic qualification."""
        def __init__(self):
            self.calls = []
        def voices(self):
            return (SimpleNamespace(id='tone', name='Synthetic tone', language='en',
                                    engine='synthetic fixture'),)
        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            assert voice_id == 'tone' and not cancelled()
            self.calls.append(text)
            tone(destination)
        def close(self):
            pass

    app = LibraryApp(tmp_path / 'library')
    voices = ToneVoices()
    service = ProductionService(app.root / 'state/studio', voices=voices,
                                runtime_factory=AudioRuntime)
    source = tone(tmp_path / 'unchanged original.wav', frames=2400)
    original_bytes = source.read_bytes()
    output = tmp_path / 'renders'
    output.mkdir()
    try:
        editor = ClipEditor(app.root, service=service)
        project = service.create('[Scene: One]\nNarrator: Exact retained words.', voice_ids=('tone',))
        project = editor.add(project, project.scenes[0].id, source, gain_db=0, pan=-1)
        scene = project.scenes[0]
        base = replace(project, schema=3, revision=project.revision + 1,
            effects=(Effect('gain', gain_db=-3),),
            roles=(replace(project.roles[0], effects=(Effect('gain', gain_db=-3),)),),
            scenes=(replace(scene, effects=(Effect('gain', gain_db=-3),),
                cues=(replace(scene.cues[0], effects=(Effect('gain', gain_db=-3),)),)),))
        service.projects.save(base, expected_revision=project.revision,
                              expected_fingerprint=project.fingerprint)
        first = service.run(service.submit(base, output))
        assert first['status'] == 'complete' and len(voices.calls) == 1
        baseline = pcm(Path(first['destination']) / 'master.wav')
        marked = service.projects.add_marker(base, scene_id=scene.id, position=10,
            label='Beyond audio without extending it', marker_id='navigation')
        assert marked.scenes == base.scenes and marked.roles == base.roles and marked.effects == base.effects
        second = service.run(service.submit(marked, output))
        assert second['status'] == 'complete' and len(voices.calls) == 1
        speech_nodes = [row for row in second['nodes'].values() if row['kind'] == 'speech']
        assert len(speech_nodes) == 1 and speech_nodes[0]['reused'] is True
        assert pcm(Path(second['destination']) / 'master.wav') == baseline
        assert baseline[0][2] / baseline[0][0] < marked.markers[0].position
        for name in ('lines', 'chapters', 'duration', 'source_sha256', 'effects'):
            assert second['result'][name] == first['result'][name]
        assert (Path(second['destination']) / 'script.vtt').read_bytes() == (
            Path(first['destination']) / 'script.vtt').read_bytes()
        exported = Project.from_dict(json.loads((Path(second['destination']) / 'project.json').read_bytes()))
        assert exported == marked
        assert source.read_bytes() == original_bytes
        backup = tmp_path / 'backup'
        receipt = create_backup(app, backup)
    finally:
        service.close()
        voices.close()

    source.unlink()
    shutil.rmtree(output)
    shutil.rmtree(app.root)
    restored = restore_backup(backup, tmp_path / 'restored', receipt)
    restored_voices = ToneVoices()
    recovered = ProductionService(restored / 'state/studio', voices=restored_voices,
                                  runtime_factory=AudioRuntime)
    try:
        current = recovered.projects.load(marked.id)
        assert current == marked and current.markers == marked.markers
        assert (recovered.root / current.scenes[0].cues[0].path).read_bytes() == original_bytes
        assert recovered.get(second['id'])['recovery']['requires_output_directory'] is True
        with monkeypatch.context() as patch:
            patch.setattr(recovered, '_runtime', forbidden)
            with pytest.raises(ValueError, match='restored|output|origin'):
                recovered.run(second['id'])
        destination = tmp_path / 'new approved render'
        destination.mkdir()
        recovered.relocate_restored_job(second['id'], destination)
        rendered = recovered.run(second['id'])
        assert rendered['status'] == 'complete'
        assert pcm(Path(rendered['destination']) / 'master.wav') == baseline
        assert rendered['result']['effects'] == project_effects(marked)
        exported = Project.from_dict(json.loads((Path(rendered['destination']) / 'project.json').read_bytes()))
        assert exported == marked
    finally:
        recovered.close()
        restored_voices.close()
