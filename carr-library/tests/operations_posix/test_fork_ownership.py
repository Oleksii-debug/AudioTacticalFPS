"""Required native POSIX fork ownership, executed in the Linux protocol lane."""
import os

import pytest

from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceStateError


@pytest.fixture
def broker(tmp_path):
    assert hasattr(os, 'fork'), 'This gate requires an actual POSIX fork host.'
    return ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1000, disk_bytes=1024 * 1024, min_free_disk_bytes=0))


def test_forked_child_cannot_release_parent_reservation(broker):
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='parent', timeout=0) as lease:
        pid = os.fork()
        if pid == 0:
            try:
                lease.release()
            except ResourceStateError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert len(broker.snapshot()['leases']) == 1

