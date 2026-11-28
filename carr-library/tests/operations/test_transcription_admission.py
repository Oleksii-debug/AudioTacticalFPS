"""Canonical supplied-worker admission using real child processes and receipts.

Synthetic protocol fixtures do not claim acoustic inference or measured hardware
limits. Existing V5/bootstrap and receipt suites remain separate required gates.
"""
from dataclasses import replace
import importlib.util
from pathlib import Path
import threading
import time

import pytest

from carr_library.audio import AudioCancelled
from carr_library.operations import (OperationFacade, ResourceBroker, ResourceLimits,
    ResourceRequest, ResourceStateError, ResourceUnavailable)
from carr_library.transcription import TranscriptionService
import carr_library.transcription as module


_spec = importlib.util.spec_from_file_location('operations_canonical_worker_fixture',
    Path(__file__).parents[1] / 'transcription' / 'test_supplied_worker_bridge.py')
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
prepared = _helpers.prepared


def admitted(prepared):
    app, workspace, _, original, configure = prepared
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=1024**3, disk_bytes=2*1024**3, min_free_disk_bytes=0))
    return TranscriptionService(app, workspace, broker=broker), broker, original, configure


def test_actual_worker_receipts_hold_capacity_until_child_cleanup_and_completed_reuse(prepared, monkeypatch):
    service, broker, original, configure = admitted(prepared)
    worker = configure()
    worker.write_text('import os\nassert os.environ["OMP_NUM_THREADS"] == "2"\n'
        'assert os.environ["OPENBLAS_NUM_THREADS"] == "2"\n'
        + _helpers.SYNTHETIC_WORKER.replace('__MODE__', repr('complete')), encoding='utf-8')
    configured = service.configuration()
    service.configure(configured['python'], worker, configured['models_dir'])
    identity = service.submit([original])
    before = original.read_bytes()
    observations = []
    actual = module.run_process

    def run(*args, **kwargs):
        observations.append(('start', broker.snapshot()))
        try:
            return actual(*args, **kwargs)
        finally:
            observations.append(('cleaned', broker.snapshot()))
    monkeypatch.setattr(module, 'run_process', run)
    # A valid long execution allowance remains governed by AudioBudget.
    result = service.run(identity, timeout=2 * 86400)
    assert result['status'] == 'completed' and result['done'] == 1
    assert len(result['receipts']) == 1 and result['artifacts']
    assert original.read_bytes() == before
    assert [phase for phase, _ in observations] == ['start', 'cleaned']
    assert all(snapshot['usage']['cpu_slots'] == 2
        and [row['operation_id'] for row in snapshot['leases']] == ['asr:' + identity]
        for _, snapshot in observations)
    assert broker.snapshot()['leases'] == []
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupied', timeout=0):
        assert service.run(identity, timeout=1)['status'] == 'completed'
        assert len(observations) == 2  # Verified completion starts no child/admission.
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['occupied']


def test_cancelled_admission_does_not_read_engine_or_start_child(prepared, monkeypatch):
    service, broker, original, _ = admitted(prepared)
    identity = service.submit([original])
    stopped, waiting, failures = threading.Event(), threading.Event(), []
    real_reserve = broker.reserve

    def reserve(*args, **kwargs):
        waiting.set()
        return real_reserve(*args, **kwargs)
    monkeypatch.setattr(broker, 'reserve', reserve)
    monkeypatch.setattr(service, 'configuration', lambda **kwargs: pytest.fail('Engine read before admission'))
    monkeypatch.setattr(module, 'run_process', lambda *args, **kwargs: pytest.fail('Child started while waiting'))

    def run():
        try:
            service.run(identity, cancelled=stopped.is_set, timeout=5)
        except BaseException as error:
            failures.append(error)
    with real_reserve(ResourceRequest(cpu_slots=2), operation_id='occupied', timeout=0):
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert waiting.wait(3)
            stopped.set()
            thread.join(3)
            assert not thread.is_alive()
            assert len(failures) == 1 and isinstance(failures[0], AudioCancelled)
            assert service.get(identity)['status'] == 'ready'
            assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['occupied']
            assert not (service.jobs_root / identity / 'job_config.json').exists()
        finally:
            stopped.set()
            thread.join(3)
    assert broker.snapshot()['leases'] == []


