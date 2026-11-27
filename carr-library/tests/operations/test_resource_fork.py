"""Actual Linux fork ownership regression; selected only by the Linux gate."""
import os
import signal
import sys
import time

import pytest

from carr_library.operations import (
    ResourceBroker, ResourceLimits, ResourceRequest, ResourceStateError, ResourceTimeout,
)


def test_forked_child_cannot_release_parent_reservation(tmp_path):
    assert sys.platform.startswith('linux') and hasattr(os, 'fork'), (
        'This actual fork regression belongs to the required Linux lane, not a Windows skip.')
    broker = ResourceBroker(tmp_path / 'runtime', limits=ResourceLimits(
        cpu_slots=2, memory_bytes=1000, disk_bytes=1024 * 1024, min_free_disk_bytes=0))
    request = ResourceRequest(cpu_slots=2)
    with broker.reserve(request, operation_id='parent', timeout=0) as lease:
        before = broker.snapshot()['leases']
        assert len(before) == 1 and lease._lock.held
        pid = os.fork()
        if pid == 0:
            try:
                try:
                    lease.release()
                except ResourceStateError:
                    pass
                else:
                    os._exit(2)
                if not lease.cancelled():
                    os._exit(3)
                try:
                    lease.checkpoint()
                except ResourceStateError:
                    os._exit(0)
                os._exit(4)
            except BaseException:
                os._exit(5)
        status = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                returned, child_status = os.waitpid(pid, os.WNOHANG)
                if returned == pid:
                    pid, status = None, child_status
                    break
                time.sleep(.01)
            assert status is not None, 'The exact fork child did not finish its bounded checks.'
            assert os.waitstatus_to_exitcode(status) == 0
            assert broker.snapshot()['leases'] == before and lease._lock.held
            with pytest.raises(ResourceTimeout):
                with broker.reserve(request, operation_id='competing', timeout=0):
                    raise AssertionError('The forked child released the parent capacity.')
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
    assert broker.snapshot()['leases'] == []
    with broker.reserve(request, operation_id='after-parent-release', timeout=0):
        assert len(broker.snapshot()['leases']) == 1
