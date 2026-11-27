"""Exact cooperative allowances and portable policy-only complete backups."""
import json
from pathlib import Path
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceCancelled
from carr_library.operations._locks import NativeLock
from carr_library.operations.policy_backup import FILENAME, decode_policy, snapshot_guard
from carr_library.ui.operations_dialog import policy_fields, policy_from_fields, MIB, GIB


def test_native_fields_preserve_exact_unedited_byte_allowances():
    policy = ResourceLimits(cpu_slots=2, memory_bytes=1000, disk_bytes=2**63-1,
        min_free_disk_bytes=37, gpu_devices=(('cuda:0', 2**63-1),), max_leases=8)
    fields = policy_fields(policy)
    assert policy_from_fields(**fields, previous=policy) == policy
    fields['memory_mib'] += 1
    changed = policy_from_fields(**fields, previous=policy)
    assert changed.memory_bytes == fields['memory_mib'] * MIB
    assert changed.disk_bytes == policy.disk_bytes and changed.gpu_devices == policy.gpu_devices


@pytest.mark.parametrize('gpu', ['cuda:0', 'cuda:0=-1', 'cuda:0=NaN', 'cuda:0=1.5',
    'cuda:0=0', 'cuda:0=1\ncuda:0=2', '../device=5', 'CUDA:0=100',
    'cuda:0=' + '9'*25], ids=['missing', 'negative', 'nan', 'fraction', 'zero',
    'duplicate', 'path', 'uppercase', 'oversized'])
def test_invalid_gpu_policy_never_persists(gpu):
    with pytest.raises(ValueError):
        policy_from_fields(cpu=2, memory_mib=1024, disk_gib=2, free_mib=256,
                           gpu=gpu, max_leases=8)


@pytest.mark.parametrize('name', ['cpu', 'memory_mib', 'disk_gib', 'free_mib', 'max_leases'])
def test_boolean_native_allowances_are_rejected(name):
    fields = dict(cpu=2, memory_mib=1024, disk_gib=2, free_mib=256, gpu='', max_leases=8)
    fields[name] = True
    with pytest.raises(ValueError):
        policy_from_fields(**fields)


def test_policy_round_trip_excludes_runtime_leases_and_local_engine_profiles(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Portable limits')
    policy = ResourceLimits(cpu_slots=2, memory_bytes=1024*MIB, disk_bytes=3*GIB,
                            min_free_disk_bytes=0, gpu_devices=(('cuda:0', 512*MIB),), max_leases=8)
    broker = ResourceBroker.for_app(app.root, limits=policy)
    local = app.root / 'state' / 'audio' / 'engines.json'
    local.parent.mkdir(parents=True)
    local.write_text('PRIVATE LOCAL EXECUTABLE PROFILE', encoding='utf-8')
    destination = tmp_path / 'backup'
    with broker.reserve(ResourceRequest(), operation_id='document:' + '1'*32, timeout=0):
        receipt = create_backup(app, destination)
        assert len(broker.snapshot()['leases']) == 1
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    assert [row['path'] for row in manifest['sidecars']] == [FILENAME]
    before = {p.relative_to(destination): (p.stat().st_mtime_ns, p.read_bytes())
              for p in destination.rglob('*') if p.is_file()}
    verify_backup(destination, receipt)
    after = {p.relative_to(destination): (p.stat().st_mtime_ns, p.read_bytes())
             for p in destination.rglob('*') if p.is_file()}
    assert before == after
    target = tmp_path / 'restored'
    restore_backup(destination, target, receipt)
    assert decode_policy((target / FILENAME).read_bytes()) == policy
    assert not (target / 'runtime').exists() and not (target / 'state/audio').exists()
    restored = LibraryApp(target)
    repeated = tmp_path / 'rebackup'
    repeated_receipt = create_backup(restored, repeated)
    verify_backup(repeated, repeated_receipt)
    assert (repeated / 'sidecars' / FILENAME).read_bytes() == (target / FILENAME).read_bytes()


def test_policy_snapshot_waits_on_same_native_admission_gate_and_can_cancel(tmp_path):
    broker = ResourceBroker.for_app(tmp_path)
    lock = NativeLock(broker.root / '.admission.lock')
    assert lock.acquire()
    entered, stop = Event(), Event()
    outcomes = []
    def work():
        entered.set()
        try:
            with snapshot_guard(tmp_path, cancelled=stop.is_set):
                outcomes.append('unexpected acquisition')
        except BaseException as exc:
            outcomes.append(exc)
    worker = Thread(target=work)
    try:
        worker.start()
        assert entered.wait(2)
        stop.set()
        worker.join(3)
        assert not worker.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], (InterruptedError, ResourceCancelled))
        assert lock.held
    finally:
        stop.set()
        lock.release()
        worker.join(3)


def test_backup_rejects_unknown_durable_resource_state_instead_of_silently_omitting_it(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    ResourceBroker.for_app(app.root)
    extra = app.root / 'state/operations/another-job.json'
    extra.write_text('{}', encoding='utf-8')
    with pytest.raises((ValueError, RuntimeError), match='Unregistered'):
        create_backup(app, tmp_path / 'backup')
    assert extra.read_text(encoding='utf-8') == '{}'


def test_policy_decoder_rejects_duplicates_and_hidden_executable_fields():
    limits = ResourceLimits().to_dict()
    with pytest.raises(ValueError, match='Duplicate'):
        decode_policy('{"schema":"carr-resource-policy/1","schema":"carr-resource-policy/1","limits":{}}')
    with pytest.raises(ValueError, match='Unsupported'):
        decode_policy(json.dumps({'schema': 'carr-resource-policy/1', 'limits': limits,
                                  'executable': '/private/runtime'}))
