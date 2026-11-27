"""Canonical studio integration, including actual FFmpeg output and recovery."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.durable_state import atomic_json
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceUnavailable
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Cue, Music, Project, Scene
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


def selected_audio(path, *, sample=100):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(sample.to_bytes(2, 'little', signed=True) * 48000)
    return path


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                   reason='Actual clip admission requires FFmpeg and ffprobe.')
def test_actual_trimmed_placed_clips_and_stems_are_in_disk_reservation(tmp_path):
    output, inputs = tmp_path / 'out', tmp_path / 'selected'
    output.mkdir()
    inputs.mkdir()
    source = selected_audio(inputs / 'performance.wav')
    original = source.read_bytes()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    inspected, rendered = [], []

    class ObservedAudio(AudioRuntime):
        def inspect(self, path, **options):
            if Path(path) == source:
                inspected.append(broker.snapshot())
            return super().inspect(path, **options)

        def capabilities(self):
            rendered.append(broker.snapshot())
            return super().capabilities()

    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]),
        input_roots=(inputs,), broker=broker,
        runtime_factory=lambda root, **options: ObservedAudio(root, budget=AudioBudget(threads=1), **options))
    project = Project('clip-admission', 'Clips and stems', (), (
        Scene('one', 'Placed selection', (), cues=(
            Cue('first', str(source), start=3, trim_start=.5, duration=.75, role='microphone'),)),
        Scene('two', 'Remaining source', (), cues=(
            Cue('second', str(source), start=1, trim_start=1, role='sfx'),))), schema=2)
    job = service.submit(project, output, stems=True)
    result = service.run(job)
    master = Path(result['destination']) / 'master.wav'
    with wave.open(str(master)) as stream:
        frames, rate = stream.getnframes(), stream.getframerate()
        pcm_bytes = frames * stream.getnchannels() * stream.getsampwidth()
    assert frames / rate == result['resource_plan']['estimated_duration'] == 5.75
    assert source.read_bytes() == original
    clips = result['result']['clips']
    assert [(row['trim_start'], row['duration'], row['start'], row['end']) for row in clips] == [
        (.5, .75, 3, 3.75), (1, 1, 4.75, 5.75)]
    stem_paths = [Path(result['destination']) / 'stems/clips' / name for name in ('microphone.wav', 'sfx.wav')]
    assert all(path.is_file() for path in stem_paths)
    request = result['resource_plan']['request']
    assert request['disk_bytes'] >= 6 * pcm_bytes + sum(path.stat().st_size for path in stem_paths)
    assert request['additional_disks'][0][1] >= 2 * pcm_bytes + sum(path.stat().st_size for path in stem_paths)
    assert inspected and rendered
    assert all(len(item['leases']) == 1 and item['leases'][0]['operation_id'] == 'studio:' + job
               for item in inspected + rendered)
    assert result['resource_plan']['source_sha256'][str(source)]
    assert not broker.snapshot()['leases']


@pytest.mark.parametrize('binding', ['other-root', 'other-policy', 'untyped'])
def test_injected_broker_cannot_escape_canonical_app_policy(tmp_path, binding):
    from carr_library.operations import ResourceStateError
    output = tmp_path / 'out'
    output.mkdir()
    if binding == 'other-root':
        other = tmp_path / 'another-library'
        other.mkdir()
        broker = ResourceBroker.for_app(other)
    elif binding == 'other-policy':
        broker = ResourceBroker(tmp_path / 'runtime/resources', policy_path=tmp_path / 'other-policy.json')
    else:
        broker = SimpleNamespace(root=tmp_path / 'runtime/resources',
            policy_path=tmp_path / 'state/operations/resources.json')
    made = []
    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]), broker=broker,
                                runtime_factory=lambda *_args, **_kwargs: made.append(True))
    job = service.submit(service.create('[Pause: 0.2]'), output)
    with pytest.raises(ResourceStateError, match='another data folder'):
        service.run(job)
    assert made == [] and service.get(job)['status'] == 'attention'
    assert list(output.iterdir()) == []


@pytest.mark.parametrize(('action', 'expected'), [('pause', 'paused'), ('cancel', 'cancelled')])
def test_waiting_stop_is_acknowledged_without_starting_engine(tmp_path, action, expected):
    from carr_library.operations import ResourceTimeout
    output = tmp_path / 'out'
    output.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    started = []
    runtime = SimpleNamespace(budget=AudioBudget(threads=1), music_models={},
                              capabilities=lambda: started.append(True))
    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]), broker=broker,
                                runtime_factory=lambda *_args, **_kwargs: runtime)
    job = service.submit(service.create('[Pause: 0.2]'), output)
    waiting, errors = threading.Event(), []

    def work():
        try:
            service.run(job, progress=lambda _: waiting.set())
        except BaseException as exc:
            errors.append(exc)

    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='occupied', timeout=0):
        thread = threading.Thread(target=work)
        thread.start()
        try:
            assert waiting.wait(3)
            assert service.get(job)['status'] == 'queued'
            (service.request_pause if action == 'pause' else service.cancel)(job)
            thread.join(3)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], InterruptedError)
            assert service.get(job)['status'] == expected
            assert [item['operation_id'] for item in broker.snapshot()['leases']] == ['occupied']
        finally:
            service.cancel(job)
            thread.join(3)
    assert not started and not broker.snapshot()['leases'] and not list(output.iterdir())


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                   reason='Actual input inspection requires FFmpeg and ffprobe.')
def test_source_changed_after_resource_review_does_not_start_render(tmp_path, monkeypatch):
    output, inputs = tmp_path / 'out', tmp_path / 'selected'
    output.mkdir()
    inputs.mkdir()
    source = selected_audio(inputs / 'performance.wav')
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    capabilities = []

    class InputAudio(AudioRuntime):
        def capabilities(self):
            capabilities.append(True)
            return super().capabilities()

    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]),
        input_roots=(inputs,), broker=broker,
        runtime_factory=lambda root, **options: InputAudio(root, budget=AudioBudget(threads=1), **options))
    project = Project('changed-input', 'Changed input', (), (
        Scene('one', 'One', (), cues=(Cue('clip', str(source), duration=1),)),), schema=2)
    job = service.submit(project, output)
    save, changed = service._save, []

    def saved(state):
        save(state)
        if 'resource_plan' in state and not changed:
            changed.append(True)
            # Real equal-length source bytes change after the plan was saved,
            # before full render admission. Existing hash review must be fresh.
            selected_audio(source, sample=200)

    monkeypatch.setattr(service, '_save', saved)
    with pytest.raises(ValueError, match='reviewed legacy Studio input changed'):
        service.run(job)
    assert changed and not capabilities
    assert service.get(job)['status'] == 'failed'
    assert not broker.snapshot()['leases'] and not list(output.iterdir())


def test_cancellation_keeps_reservation_through_actual_child_cleanup(tmp_path, monkeypatch):
    from carr_library.audio import AudioCancelled
    from carr_library.audio import process as audio_process
    output = tmp_path / 'out'
    output.mkdir()
    ready = tmp_path / 'child-ready'
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    children, cleanup, failures = [], [], []
    original = audio_process.subprocess.Popen

    def launched(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(audio_process.subprocess, 'Popen', launched)
    runtime = SimpleNamespace(budget=AudioBudget(threads=1), music_models={})
    service = ProductionService(tmp_path / 'state/studio', voices=VoiceRegistry([]), broker=broker,
                                runtime_factory=lambda *_args, **_kwargs: runtime)
    job = service.submit(service.create('[Pause: 0.2]'), output)
    control = service.jobs_root / job / 'control.json'

    def probe():
        try:
            audio_process.run_process([sys.executable, '-I', '-B', '-c',
                'import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text("ready"); time.sleep(30)', str(ready)],
                budget=AudioBudget(timeout=10), cancelled=control.exists)
        finally:
            cleanup.append((children[0].poll(), broker.snapshot()))

    runtime.capabilities = probe

    def work():
        try:
            service.run(job)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline and worker.is_alive():
            time.sleep(.01)
        assert ready.exists() and children[0].poll() is None
        service.cancel(job)
        worker.join(6)
        assert not worker.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], AudioCancelled)
        assert cleanup and cleanup[0][0] is not None
        assert [item['operation_id'] for item in cleanup[0][1]['leases']] == ['studio:' + job]
        assert service.get(job)['status'] == 'cancelled'
        assert not broker.snapshot()['leases'] and not list(output.iterdir())
    finally:
        service.cancel(job)
        worker.join(6)
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)


@pytest.mark.parametrize('binding', ['retained', 'approved'])
@pytest.mark.parametrize('stop', ['callback', 'service-close'])
def test_retained_input_hash_stops_after_first_real_chunk_without_starting_runtime(
        tmp_path, monkeypatch, binding, stop):
    output = tmp_path / 'out'
    output.mkdir()
    broker = ResourceBroker.for_app(tmp_path, limits=ResourceLimits(cpu_slots=1))
    voices = VoiceRegistry([])
    runtime_calls, registry_closes = [], []
    service = ProductionService(tmp_path / 'state/studio', voices=voices, broker=broker,
        runtime_factory=lambda *_args, **_kwargs: runtime_calls.append(True))
    source = tmp_path / 'selected.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x64\x00' * (3 * 1024 ** 2 // 2))
    original = source.read_bytes()
    digest = sha256(original).hexdigest()
    if binding == 'retained':
        source = source.rename(service.inputs / (digest + '.audio'))
    else:
        atomic_json(service.root / 'legacy-input-review.json', {
            'schema': 1, 'pending': [], 'approved': {str(source): {
                'size_bytes': len(original), 'sha256': digest}}})
    project = Project('cancelled-input-hash', 'Retained input cancellation', (), (
        Scene('one', 'One', (), cues=(Cue('clip', str(source), duration=1),)),), schema=2)
    job = service.submit(project, output)
    cancelled = threading.Event()
    reads = []
    original_open, original_close = Path.open, voices.close

    class ObservedStream:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): self.stream.__enter__(); return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def __getattr__(self, name): return getattr(self.stream, name)
        def read(self, size=-1):
            block = self.stream.read(size)
            reads.append(len(block))
            if block:
                if stop == 'callback': cancelled.set()
                else: service.close()
            return block

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get('mode', 'r')
        return ObservedStream(stream) if path == source and mode == 'rb' else stream

    def closed():
        registry_closes.append(True)
        return original_close()

    monkeypatch.setattr(voices, 'close', closed)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(Path, 'open', opened)
            with pytest.raises(InterruptedError, match='input review was cancelled'):
                service.run(job, cancelled=cancelled.is_set)
        assert reads == [1024 ** 2], 'Cancellation must not read the remainder of a large retained input.'
        assert not runtime_calls and not registry_closes
        assert service.get(job)['status'] == 'paused'
        assert not broker.snapshot()['leases'] and not list(output.iterdir())
        assert source.read_bytes() == original
        assert Project.from_dict(service.get(job)['project']) == project
        service.close()
        assert not registry_closes, 'The cancelled service does not own its supplied voice registry.'
    finally:
        service.close()
        voices.close()
    assert registry_closes == [True]
