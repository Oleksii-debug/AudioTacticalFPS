"""Native POSIX-only inheritance gate, explicitly required on POSIX CI."""
import os

from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceStateError


def test_forked_child_cannot_release_parent_reservation(tmp_path):
    broker = ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1000, disk_bytes=1024 * 1024, min_free_disk_bytes=0))
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

