"""Real PCM Director effect buses, selective cache reuse and retained owners.

The deterministic voice fixture writes declared PCM; actual FFmpeg, the shared
broker, immutable cache and publication paths are exercised. No acoustic voice
quality, private model inference or human listening is claimed.
"""
from collections import Counter
from dataclasses import replace
from hashlib import sha256
import json
import math
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import cleanup, dsp, process
from carr_library.audio.director import ProductionDirector, file_sha256
from carr_library.audio.models import AudioBudget, Effect
from carr_library.audio.projects import (
    Cue, EFFECTS_STEMS_NOTICE, Project, ProjectStore, Role, Scene, Settings,
    canonical, parse_script, read_json)
from carr_library.audio.voices import Voice
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceUnavailable
from carr_library.operations._locks import NativeLock


SCRIPT = '# First\nAlice: First unchanged line.\n# Second\nBob: Second unchanged line.\n'


def effects(project, scope, chain, index=0):
    if scope == 'master':
        return replace(project, effects=chain, revision=project.revision + 1)
    name = 'roles' if scope == 'role' else 'scenes'
    values = list(getattr(project, name))
    values[index] = replace(values[index], effects=chain)
    return replace(project, **{name: tuple(values)}, revision=project.revision + 1)


class PCMVoices:
    def __init__(self, broker):
        self.broker, self.calls = broker, []
        self.voice = Voice('fixture:pcm', 'fixture', 'Declared synthetic PCM', ('en',), ('pcm',))

    def list_voices(self):
        leases = self.broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['request']['cpu_slots'] == 2
        return (self.voice,)

    def fingerprint(self):
        return 'director-effects-synthetic-pcm-v1'

    def synthesize(self, text, destination, *, cancelled=None, **kwargs):
        self.list_voices()
        assert not cancelled()
        self.calls.append((text, Path(destination)))
        frequency = 300 + sum(text.encode('utf-8')) % 500
        samples = [round(3000 * math.sin(2 * math.pi * frequency * index / 8000))
                   for index in range(2400)]
        with wave.open(str(destination), 'wb') as output:
            output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
        return SimpleNamespace(path=Path(destination))


def prepared(tmp_path, *, with_effects=True, disk=256 * 1024**2):
    root = tmp_path / 'library'
    store = ProjectStore(root)
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=512 * 1024**2, disk_bytes=disk, min_free_disk_bytes=0))
    project = parse_script(SCRIPT, ['fixture:pcm'], settings=Settings(
        sample_rate=8000, channels=2, stems=True, chapter_gap_seconds=.1))
    project = replace(project, roles=tuple(replace(role, pan=-1 if index == 0 else 1,
        effects=(Effect('gain', gain_db=0),) if with_effects else ())
        for index, role in enumerate(project.roles)),
        scenes=tuple(replace(scene, effects=(Effect('gain', gain_db=0),) if with_effects else ())
                     for scene in project.scenes))
    store.create(project)
    voices = PCMVoices(broker)
    return store, project, voices, broker, ProductionDirector(root, registry=voices, music_options={}, broker=broker)


def pcm(path):
    with wave.open(str(path), 'rb') as source:
        assert source.getsampwidth() == 2 and source.getnchannels() == 2
        return source.getnframes(), source.readframes(source.getnframes())


def cache_rows(store, project):
    cache = store.path(project.id) / 'cache'
    return {path.stem: read_json(path) for path in cache.glob('*.json')}


def added(before, after):
    return Counter(row['kind'] for key, row in after.items() if key not in before)


