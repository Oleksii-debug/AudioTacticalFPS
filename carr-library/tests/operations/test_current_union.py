"""Concrete current-composition boundaries; no hardware/model qualification."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import threading
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.cleanup import pending_cleanup
from carr_library.audio.models import AudioCleanupPending
from carr_library.durable_state import atomic_json, read_json
from carr_library.operations import (OperationFacade, ResourceBroker, ResourceLimits,
    ResourceRequest, ResourceStateError, ResourceTimeout)


@pytest.mark.parametrize('interruption', [None, KeyboardInterrupt, SystemExit],
                         ids=['cleanup', 'keyboard-interrupt', 'system-exit'])
def test_pending_native_owner_retains_actual_lease_until_quiescent(tmp_path, monkeypatch, interruption):
    broker = ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(cpu_slots=1))
    pending, settling, finish = threading.Event(), threading.Event(), threading.Event()
    errors, observed = [], []
    original_start = threading.Thread.start

    def start(thread):
        if thread.name == 'audio-cleanup':
            raise RuntimeError('Synthetic background-thread admission failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)

    def work():
        try:
            with broker.reserve(ResourceRequest(), operation_id='owned-cleanup', timeout=0):
                error = pending_cleanup(lambda: finish.is_set(), message='Synthetic owned family')
                owner = error.cleanup_owner
                event = owner.quiescent_event
                class InterruptedEvent:
                    raised = False
                    def is_set(self): return event.is_set()
                    def set(self): event.set()
                    def wait(self, timeout=None):
                        settling.set()
                        if interruption is not None and not self.raised:
                            self.raised = True
                            observed.append('interrupt')
                            raise interruption('Synthetic interrupt during retained cleanup')
                        return event.wait(timeout)
                owner.quiescent_event = InterruptedEvent()
                pending.set()
                raise error
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert pending.wait(3) and settling.wait(3)
        assert thread.is_alive() and not errors
        snapshot = broker.snapshot()
        assert [row['operation_id'] for row in snapshot['leases']] == ['owned-cleanup']
        with pytest.raises(ResourceTimeout):
            with broker.reserve(ResourceRequest(), operation_id='too-early', timeout=0):
                pytest.fail('A still-owned native family lost its resource lease')
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1
    assert isinstance(errors[0], interruption or AudioCleanupPending)
    assert observed == (['interrupt'] if interruption is not None else [])
    assert broker.snapshot()['leases'] == []


def test_copied_application_policy_rebinds_relative_without_opening_old_root(tmp_path, monkeypatch):
    original = tmp_path / 'original'
    original.mkdir()
    broker = ResourceBroker.for_app(original, limits=ResourceLimits(cpu_slots=2))
    # Exercise the exact old binding migration as well as current relative
    # policy ownership. Runtime metadata is not normal backup payload.
    atomic_json(broker.root / '.policy-location.json',
        {'schema': 'carr-resource-policy-location/1', 'path': str(broker.policy_path)}, replace=True)
    copied = tmp_path / 'copied'
    shutil.copytree(original, copied)
    shutil.rmtree(original)
    actual = Path.open
    def opened(path, *args, **kwargs):
        assert not path.is_relative_to(original), 'A historical policy path was reopened'
        return actual(path, *args, **kwargs)
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, 'open', opened)
        rebound = ResourceBroker.for_app(copied)
        assert rebound.limits.cpu_slots == 2 and rebound.snapshot()['leases'] == []
    assert read_json(rebound.root / '.policy-location.json') == {
        'schema': 'carr-application-resource-policy/1', 'path': 'state/operations/resources.json'}


def test_disappearing_unowned_lock_leaf_is_rechecked_before_real_native_acquisition(tmp_path, monkeypatch):
    from carr_library.operations import _locks
    actual = _locks.local_path
    leaf = tmp_path / 'ordinary.lock'
    calls = []
    def raced(path, *args, **kwargs):
        if Path(path) == leaf and not calls:
            calls.append(True)
            raise FileNotFoundError('Synthetic released Windows lock leaf')
        return actual(path, *args, **kwargs)
    monkeypatch.setattr(_locks, 'local_path', raced)
    lock = _locks.NativeLock(leaf)
    try:
        assert lock.acquire() and lock.held
    finally:
        lock.release()
    assert calls == [True]


def test_same_path_replaced_library_invalidates_existing_operation_view(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    facade = OperationFacade(app)
    old = tmp_path / 'moved-original'
    app.root.rename(old)
    app.root.mkdir()
    with pytest.raises(ResourceStateError, match='data folder changed'):
        facade.list()
    assert old.exists() and list(app.root.iterdir()) == []


def test_aggregate_checkpoint_budget_stops_before_reading_next_private_payload(tmp_path, monkeypatch):
    from carr_library.operations import facade as module
    app = LibraryApp(tmp_path / 'library')
    paths = []
    for _ in range(2):
        identity = uuid4().hex
        path = app.root / 'state/studio/jobs' / identity / 'job.json'
        atomic_json(path, {'schema': 1, 'id': identity, 'status': 'pending', 'nodes': {}})
        paths.append(path)
    monkeypatch.setattr(module, '_MAX_METADATA_BYTES', max(path.stat().st_size for path in paths))
    opened, actual = [], module.read_json
    def read(path, **options):
        opened.append(path)
        return actual(path, **options)
    monkeypatch.setattr(module, 'read_json', read)
    with pytest.raises(ResourceStateError, match='dashboard read budget'):
        OperationFacade(app).list()
    assert len(opened) == 1


@pytest.mark.parametrize('damage', ['unknown', 'path', 'boolean'])
def test_resource_policy_backup_rejects_unknown_or_untyped_authority(tmp_path, damage):
    from carr_library.ingest.restore import RestoreError
    app = LibraryApp(tmp_path / 'library')
    broker = ResourceBroker.for_app(app.root)
    policy = read_json(broker.policy_path)
    if damage == 'unknown': policy['shell'] = 'must never execute'
    elif damage == 'path': policy['limits']['external_runtime'] = '/never/open'
    else: policy['limits']['cpu_slots'] = True
    atomic_json(broker.policy_path, policy, replace=True)
    with pytest.raises((ValueError, RestoreError)):
        app.backup(tmp_path / 'rejected-backup')
    assert not (tmp_path / 'rejected-backup').exists()


def test_recorded_resource_plan_roundtrips_canonical_full_backup_without_old_path_io(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.audio.models import AudioBudget
    from carr_library.library_backup import restore_backup, verify_backup
    from carr_library.speech import VoiceRegistry
    from carr_library.studio.render import ProductionService

    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Passive declared resource plan')
    output = tmp_path / 'old-output'
    output.mkdir()
    broker = ResourceBroker.for_app(app.root)
    def unavailable():
        raise RuntimeError('Synthetic engine probe, never a completed render')
    runtime = SimpleNamespace(budget=AudioBudget(), music_models={}, capabilities=unavailable)
    service = ProductionService(app.root / 'state/studio', voices=VoiceRegistry([]), broker=broker,
        runtime_factory=lambda *_args, **_kwargs: runtime)
    identity = service.submit(service.create('[Pause: 0.2]'), output)
    with pytest.raises(RuntimeError, match='Synthetic engine probe'):
        service.run(identity)
    recorded = service.get(identity)
    assert recorded['status'] == 'failed' and recorded['resource_plan']['request']['cpu_slots'] == 2
    backup = tmp_path / 'complete-backup'
    manifest = app.backup(backup)
    verify_backup(backup, expected_manifest_sha256=manifest)
    old_root = app.root
    shutil.rmtree(old_root)
    shutil.rmtree(output)
    actual = Path.open
    def opened(path, *args, **kwargs):
        assert not path.is_relative_to(old_root) and not path.is_relative_to(output)
        return actual(path, *args, **kwargs)
    restored = tmp_path / 'restored'
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, 'open', opened)
        restore_backup(backup, restored, expected_manifest_sha256=manifest)
        reopened = LibraryApp(restored)
        view = OperationFacade(reopened).get('studio', identity)
        assert view.status == 'failed' and view.live is None
        restored_job = read_json(restored / 'state/studio/jobs' / identity / 'job.json')
        assert restored_job['resource_plan'] == recorded['resource_plan']
        assert ResourceBroker.for_app(restored).snapshot()['leases'] == []
    assert not old_root.exists() and not output.exists()
