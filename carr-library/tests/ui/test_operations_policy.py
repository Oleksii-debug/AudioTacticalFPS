"""User resource policy is parsed explicitly and persisted by the shared broker."""
import pytest

from carr_library.operations import ResourceBroker, ResourceRequest
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