def test_empty_chains_keep_the_exact_legacy_project_and_role_json():
    role = Role('r', 'Narrator', 'fixture:pcm')
    project = Project('p', 'Title', 'Text', (role,),
        (Scene('s', 'Chapter', (Cue('c', 'speech', 'Text', 'r', source_end=4),)),),
        created_at='2026-01-01T00:00:00+00:00')
    expected_role = {'id': 'r', 'name': 'Narrator', 'voice_id': 'fixture:pcm',
                     'language': 'en', 'rate': 1.0, 'volume': 1.0, 'pan': 0.0}
    expected = {'id': 'p', 'title': 'Title', 'script': 'Text', 'roles': (expected_role,),
        'scenes': ({'id': 's', 'title': 'Chapter', 'cues': ({
            'id': 'c', 'kind': 'speech', 'text': 'Text', 'role': 'r', 'seconds': 0,
            'asset': '', 'gain_db': 0, 'source_start': 0, 'source_end': 4},), 'music': None},),
        'settings': {'sample_rate': 24000, 'channels': 2, 'format': 'wav', 'stems': True,
            'chunk_characters': 700, 'chapter_gap_seconds': .75, 'disk_budget_bytes': 0},
        'revision': 1, 'created_at': '2026-01-01T00:00:00+00:00', 'source': {},
        'kind': 'research-audio-project', 'version': 1, 'narration': {},
        'script_sha256': sha256(b'Text').hexdigest()}
    assert project.to_dict() == expected and role.to_dict() == expected_role
    assert canonical(project.to_dict()) == canonical(expected)
    assert project.fingerprint == sha256(canonical(expected).encode('utf-8')).hexdigest()
    assert Project.from_dict(expected) == project
    legacy_without_narration = dict(expected)
    legacy_without_narration.pop('narration')
    assert Project.from_dict(legacy_without_narration) == project


@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
def test_ordered_effects_roundtrip_keep_original_text_and_detached_numeric_data(scope):
    project = parse_script(SCRIPT, ['fixture:pcm'])
    controls = {'gain_db': -4}
    value = effects(project, scope, (Effect('gain', controls),
        Effect('reverb', room_ms=30, mix=.2, enabled=False)))
    controls['gain_db'] = 99
    saved = value.to_dict()
    assert Project.from_dict(json.loads(canonical(saved))) == value
    assert value.script == project.script and value.script_sha256 == project.script_sha256
    assert value.fingerprint != project.fingerprint
    chain = saved['effects'] if scope == 'master' else saved['roles' if scope == 'role' else 'scenes'][0]['effects']
    assert [item['kind'] for item in chain] == ['gain', 'reverb']
    assert chain[0]['parameters'] == {'gain_db': -4} and chain[1]['enabled'] is False
    chain[0]['parameters']['gain_db'] = 17
    actual = value.effects if scope == 'master' else getattr(value, 'roles' if scope == 'role' else 'scenes')[0].effects
    assert actual[0].parameters['gain_db'] == -4


@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
@pytest.mark.parametrize('invalid', ['raw-filter', 'unknown-control', 'nonfinite', 'too-many', 'bad-shape'])
def test_project_rejects_untyped_or_unbounded_effect_declarations(scope, invalid):
    project = parse_script('Kept source.', ['fixture:pcm'])
    data = project.to_dict()
    if invalid == 'raw-filter':
        chain = ['volume=2;amovie=private.wav']
    elif invalid == 'unknown-control':
        chain = [Effect('gain', filter_graph='volume=2').to_dict()]
    elif invalid == 'nonfinite':
        chain = [Effect('gain', gain_db=float('nan'), enabled=False).to_dict()]
    elif invalid == 'too-many':
        chain = [Effect('gain').to_dict()] * 33
    else:
        chain = [{'kind': 'gain', 'parameters': {'gain_db': 0}}]
    target = data if scope == 'master' else data['roles' if scope == 'role' else 'scenes'][0]
    target['effects'] = chain
    with pytest.raises(ValueError):
        Project.from_dict(data)


@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
def test_project_checks_bypassed_nyquist_and_enabled_tempo_at_actual_output_rate(scope):
    project = parse_script('Kept source.', ['fixture:pcm'], settings=Settings(sample_rate=8000))
    with pytest.raises(ValueError, match='half'):
        effects(project, scope, (Effect('equalizer', frequency=4000, enabled=False),))
    with pytest.raises(ValueError, match='preserve duration'):
        effects(project, scope, (Effect('tempo', factor=1.2),))


def test_saved_chapter_effects_follow_only_an_unambiguous_title_and_roles_keep_their_chain():
    old = parse_script(SCRIPT, ['fixture:pcm'])
    old = effects(effects(effects(old, 'role', (Effect('gain', gain_db=-3),)),
        'scene', (Effect('reverb'),)), 'master', (Effect('compressor'),))
    swapped = '# Second\nBob: Second unchanged line.\n# First\nAlice: A changed line.\n'
    new = parse_script(swapped, ['fixture:pcm'], previous=old)
    assert new.effects == old.effects
    assert next(role for role in new.roles if role.name == 'Alice').effects == old.roles[0].effects
    assert new.scenes[1].effects == old.scenes[0].effects and not new.scenes[0].effects
    assert new.script == swapped and old.script == SCRIPT
    for script in (SCRIPT.replace('# First', '# Renamed'),
                   SCRIPT + '# First\nAlice: Ambiguous scene.\n'):
        with pytest.raises(ValueError, match='renamed, removed or became ambiguous'):
            parse_script(script, ['fixture:pcm'], previous=old)


