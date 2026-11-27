"""Native subprocess startup failure cannot outlive its shared reservation."""
from pathlib import Path
import sys
import threading

import pytest

from carr_library.operations import process
from carr_library.operations import ResourceBroker, ResourceRequest


@pytest.mark.parametrize('failed_reader', [1, 2])
def test_pipe_reader_start_failure_stops_real_child_before_resource_release(tmp_path, monkeypatch, failed_reader):
    broker = ResourceBroker.for_app(tmp_path)
    children, readers = [], []
    native_popen, native_start = process.subprocess.Popen, threading.Thread.start

    def popen(*args, **kwargs):
        child = native_popen(*args, **kwargs)
        children.append(child)
        return child

    def start(thread):
        readers.append(thread)
        if len(readers) == failed_reader:
            raise RuntimeError('Synthetic diagnostic reader startup failure')
        return native_start(thread)

    monkeypatch.setattr(process.subprocess, 'Popen', popen)
    monkeypatch.setattr(threading.Thread, 'start', start)
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
