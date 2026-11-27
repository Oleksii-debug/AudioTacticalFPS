"""Actual synthetic workers under the shared broker; no ASR quality claim."""
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
import time

import pytest

import carr_library.transcription as module
from carr_library.durable_state import atomic_json, read_json, state_lock
from carr_library.operations import (ResourceBroker, ResourceLimits, ResourceRequest,
    ResourceStateError, ResourceTimeout, ResourceUnavailable)
from carr_library.transcription import TranscriptionService, TranscriptionSettings


_spec = importlib.util.spec_from_file_location('resource_transcription_fixtures',
    Path(__file__).with_name('test_transcription.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
engine, source, service = _fixtures.engine, _fixtures.source, _fixtures.service


def policy(service, **changes):
    limits = replace(ResourceLimits(cpu_slots=2, memory_bytes=1024**3,
        disk_bytes=2 * 1024**3, min_free_disk_bytes=0), **changes)
    broker = ResourceBroker.for_app(service.directory.parent.parent, limits=limits)
    service._broker = broker
    return broker


def eventually(predicate, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate(), 'The bounded synthetic worker acknowledgement did not arrive.'


@contextmanager
def running(service, identity, spec, **kwargs):
    cancel, result, errors = threading.Event(), [], []
    def execute():
        try:
            result.append(service.run(identity, engine=spec, cancelled=cancel.is_set,
                timeout=12, stop_grace=.2, **kwargs))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    try:
        yield thread, cancel, result, errors
    finally:
        cancel.set()
        thread.join(8)
        assert not thread.is_alive(), 'The owned worker did not finish cleanup.'


def test_real_batch_holds_lease_through_cleanup_and_completed_replay_bypasses_it(
        service, source, engine, tmp_path, monkeypatch):
    broker = policy(service)
    other = tmp_path / 'second.wav'
    other.write_bytes(source.read_bytes() + b' distinct source')
    identity = service.submit([source, other])
    root = service.jobs / identity
    original_popen, original_stop = module.subprocess.Popen, module.stop_process
    original_config, original_validate = module.atomic_json, service._validate_result
    launched, cleaned, attempts, boundaries = [], [], [], []
    def assert_lease():
        rows = broker.snapshot()['leases']
        assert len(rows) == 1 and rows[0]['operation_id'] == 'asr:' + identity
        request = rows[0]['request']
        assert request['cpu_slots'] == 2 and request['memory_bytes'] == 1024**3
        assert request['disk_bytes'] >= module.MAX_OUTPUT_BYTES
        assert len(request['additional_disks']) == 1
    def launch(*args, **kwargs):
        assert_lease()
        attempts.append(service.get(identity, verify=False)['active_attempt_id'])
        process = original_popen(*args, **kwargs)
        launched.append(process)
        return process
    def cleanup(process):
        try:
            assert_lease()
        finally:
            original_stop(process)
        cleaned.append(process)
    def save(path, *args, **kwargs):
        if path.name == 'job_config.json':
            assert_lease()
            boundaries.append('configuration')
        return original_config(path, *args, **kwargs)
    def validate(*args, **kwargs):
        if service.get(identity, verify=False).get('active_attempt_id') is not None:
            assert_lease()
            boundaries.append('terminal verification')
        return original_validate(*args, **kwargs)
    _fixtures.patch_worker_popen(monkeypatch, launch)
    monkeypatch.setattr(module, 'stop_process', cleanup)
    monkeypatch.setattr(module, 'atomic_json', save)
    monkeypatch.setattr(service, '_validate_result', validate)
    spec = engine()
    result = service.run(identity, engine=spec)
    assert result['state'] == 'succeeded' and len(result['receipt']['artifacts']) == 8
    assert result['active_attempt_id'] is None and len(launched) == 1
    assert boundaries == ['configuration', 'terminal verification']
    assert cleaned and all(process.poll() is not None for process in cleaned)
    assert broker.snapshot()['leases'] == []
    before = {name: (root / name).read_bytes() for name in ('job_config.json', 'job_result.json')}
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Completed replay launched a worker.'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='another-operation', timeout=0):
        assert service.run(identity, engine=spec, resource_timeout=0) == result
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['another-operation']
    assert before == {name: (root / name).read_bytes() for name in before}
    assert service.request_stop(identity, expected_attempt_id=attempts[0]) is False
    assert service.set_paused(identity, True, expected_attempt_id=attempts[0]) is False


@pytest.mark.parametrize('control', ['cancel', 'stop'])
def test_waiting_cancel_or_exact_stop_never_configures_or_launches(
        service, source, engine, monkeypatch, control):
    broker = policy(service)
    identity = service.submit([source])
    root, waiting = service.jobs / identity, threading.Event()
    spec = engine()
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Waiting work launched a worker.'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
        with running(service, identity, spec,
                progress=lambda *args: waiting.set() if args[-1].startswith('Waiting') else None) as state:
            thread, cancel, result, errors = state
            assert waiting.wait(5)
            row = service.get(identity, verify=False)
            assert row['state'] == 'running'
            assert not (root / 'job_config.json').exists()
            assert service.request_stop(identity, expected_attempt_id='0' * 32) is False
            if control == 'stop':
                assert service.request_stop(identity, expected_attempt_id=row['active_attempt_id']) is True
            else:
                cancel.set()
            thread.join(5)
            assert not thread.is_alive() and not errors
            assert result[0]['state'] == 'cancelled' and result[0]['active_attempt_id'] is None
            assert [item['operation_id'] for item in broker.snapshot()['leases']] == ['occupier']
    assert not (root / 'job_config.json').exists()
    assert not (root / 'job_result.json').exists()
    assert broker.snapshot()['leases'] == []


def test_pause_during_wait_survives_admission_and_old_attempt_cannot_control_resume(
        service, source, engine):
    broker = policy(service)
    identity, waiting = service.submit([source]), threading.Event()
    root = service.jobs / identity
    spec = engine('pause')
    holder = broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0)
    holder.__enter__()
    held = True
    try:
        with running(service, identity, spec,
                progress=lambda *args: waiting.set() if args[-1].startswith('Waiting') else None) as state:
            thread, _, result, errors = state
            assert waiting.wait(5)
            first = service.get(identity, verify=False)['active_attempt_id']
            assert service.set_paused(identity, True, expected_attempt_id=first)
            holder.__exit__(None, None, None)
            held = False
            eventually(lambda: (root / 'test_pause_seen.json').exists())
            assert service.set_paused(identity, False, expected_attempt_id=first)
            eventually(lambda: (root / 'test_continue_seen.json').exists())
            assert service.request_stop(identity, expected_attempt_id=first)
            thread.join(5)
            assert not errors and result[0]['state'] == 'cancelled'
        # A fresh explicit resume reuses the job, but never its control token.
        waiting.clear()
        with running(service, identity, engine('cooperative'),
                progress=lambda *args: waiting.set() if args[-1].startswith('Waiting') else None) as state:
            thread, _, result, errors = state
            assert waiting.wait(5)
            second = service.get(identity, verify=False)['active_attempt_id']
            assert second != first
            assert service.set_paused(identity, True, expected_attempt_id=first) is False
            assert service.request_stop(identity, expected_attempt_id=first) is False
            assert not (root / 'pause.flag').exists() and not (root / 'stop.flag').exists()
            assert service.request_stop(identity, expected_attempt_id=second)
            thread.join(5)
            assert not errors and result[0]['state'] == 'cancelled'
        assert broker.snapshot()['leases'] == []
    finally:
        if held:
            holder.__exit__(None, None, None)


def test_stale_running_journal_and_backup_run_lock_cannot_authorize_controls(service, source):
    identity = service.submit([source])
    root = service.jobs / identity
    row = service.get(identity, verify=False)
    row.update(state='running', active_attempt_id='a' * 32)
    atomic_json(root / 'job.json', row, replace=True)
    with state_lock(root, 'transcription'):
        assert service.request_stop(identity, expected_attempt_id='a' * 32) is False
        assert service.set_paused(identity, True, expected_attempt_id='a' * 32) is False
        with pytest.raises(ValueError, match='Start or explicitly resume'):
            service.set_paused(identity, True)
    assert not (root / 'stop.flag').exists() and not (root / 'pause.flag').exists()


def test_wait_timeout_keeps_job_resumable_without_configuration(service, source, engine, monkeypatch):
    broker = policy(service)
    identity = service.submit([source])
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Timed-out work launched.'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
        with pytest.raises(ResourceTimeout):
            service.run(identity, engine=engine(), resource_timeout=.06)
        row = service.get(identity, verify=False)
        assert row['state'] == 'queued' and row['active_attempt_id'] is None
        assert 'Timed out' in row['error']
    assert not (service.jobs / identity / 'job_config.json').exists()


@pytest.mark.parametrize('kind', ['source', 'worker'])
def test_wait_does_not_extend_authority_over_changed_inputs_or_worker(
        service, source, engine, monkeypatch, kind):
    broker = policy(service)
    identity, waiting = service.submit([source]), threading.Event()
    spec = engine()
    root = service.jobs / identity
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Changed bytes launched.'))
    holder = broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0)
    holder.__enter__()
    held = True
    try:
        with running(service, identity, spec,
                progress=lambda *args: waiting.set() if args[-1].startswith('Waiting') else None) as state:
            thread, _, result, errors = state
            assert waiting.wait(5)
            target = (root / service.get(identity, verify=False)['inputs'][0]['path']
                      if kind == 'source' else Path(spec.worker_path))
            target.chmod(0o600)
            target.write_bytes(b'Changed after initial validation')
            holder.__exit__(None, None, None)
            held = False
            thread.join(5)
            assert not result and len(errors) == 1 and isinstance(errors[0], ValueError)
        assert not (root / 'job_config.json').exists()
        assert read_json(root / 'job.json')['state'] == 'failed'
        assert broker.snapshot()['leases'] == []
    finally:
        if held:
            holder.__exit__(None, None, None)


@pytest.mark.parametrize('settings,changes,reason', [
    (TranscriptionSettings(device='auto'), {}, 'cpu or cuda explicitly'),
    (TranscriptionSettings(device='cuda'), {}, 'explicit shared resource capacity'),
    (TranscriptionSettings(), {'memory_bytes': 0}, 'positive declared memory'),
])
def test_unreserved_device_or_zero_memory_never_starts(
        service, source, engine, monkeypatch, settings, changes, reason):
    broker = policy(service, **changes)
    identity = service.submit([source], settings)
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Unreserved engine launched.'))
    with pytest.raises(ResourceUnavailable, match=reason):
        service.run(identity, engine=engine())
    assert not (service.jobs / identity / 'job_config.json').exists()
    assert broker.snapshot()['leases'] == []


def test_explicit_cuda_policy_is_reserved_without_gpu_or_remapping_claim(
        service, source, engine, monkeypatch):
    broker = policy(service, gpu_devices=(('cuda:0', 700),))
    identity = service.submit([source], TranscriptionSettings(device='cuda'))
    original = module.subprocess.Popen
    checked = []
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '9')
    def launch(*args, **kwargs):
        snapshot = broker.snapshot()
        assert snapshot['gpu_devices_reserved'] == ['cuda:0']
        assert snapshot['leases'][0]['request']['gpu_memory_bytes'] == 700
        assert 'CUDA_VISIBLE_DEVICES' not in kwargs['env']
        checked.append(True)
        return original(*args, **kwargs)
    _fixtures.patch_worker_popen(monkeypatch, launch)
    assert service.run(identity, engine=engine())['state'] == 'succeeded'
    assert checked and broker.snapshot()['leases'] == []


