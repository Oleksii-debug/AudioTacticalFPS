"""Native subprocess startup failure cannot outlive its shared reservation."""
from pathlib import Path
import sys
import threading

import pytest

from carr_library.audio import process
from carr_library.operations import ResourceBroker, ResourceRequest


@pytest.mark.parametrize('failed_reader', [1, 2])
@pytest.mark.parametrize('phase', ['allocation', 'start'])
def test_pipe_reader_start_failure_stops_real_child_before_resource_release(tmp_path, monkeypatch, failed_reader, phase):
    broker = ResourceBroker.for_app(tmp_path)
    children, readers = [], []
    native_popen, native_start, native_thread = process.subprocess.Popen, threading.Thread.start, threading.Thread
    allocated = []

    def popen(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def allocate(*args, **kwargs):
        allocated.append(True)
        if phase == 'allocation' and len(allocated) == failed_reader:
            raise RuntimeError('Synthetic diagnostic reader startup failure during allocation')
        return native_thread(*args, **kwargs)

    def start(thread):
        readers.append(thread)
        if phase == 'start' and len(readers) == failed_reader:
            raise RuntimeError('Synthetic diagnostic reader startup failure')
        return native_start(thread)

    monkeypatch.setattr(process.subprocess, 'Popen', popen)
    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(process.threading, 'Thread', allocate)
    with broker.reserve(ResourceRequest(), operation_id='startup-failure'):
        try:
            with pytest.raises(RuntimeError, match='reader startup failure'):
                process.run_process([str(Path(sys.executable).absolute()), '-I', '-c',
                                     'import time; time.sleep(30)'])
            assert len(children) == 1 and children[0].poll() is not None
            assert all(not reader.is_alive() for reader in readers)
            assert children[0].stdout.closed and children[0].stderr.closed
            assert len(broker.snapshot()['leases']) == 1
        finally:
            # A failing regression must also clean its actual native fixture.
            for child in children:
                process._terminate(child)
    assert broker.snapshot()['leases'] == []
