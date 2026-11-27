"""Retained Studio receipts stay passive, typed and compatible across versions."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import backup_producers
from carr_library.audio import AudioBudget, AudioRuntime, Effect
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Project, Scene
from carr_library.studio.render import ProductionService


def timeline(version=3):
    value = {'schema': 1, 'project': 'project', 'revision': 2,
        'source_sha256': sha256(b'').hexdigest(),
        'chapters': [{'id': 'scene', 'title': 'Scene', 'start': 0, 'end': 2}],
        'lines': [], 'duration': 2, 'sample_rate': 48000, 'channels': 2,
        'mode': 'authored', 'local_only': True, 'loudness': {},
        'music_kind': 'explicit local selection', 'timing_notice': 'No inferred transcript.'}
    if version >= 2:
        value['clips'] = [{'scene': 'scene', 'id': 'clip', 'role': 'microphone', 'title': '',
            'source_sha256': 'a' * 64, 'source_duration': 2, 'trim_start': .25,
            'duration': .5, 'start': 1, 'end': 1.5, 'gain_db': -6, 'pan': 0,
            'fade_in': .1, 'fade_out': .1}]
        value['clip_stems_notice'] = 'Aligned track stems; no inferred transcript.'
    if version >= 3:
        value['effects'] = {'master': [Effect('gain', gain_db=-6).to_dict()],
            'roles': {}, 'scenes': {'scene': []},
            'clips': {'scene': {'clip': [Effect('gain', enabled=False, gain_db=-3).to_dict()]}},
            'stems': 'Track effects included; mixed bus effects remain on their buses.'}
    return value


def check(value):
    return backup_producers.validate(None, 'state/studio/jobs/job/timeline.json', value)


@pytest.mark.parametrize('version', [1, 2, 3])
def test_legacy_and_edited_receipts_validate_without_changing_retained_bytes(version, monkeypatch):
    value = timeline(version)
    raw = json.dumps(value, sort_keys=True)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Passive restore must not open external paths or start engines.')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(AudioRuntime, '__init__', forbidden)
    monkeypatch.setattr(VoiceRegistry, '__init__', forbidden)
    check(value)
    assert json.dumps(value, sort_keys=True) == raw


@pytest.mark.parametrize('change', [
    lambda v: v['clips'].append(deepcopy(v['clips'][0])),
    lambda v: v['clips'][0].update(scene='absent'),
    lambda v: v['clips'][0].update(id='../outside'),
    lambda v: v['clips'][0].update(source_sha256='changed'),
    lambda v: v['clips'][0].update(source_duration=float('nan')),
    lambda v: v['clips'][0].update(trim_start=3),
    lambda v: v['clips'][0].update(duration=True),
    lambda v: v['clips'][0].update(end=1.75),
    lambda v: v['clips'][0].update(fade_in=1),
    lambda v: v['clips'][0].update(role='default-device'),
    lambda v: v['clips'][0].update(path='/do-not-read/private-key'),
    lambda v: v.pop('clip_stems_notice'),
    lambda v: v['effects']['master'].append(Effect('tempo', factor=2).to_dict()),
    lambda v: v['effects']['master'].append({'kind': 'unknown', 'parameters': {}, 'enabled': True}),
    lambda v: v['effects']['master'][0].update(unreviewed=True),
    lambda v: v['effects']['master'][0].update(enabled=1),
    lambda v: v['effects']['master'][0]['parameters'].update(gain_db=float('inf')),
    lambda v: v['effects']['scenes'].update(absent=[]),
    lambda v: v['effects']['clips']['scene'].update(absent=[]),
    lambda v: v['effects']['clips']['scene'].pop('clip'),
    lambda v: v['effects'].update(command='execute'),
    lambda v: v.update(local_only=False),
])
def test_changed_or_executable_looking_timeline_is_rejected_before_restore(change):
    value = timeline()
    change(value)
    with pytest.raises(ValueError):
        check(value)


def test_disabled_tempo_remains_passive_but_cannot_change_timeline_duration():
    value = timeline()
    value['effects']['master'] = [Effect('tempo', enabled=False, factor=2).to_dict()]
    check(value)


def actual_job(tmp_path):
    root = tmp_path / 'library' / 'state' / 'studio'
    output = tmp_path / 'output'
    output.mkdir()
    service = ProductionService(root, voices=VoiceRegistry([]))
    project = Project('project', 'Passive backup', (), (Scene('scene', 'Silence', (), minimum_duration=1),))
    identity = service.submit(project, output)
    state = service.get(identity)
    runtime = SimpleNamespace(budget=AudioBudget(), music_models={})
    _, state['resource_plan'] = service._resource_plan(project, state, runtime)
    service.close()
    return identity, state


def test_actual_resource_plan_and_prior_wire_shape_are_both_passive(tmp_path, monkeypatch):
    identity, state = actual_job(tmp_path)
    plan = state['resource_plan']
    assert plan['estimated_duration'] == 1 and plan['source_sha256'] == {}
    plan['source_sha256'] = {'/unavailable/original.wav': 'f' * 64}
    raw = json.dumps(state, sort_keys=True)
    def forbidden(*_args, **_kwargs):
        raise AssertionError('A saved estimate must never authorize opening its source.')
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(Path, 'resolve', forbidden)
    backup_producers.validate(None, f'state/studio/jobs/{identity}/job.json', state)
    assert json.dumps(state, sort_keys=True) == raw
    plan.pop('estimated_duration')
    plan.pop('source_sha256')
    backup_producers.validate(None, f'state/studio/jobs/{identity}/job.json', state)


@pytest.mark.parametrize('change', [
    lambda p: p.pop('source_sha256'),
    lambda p: p.pop('estimated_duration'),
    lambda p: p.update(source_sha256={'https://unrequested.invalid/audio': 'f' * 64}),
    lambda p: p.update(source_sha256={'/local/audio': 'bad-digest'}),
    lambda p: p.update(source_sha256=[]),
    lambda p: p.update(estimated_duration=True),
    lambda p: p.update(estimated_duration=float('nan')),
    lambda p: p.update(estimated_duration=-1),
])
def test_untrusted_resource_identity_or_estimate_cannot_enter_backup(tmp_path, change):
    identity, state = actual_job(tmp_path)
    change(state['resource_plan'])
    with pytest.raises(ValueError):
        backup_producers.validate(None, f'state/studio/jobs/{identity}/job.json', state)


@pytest.mark.parametrize('marker', [True, False])
def test_passive_rebackup_retains_the_exact_restored_output_marker(tmp_path, marker):
    identity, state = actual_job(tmp_path)
    state['restore_requires_new_output'] = marker
    backup_producers.validate(None, f'state/studio/jobs/{identity}/job.json', state)
    assert state['restore_requires_new_output'] is marker


@pytest.mark.parametrize('marker', [0, 1, 'false', None])
def test_restored_output_marker_cannot_be_truthy_coercion(tmp_path, marker):
    identity, state = actual_job(tmp_path)
    state['restore_requires_new_output'] = marker
    with pytest.raises(ValueError, match='output authorization'):
        backup_producers.validate(None, f'state/studio/jobs/{identity}/job.json', state)
