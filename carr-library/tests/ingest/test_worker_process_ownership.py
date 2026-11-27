"""Real child-family ownership at the existing isolated ingest launch boundary.

Fixed local actors exercise native lifetime only; they are not parser, OCR or
publication evidence. The ordinary import/publication suites retain those gates.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest

from carr_library.ingest import worker
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest


DESCENDANT = '''import os,pathlib,signal,sys,time
from filelock import FileLock
if os.name == 'posix':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with FileLock(sys.argv[1], timeout=0):
    pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')
    while True:
        time.sleep(.05)
'''

LEADER = '''import json,pathlib,subprocess,sys,time
subprocess.Popen([sys.executable, '-I', '-B', '-c', sys.argv[1], sys.argv[2], sys.argv[3]])
while not pathlib.Path(sys.argv[3]).exists():
    time.sleep(.01)
if sys.argv[4] == 'orphan':
    print(json.dumps({'result': {'state': 'complete'}}))
else:
    if sys.argv[4] == 'overflow':
        sys.stderr.write('x' * 1000001)
        sys.stderr.flush()
    time.sleep(30)
'''


def _available(path):
    lock = FileLock(path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return False
    lock.release()
    return True


def _actor(monkeypatch, root, mode):
    ready, lock_path = root / 'descendant-ready', root / 'descendant.lock'
    processes = []
    actual = getattr(worker, '_spawn', worker.subprocess.Popen)

    def launch(command, **options):
        process = actual([command[0], '-I', '-B', '-c', LEADER,
                          DESCENDANT, str(lock_path), str(ready), mode], **options)
        processes.append(process)
        return process

    if hasattr(worker, '_spawn'):
        monkeypatch.setattr(worker, '_spawn', launch)
    else:
        # Preserve the exact pre-correction negative control without mutating
        # stdlib subprocess calls made by the broker or other tests.
        monkeypatch.setattr(worker, 'subprocess', SimpleNamespace(
            **{name: getattr(subprocess, name) for name in (
                'TimeoutExpired', 'SubprocessError', 'run', 'DEVNULL')}, Popen=launch))
    return ready, lock_path, processes


@pytest.mark.parametrize('mode, expected', [
    ('orphan', 'complete'), ('cancel', 'cancelled'),
    ('timeout', 'timed_out'), ('overflow', 'failed'),
])
def test_return_or_stop_waits_for_actual_descendant_native_lock(tmp_path, monkeypatch, mode, expected):
    ready, lock_path, processes = _actor(monkeypatch, tmp_path, mode)
    library = tmp_path / 'library'
    library.mkdir()
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=1))
    if mode == 'timeout':
        real_time, armed = worker.time, []

        def after_ready():
            now = real_time.monotonic()
            expired = bool(armed and ready.exists())
            armed.append(True)
            return now + (100 if expired else 0)

        # Only the existing supervisor deadline advances. Native close/waits
        # keep their real clocks, and the real five-second watchdog remains.
        monkeypatch.setattr(worker, 'time', SimpleNamespace(
            monotonic=after_ready, sleep=real_time.sleep))
    try:
        with broker.reserve(ResourceRequest(cpu_slots=1),
                            operation_id='ingest:ownership-regression', timeout=1):
            result = worker._invoke({'fixture': 'fixed local native actor'}, timeout=5,
                cancelled=lambda: mode == 'cancel' and ready.exists())
        assert ready.exists(), 'The real descendant never reached the intended boundary.'
        assert result['state'] == expected
        assert processes[0].poll() is not None
        assert _available(lock_path), 'The supervisor returned while its native descendant was still alive.'
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='competitor', timeout=0):
            assert _available(lock_path), 'Competing admission overlapped the native descendant.'
    finally:
        # Negative-control failures must not leave their fixed synthetic actor.
        if ready.exists() and not _available(lock_path):
            if os.name == 'posix':
                try:
                    os.killpg(processes[0].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                os.kill(int(ready.read_text(encoding='ascii')), signal.SIGTERM)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        with FileLock(lock_path, timeout=5):
            pass
