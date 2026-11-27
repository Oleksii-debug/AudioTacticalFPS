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
