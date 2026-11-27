"""Actual shared admission and retained primary Director ownership.

The small local Python voice writes declared fixture PCM. FFmpeg inspection,
mixing, cache publication, broker leases and native locks are real; these tests
make no claim about a neural model or human listening.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import cleanup, process, render, voices
from carr_library.audio.director import ProductionDirector, file_sha256
from carr_library.audio.projects import ProjectStore, Settings, atomic_json, parse_script
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceUnavailable
from carr_library.operations._locks import NativeLock


class FixtureRegistry:
    def __init__(self, root, broker):
        self.broker = broker
        self.calls = []
        self.worker = root / 'fixture_voice.py'
        self.worker.write_text(
            'import sys,wave\n'
            'with wave.open(sys.argv[1],"wb") as out:\n'
            ' out.setparams((1,2,8000,0,"NONE","not compressed"))\n'
            ' out.writeframes(b"\\xe8\\x03"*800)\n', encoding='utf-8')
        self.voice = voices.Voice('fixture:pcm', 'fixture', 'Declared synthetic PCM', ('en',), ('pcm',))

    def list_voices(self):
        leases = self.broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['request']['cpu_slots'] == 2
        assert leases[0]['request']['memory_bytes'] >= 512 * 1024**2
        return (self.voice,)

    def fingerprint(self):
        return sha256(self.worker.read_bytes()).hexdigest()

    def synthesize(self, text, destination, *, cancelled=None, **kwargs):
        self.list_voices()
        self.calls.append((text, Path(destination)))
        voices._run([sys.executable, '-I', '-B', str(self.worker), str(destination)],
                    Path(destination).parent, timeout_seconds=10, cancelled=cancelled,
                    output=Path(destination))
        return SimpleNamespace(path=Path(destination))


def setup(tmp_path, *, disk=256 * 1024**2, script='Narrator: Kept source.'):
    root = tmp_path / 'library'
    store = ProjectStore(root)
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=512 * 1024**2, disk_bytes=disk, min_free_disk_bytes=0))
    project = parse_script(script, ['fixture:pcm'], settings=Settings(
        sample_rate=8000, channels=1, stems=False, chapter_gap_seconds=0))
    store.create(project)
    registry = FixtureRegistry(tmp_path, broker)
    director = ProductionDirector(root, registry=registry, music_options={}, broker=broker)
    return store, broker, project, registry, director


def deny_discovery(monkeypatch):
    monkeypatch.setattr(voices.VoiceRegistry, 'for_library',
                        classmethod(lambda *a, **k: pytest.fail('Admission must precede local voice discovery.')))


def test_impossible_saved_policy_rejects_before_runtime_voice_or_output(tmp_path, monkeypatch):
    store, broker, project, registry, director = setup(tmp_path, disk=8 * 1024**2)
    director._registry = None
    deny_discovery(monkeypatch)
    monkeypatch.setattr(render, 'runtime_identity', lambda *a, **k: pytest.fail('Native discovery ran before admission.'))
    with pytest.raises(ResourceUnavailable, match='disk_bytes'):
        director.render(project.id, tmp_path / 'never')
    job, = director.jobs(project.id)
    assert job['state'] == 'failed'
    assert not (tmp_path / 'never').exists()
    assert not list((store.path(project.id) / 'cache').iterdir())
    assert registry.calls == [] and broker.snapshot()['leases'] == []


def test_waiting_director_cancellation_keeps_competing_real_lease_and_starts_no_engine(tmp_path, monkeypatch):
    store, broker, project, registry, director = setup(tmp_path)
    director._registry = None
    deny_discovery(monkeypatch)
    stop, observed = threading.Event(), []
    reason = broker._reason
    def blocked(request, volumes, active, limits):
        result = reason(request, volumes, active, limits)
        if active and result is not None:
            observed.append((result, active[0]['operation_id']))
            stop.set()
        return result
    monkeypatch.setattr(broker, '_reason', blocked)
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='another-reviewed-operation') as other:
        with pytest.raises(InterruptedError, match='waiting'):
            director.render(project.id, tmp_path / 'never', cancelled=stop.is_set)
        assert observed and observed[0][1] == other.operation_id
        assert [row['id'] for row in broker.snapshot()['leases']] == [other.id]
    assert director.jobs(project.id)[0]['state'] == 'paused'
    assert not (tmp_path / 'never').exists() and registry.calls == []
    assert broker.snapshot()['leases'] == []


def test_real_primary_audio_is_admitted_and_completed_receipts_are_passive(tmp_path, monkeypatch):
    store, broker, project, registry, director = setup(tmp_path)
    result = director.render(project.id, tmp_path / 'finished')
    assert result['state'] == 'complete' and registry.calls
    with wave.open(result['master'], 'rb') as stream:
        assert stream.getnframes() == 800 and stream.getframerate() == 8000
    expected = {path.name: path.read_bytes() for path in Path(result['output_dir']).iterdir()}
    assert broker.snapshot()['leases'] == []
    deny_discovery(monkeypatch)
    monkeypatch.setattr(ResourceBroker, 'for_app',
                        classmethod(lambda *a, **k: pytest.fail('A completed receipt needs no new resource lease.')))
    reopened = ProductionDirector(store.root)
    assert reopened.jobs(project.id)[0]['state'] == 'complete'
    assert reopened.render(project.id, job_id=result['job_id']) == result
    assert {path.name: path.read_bytes() for path in Path(result['output_dir']).iterdir()} == expected


def test_passive_job_creation_and_restored_job_refusal_precede_discovery_and_broker(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / 'library')
    project = parse_script('Narrator: Retained source.', ['fixture:pcm'],
                           settings=Settings(stems=False))
    store.create(project)
    deny_discovery(monkeypatch)
    monkeypatch.setattr(ResourceBroker, 'for_app',
                        classmethod(lambda *a, **k: pytest.fail('Passive/revoked state cannot enter resource admission.')))
    director = ProductionDirector(store.root)
    identity = director.create_job(project.id, tmp_path / 'output')
    assert director.status(project.id, identity)['state'] == 'queued'
    job = director.status(project.id, identity)
    job['restore_requires_new_output'] = True
    director._write_job(job)
    with pytest.raises(ValueError, match='restored'):
        director.render(project.id, job_id=identity)
    assert not (tmp_path / 'output').exists()


@pytest.fixture
def uncertain_exit(monkeypatch):
    owners, release, selected = [], threading.Event(), []
    stop = process._stop_owned
    predicate = [lambda child: False]
    def manual(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    def uncertain(child):
        assert stop(child) is True
        if predicate[0](child) and not release.is_set():
            selected.append(child)
            raise TimeoutError('Synthetic uncertainty after actual selected child exit')
        return True
    monkeypatch.setattr(cleanup, 'pending_cleanup', manual)
    monkeypatch.setattr(process, '_stop_owned', uncertain)
    yield predicate, owners, release, selected
    release.set()
    for owner in owners:
        assert owner.close()


@pytest.mark.parametrize('masked_journal', [False, True], ids=['direct-pending', 'journal-masks-pending'])
def test_uncertain_primary_worker_retains_actual_stage_render_lock_and_broker_until_retry(
        tmp_path, monkeypatch, uncertain_exit, masked_journal):
    store, broker, project, registry, director = setup(tmp_path)
    predicate, owners, release, selected = uncertain_exit
    predicate[0] = lambda child: str(registry.worker) in tuple(map(str, child.args))
    write = director._write_job
    if masked_journal:
        def failed_journal(value):
            if value['state'] == 'failed':
                raise OSError('Synthetic failed status publication')
            return write(value)
        monkeypatch.setattr(director, '_write_job', failed_journal)
    with pytest.raises((cleanup.AudioCleanupPending, OSError)) as caught:
        director.render(project.id, tmp_path / 'finished')
    actual_owners = cleanup._pending_owners(caught.value)
    assert actual_owners and set(actual_owners) == set(owners)
    assert selected and selected[0].poll() == 0
    stage = registry.calls[0][1]
    assert stage.is_file()
    with wave.open(str(stage), 'rb') as stream:
        assert stream.getnframes() == 800
    job, = director.jobs(project.id)
    lock = NativeLock(store.path(project.id) / '.render.lock')
    assert lock.acquire() is False
    assert len(broker.snapshot()['leases']) == 1
    assert not (tmp_path / 'finished' / 'master.wav').exists()
    with pytest.raises(RuntimeError, match='already being changed'):
        director.render(project.id, job_id=job['id'])
    release.set()
    for owner in owners:
        assert owner.close()
    assert not stage.exists() and broker.snapshot()['leases'] == []
    assert lock.acquire()
    lock.release()
    monkeypatch.setattr(director, '_write_job', write)
    finished = director.render(project.id, job_id=job['id'])
    assert finished['state'] == 'complete' and broker.snapshot()['leases'] == []


def test_pending_cached_audio_probe_does_not_quarantine_or_regenerate_accepted_bytes(tmp_path, uncertain_exit):
    store, broker, project, registry, director = setup(tmp_path)
    first = director.render(project.id, tmp_path / 'first')
    cache = store.path(project.id) / 'cache'
    metadata = next(path for path in cache.glob('*.json') if json.loads(path.read_text())['kind'] == 'speech')
    saved = {path: path.read_bytes() for path in (metadata, metadata.with_suffix('.wav'))}
    old_calls = len(registry.calls)
    predicate, owners, release, selected = uncertain_exit
    predicate[0] = lambda child: str(metadata.with_suffix('.wav')) in tuple(map(str, child.args))
    with pytest.raises(cleanup.AudioCleanupPending):
        director.render(project.id, tmp_path / 'second')
    assert selected and owners and len(registry.calls) == old_calls
    assert {path: path.read_bytes() for path in saved} == saved
    assert not list(cache.glob('*.invalid-*'))
    assert len(broker.snapshot()['leases']) == 1
    assert Path(first['master']).is_file()
    release.set()
    for owner in owners:
        assert owner.close()
    assert broker.snapshot()['leases'] == []


def test_asset_import_uses_real_shared_inspect_and_convert_admissions_and_event_cancel(tmp_path, monkeypatch):
    store, broker, project, registry, director = setup(tmp_path)
    source = tmp_path / 'selected.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x01\x00' * 800)
    original = source.read_bytes()
    real_start = process._start_process
    observations = []
    def start(args, **kwargs):
        leases = broker.snapshot()['leases']
        assert len(leases) == 1 and leases[0]['operation_id'].startswith('director-asset:')
        observations.append(leases[0]['request'])
        return real_start(args, **kwargs)
    monkeypatch.setattr(process, '_start_process', start)
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(render.RenderCancelled):
        director.import_asset(project.id, source, cancelled=stopped)
    assert observations == []
    stopped.clear()
    identity = director.import_asset(project.id, source, cancelled=stopped)
    path = store.path(project.id) / 'assets' / (identity + '.wav')
    assert file_sha256(path) == identity and source.read_bytes() == original
    assert any(row['disk_bytes'] == 0 for row in observations)
    assert any(row['disk_bytes'] > 0 for row in observations)
    assert broker.snapshot()['leases'] == [] and not list(store.path(project.id).glob('.asset-*'))
    with wave.open(str(path), 'rb') as stream:
        assert stream.getnframes() == 2400 and stream.getnchannels() == 2


@pytest.mark.parametrize('masked', [False, True], ids=['direct-music-pending', 'masked-music-pending'])
def test_actual_music_stage_and_primary_lease_survive_uncertain_native_exit(
        tmp_path, monkeypatch, uncertain_exit, masked):
    from carr_library.audio import music
    store, broker, project, registry, director = setup(
        tmp_path, script='@music calm 80\n@pause 0.2')
    predicate, owners, release, selected = uncertain_exit
    predicate[0] = lambda child: any('.carr-music-' in str(part) for part in child.args)
    original = music.run_process
    if masked:
        def failed_adapter(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except cleanup.AudioCleanupPending as exc:
                raise ValueError('Synthetic music adapter error') from exc
        monkeypatch.setattr(music, 'run_process', failed_adapter)
    with pytest.raises((cleanup.AudioCleanupPending, ValueError)) as caught:
        director.render(project.id, tmp_path / 'music-output')
    assert cleanup._pending_owners(caught.value) and selected
    stages = list(store.path(project.id).rglob('.carr-music-*'))
    assert len(stages) == 1
    staged, = stages[0].glob('*.wav')
    with wave.open(str(staged), 'rb') as stream:
        assert stream.getnframes() == 1600
    assert len(broker.snapshot()['leases']) == 1
    assert not (tmp_path / 'music-output' / 'master.wav').exists()
    release.set()
    for owner in owners:
        assert owner.close()
    assert not stages[0].exists() and broker.snapshot()['leases'] == []
    job, = director.jobs(project.id)
    assert director.render(project.id, job_id=job['id'])['state'] == 'complete'
