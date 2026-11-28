"""Canonical studio integration, including actual FFmpeg output and recovery."""
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from carr_library.audio import AudioBudget, AudioError, AudioRuntime
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceUnavailable
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Music
from carr_library.studio.render import ProductionService


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual canonical studio output requires installed FFmpeg and ffprobe.')
def test_actual_studio_admits_threads_releases_and_completed_job_needs_no_capacity(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    made, observed = [], []
    def factory(root, **options):
        runtime = AudioRuntime(root, budget=AudioBudget(threads=1), **options)
        made.append(runtime)
        return runtime
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
                                runtime_factory=factory, broker=broker)
    project = service.create('[Pause: 0.2]', title='Actual silent admission fixture')
    identity = service.submit(project, outputs, stems=False)
    def progress(message):
        if message.startswith('Scene '):
            observed.append(broker.snapshot())
    complete = service.run(identity, progress=progress)
    assert complete['status'] == 'complete'
    assert observed and observed[0]['usage']['cpu_slots'] == 1
    assert observed[0]['leases'][0]['operation_id'] == 'studio:' + identity
    assert observed[0]['usage']['disk_bytes'] > 0
    assert Path(complete['destination'], 'master.wav').stat().st_size > 44
    assert not broker.snapshot()['leases']
    before = {item['path']: Path(item['path']).stat().st_mtime_ns for item in complete['artifacts']}
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='other-heavy-engine', timeout=0):
        assert service.run(identity)['status'] == 'complete'
        assert len(made) == 1
        assert before == {path: Path(path).stat().st_mtime_ns for path in before}
        assert len(broker.snapshot()['leases']) == 1
    # Pause while waiting must not launch work or release somebody else's lease.
    waiting = service.submit(project, outputs)
    def pause_when_waiting(message):
        if message == 'Waiting for shared local resources.':
            service.request_pause(waiting)
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='occupier', timeout=0):
        with pytest.raises(InterruptedError, match='waiting for resources'):
            service.run(waiting, progress=pause_when_waiting)
        assert service.get(waiting)['status'] == 'paused'
        assert len(broker.snapshot()['leases']) == 1
    assert not broker.snapshot()['leases']


def test_gpu_policy_checked_before_model_execution_and_failed_probe_releases(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=2))
    probes = []
    # This is an admission boundary fixture, never neural inference evidence.
    runtime = SimpleNamespace(budget=AudioBudget(threads=2),
                              music_models={'musicgen': (SimpleNamespace(device='cuda'), {})})
    def probe():
        probes.append('called')
        raise RuntimeError('fixture runtime probe failed')
    runtime.capabilities = probe
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
                                runtime_factory=lambda *_args, **_kwargs: runtime, broker=broker)
    project = service.create('[Pause: 1]')
    project = replace(project, revision=project.revision + 1,
                      scenes=(replace(project.scenes[0], music=Music(engine='musicgen')),))
    identity = service.submit(project, outputs)
    with pytest.raises(ResourceUnavailable, match='explicit shared resource capacity'):
        service.run(identity)
    assert not probes and not broker.snapshot()['leases']
    assert service.get(identity)['status'] == 'attention'
    broker.configure(replace(broker.limits, gpu_devices=(('cuda:0', 8 * 1024 ** 3),)))
    with pytest.raises(RuntimeError, match='fixture runtime probe failed'):
        service.run(identity)
    assert probes == ['called']
    assert service.get(identity)['status'] == 'failed'
    request = service.get(identity)['resource_plan']['request']
    assert request['gpu_device'] == 'cuda:0' and request['gpu_memory_bytes'] == 8 * 1024 ** 3
    assert request['memory_bytes'] == broker.limits.memory_bytes
    assert not broker.snapshot()['leases']
    assert not list(outputs.iterdir())


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual total-budget recovery requires FFmpeg and ffprobe.')
def test_total_disk_budget_failure_releases_lease_and_resume_preserves_accepted_audio(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    budget = AudioBudget(threads=1, max_output_bytes=4 * 1024**2, reserve_disk_bytes=0, timeout=30)
    factory = lambda root, **options: AudioRuntime(root, budget=budget, **options)
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
        runtime_factory=factory, broker=broker, total_disk_bytes=4 * 1024**2)
    project = service.create('[Pause: 30]', mode='authored')
    project = replace(project, sample_rate=16000, revision=project.revision + 1)
    identity = service.submit(project, outputs, stems=True, format='flac')
    with pytest.raises(AudioError, match='budget'):
        service.run(identity)
    failed = service.get(identity)
    assert failed['status'] == 'failed' and failed['nodes']
    assert not broker.snapshot()['leases']
    accepted = {path: path.read_bytes() for path in service.cache.glob('*.wav')}
    assert accepted
    resumed = ProductionService(service.root, voices=VoiceRegistry([]), runtime_factory=factory,
        broker=broker, total_disk_bytes=32 * 1024**2).run(identity)
    assert resumed['status'] == 'complete' and resumed['total_disk_bytes'] == 32 * 1024**2
    assert all(resumed['nodes'][key]['reused'] for key in failed['nodes'])
    assert all(path.read_bytes() == content for path, content in accepted.items())
    assert not broker.snapshot()['leases']


