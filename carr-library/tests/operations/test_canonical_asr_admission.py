"""Accepted workspace ASR and Studio share actual native admission.

Synthetic supplied-worker protocol fixtures prove ownership, receipts and scope,
not acoustic recognition, hardware capacity or orphan-child crash containment.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

# Bind a child only to the package already imported by this test's caller.
# Installed-package acceptance never substitutes checkout/src on sys.path.
if __name__ == '__main__':
    if len(sys.argv) != 6 or sys.argv[1] != '--studio-admission-actor':
        raise SystemExit('Use the explicit synthetic Studio admission entrypoint.')
    package_file = Path(sys.argv[4]).resolve(strict=True)
    spec = importlib.util.spec_from_file_location('carr_library', package_file,
        submodule_search_locations=[str(package_file.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules['carr_library'] = module
    spec.loader.exec_module(module)

import pytest
import carr_library
from carr_library.application import LibraryApp
from carr_library import transcription
from carr_library.audio import AudioBudget, AudioRuntime
from carr_library.audio.models import AudioCancelled, AudioError
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import (OperationFacade, ResourceBroker, ResourceLimits,
                                     ResourceRequest, ResourceTimeout, ResourceUnavailable)
from carr_library.speech import VoiceRegistry
from carr_library.studio.project import Music
from carr_library.studio.render import ProductionService


def _fixture(name):
    path = Path(__file__).resolve().parents[1] / 'transcription' / name
    spec = importlib.util.spec_from_file_location('operations_' + path.stem, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = value
    spec.loader.exec_module(value)
    return value


_bridge = _fixture('test_supplied_worker_bridge.py')
_v5 = _fixture('test_v5_worker_profile.py')
prepared, v5 = _bridge.prepared, _v5.v5


def _policy(app):
    return ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))


def _studio(app, broker=None):
    return ProductionService(app.root / 'state' / 'studio', voices=VoiceRegistry([]),
        runtime_factory=lambda root, **options: AudioRuntime(root, budget=AudioBudget(threads=1), **options),
        broker=broker)


def _wait(predicate, *, process=None, seconds=15):
    deadline = time.monotonic() + seconds
    while not predicate():
        if process is not None and process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError('Synthetic actor exited before its barrier: ' + stdout + stderr)
        if time.monotonic() >= deadline:
            raise AssertionError('Actual fixture did not reach its bounded barrier.')
        time.sleep(.02)


def _studio_actor():
    assert Path(carr_library.__file__).resolve() == Path(sys.argv[4]).resolve()
    assert Path(sys.modules[ProductionService.__module__].__file__).resolve() == Path(sys.argv[5]).resolve()
    app, identity = LibraryApp(Path(sys.argv[2])), sys.argv[3]
    service = _studio(app)
    job = service.jobs_root / identity

    def hold(message):
        if message.startswith('Scene ') and not (job / 'fixture-admitted.json').exists():
            # At this real service boundary, its native capability probe has
            # finished and its resource lease is held; no render child runs yet.
            atomic_json(job / 'fixture-admitted.json', {'pid': os.getpid()})
            deadline = time.monotonic() + 25
            while not (job / 'fixture-release').exists():
                if (job / 'fixture-crash').exists():
                    os._exit(73)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Parent did not release its Studio fixture.')
                time.sleep(.02)
    result = service.run(identity, progress=hold)
    print(json.dumps({'status': result['status'], 'id': identity}), flush=True)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),
                    reason='Actual cross-service Studio admission requires installed FFmpeg and ffprobe.')
@pytest.mark.parametrize('outcome', ['complete', 'cancel-waiter', 'crash-holder'])
def test_actual_cross_process_studio_asr_contention_cancel_and_crash(prepared, tmp_path, monkeypatch, outcome):
    app, workspace, service, original, _ = prepared
    broker = _policy(app)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    studio = _studio(app, broker)
    studio_id = studio.submit(studio.create('[Pause: 0.2]', title='Synthetic shared admission'), outputs, stems=False)
    studio_path = studio.jobs_root / studio_id
    identity = service.submit([original])
    before = (service._path(identity) / 'job.json').read_bytes()
    waiting, stop = threading.Event(), threading.Event()
    launches = []
    original_run = transcription.run_process

    def actual_child(*args, **kwargs):
        leases = broker.snapshot()['leases']
        launches.append([row['operation_id'] for row in leases])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(transcription, 'run_process', actual_child)
    actor = subprocess.Popen([sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
        '--studio-admission-actor', str(app.root), studio_id, str(Path(carr_library.__file__).resolve()),
        str(Path(sys.modules[ProductionService.__module__].__file__).resolve())],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8')
    pool = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        _wait(lambda: (studio_path / 'fixture-admitted.json').exists(), process=actor)
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['studio:' + studio_id]
        def progress(done, total, phase):
            if phase == 'Waiting for shared local transcription resources.':
                waiting.set()
        future = pool.submit(service.run, identity, cancelled=stop.is_set, progress=progress, timeout=15)
        assert waiting.wait(10)
        assert not future.done() and not launches
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['studio:' + studio_id]
        if outcome == 'cancel-waiter':
            stop.set()
            with pytest.raises(AudioCancelled, match='waiting for shared resources'):
                future.result(timeout=10)
            assert not launches
            assert (service._path(identity) / 'job.json').read_bytes() == before
            assert len(broker.snapshot()['leases']) == 1
        marker = 'fixture-crash' if outcome == 'crash-holder' else 'fixture-release'
        (studio_path / marker).write_text('Synthetic controlled actor outcome.', encoding='utf-8')
        stdout, stderr = actor.communicate(timeout=20)
        assert actor.returncode == (73 if outcome == 'crash-holder' else 0), stdout + stderr
        if outcome == 'cancel-waiter':
            result = service.run(identity, timeout=15)
        else:
            result = future.result(timeout=20)
        assert result['status'] == 'completed' and result['done'] == 1
        expected = 'asr:' + service.jobs_root.parent.name + ':' + identity
        assert launches == [[expected]], launches
        assert service.verify(identity)['status'] == 'completed'
        assert not broker.snapshot()['leases']
        if outcome == 'crash-holder':
            assert studio.get(studio_id)['status'] == 'running'
            assert not Path(studio.get(studio_id)['destination']).exists()
            assert studio.run(studio_id)['status'] == 'complete'
        else:
            assert studio.get(studio_id)['status'] == 'complete'
        assert original.read_bytes().startswith(b'Synthetic media')
        assert not broker.snapshot()['leases']
    finally:
        stop.set()
        (studio_path / 'fixture-release').write_text('Fixture cleanup.', encoding='utf-8')
        if actor.poll() is None:
            try:
                actor.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                actor.terminate()
                actor.communicate(timeout=5)
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('changed', ['workspace', 'worker', 'model', 'retained-source'])
def test_waiting_v5_reauthorizes_original_scope_runtime_and_model_before_child(v5, monkeypatch, changed):
    app, workspace, service, original, runtime, models = v5
    broker = _policy(app)
    identity = service.submit([original])
    path = service._path(identity)
    before = (path / 'job.json').read_bytes()
    waiting = threading.Event()
    launched = []
    monkeypatch.setattr(transcription, 'run_process', lambda *args, **kwargs: launched.append(True))
    pool = ThreadPoolExecutor(max_workers=1)
    holder = broker.reserve(ResourceRequest(cpu_slots=1), operation_id='occupied', timeout=0)
    holder.__enter__()
    released = False
    try:
        future = pool.submit(service.run, identity, timeout=10,
            progress=lambda done, total, phase: waiting.set()
                if phase == 'Waiting for shared local transcription resources.' else None)
        assert waiting.wait(10)
        if changed == 'workspace':
            app.archive_workspace(workspace)
        else:
            target = (runtime / 'local_models.py' if changed == 'worker' else
                      models / 'small' / 'model.bin' if changed == 'model' else
                      path / 'input' / service.get(identity)['sources'][0]['filename'])
            target.write_bytes(target.read_bytes() + b' Changed while admission was blocked.')
        holder.__exit__(None, None, None)
        released = True
        with pytest.raises((ValueError, PermissionError), match='changed|archived'):
            future.result(timeout=15)
        assert not launched
        assert (path / 'job.json').read_bytes() == before
        assert not broker.snapshot()['leases']
    finally:
        if not released:
            holder.__exit__(None, None, None)
        pool.shutdown(wait=True, cancel_futures=True)


def test_completed_verified_job_bypasses_resources_without_engine_relaunch(prepared, monkeypatch):
    app, _, service, original, _ = prepared
    broker = _policy(app)
    identity = service.submit([original])
    result = service.run(identity, timeout=15)
    assert result['status'] == 'completed'
    before = (service._path(identity) / 'job.json').read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('A verified completed job attempted heavy admission or native inference.')
    monkeypatch.setattr(service, '_admission_broker', forbidden)
    monkeypatch.setattr(transcription, 'run_process', forbidden)
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='busy', timeout=0):
        assert service.run(identity) == result
        assert (service._path(identity) / 'job.json').read_bytes() == before
        assert len(broker.snapshot()['leases']) == 1


def test_actual_stopped_child_is_reaped_before_its_native_lease_releases(prepared, monkeypatch):
    from carr_library.audio import process
    app, _, service, original, configure = prepared
    broker = _policy(app)
    configure('pause-once')
    identity = service.submit([original])
    path = service._path(identity)
    children, observed = [], []
    native_popen, native_run = process.subprocess.Popen, transcription.run_process
    def capture(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child
    def run(*args, **kwargs):
        try:
            return native_run(*args, **kwargs)
        finally:
            observed.append((all(child.poll() is not None for child in children),
                             len(broker.snapshot()['leases'])))
    monkeypatch.setattr(process.subprocess, 'Popen', capture)
    monkeypatch.setattr(transcription, 'run_process', run)
    result = service.run(identity, timeout=10, cancelled=lambda: (path / 'job_status.json').exists())
    assert result['status'] == 'stopped'
    assert children and observed == [(True, 1)]
    assert all(child.poll() is not None for child in children)
    assert not broker.snapshot()['leases']


@pytest.mark.parametrize('mode,expected', [('complete', 'completed'), ('missing-output', 'failed')])
def test_resource_metadata_cleanup_notice_preserves_actual_transcription_outcome(prepared, monkeypatch, mode, expected):
    app, _, service, original, configure = prepared
    broker = _policy(app)
    configure(mode)
    identity = service.submit([original])
    unlink = Path.unlink
    def fail_metadata(path, *args, **kwargs):
        if path.parent == broker.leases_root and path.suffix == '.json':
            raise OSError('Synthetic disposable lease metadata failure.')
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'unlink', fail_metadata)
        result = service.run(identity, timeout=15)
    assert result['status'] == expected == service.get(identity)['status']
    assert 'cleanup is pending' in result['resource_cleanup_notice']
    if expected == 'completed':
        assert service.verify(identity)['done'] == 1 and not result['last_error']
    else:
        assert 'export is missing' in result['last_error'] and result['artifacts'] == []
    assert not broker.snapshot()['leases']


def test_asr_and_studio_generic_cuda_policy_has_one_shared_exclusive_token(prepared, tmp_path, monkeypatch):
    app, _, service, original, _ = prepared
    broker = _policy(app)
    identity = service.submit([original], options={'device': 'cuda', 'compute_type': 'float16'})
    job = service.get(identity)
    with pytest.raises(ResourceUnavailable, match='generic cuda'):
        service._resource_plan(job, service._path(identity), broker)
    broker.configure(replace(broker.limits, gpu_devices=(('cuda', 1024), ('cuda:0', 1024))))
    asr_request, _ = service._resource_plan(job, service._path(identity), broker)
    for name in ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER', 'HIP_VISIBLE_DEVICES', 'ROCR_VISIBLE_DEVICES'):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / 'gpu-output'
    output.mkdir()
    studio = _studio(app, broker)
    project = studio.create('[Pause: 1]')
    project = replace(project, revision=project.revision + 1,
        scenes=(replace(project.scenes[0], music=Music(engine='musicgen')),))
    studio_id = studio.submit(project, output)
    runtime = SimpleNamespace(budget=AudioBudget(threads=1),
        music_models={'musicgen': (SimpleNamespace(device='cuda'), {})})
    studio_request, _ = studio._resource_plan(project, studio.get(studio_id), runtime)
    assert asr_request.devices == studio_request.devices == ('accelerator:cuda',)
    assert job['settings']['device'] == service.get(identity)['settings']['device'] == 'cuda'
    first = replace(asr_request, cpu_slots=0, memory_bytes=0, disk_bytes=0, disk_path=None)
    second = replace(studio_request, cpu_slots=0, memory_bytes=0, disk_bytes=0, disk_path=None, additional_disks=())
    with broker.reserve(first, operation_id='synthetic-cuda-asr', timeout=0):
        with pytest.raises(ResourceTimeout, match='exclusive'):
            with broker.reserve(second, operation_id='synthetic-cuda-studio', timeout=0):
                pytest.fail('Participating CUDA adapters used different exclusion identities.')
    assert not broker.snapshot()['leases']


@pytest.mark.parametrize('stop_kind', ['timeout', 'cancel'])
def test_pending_actual_asr_child_keeps_capacity_job_lock_and_unverified_files(prepared, tmp_path,
                                                                           monkeypatch, stop_kind):
    from carr_library.audio import process
    from carr_library.audio.models import AudioCleanupPending

    app, _, service, original, configure = prepared
    broker = _policy(app)
    completed_id = service.submit([original])
    completed = service.run(completed_id, timeout=15)
    assert completed['status'] == 'completed'
    completed_path = service._path(completed_id)
    completed_before = (completed_path / 'job.json').read_bytes()
    accepted_files = {row['path']: (completed_path / 'results' / row['path']).read_bytes()
                      for row in completed['artifacts']}
    source_before = original.read_bytes()

    configure('timeout')  # An actual synthetic child remains alive for 30 seconds.
    identity = service.submit([original])
    path = service._path(identity)
    status_path = path / 'job_status.json'
    children, status_reads, receipt_reads = [], [], []
    allowed = threading.Event()
    owner = None
    native_popen, native_terminate = process.subprocess.Popen, process._terminate
    original_read, original_receipts = transcription.read_json, service._receipts

    def capture(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def delayed_cleanup(child):
        if not allowed.is_set():
            raise OSError('Synthetic native cleanup has not confirmed shutdown.')
        return native_terminate(child)

    def read(value):
        if Path(value) == status_path:
            status_reads.append(True)
            assert all(child.poll() is not None for child in children), (
                'A final status reader opened the actual pending child status.')
        return original_read(value)

    def receipts(job_path, *args, **kwargs):
        if Path(job_path) == path:
            receipt_reads.append(True)
            assert all(child.poll() is not None for child in children), (
                'Pending native output entered artifact verification.')
        return original_receipts(job_path, *args, **kwargs)

    def lock_released():
        try:
            with transcription.state_lock(path, 'transcribe'):
                return True
        except RuntimeError:
            return False

    monkeypatch.setattr(process.subprocess, 'Popen', capture)
    monkeypatch.setattr(process, '_terminate', delayed_cleanup)
    monkeypatch.setattr(transcription, 'read_json', read)
    monkeypatch.setattr(service, '_receipts', receipts)
    # Exercise the no-live-status Windows contract on both supported platforms.
    monkeypatch.setattr(transcription, '_LIVE_STATUS_SNAPSHOTS', False)
    try:
        with pytest.raises(AudioCleanupPending) as pending:
            service.run(identity, timeout=1 if stop_kind == 'timeout' else 10,
                        cancelled=lambda: stop_kind == 'cancel' and status_path.exists())
        owner = pending.value.cleanup_owner
        assert len(children) == 1 and children[0].poll() is None
        assert not owner.quiescent
        assert status_reads == receipt_reads == []
        saved = service.get(identity)
        assert saved['status'] == 'running' and saved['receipts'] == saved['artifacts'] == []
        host_before = (path / 'job.json').read_bytes()
        retained_before = {item.name: item.read_bytes() for item in (path / 'input').iterdir()}
        active = broker.snapshot()['leases']
        assert len(active) == 1
        assert active[0]['operation_id'] == 'asr:' + service.jobs_root.parent.name + ':' + identity
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='competing-heavy-work', timeout=0):
                pytest.fail('The actual pending child released shared capacity.')

        selected_source = saved['sources'][0]['filename']
        export_path = tmp_path / 'pending-export'
        actions = (
            lambda: service.run(identity, timeout=1),
            lambda: service.verify(identity),
            lambda: service.text(identity, selected_source),
            lambda: service.export(identity, export_path),
            lambda: service.save_edit(identity, selected_source, 'Human derivative must wait.'),
        )
        for action in actions:
            with pytest.raises(RuntimeError, match='already being changed'):
                action()
        assert len(children) == 1 and status_reads == receipt_reads == []
        assert (path / 'job.json').read_bytes() == host_before
        assert not export_path.exists() and not (path / 'edits').exists()

        # Another verified completed job remains readable without shared heavy
        # admission, and none of its accepted receipts or files is rewritten.
        assert service.run(completed_id) == completed
        assert service.verify(completed_id)['status'] == 'completed'
        assert (completed_path / 'job.json').read_bytes() == completed_before
        assert accepted_files == {name: (completed_path / 'results' / name).read_bytes()
                                  for name in accepted_files}
        assert len(broker.snapshot()['leases']) == 1
        assert original.read_bytes() == source_before
        assert not owner.close() and not owner.quiescent
    finally:
        allowed.set()
        if owner is not None:
            _wait(lambda: owner.quiescent, seconds=10)
            _wait(lambda: not broker.snapshot()['leases'], seconds=10)
            _wait(lock_released, seconds=10)
        else:
            # A failing assertion must still clean the actual owned fixture.
            for child in children:
                native_terminate(child)
        assert all(child.poll() is not None for child in children)

    assert status_reads == receipt_reads == []
    assert (path / 'job.json').read_bytes() == host_before
    assert retained_before == {item.name: item.read_bytes() for item in (path / 'input').iterdir()}
    # Quiescence releases only ownership. Explicit resume performs all original
    # runtime/source checks and receipt verification; no cleanup thread does it.
    configure('complete')
    resumed = service.run(identity, timeout=15)
    assert resumed['status'] == 'completed' and resumed['done'] == 1
    assert status_reads and receipt_reads and len(children) == 2
    assert service.verify(identity)['status'] == 'completed'
    assert not broker.snapshot()['leases']
    assert original.read_bytes() == source_before


if __name__ == '__main__':
    _studio_actor()