def test_project_history_export_and_failed_cas_preserve_reviewed_effects(tmp_path):
    store = ProjectStore(tmp_path / 'library')
    first = store.create(parse_script(SCRIPT, ['fixture:pcm']))
    updated = effects(first, 'master', (Effect('reverb'), Effect('compressor')))
    store.save(updated, expected_fingerprint=first.fingerprint)
    assert Project.from_dict(read_json(store.path(first.id) / 'revisions' / '00000001.json')) == first
    before = (store.path(first.id) / 'project.json').read_bytes()
    with pytest.raises(ValueError, match='another window'):
        store.save(effects(first, 'role', (Effect('gain', gain_db=-6),)), expected_fingerprint=first.fingerprint)
    assert (store.path(first.id) / 'project.json').read_bytes() == before
    exported = tmp_path / 'project.json'
    store.export(updated.id, exported)
    restored = store.import_project(exported)
    assert restored.id != updated.id and restored.effects == updated.effects
    assert restored.script == updated.script and restored.scenes == updated.scenes


def test_actual_role_scene_master_edits_reuse_only_unaffected_cached_nodes(tmp_path):
    store, project, voices, broker, director = prepared(tmp_path)
    first = director.render(project.id, tmp_path / 'first')
    baseline = cache_rows(store, project)
    calls = list(voices.calls)
    assert first['state'] == 'complete' and len(calls) == 2
    assert first['cache_notes'] and 'First adoption' in first['cache_notes'][0]
    original = {path.name: path.read_bytes() for path in Path(first['output_dir']).iterdir()}

    role_edit = effects(project, 'role', (Effect('gain', gain_db=-9),))
    store.save(role_edit, expected_fingerprint=project.fingerprint)
    second = director.render(project.id, tmp_path / 'role')
    after_role = cache_rows(store, project)
    assert added(baseline, after_role) == Counter({
        'role-effects': 1, 'scene': 1, 'scene-effects': 1, 'stem': 1, 'master': 1, 'master-stem': 1})
    assert second['generated_steps'] == 6 and voices.calls == calls
    assert pcm(Path(first['output_dir']) / 'chapter-0002.wav') == pcm(Path(second['output_dir']) / 'chapter-0002.wav')
    assert pcm(Path(first['output_dir']) / ('stem-' + project.roles[1].id + '.wav')) == pcm(
        Path(second['output_dir']) / ('stem-' + project.roles[1].id + '.wav'))
    assert pcm(Path(first['output_dir']) / 'chapter-0001.wav') != pcm(Path(second['output_dir']) / 'chapter-0001.wav')

    scene_edit = effects(role_edit, 'scene', (Effect('gain', gain_db=-3),))
    store.save(scene_edit, expected_fingerprint=role_edit.fingerprint)
    third = director.render(project.id, tmp_path / 'scene')
    after_scene = cache_rows(store, project)
    assert added(after_role, after_scene) == Counter({'scene-effects': 1, 'master': 1})
    for role in project.roles:
        assert pcm(Path(second['output_dir']) / ('stem-' + role.id + '.wav')) == pcm(
            Path(third['output_dir']) / ('stem-' + role.id + '.wav'))

    master_edit = effects(scene_edit, 'master', (Effect('compressor', threshold_db=-35, ratio=4),))
    store.save(master_edit, expected_fingerprint=scene_edit.fingerprint)
    fourth = director.render(project.id, tmp_path / 'master')
    assert added(after_scene, cache_rows(store, project)) == Counter({
        'master-effects': 1, 'master-effect-chapter': 2})
    assert voices.calls == calls and broker.snapshot()['leases'] == []
    assert {path.name: path.read_bytes() for path in Path(first['output_dir']).iterdir()} == original
    assert store.load(project.id).script == SCRIPT
    report = read_json(Path(fourth['output_dir']) / 'production.json')
    assert report['effects']['master'] == [effect.to_dict() for effect in master_edit.effects]
    assert EFFECTS_STEMS_NOTICE in report['limitations']


