"""A diagnostic-reader startup failure must not leave its native worker alive."""
from pathlib import Path
import sys
import threading

import pytest

from carr_library.audio import process


@pytest.mark.parametrize('failed_reader', [1, 2])
def test_pipe_reader_start_failure_stops_real_child(tmp_path, monkeypatch, failed_reader):
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
    try:
        with pytest.raises(RuntimeError, match='reader startup failure'):
            process.run_process([str(Path(sys.executable).absolute()), '-I', '-B', '-c',
                                 'import time; time.sleep(30)'])
        assert len(children) == 1 and children[0].poll() is not None
        assert all(not reader.is_alive() for reader in readers)
        assert children[0].stdout.closed and children[0].stderr.closed
    finally:
        # The negative control also cleans its actual native fixture.
        for child in children:
            process._terminate(child)
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=2)
        for child in children:
            for pipe in (child.stdout, child.stderr):
                if not pipe.closed:
                    pipe.close()
