"""Persisted envelopes use existing Studio history, receipts and passive restore."""
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
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime, Effect
from carr_library.audio.envelopes import envelope_effect
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import (
    Cue, Line, Project, Role, Scene, project_effects, validate_project_effects,
)
from carr_library.studio.render import ProductionService


def project():
    gain = envelope_effect('gain_envelope', ((0, 0), (.25, -6), (.5, 0)))
    pan = envelope_effect('pan_envelope', ((0, -1), (.5, 1)))
    width = envelope_effect('width_envelope', ((0, 1), (.5, 0)))
    return Project('curves', 'Reviewed envelopes', (Role('r', 'Narrator', effects=(pan,)),),
        (Scene('s', 'Scene', (Line('line', 'r', 'Immutable words.'),),
            cues=(Cue('c', 'inputs/' + '0' * 64 + '.audio', effects=(gain,)),),
            effects=(width,)),), original_script='Immutable words.', schema=3, effects=(gain,))


def test_existing_schema_three_carries_exact_ordered_numeric_curves():
    before = project()
    raw = encode(before.to_dict())
    restored = Project.from_dict(json.loads(raw))
    assert restored == before and restored.fingerprint == sha256(raw).hexdigest()
    assert encode(project_effects(restored)) == encode(project_effects(before))
    assert restored.original_script == 'Immutable words.'
    validate_project_effects(restored, {'effects': project_effects(before)})


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_empty_curve_extension_does_not_change_legacy_project_or_role_wire(schema):
    old = Project('legacy', 'Legacy', (Role('r', 'Narrator'),),
                  (Scene('s', 'Scene', (Line('l', 'r', 'Original.'),)),), schema=schema)
    expected = {'id': 'legacy', 'title': 'Legacy', 'roles': [
        {'id': 'r', 'name': 'Narrator', 'voice_id': '', 'language': '', 'pan': 0,
         'gain_db': -3, 'rate': 0, 'volume': 100}],
        'scenes': [{'id': 's', 'title': 'Scene', 'lines': [
            {'id': 'l', 'role': 'r', 'text': 'Original.', 'pause_after': .3, 'source_ref': ''}],
            'music': None, 'cues': [], 'minimum_duration': 0}],
        'original_script': '', 'mode': 'faithful', 'sample_rate': 24000,
        'revision': 1, 'schema': schema}
    if schema == 3:
        expected['effects'] = []
        expected['roles'][0]['effects'] = []
        expected['scenes'][0]['effects'] = []
    assert encode(old.to_dict()) == encode(expected)
    assert old.fingerprint == sha256(encode(expected)).hexdigest()
    assert 'effects' not in old.roles[0].to_dict()


@pytest.mark.parametrize('scope', ['master', 'scene', 'role-fixed', 'clip-fixed', 'role-width', 'clip-width'])
def test_invalid_curve_placement_is_rejected_on_deserialization(scope):
    value = project().to_dict()
    # Use a JSON processing copy: stored tuples are serialized arrays.
    value = json.loads(encode(value))
    pan, width = Effect('pan_envelope').to_dict(), Effect('width_envelope').to_dict()
    if scope == 'master':
        value['effects'] = [pan]
    elif scope == 'scene':
        value['scenes'][0]['effects'] = [pan]
    elif scope == 'role-fixed':
        value['roles'][0]['pan'] = .5
    elif scope == 'clip-fixed':
        value['scenes'][0]['cues'][0].update(pan=-.5, effects=[pan])
    elif scope == 'role-width':
        value['roles'][0]['effects'] = [width]
    else:
        value['scenes'][0]['cues'][0]['effects'] = [width]
    with pytest.raises(ValueError, match='envelope'):
        Project.from_dict(value)


@pytest.mark.parametrize('scope', ['master', 'roles', 'scenes', 'clips'])
def test_timeline_receipt_cannot_relabel_saved_curve_points(scope):
    before = project()
    metadata = deepcopy(project_effects(before))
    if scope == 'master':
        target = metadata['master'][0]
    elif scope == 'roles':
        target = metadata['roles']['r'][0]
    elif scope == 'scenes':
        target = metadata['scenes']['s'][0]
    else:
        target = metadata['clips']['s']['c'][0]
    target['parameters']['time_2'] += .1
    with pytest.raises(ValueError, match='differ'):
        validate_project_effects(before, {'effects': metadata})