def test_master_reverb_is_processed_once_and_chapters_copy_exact_master_frames(tmp_path, monkeypatch):
    store, project, voices, broker, director = prepared(tmp_path)
    project = effects(project, 'master', (Effect('reverb', room_ms=70, decay=.6, mix=.7),))
    store.save(project)
    actual = dsp.AudioRuntime.convert
    buses = []
    def convert(self, source, destination, **kwargs):
        if any(effect.kind == 'reverb' for effect in kwargs.get('effects', ())):
            buses.append((Path(source), Path(destination)))
        return actual(self, source, destination, **kwargs)
    monkeypatch.setattr(dsp.AudioRuntime, 'convert', convert)
    result = director.render(project.id, tmp_path / 'complete')
    master_frames, master = pcm(result['master'])
    report = read_json(Path(result['output_dir']) / 'production.json')
    assert len(buses) == 1 and master_frames == 5600
    combined = b''
    for index, row in enumerate(report['chapters'], 1):
        path = Path(result['output_dir']) / f'chapter-{index:04}.wav'
        frames, data = pcm(path)
        assert frames == row['frames']
        assert data == master[row['start_frame'] * 4:(row['start_frame'] + frames) * 4]
        assert file_sha256(path) == row['sha256']
        combined += data
    assert combined == master and broker.snapshot()['leases'] == []
    assert len(voices.calls) == 2


def test_pause_reopen_and_resume_keeps_accepted_effect_nodes_and_raw_speech(tmp_path):
    store, project, voices, broker, director = prepared(tmp_path)
    stop = threading.Event()
    def progress(value):
        if value['message'].startswith('Scene 1 saved'):
            stop.set()
    with pytest.raises(InterruptedError):
        director.render(project.id, tmp_path / 'output', cancelled=stop.is_set, progress=progress)
    job, = director.jobs(project.id)
    assert job['state'] == 'paused' and broker.snapshot()['leases'] == []
    accepted = {path.name: path.read_bytes() for path in (store.path(project.id) / 'cache').iterdir()}
    assert any(row['kind'] == 'scene-effects' for row in cache_rows(store, project).values())
    stop.clear()
    result = ProductionDirector(store.root, registry=voices, music_options={}, broker=broker).render(
        project.id, job_id=job['id'])
    assert result['state'] == 'complete' and len(voices.calls) == 2
    assert all((store.path(project.id) / 'cache' / name).read_bytes() == data for name, data in accepted.items())
    assert store.load(project.id).script == SCRIPT


def test_processed_cache_tamper_regenerates_that_node_without_new_synthesis(tmp_path):
    store, project, voices, broker, director = prepared(tmp_path)
    first = director.render(project.id, tmp_path / 'first')
    old_master = Path(first['master']).read_bytes()
    calls = list(voices.calls)
    key = next(key for key, row in cache_rows(store, project).items() if row['kind'] == 'role-effects')
    target = store.path(project.id) / 'cache' / (key + '.wav')
    target.write_bytes(b'changed accepted PCM')
    second = director.render(project.id, tmp_path / 'second')
    assert second['generated_steps'] == 1 and voices.calls == calls
    assert Path(first['master']).read_bytes() == old_master
    assert Path(second['master']).read_bytes() == old_master
    assert list(target.parent.glob(key + '.wav.invalid-*'))


@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
def test_wholly_bypassed_chain_keeps_legacy_pcm_without_starting_effect_runtime(tmp_path, monkeypatch, scope):
    store, project, voices, broker, director = prepared(tmp_path, with_effects=False)
    first = director.render(project.id, tmp_path / 'dry')
    changed = effects(project, scope, (Effect('gain', gain_db=-18, enabled=False),))
    store.save(changed)
    monkeypatch.setattr(dsp, 'AudioRuntime', lambda *a, **k: pytest.fail('Bypassed chain entered DSP.'))
    second = director.render(project.id, tmp_path / 'bypassed')
    assert pcm(first['master']) == pcm(second['master']) and len(voices.calls) == 2
    assert not any(row['kind'].endswith('-effects') for row in cache_rows(store, project).values())
    assert broker.snapshot()['leases'] == []


