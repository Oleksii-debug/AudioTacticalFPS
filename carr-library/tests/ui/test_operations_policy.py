"""User resource policy is parsed explicitly and persisted by the shared broker."""
import pytest

from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest
from carr_library.ui.operations_dialog import policy_from_fields, MIB, GIB


def test_explicit_native_policy_persists_without_enabling_unlisted_gpu(tmp_path):
    value = policy_from_fields(cpu=2, memory_mib=1024, disk_gib=3, free_mib=0,
        gpu='cuda:0=512\ncuda:1=1024', max_leases=8)
    broker = ResourceBroker.for_app(tmp_path)
    broker.configure(value)
    reopened = ResourceBroker.for_app(tmp_path)
    assert reopened.limits == value
    assert value.memory_bytes == 1024*MIB and value.disk_bytes == 3*GIB
    assert value.gpu_devices == (('cuda:0', 512*MIB), ('cuda:1', 1024*MIB))
    with reopened.reserve(ResourceRequest(cpu_slots=1, gpu_device='cuda:1', gpu_memory_bytes=128*MIB),
                          operation_id='asr:synthetic', timeout=0):
        assert len(reopened.snapshot()['leases']) == 1
    assert not reopened.snapshot()['leases']
    disabled = policy_from_fields(cpu=2, memory_mib=1024, disk_gib=3, free_mib=0,
        gpu='', max_leases=8)
    assert disabled.gpu_devices == ()


@pytest.mark.parametrize('gpu', ['cuda:0', 'cuda:0=-1', 'cuda:0=NaN', 'cuda:0=1.5',
    'cuda:0=0', 'cuda:0=1\ncuda:0=2', '../device=5', 'CUDA:0=100', 'a'*4097])
def test_invalid_gpu_policy_is_rejected_without_persistence(gpu):
    with pytest.raises(ValueError):
        policy_from_fields(cpu=2, memory_mib=1024, disk_gib=2, free_mib=256,
            gpu=gpu, max_leases=8)


@pytest.mark.parametrize('field', ['cpu', 'memory_mib', 'disk_gib', 'free_mib', 'max_leases'])
@pytest.mark.parametrize('value', [True, False, 1.5, float('nan'), 10**400])
def test_policy_numeric_fields_reject_invalid_values(field, value):
    values = dict(cpu=2, memory_mib=1024, disk_gib=2, free_mib=256, gpu='', max_leases=8)
    values[field] = value
    with pytest.raises(ValueError):
        policy_from_fields(**values)


def test_unchanged_native_fields_preserve_exact_saved_byte_allowances():
    before = ResourceLimits(cpu_slots=1, memory_bytes=MIB + 1, disk_bytes=GIB + 1,
                            min_free_disk_bytes=MIB + 2, gpu_devices=(('cuda:0', MIB + 3),))
    after = policy_from_fields(cpu=2, memory_mib=2, disk_gib=2, free_mib=2,
                              gpu='cuda:0=2', max_leases=64, previous=before)
    assert after.cpu_slots == 2
    assert (after.memory_bytes, after.disk_bytes, after.min_free_disk_bytes, after.gpu_devices) == (
        before.memory_bytes, before.disk_bytes, before.min_free_disk_bytes, before.gpu_devices)


def test_display_limit_never_silently_reduces_existing_large_byte_policy():
    before = ResourceLimits(memory_bytes=2**63-1, disk_bytes=2**63-1, min_free_disk_bytes=2**63-1)
    after = policy_from_fields(cpu=before.cpu_slots, memory_mib=2147483647, disk_gib=2147483647,
                              free_mib=2147483647, gpu='', max_leases=before.max_leases, previous=before)
    assert after == before