def test_broker_from_another_library_is_rejected(service, source, engine, tmp_path, monkeypatch):
    other = tmp_path / 'other-library'
    other.mkdir()
    service._broker = ResourceBroker.for_app(other)
    identity = service.submit([source])
    _fixtures.patch_worker_popen(monkeypatch, lambda *a, **k: pytest.fail('Another policy launched work.'))
    with pytest.raises(ResourceStateError, match='another data folder'):
        service.run(identity, engine=engine())
    assert not (service.jobs / identity / 'job_config.json').exists()


def test_unresponsive_child_is_cleaned_before_releasing_resources(
        service, source, engine, monkeypatch):
    broker = policy(service)
    identity = service.submit([source])
    root = service.jobs / identity
    original = module.stop_process
    observed = []
    def cleanup(process):
        try:
            assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['asr:' + identity]
        finally:
            original(process)
        observed.append(process.poll())
    monkeypatch.setattr(module, 'stop_process', cleanup)
    with running(service, identity, engine('unresponsive')) as state:
        thread, _, result, errors = state
        eventually(lambda: (root / 'test_child.json').exists())
        token = service.get(identity, verify=False)['active_attempt_id']
        assert service.request_stop(identity, expected_attempt_id=token)
        thread.join(6)
        assert not errors and result[0]['state'] == 'cancelled'
    assert observed and all(code is not None for code in observed)
    assert broker.snapshot()['leases'] == []


def test_real_worker_failure_releases_lease_and_preserves_decisive_diagnostic(service, source, engine):
    broker = policy(service)
    identity = service.submit([source])
    with pytest.raises(RuntimeError, match='Synthetic startup error'):
        service.run(identity, engine=engine('early-failure'))
    result = service.get(identity, verify=False)
    assert result['state'] == 'failed' and result['active_attempt_id'] is None
    assert 'exit 17' in result['error']
    assert broker.snapshot()['leases'] == []


def test_late_stop_observer_cannot_rewrite_verified_completion(service, source, engine):
    policy(service)
    identity, token, stopped = service.submit([source]), [], []
    def observe(done, total, phase):
        if phase.startswith('Waiting'):
            token.append(service.get(identity, verify=False)['active_attempt_id'])
        if phase == 'Verified every requested transcript output':
            stopped.append(service.request_stop(identity, expected_attempt_id=token[0]))
    result = service.run(identity, engine=engine(), progress=observe)
    assert result['state'] == 'succeeded' and stopped == [False]
    assert service.get(identity) == result