def test_effect_admission_reserves_extra_pcm_and_refuses_before_runtime_construction(tmp_path, monkeypatch):
    store, project, voices, broker, director = prepared(tmp_path, disk=8 * 1024**2)
    monkeypatch.setattr(dsp, 'AudioRuntime', lambda *a, **k: pytest.fail('DSP preceded resource admission.'))
    with pytest.raises(ResourceUnavailable, match='disk_bytes'):
        director.render(project.id, tmp_path / 'never')
    assert voices.calls == [] and not (tmp_path / 'never').exists()
    assert broker.snapshot()['leases'] == []
    plain_project = replace(project,
        roles=tuple(replace(role, effects=()) for role in project.roles),
        scenes=tuple(replace(scene, effects=()) for scene in project.scenes))
    folder = store.path(project.id)
    base, _ = director._resource_request(plain_project, folder, tmp_path / 'base', lambda: False)
    actual, _ = director._resource_request(project, folder, tmp_path / 'fx', lambda: False)
    seconds = sum(len(cue.text) * .5 / next(role.rate for role in project.roles if role.id == cue.role)
                  for scene in project.scenes for cue in scene.cues)
    seconds += project.settings.chapter_gap_seconds
    estimated_pcm = math.ceil(seconds * 8000 * 2 * 2)
    assert actual.disk_bytes - base.disk_bytes == 4 * estimated_pcm
    assert actual.cpu_slots == base.cpu_slots == 2
    assert actual.memory_bytes == base.memory_bytes == 512 * 1024**2


@pytest.mark.parametrize('masked', [False, True], ids=['direct-pending', 'masked-pending'])
def test_actual_effect_worker_retains_pcm_stage_lock_and_broker_until_native_retry(tmp_path, monkeypatch, masked):
    store, project, voices, broker, director = prepared(tmp_path)
    owners, release, selected = [], threading.Event(), []
    actual_stop, actual_run = process._stop_owned, dsp.run_process
    actual_pending = cleanup.pending_cleanup
    observed_thread, staged_outputs = threading.get_ident(), set()
    def manual(attempt, *, message, callbacks=()):
        if threading.get_ident() != observed_thread:
            return actual_pending(attempt, message=message, callbacks=callbacks)
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    def stop(child):
        quiet = actual_stop(child)
        if not any(str(arg) in staged_outputs for arg in child.args):
            return quiet
        assert quiet is True
        if not release.is_set():
            selected.append(child)
            raise TimeoutError('Declared uncertainty after actual selected FFmpeg exit.')
        return quiet
    def run(*args, **kwargs):
        if threading.get_ident() != observed_thread:
            return actual_run(*args, **kwargs)
        command = args[0]
        output = Path(command[-1])
        if (output.name == 'render.wav' and output.parent.name.startswith('.audio-')
                and output.is_relative_to(store.path(project.id))):
            staged_outputs.add(str(output))
        leases = broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['request']['cpu_slots'] == 2
        try:
            return actual_run(*args, **kwargs)
        except cleanup.AudioCleanupPending as exc:
            if masked:
                raise ValueError('Declared wrapper failure') from exc
            raise
    monkeypatch.setattr(cleanup, 'pending_cleanup', manual)
    monkeypatch.setattr(process, '_stop_owned', stop)
    monkeypatch.setattr(dsp, 'run_process', run)
    try:
        with pytest.raises((cleanup.AudioCleanupPending, ValueError)) as caught:
            director.render(project.id, tmp_path / 'output')
        assert selected and selected[0].poll() == 0
        assert set(cleanup._pending_owners(caught.value)) == set(owners)
        stages = list(store.path(project.id).rglob('.audio-*'))
        assert len(stages) == 1
        staged = stages[0] / 'render.wav'
        assert staged.is_file() and pcm(staged)[0] == 2400
        lock = NativeLock(store.path(project.id) / '.render.lock')
        assert not lock.acquire() and len(broker.snapshot()['leases']) == 1
        assert not (tmp_path / 'output' / 'master.wav').exists()
        release.set()
        for owner in owners:
            assert owner.close()
        assert not stages[0].exists() and broker.snapshot()['leases'] == []
        assert lock.acquire()
        lock.release()
        job, = director.jobs(project.id)
        assert director.render(project.id, job_id=job['id'])['state'] == 'complete'
        assert len(voices.calls) == 2
    finally:
        release.set()
        for owner in owners:
            assert owner.close()