def test_saved_production_admission_is_a_bounded_passive_backup_record(tmp_path, monkeypatch):
    from carr_library import backup_producers
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]))
    project = service.create('[Pause: 1]')
    identity = service.submit(project, outputs)
    saved = service.get(identity)
    _, plan = service._resource_plan(project, saved, SimpleNamespace(budget=AudioBudget(threads=1)))
    saved.update(resource_plan=plan, total_disk_bytes=service.total_disk_bytes, status='queued')
    name = 'state/studio/jobs/' + identity + '/job.json'
    # Reading this checkpoint may not acquire locks or resolve external paths.
    monkeypatch.setattr(ResourceBroker, '__init__', lambda *_a, **_kw:
                        pytest.fail('Backup validation started a runtime coordinator.'))
    for status in ('queued', 'attention', 'failed'):
        saved['status'] = status
        backup_producers.validate(None, name, saved)
    changes = (
        lambda row: row['resource_plan'].update(schema=True),
        lambda row: row['resource_plan']['request'].update(cpu_slots=-1),
        lambda row: row['resource_plan']['request'].update(disk_path='relative/cache'),
        lambda row: row['resource_plan'].update(unreviewed=True),
        lambda row: row.update(total_disk_bytes=True),
    )
    for change in changes:
        malformed = deepcopy(saved)
        change(malformed)
        with pytest.raises(ValueError):
            backup_producers.validate(None, name, malformed)
    legacy = {key: value for key, value in saved.items() if key not in ('resource_plan', 'total_disk_bytes')}
    legacy['status'] = 'pending'
    backup_producers.validate(None, name, legacy)


def test_restored_output_authority_blocks_before_broker_or_runtime(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()

    class ForbiddenBroker:
        @property
        def limits(self):
            pytest.fail('Restored output started resource admission.')

        def reserve(self, *_args, **_kwargs):
            pytest.fail('Restored output started resource admission.')

    def forbidden_runtime(*_args, **_kwargs):
        pytest.fail('Restored output constructed an audio runtime.')

    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
                                runtime_factory=forbidden_runtime, broker=ForbiddenBroker())
    identity = service.submit(service.create('[Pause: 1]'), outputs)
    state = service.get(identity)
    state['restore_requires_new_output'] = True
    service._save(state)
    with pytest.raises(ValueError, match='requires a new output'):
        service.run(identity)
    assert not list(outputs.iterdir())