def test_actual_worker_stop_keeps_lease_through_cleanup_and_view_has_no_invented_controls(prepared, monkeypatch):
    service, broker, original, configure = admitted(prepared)
    configure('pause-once')
    identity = service.submit([original])
    stopped, results, failures, cleanup = threading.Event(), [], [], []
    actual = module.run_process
    def run_child(*args, **kwargs):
        try:
            return actual(*args, **kwargs)
        finally:
            cleanup.append(broker.snapshot())
    monkeypatch.setattr(module, 'run_process', run_child)
    def run():
        try:
            results.append(service.run(identity, cancelled=stopped.is_set, timeout=15))
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    facade = OperationFacade(service.app, broker=broker, transcription=service)
    try:
        deadline = time.monotonic() + 5
        while not (service.jobs_root / identity / 'job_status.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (service.jobs_root / identity / 'job_status.json').exists()
        view = facade.get('asr', identity, workspace=service.workspace)
        assert view.live is True and view.controls == () and view.completed == 0
        for action in ('pause', 'cancel'):
            with pytest.raises(ValueError, match='does not expose'):
                facade.control('asr', identity, action, workspace=service.workspace)
        stopped.set()
        thread.join(7)
        assert not thread.is_alive() and not failures
        assert results[0]['status'] == 'stopped' and results[0]['done'] == 0
        assert cleanup and [row['operation_id'] for row in cleanup[0]['leases']] == ['asr:' + identity]
        assert broker.snapshot()['leases'] == []
        assert facade.get('asr', identity, workspace=service.workspace).controls == ()
    finally:
        stopped.set()
        thread.join(7)


@pytest.mark.parametrize('case', ['foreign-root', 'foreign-policy', 'untyped', 'parallel', 'gpu'])
def test_explicit_policy_and_scope_refuse_before_any_worker_configuration(prepared, monkeypatch, tmp_path, case):
    service, broker, original, _ = admitted(prepared)
    options = {'parallel_files': 2} if case == 'parallel' else {'device': 'cuda'} if case == 'gpu' else None
    identity = service.submit([original], options=options)
    if case == 'foreign-root':
        other = tmp_path / 'other'
        other.mkdir()
        service._broker = ResourceBroker.for_app(other)
    elif case == 'foreign-policy':
        service._broker = ResourceBroker(tmp_path / 'foreign-runtime')
    elif case == 'untyped':
        service._broker = object()
    monkeypatch.setattr(service, 'configuration', lambda **kwargs: pytest.fail('Configuration read before refusal'))
    monkeypatch.setattr(module, 'run_process', lambda *args, **kwargs: pytest.fail('Unapproved worker execution'))
    error = ResourceStateError if case in ('foreign-root', 'foreign-policy', 'untyped') else ResourceUnavailable
    with pytest.raises(error):
        service.run(identity, timeout=1)
    assert service.get(identity)['status'] == 'ready'
    assert broker.snapshot()['leases'] == []


def test_policy_survives_actual_backup_without_live_reservations(prepared, tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup
    service, broker, original, _ = admitted(prepared)
    identity = service.submit([original])
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='owner-live', timeout=0):
        digest = service.app.backup(backup)
        restore_backup(backup, restored, expected_manifest_sha256=digest)
        reopened = LibraryApp(restored)
        assert not (restored / 'runtime/resources').exists()
        after = ResourceBroker.for_app(restored)
        assert after.limits == broker.limits and after.snapshot()['leases'] == []
        row, = OperationFacade(reopened, broker=after).list(workspace=service.workspace)
        assert row.kind == 'asr' and row.id == identity and row.controls == ()
        assert row.live is None and row.status == 'ready'
        assert [value['operation_id'] for value in broker.snapshot()['leases']] == ['owner-live']


def test_retained_cleanup_owner_blocks_terminal_status_reads_and_capacity_release(prepared, monkeypatch):
    from carr_library.audio.cleanup import pending_cleanup
    from carr_library.durable_state import atomic_json
    service, broker, original, configure = admitted(prepared)
    configure()
    identity = service.submit([original])
    path = service.jobs_root / identity
    entered, settled, finish = threading.Event(), threading.Event(), threading.Event()
    results, failures, owners, terminal_reads = [], [], [], []
    actual_read = module.read_json

    def pending_worker(*_args, **_kwargs):
        atomic_json(path / 'job_status.json', {'state': 'running', 'done': 0, 'total': 1})
        error = pending_cleanup(lambda: finish.is_set(), message='Synthetic retained native family')
        owners.append(error.cleanup_owner)
        event = error.cleanup_owner.quiescent_event
        class ObservedEvent:
            def is_set(self): return event.is_set()
            def set(self): event.set()
            def wait(self, timeout=None):
                settled.set()
                return event.wait(timeout)
        error.cleanup_owner.quiescent_event = ObservedEvent()
        entered.set()
        raise error

    def read(target, **options):
        if Path(target) == path / 'job_status.json':
            assert finish.is_set() and owners[0].quiescent_event.is_set()
            terminal_reads.append(True)
        return actual_read(target, **options)
    monkeypatch.setattr(module, 'run_process', pending_worker)
    monkeypatch.setattr(module, 'read_json', read)

    def run():
        try:
            results.append(service.run(identity, timeout=10))
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3) and settled.wait(3)
        assert thread.is_alive() and not failures and not terminal_reads
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['asr:' + identity]
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert results[0]['status'] == 'failed' and results[0]['receipts'] == []
    assert terminal_reads and broker.snapshot()['leases'] == []
