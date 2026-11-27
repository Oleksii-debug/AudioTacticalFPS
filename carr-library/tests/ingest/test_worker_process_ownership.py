"""Real child-family ownership at the existing isolated ingest launch boundary.

Fixed local actors exercise native lifetime only; they are not parser, OCR or
publication evidence. The ordinary import/publication suites retain those gates.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from tempfile import TemporaryFile
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pytest

from carr_library.ingest import worker
from carr_library.audio.models import AudioCleanupPending
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


def test_explicit_native_stdio_preserves_binary_bytes_and_parent_handles():
    payload = 'Точний локальний запит Ω'.encode('utf-8') + b'\0\xff\n'
    diagnostic = b'bounded diagnostic\0\xfe'
    with TemporaryFile() as source, TemporaryFile() as output, TemporaryFile() as errors:
        streams = (source, output, errors)
        inherited = tuple(os.get_inheritable(stream.fileno()) for stream in streams)
        source.write(payload)
        source.seek(0)
        command = [sys.executable, '-I', '-B', '-c',
            'import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data[::-1]);'
            'sys.stderr.buffer.write(' + repr(diagnostic) + ')']
        process = worker._spawn(command, stdin=source, stdout=output, stderr=errors,
                                env=dict(os.environ, PYTHONUTF8='1'))
        try:
            deadline = time.monotonic() + 15
            while worker._poll(process) is None:
                assert time.monotonic() < deadline, 'The actual stdio child did not finish.'
                time.sleep(.01)
        finally:
            worker._stop(process)
        assert process.returncode == 0
        assert not any(stream.closed for stream in streams)
        assert tuple(os.get_inheritable(stream.fileno()) for stream in streams) == inherited
        output.seek(0)
        errors.seek(0)
        assert output.read() == payload[::-1]
        assert errors.read() == diagnostic


@pytest.mark.parametrize('operation', ['request', 'review'])
def test_failed_family_cleanup_retains_streams_request_and_review_source(
        tmp_path, monkeypatch, operation):
    from carr_library.application import LibraryApp
    from carr_library.extract.page_ocr import Config
    import json

    ready, child_lock, processes = _actor(monkeypatch, tmp_path, 'orphan')
    spawn, native_stop = worker._spawn, worker._stop
    allowed, notified = threading.Event(), threading.Event()
    streams, messages, native_attempts = [], [], []

    def capture(command, **options):
        streams.extend(options[name] for name in ('stdin', 'stdout', 'stderr'))
        incoming = options['stdin']
        offset = incoming.tell()
        incoming.seek(0)
        messages.append(json.loads(incoming.read()))
        incoming.seek(offset)
        return spawn(command, **options)

    def denied_until_released(process):
        native_attempts.append(process)
        if not allowed.is_set():
            raise OSError('Injected native family-close failure; actual child remains alive.')
        return native_stop(process)

    monkeypatch.setattr(worker, '_spawn', capture)
    monkeypatch.setattr(worker, '_stop', denied_until_released)
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Native cleanup ownership')
    selected = tmp_path / 'selected.txt'
    selected.write_text('Original source remains unchanged.', encoding='utf-8')
    metadata = dict(title=None, authors=(), language=None, edition=None,
                    volume=None, collections=(), work_id=None, edition_id=None)
    request = worker.prepare_request(app.root, workspace, selected, metadata, Config())
    owner, reviewed = None, None
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            if operation == 'request':
                app.resume_import(workspace, request['request_id'], timeout=10)
            else:
                worker.isolated_render_fingerprint(b'%PDF-1.7\nfixed review source', 1)
        owner = failure.value.cleanup_owner
        owner.add_quiescent_callback(notified.set)
        assert ready.exists() and native_attempts and not owner.quiescent
        assert not any(stream.closed for stream in streams)
        assert not _available(child_lock)
        if operation == 'request':
            saved = worker.load_request(app.root, request['request_id'])
            assert saved['state'] == 'running' and 'result' not in saved
            with pytest.raises(ValueError, match='already running'):
                with worker.request_lock(app.root, request['request_id']):
                    pytest.fail('The same request was admitted before native cleanup.')
            assert app.browse(workspace) == ()
        else:
            reviewed = Path(messages[0]['source'])
            assert reviewed.read_bytes() == b'%PDF-1.7\nfixed review source'
        assert selected.read_text(encoding='utf-8') == 'Original source remains unchanged.'
        assert owner.close() is False and not notified.is_set()
        allowed.set()
        owner.close()
        assert notified.wait(10) and owner.quiescent
        assert all(stream.closed for stream in streams)
        assert _available(child_lock)
        if operation == 'request':
            with worker.request_lock(app.root, request['request_id']):
                pass
        else:
            assert not reviewed.parent.exists()
    finally:
        allowed.set()
        if owner is not None:
            owner.close()
            assert notified.wait(10)
        for process in processes:
            native_stop(process)
