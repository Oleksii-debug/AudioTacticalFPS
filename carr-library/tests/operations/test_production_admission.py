"""Canonical studio integration, including actual FFmpeg output and recovery."""
from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
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
                    reason='Actual default rendering requires installed FFmpeg and ffprobe.')
def test_default_render_on_two_logical_cpus_uses_its_explicit_default_allowance(tmp_path, monkeypatch):
    import carr_library.operations.models as model
    with monkeypatch.context() as patched:
        patched.setattr(model.os, 'cpu_count', lambda: 2)
        broker = ResourceBroker.for_app(tmp_path)
    assert broker.limits.cpu_slots == 1
    outputs = tmp_path / 'out'
    outputs.mkdir()
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]), broker=broker)
    identity = service.submit(service.create('[Pause: 0.2]', title='Default CPU selection'), outputs, stems=False)
    result = service.run(identity)
    assert result['status'] == 'complete'
    assert result['resource_plan']['request']['cpu_slots'] == 1
    assert Path(result['destination'], 'master.wav').stat().st_size > 44
    assert broker.snapshot()['leases'] == []


def test_explicit_runtime_threads_are_not_silently_rewritten_to_policy(tmp_path):
    outputs = tmp_path / 'out'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    budget = AudioBudget(threads=2)
    runtime = SimpleNamespace(budget=budget, music_models={})
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]), broker=broker,
                                runtime_factory=lambda *_args, **_kwargs: runtime)
    identity = service.submit(service.create('[Pause: 0.2]'), outputs)
    with pytest.raises(ResourceUnavailable, match='cpu_slots'):
        service.run(identity)
    assert runtime.budget is budget and budget.threads == 2
    assert service.get(identity)['status'] == 'attention'
    assert broker.snapshot()['leases'] == []


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual canonical studio output requires installed FFmpeg and ffprobe.')
def test_completed_studio_is_not_failed_by_resource_metadata_cleanup(tmp_path, monkeypatch):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options),
        broker=broker)
    identity = service.submit(service.create('[Pause: 0.2]'), outputs, stems=False)
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path.parent == broker.leases_root and path.suffix == '.json':
            raise OSError('Synthetic resource metadata cleanup failure')
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'unlink', unlink)
        result = service.run(identity)
    assert result['status'] == 'complete'
    assert 'Resource reservation metadata cleanup' in result['progress']
    reopened = service.get(identity)
    assert reopened['status'] == 'complete' and reopened['progress'] == result['progress']
    service._verify_artifacts(reopened)
    assert not broker.snapshot()['leases']


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual canonical studio output requires installed FFmpeg and ffprobe.')
def test_explicit_policy_recovery_preserves_actual_studio_thread_request(tmp_path):
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    runtimes = []

    def factory(root, **options):
        runtime = AudioRuntime(root, budget=AudioBudget(threads=2), **options)
        runtimes.append(runtime)
        return runtime

    service = ProductionService(tmp_path / 'state' / 'studio', voices=VoiceRegistry([]),
                                runtime_factory=factory, broker=broker)
    identity = service.submit(service.create('[Pause: 0.2]'), outputs, stems=False)
    with pytest.raises(ResourceUnavailable, match='cpu_slots'):
        service.run(identity)
    assert service.get(identity)['status'] == 'attention'
    assert broker.limits.cpu_slots == 1 and not list(outputs.iterdir())
    assert not broker.snapshot()['leases']
    broker.configure(replace(broker.limits, cpu_slots=2))
    assert service.run(identity)['status'] == 'complete'
    assert all(runtime.budget.threads == 2 for runtime in runtimes)
    assert service.get(identity)['resource_plan']['request']['cpu_slots'] == 2
