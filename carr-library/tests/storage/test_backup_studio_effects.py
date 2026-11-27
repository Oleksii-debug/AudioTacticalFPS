"""Passive schema3 Studio backup bindings; synthetic interrupted records are not audio acceptance."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, Effect
from carr_library.durable_state import atomic_json, encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import VoiceRegistry
from carr_library.state_backup import restore_state, snapshot_state
from carr_library.studio.project import Cue, Project, Role, Scene, project_effects
from carr_library.studio.render import ProductionService


@pytest.fixture
def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    selected = tmp_path / 'explicit-original.wav'
    with wave.open(str(selected), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\x01\x00' * 24000)
    raw = selected.read_bytes()
    retained = service.inputs / (sha256(raw).hexdigest() + '.audio')
    retained.write_bytes(raw)
    project = Project('processed', 'Passive processing fixture', (Role('role', 'Unused narrator'),),
        (Scene('scene', 'One', (), cues=(Cue('cue', 'inputs/' + retained.name,
            effects=(Effect('gain', gain_db=-3),)),),
            effects=(Effect('highpass', frequency=80),)),),
        schema=3, effects=(Effect('gain', gain_db=-6), Effect('denoise', enabled=False)))
    service.projects.save(project)
    output = tmp_path / 'selected-output'
    output.mkdir()
    identity = service.submit(project, output)
    job = service.get(identity)
    # These are deliberately synthetic interrupted-render declarations. They
    # test passive wire/linkage validation; measured audio is covered elsewhere.
    metadata = {'schema': 1, 'project': project.id, 'revision': project.revision,
        'source_sha256': sha256(project.original_script.encode()).hexdigest(),
        'chapters': [{'id': 'scene', 'title': 'One', 'start': 0, 'end': 1}],
        'lines': [], 'duration': 1, 'sample_rate': 24000, 'channels': 2,
        'mode': 'faithful', 'local_only': True, 'loudness': {},
        'music_kind': 'No music', 'timing_notice': 'Synthetic passive fixture',
        'effects': project_effects(project)}
    job['status'], job['result'] = 'failed', metadata
    service._save(job)
    job_root = service.jobs_root / identity
    atomic_json(job_root / 'project.json', project.to_dict())
    atomic_json(job_root / 'timeline.json', metadata)
    service.close()
    return app, project, identity, selected, raw, job


def snapshot(app, folder, route):
    if route == 'library':
        create_backup(app, folder)
        return folder, json.loads((folder / 'manifest.json').read_bytes()), route
    folder.mkdir()
    return folder, snapshot_state(app.root / 'state', folder), route


def entry_list(saved):
    return saved[1]['sidecars'] if saved[2] == 'library' else saved[1]


def name_for(saved, relative):
    return ('state/' if saved[2] == 'library' else '') + relative


def put_record(saved, relative, value):
    raw = value if isinstance(value, bytes) else encode(value)
    name = name_for(saved, relative)
    path = saved[0] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    entries = entry_list(saved)
    entries[:] = [entry for entry in entries if entry['filename'] != name]
    entries.append({'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})


def restore(saved, root):
    folder, manifest, route = saved
    if route == 'library':
        raw = encode(manifest)
        (folder / 'manifest.json').write_bytes(raw)
        return restore_backup(folder, root, sha256(raw).hexdigest())
    restore_state(folder, root / 'state', manifest)
    return root


def forbidden(*args, **kwargs):
    pytest.fail('Passive processing backup started a codec, source producer or child process.')


@pytest.mark.parametrize('route', ['library', 'state'])
def test_processing_project_roundtrip_preserves_bytes_and_excludes_output_authority(
        prepared, tmp_path, monkeypatch, route):
    app, project, identity, selected, original, job = prepared
    relative = 'studio/projects/' + project.id + '/project.json'
    expected = (app.root / 'state' / relative).read_bytes()
    old_output = Path(job['destination']).parent
    actual_open = Path.open
    def guarded(path, *args, **kwargs):
        if Path(path).absolute().is_relative_to(old_output.absolute()):
            pytest.fail('Passive backup opened an old output destination.')
        return actual_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(AudioRuntime, '__init__', forbidden)
        patch.setattr(ProductionService, 'run', forbidden)
        patch.setattr(subprocess, 'Popen', forbidden)
        patch.setattr(Path, 'open', guarded)
        saved = snapshot(app, tmp_path / 'backup', route)
        assert not any('.local-origin.json' in item['filename'] for item in entry_list(saved))
        selected.unlink()
        shutil.rmtree(app.root)
        root = restore(saved, tmp_path / 'restored')
    assert (root / 'state' / relative).read_bytes() == expected
    assert (root / 'state/studio' / project.scenes[0].cues[0].path).read_bytes() == original
    assert not (root / 'state/studio/.local-origin.json').exists()
    recovered = ProductionService(root / 'state/studio', voices=VoiceRegistry([]))
    try:
        assert recovered.projects.load(project.id) == project
        assert recovered.get(identity)['recovery']['requires_output_directory']
    finally:
        recovered.close()


@pytest.mark.parametrize('route', ['library', 'state'])
@pytest.mark.parametrize('damage', ['project-control', 'project-tempo', 'result-chain',
    'result-missing-effects', 'timeline-chain', 'timeline-other-field',
    'exported-project', 'missing-input', 'changed-input'])
def test_resigned_processing_backup_cannot_change_typed_project_or_linked_receipts(
        prepared, tmp_path, route, damage):
    app, project, identity, selected, original, job = prepared
    saved = snapshot(app, tmp_path / 'backup', route)
    job_name = 'studio/jobs/' + identity + '/job.json'
    if damage in ('project-control', 'project-tempo'):
        current = json.loads(encode(project.to_dict()))
        current['effects'][0] = ({'kind': 'gain', 'parameters': {'gain_db': 'volume=9'}, 'enabled': True}
            if damage == 'project-control' else
            {'kind': 'tempo', 'parameters': {'factor': 2}, 'enabled': True})
        put_record(saved, 'studio/projects/' + project.id + '/project.json', current)
    elif damage.startswith('result-'):
        current = deepcopy(job)
        if damage == 'result-chain':
            current['result']['effects']['master'][0]['parameters']['gain_db'] = -12
        else:
            current['result'].pop('effects')
        put_record(saved, job_name, current)
    elif damage.startswith('timeline-'):
        current = deepcopy(job['result'])
        if damage == 'timeline-chain':
            current['effects']['clips']['scene']['cue'][0]['parameters']['gain_db'] = -9
        else:
            current['timing_notice'] = 'This differs from the saved job receipt.'
        put_record(saved, 'studio/jobs/' + identity + '/timeline.json', current)
    elif damage == 'exported-project':
        current = json.loads(encode(project.to_dict()))
        current['title'] = 'Another exported project'
        put_record(saved, 'studio/jobs/' + identity + '/project.json', current)
    else:
        relative = 'studio/' + project.scenes[0].cues[0].path
        if damage == 'changed-input':
            put_record(saved, relative, original[:-1] + bytes([original[-1] ^ 1]))
        else:
            name = name_for(saved, relative)
            (saved[0] / name).unlink()
            rows = entry_list(saved)
            rows[:] = [entry for entry in rows if entry['filename'] != name]
    with pytest.raises((ValueError, FileNotFoundError)):
        restore(saved, tmp_path / 'unaccepted')
    assert selected.read_bytes() == original
    assert (app.root / 'state/studio/projects' / project.id / 'project.json').read_bytes() == encode(project.to_dict())


def test_historical_processing_result_must_retain_the_same_reviewed_chain(prepared):
    from carr_library.backup_producers import _studio_job
    app, project, identity, selected, original, job = prepared
    current = deepcopy(job)
    previous = {key: deepcopy(job.get(key)) for key in
                ('origin', 'destination', 'status', 'nodes', 'artifacts', 'runtime', 'result')}
    current['recovery'] = {'requires_output_directory': False, 'previous': [previous],
                           'output_approved': job['updated']}
    _studio_job(current, identity)
    previous['result']['effects']['master'][0]['parameters']['gain_db'] = -9
    with pytest.raises(ValueError, match='effects'):
        _studio_job(current, identity)