@pytest.mark.parametrize('failed_status_save', [False, True])
def test_pending_native_cleanup_retains_actual_resource_lock_until_quiescent(
        tmp_path, monkeypatch, failed_status_save):
    from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
    from carr_library.operations import ResourceTimeout

    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    quiescent = [False]
    owner = CleanupOwner(lambda: quiescent[0])
    pending = AudioCleanupPending('Synthetic owned worker shutdown is pending.', cleanup_owner=owner)

    def probe():
        raise pending

    runtime = SimpleNamespace(budget=AudioBudget(threads=1), music_models={},
                              capabilities=probe)
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
                                runtime_factory=lambda *_a, **_kw: runtime, broker=broker)
    identity = service.submit(service.create('[Pause: 1]'), outputs)
    save = service._save

    def status_save(state):
        if failed_status_save and state['status'] == 'failed':
            raise OSError('Synthetic status persistence failure')
        return save(state)

    monkeypatch.setattr(service, '_save', status_save)
    try:
        with pytest.raises(OSError if failed_status_save else AudioCleanupPending) as failure:
            service.run(identity)
        if failed_status_save:
            assert failure.value.__context__ is pending
            assert service.get(identity)['status'] == 'queued'
        else:
            assert failure.value is pending
            assert service.get(identity)['status'] == 'failed'
        assert len(broker.snapshot()['leases']) == 1
        assert not owner.close()
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=1),
                                operation_id='other-engine', timeout=0):
                pytest.fail('A live cleanup owner lost its resource reservation.')
        quiescent[0] = True
        assert owner.close()
        assert not broker.snapshot()['leases']
        assert owner.close()  # Repeated completion cannot release a later owner.
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='after-cleanup', timeout=0):
            assert len(broker.snapshot()['leases']) == 1
    finally:
        quiescent[0] = True
        owner.close()
    assert not broker.snapshot()['leases']


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual saved artifact details require FFmpeg and ffprobe.')
def test_facade_records_actual_pcm_identity_without_reverifying_missing_output(
        tmp_path, monkeypatch):
    from hashlib import sha256
    import wave
    from carr_library.application import LibraryApp
    from carr_library.operations import OperationFacade

    app = LibraryApp(tmp_path / 'library')
    outputs = tmp_path / 'private-outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1))
    def factory(root, **options):
        return AudioRuntime(root, budget=AudioBudget(threads=1), **options)
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]),
                                runtime_factory=factory, broker=broker)
    try:
        project = service.create('[Pause: 0.2]', title='PRIVATE_STORED_TITLE')
        identity = service.submit(project, outputs, stems=False)
        completed = service.run(identity)
        assert completed['status'] == 'complete'
        master = Path(completed['destination']) / 'master.wav'
        raw_pcm = master.read_bytes()
        with wave.open(str(master), 'rb') as audio:
            assert audio.getnframes() > 0 and audio.getsampwidth() == 2
            assert audio.getframerate() == project.sample_rate and audio.getnchannels() == 2
        master_row, = [row for row in completed['artifacts'] if Path(row['path']) == master]
        assert master_row['sha256'] == sha256(raw_pcm).hexdigest()
        assert master_row['size'] == len(raw_pcm)
        assert not broker.snapshot()['leases']
        checkpoint = service.jobs_root / identity / 'job.json'
        saved_bytes = checkpoint.read_bytes()
        cache = {path: path.read_bytes() for path in service.cache.glob('*.wav')}
        assert cache

        def forbidden(*args, **kwargs):
            pytest.fail('Reading stored details started current audio verification.')
        monkeypatch.setattr(service, '_runtime', forbidden)
        monkeypatch.setattr(service, '_verify_artifacts', forbidden)
        facade = OperationFacade(app, studio=service, broker=broker)
        before = facade.get('studio', identity)
        assert before.status == 'complete' and before.controls == () and before.live is None
        assert before.details.resource_plan.cpu_slots == 1
        assert before.details.completed_artifact_count == len(completed['artifacts'])
        assert [(item.sha256, item.size) for item in before.details.completed_artifacts] == [
            (row['sha256'], row['size']) for row in completed['artifacts']]
        assert before.details.settings_sha256 is before.details.adapter is None

        # A stored completion remains a historical declaration when bytes vanish.
        # The dashboard must neither replay production nor claim current verification.
        master.unlink()
        after = facade.get('studio', identity)
        assert after == before
        assert 'artifact bytes and current assets were not checked' in after.details.note
        assert not master.exists() and not broker.snapshot()['leases']
        assert checkpoint.read_bytes() == saved_bytes
        assert all(path.read_bytes() == raw for path, raw in cache.items())
    finally:
        service.close()