def test_curve_save_uses_existing_revision_cas_and_retains_previous_bytes(tmp_path):
    service = ProductionService(tmp_path / 'library/state/studio', voices=VoiceRegistry([]))
    editor = ClipEditor(tmp_path / 'library', service=service)
    original = service.projects.save(project())
    curve = envelope_effect('gain_envelope', ((0, -9), (.75, 0)))
    try:
        changed = editor.set_effects(original, 'master', (curve,))
        assert changed.revision == 2 and changed.schema == 3
        assert changed.original_script == original.original_script
        assert service.projects.load(original.id) == changed
        historical = service.projects.directory(original.id) / 'revisions' / (
            '00000001-' + original.fingerprint + '.json')
        assert historical.read_bytes() == encode(original.to_dict())
        with pytest.raises(ValueError, match='changed since'):
            editor.set_effects(original, 'master', ())
        assert not list(service.cache.iterdir())
        assert changed.roles == original.roles and changed.scenes == original.scenes
    finally:
        service.close()


def test_passive_backup_restores_curve_bytes_without_codec_or_output_authority(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]))
    raw_path = tmp_path / 'original.wav'
    with wave.open(str(raw_path), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        output.writeframes(b'\x01\x00' * 24000)
    raw = raw_path.read_bytes()
    relative = 'inputs/' + sha256(raw).hexdigest() + '.audio'
    (service.root / relative).write_bytes(raw)
    before = project()
    before = replace(before, scenes=(replace(before.scenes[0], lines=(),
        cues=(replace(before.scenes[0].cues[0], path=relative),)),))
    service.projects.save(before)
    selected_output = tmp_path / 'output'
    selected_output.mkdir()
    job_id = service.submit(before, selected_output)
    service.close()
    expected = encode(before.to_dict())
    def forbidden(*args, **kwargs):
        pytest.fail('Passive envelope backup attempted runtime execution.')
    with monkeypatch.context() as patch:
        patch.setattr(AudioRuntime, '__init__', forbidden)
        patch.setattr(ProductionService, 'run', forbidden)
        patch.setattr(subprocess, 'Popen', forbidden)
        receipt = create_backup(app, tmp_path / 'backup')
        raw_path.unlink()
        shutil.rmtree(app.root)
        restored_root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', receipt)
    recovered = ProductionService(restored_root / 'state/studio', voices=VoiceRegistry([]))
    try:
        assert recovered.projects.load(before.id) == before
        assert (recovered.projects.directory(before.id) / 'project.json').read_bytes() == expected
        assert (recovered.root / relative).read_bytes() == raw
        assert not (recovered.root / '.local-origin.json').exists()
        assert recovered.get(job_id)['recovery']['requires_output_directory']
    finally:
        recovered.close()


def _samples(path):
    with wave.open(str(path), 'rb') as source:
        shape = source.getframerate(), source.getnchannels(), source.getnframes()
        values = array('h')
        values.frombytes(source.readframes(shape[2]))
    if sys.byteorder != 'little':
        values.byteswap()
    return values, shape


def test_actual_render_reuses_completed_scene_nodes_and_exports_exact_curve_receipt(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual FFmpeg and ffprobe are required.'
    app = LibraryApp(tmp_path / 'library')
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]),
                                runtime_factory=AudioRuntime)
    editor = ClipEditor(app.root, service=service)
    source = tmp_path / 'actual.wav'
    values = array('h', (round(2400 * math.sin(2 * math.pi * 317 * i / 24000)) for i in range(24000)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(values.tobytes())
    original = source.read_bytes()
    output = tmp_path / 'outputs'
    output.mkdir()
    try:
        before = editor.create('Measured envelope')
        before = editor.add(before, before.scenes[0].id, source, gain_db=0, pan=0)
        dry = service.run(service.submit(before, output))
        curve = envelope_effect('gain_envelope', ((0, 0), (.5, -12)), interpolation='hold')
        after = editor.set_effects(before, 'master', (curve,))
        wet = service.run(service.submit(after, output))
        old, old_shape = _samples(Path(dry['destination']) / 'master.wav')
        new, new_shape = _samples(Path(wet['destination']) / 'master.wav')
        assert old_shape == new_shape == (24000, 2, 24000)
        for frame, gain in ((6013, 1), (18013, 10 ** (-12 / 20))):
            for channel in (0, 1):
                at = frame * 2 + channel
                assert abs(new[at] - old[at] * gain) <= 3
        scenes = [node for node in wet['nodes'].values() if node['kind'] == 'scene mix']
        assert scenes and all(node['reused'] for node in scenes)
        assert wet['result']['effects'] == project_effects(after)
        assert wet['result']['source_sha256'] == dry['result']['source_sha256']
        chapter, = (Path(wet['destination']) / 'chapters').glob('*.wav')
        assert _samples(chapter) == (new, new_shape)
        assert source.read_bytes() == original
        assert service.broker.snapshot()['leases'] == []
    finally:
        service.close()