def test_effect_project_publishes_through_the_existing_primary_source_validator(tmp_path):
    from carr_library.studio.publishing_source import DirectorPublicationSource
    store, project, voices, broker, director = prepared(tmp_path)
    result = director.render(project.id, tmp_path / 'output')
    source = DirectorPublicationSource(store.root, project.id)
    state = source.get(result['job_id'])
    with source.publication_lock(result['job_id']):
        snapshot = source.publication_snapshot(state, AudioBudget(max_output_bytes=64 * 1024**2),
                                                lambda: None, lambda value: None)
    assert snapshot
    assert source.get(result['job_id']) == state
    assert store.load(project.id).script == SCRIPT


def test_actual_dynamics_order_changes_pcm_without_changing_time_or_synthesizing_again(tmp_path):
    store, project, voices, broker, director = prepared(tmp_path)
    gain = Effect('gain', gain_db=12)
    compressor = Effect('compressor', threshold_db=-30, ratio=8, attack_ms=.1, release_ms=10)
    first_project = effects(project, 'master', (gain, compressor))
    store.save(first_project)
    first = director.render(project.id, tmp_path / 'gain-first')
    second_project = effects(first_project, 'master', (compressor, gain))
    store.save(second_project)
    second = director.render(project.id, tmp_path / 'compress-first')
    assert pcm(first['master'])[0] == pcm(second['master'])[0] == 5600
    assert pcm(first['master'])[1] != pcm(second['master'])[1]
    assert len(voices.calls) == 2 and broker.snapshot()['leases'] == []
    assert first_project.effects == (gain, compressor)
    assert second_project.effects == (compressor, gain)


def test_unavailable_room_filter_refuses_without_substitution_or_master_publication(tmp_path, monkeypatch):
    from carr_library.audio.models import AudioUnavailable
    store, project, voices, broker, director = prepared(tmp_path)
    project = effects(project, 'scene', (Effect('reverb'),))
    store.save(project)
    original = dsp.AudioRuntime.capabilities
    def no_room(self, **kwargs):
        result = original(self, **kwargs)
        result['filters'] = [name for name in result['filters'] if name != 'aecho']
        return result
    monkeypatch.setattr(dsp.AudioRuntime, 'capabilities', no_room)
    with pytest.raises(AudioUnavailable, match='reverb'):
        director.render(project.id, tmp_path / 'output')
    assert not (tmp_path / 'output' / 'master.wav').exists()
    assert any(row['kind'] == 'speech' for row in cache_rows(store, project).values())
    assert not any(row['kind'] == 'scene-effects' for row in cache_rows(store, project).values())
    assert broker.snapshot()['leases'] == [] and store.load(project.id) == project


@pytest.mark.parametrize('boundary', ['scene-input', 'master-chapter-input'])
def test_changed_accepted_input_cannot_publish_an_effect_cache_receipt(tmp_path, monkeypatch, boundary):
    store, project, voices, broker, director = prepared(tmp_path)
    if boundary == 'master-chapter-input':
        project = effects(project, 'master', (Effect('gain', gain_db=-3),))
        store.save(project)
    method = 'convert' if boundary == 'scene-input' else 'extract_pcm_frames'
    original = getattr(dsp.AudioRuntime, method)
    touched = []
    def change(self, source, destination, **kwargs):
        result = original(self, source, destination, **kwargs)
        if not touched:
            path = Path(source)
            raw = bytearray(path.read_bytes())
            raw[-2:] = b'\x00\x00' if raw[-2:] != b'\x00\x00' else b'\x01\x00'
            path.write_bytes(raw)
            touched.append(path)
        return result
    monkeypatch.setattr(dsp.AudioRuntime, method, change)
    with pytest.raises(ValueError, match='changed'):
        director.render(project.id, tmp_path / 'output')
    assert touched and not (tmp_path / 'output' / 'master.wav').exists()
    refused_kind = 'scene-effects' if boundary == 'scene-input' else 'master-effect-chapter'
    assert not any(row['kind'] == refused_kind for row in cache_rows(store, project).values())
    assert broker.snapshot()['leases'] == [] and store.load(project.id).script == SCRIPT
