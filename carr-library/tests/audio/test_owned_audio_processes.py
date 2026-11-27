"""Actual owned descendants and synchronous cleanup through current adapters.

These use public synthetic Python children, never installed speech models or a
physical microphone. Windows executes JobProcess itself; POSIX executes its own
session. Fault injections test retained-owner decisions without claiming native
hardware or listening qualification.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

from carr_library.audio import capture_process as process
from carr_library.audio import engines, ffmpeg, windows_job
from carr_library.audio.capture_types import AudioCleanupPending
from carr_library.audio.cleanup import CleanupOwner
from carr_library.audio.jobs import RenderCancelled


PYTHON = str(Path(sys.executable).resolve())


def _running(pid):
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87, 'Owned child state is not verifiable.'
            return False
        try:
            outcome = kernel.WaitForSingleObject(handle, 0)
            assert outcome in (0, 0x102)
            return outcome == 0x102
        finally:
            assert kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith('linux'):
        status = Path('/proc') / str(pid) / 'stat'
        try:
            return status.read_text().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
        except (FileNotFoundError, ProcessLookupError):
            # init can reap the process after kill(pid, 0) succeeds. Missing
            # procfs state is not itself proof; require ESRCH for that PID.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            raise AssertionError('Owned child state is not verifiable.')
    return True


def _last_cleanup(children):
    for child in children:
        if os.name == 'nt':
            child.close()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


@pytest.mark.parametrize('boundary', ['before-open', 'during-read'])
def test_process_liveness_probe_does_not_resurrect_a_reaped_child(monkeypatch, boundary):
    child = subprocess.Popen([PYTHON, '-I', '-c', 'import sys; sys.stdin.buffer.read()'],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        if sys.platform.startswith('linux'):
            actual_kill = os.kill
            inspected = []

            def exit_after_liveness(pid, selected):
                result = actual_kill(pid, selected)
                if pid == child.pid and selected == 0 and not inspected:
                    inspected.append(pid)
                    child.stdin.close()
                    assert child.wait(timeout=5) == 0
                return result

            if boundary == 'before-open':
                monkeypatch.setattr(os, 'kill', exit_after_liveness)
            else:
                real_read = Path.read_text
                selected = Path('/proc') / str(child.pid) / 'stat'

                def exit_during_read(path, *args, **kwargs):
                    if path != selected:
                        return real_read(path, *args, **kwargs)
                    # Hold the real procfs descriptor across actual child exit.
                    # Linux may report ESRCH from read, not only ENOENT at open.
                    with path.open('r', encoding='utf-8') as stream:
                        assert stream.read(1)
                        inspected.append(child.pid)
                        child.stdin.close()
                        assert child.wait(timeout=5) == 0
                        raise ProcessLookupError(errno.ESRCH, 'Child exited during procfs read')

                monkeypatch.setattr(Path, 'read_text', exit_during_read)
        else:
            # Windows uses a pinned native process handle instead of /proc.
            child.stdin.close()
            assert child.wait(timeout=5) == 0
        assert not _running(child.pid)
        if sys.platform.startswith('linux'):
            assert inspected == [child.pid]
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def _subject(kind, tmp_path, cancelled=lambda: False):
    if kind == 'ffmpeg':
        return ffmpeg.run_ffmpeg(PYTHON, [], cancelled=cancelled)
    if kind == 'identity':
        return ffmpeg.ffmpeg_identity(PYTHON, cancelled=cancelled)
    if kind == 'speech':
        return engines._run([PYTHON, '-I', '-c', 'pass'], data=b'selected text',
                            cancelled=cancelled, timeout=10)
    engine = object.__new__(engines.DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=tmp_path)
    engine.timeout = 10
    return engine._stream([PYTHON, '-I', '-c', 'pass'],
                         {'text': 'synthetic public request'}, tmp_path / 'speech.wav', cancelled)


@pytest.mark.parametrize('kind', ['ffmpeg', 'identity', 'speech', 'stream'])
@pytest.mark.parametrize('cancel', [False, True], ids=['normal-exit', 'cancel'])
def test_adapter_reaps_actual_descendant_after_its_leader_exits(tmp_path, monkeypatch, kind, cancel):
    helper = tmp_path / 'helper.py'
    record, ready = tmp_path / 'child.json', tmp_path / 'ready'
    helper.write_text("""
import json, subprocess, sys, time
from pathlib import Path
record, ready, kind = sys.argv[1:]
child_code = "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_text('ready'); time.sleep(30)"
child = subprocess.Popen([sys.executable, '-I', '-c', child_code, ready])
deadline = time.monotonic() + 5
while not Path(ready).exists():
    if time.monotonic() > deadline:
        raise SystemExit(2)
    time.sleep(.01)
Path(record).write_text(json.dumps({'pid': child.pid}), encoding='utf-8')
if kind == 'stream':
    sys.stdin.readline()
    print('{"pcm":"AQA="}', flush=True)
else:
    print('ffmpeg synthetic identity', flush=True)
""", encoding='utf-8')
    actual = process.start_owned_process
    children = []
    module = engines if kind in ('speech', 'stream') else ffmpeg

    def launch(command, **options):
        child = actual([PYTHON, '-I', str(helper), str(record), str(ready), kind], **options)
        children.append(child)
        # The stream needs its real request before exiting. Other adapters are
        # deliberately handed an already-exited leader with a live descendant.
        if kind != 'stream':
            assert child.wait(timeout=8) == 0
            assert _running(json.loads(record.read_text())['pid'])
        return child

    monkeypatch.setattr(module, 'start_owned_process', launch)
    callback = (lambda: record.exists()) if cancel else (lambda: False)
    try:
        if cancel:
            expected = RenderCancelled if kind in ('ffmpeg', 'identity') else InterruptedError
            with pytest.raises(expected):
                _subject(kind, tmp_path, callback)
        else:
            _subject(kind, tmp_path)
        assert record.exists()
        pid = json.loads(record.read_text())['pid']
        assert not _running(pid), 'A returned operation still owned a live descendant.'
        assert children and all(child.poll() is not None for child in children)
    finally:
        _last_cleanup(children)


def test_engine_stdin_is_explicit_readonly_and_echo_remains_compatible(tmp_path, monkeypatch):
    actual = process.start_owned_process
    captured = []

    def launch(command, **options):
        incoming = options['stdin_file']
        assert incoming.readable() and not incoming.writable()
        assert not options.get('stdin_pipe', False)
        captured.append(incoming)
        return actual(command, **options)

    monkeypatch.setattr(engines, 'start_owned_process', launch)
    selected = 'Synthetic Україна \u0001 remains data'.encode('utf-8')
    result = engines._run([PYTHON, '-I', '-c',
        'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'], data=selected, timeout=10)
    assert result == selected
    assert captured[0].closed


def test_synchronous_cleanup_keeps_streams_and_cross_thread_file_lock(tmp_path, monkeypatch):
    allow, entered, done = threading.Event(), threading.Event(), threading.Event()
    actual_close, actual_start = process._close_owned_family, engines.start_owned_process
    streams, children, errors, results = [], [], [], []
    lock_path = tmp_path / 'owned-stage.lock'

    def defer(child):
        entered.set()
        if not allow.is_set():
            raise OSError('injected unconfirmed native family')
        return actual_close(child)

    def launch(command, **options):
        streams.extend(options[name] for name in ('stdin_file', 'stdout', 'stderr'))
        child = actual_start(command, **options)
        children.append(child)
        return child

    monkeypatch.setattr(process, '_close_owned_family', defer)
    monkeypatch.setattr(engines, 'start_owned_process', launch)

    def run():
        try:
            with FileLock(lock_path, timeout=0, thread_local=False):
                results.append(engines._run([PYTHON, '-I', '-c', "import os;os.write(1,b'completed\\n')"], timeout=10))
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert entered.wait(8)
        assert not done.is_set()
        assert len(streams) == 3 and all(not stream.closed for stream in streams)
        with pytest.raises(Timeout):
            FileLock(lock_path, timeout=0).acquire()
        allow.set()
        worker.join(10)
        assert not worker.is_alive() and not errors
        assert results == [b'completed\n']
        assert all(stream.closed for stream in streams)
        with FileLock(lock_path, timeout=0):
            pass
    finally:
        allow.set()
        worker.join(10)
        if not worker.is_alive():
            _last_cleanup(children)


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_wait_defers_interrupt_until_the_retained_owner_is_quiescent(interrupt):
    attempts = []

    def attempt():
        attempts.append(True)
        if len(attempts) == 1:
            raise interrupt('deferred synthetic interrupt')
        return len(attempts) >= 3

    owner = CleanupOwner(attempt)
    pending = AudioCleanupPending('synthetic pending cleanup', cleanup_owner=owner)
    with pytest.raises(interrupt, match='deferred synthetic interrupt'):
        process.wait_for_cleanup(pending)
    assert owner.quiescent and len(attempts) == 3


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_stop_defers_its_initial_interrupt_before_unwinding_native_resources(monkeypatch, interrupt):
    attempts = []

    def close(child):
        attempts.append(child)
        if len(attempts) == 1:
            raise interrupt('initial cleanup interrupt')
        return True

    monkeypatch.setattr(process, '_close_owned_family', close)
    child = object()
    with pytest.raises(interrupt, match='initial cleanup interrupt'):
        process.stop_owned_process(child)
    assert attempts == [child, child]


def test_startup_pending_cleanup_does_not_close_explicit_caller_streams_early(monkeypatch):
    allow, attempted, done = threading.Event(), threading.Event(), threading.Event()
    streams, caught = [], []

    def attempt():
        attempted.set()
        return allow.is_set()

    owner = CleanupOwner(attempt)
    error = AudioCleanupPending('synthetic native admission failed', cleanup_owner=owner)

    def failed(*args, **kwargs):
        streams.extend([kwargs['stdout'], kwargs['stderr']])
        raise error

    # Only the backend selector is injected. The original explicit binary
    # stream validation and wait policy execute unchanged on every platform.
    monkeypatch.setattr(process, 'os', SimpleNamespace(name='nt', environ=os.environ))
    monkeypatch.setattr(windows_job, 'JobProcess', failed)

    def run():
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
                process.start_owned_process([PYTHON, '-I', '-c', 'pass'],
                                            stdout=output, stderr=diagnostic)
        except BaseException as exc:
            caught.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert attempted.wait(5) and not done.is_set()
        assert len(streams) == 2 and all(not stream.closed for stream in streams)
        allow.set()
        worker.join(5)
        assert not worker.is_alive() and caught == [error] and owner.quiescent
        assert all(stream.closed for stream in streams)
    finally:
        allow.set()
        worker.join(5)


@pytest.mark.parametrize('boundary', ['construct', 'start'])
def test_stream_thread_admission_failure_reaps_actual_owned_child(tmp_path, monkeypatch, boundary):
    actual_start, actual_thread = process.start_owned_process, threading.Thread
    children = []

    def launch(command, **options):
        child = actual_start([PYTHON, '-I', '-c', 'import time;time.sleep(30)'], **options)
        children.append(child)
        return child

    def fail(*args, **kwargs):
        raise RuntimeError('synthetic thread admission failure')

    monkeypatch.setattr(engines, 'start_owned_process', launch)
    if boundary == 'construct':
        monkeypatch.setattr(engines.threading, 'Thread', fail)
    else:
        monkeypatch.setattr(actual_thread, 'start', fail)
    try:
        with pytest.raises(RuntimeError, match='thread admission'):
            _subject('stream', tmp_path)
        assert len(children) == 1 and children[0].poll() is not None
        assert children[0].stdin.closed and children[0].stdout.closed
    finally:
        _last_cleanup(children)


@pytest.mark.parametrize('kind', ['ffmpeg', 'identity'])
def test_fast_codec_diagnostic_overflow_is_checked_after_actual_exit(tmp_path, monkeypatch, kind):
    actual = process.start_owned_process

    def launch(command, **options):
        child = actual([PYTHON, '-I', '-c', "import os;os.write(2,b'x'*(2*1024*1024))"], **options)
        assert child.wait(timeout=8) == 0
        return child

    monkeypatch.setattr(ffmpeg, 'start_owned_process', launch)
    with pytest.raises(RuntimeError, match='excessive'):
        _subject(kind, tmp_path)


def test_version_identity_keeps_combined_output_hash_and_failure_diagnostics(tmp_path, monkeypatch):
    actual = process.start_owned_process
    raw = b'synthetic ffmpeg version\nsecond diagnostic line\n'

    def launch(command, **options):
        return actual([PYTHON, '-I', '-c',
            "import os;os.write(1,b'synthetic ffmpeg version\\n');os.write(2,b'second diagnostic line\\n')"],
            **options)

    monkeypatch.setattr(ffmpeg, 'start_owned_process', launch)
    identity = ffmpeg.ffmpeg_identity(PYTHON)
    assert identity['version'] == 'synthetic ffmpeg version'
    assert identity['version_sha256'] == hashlib.sha256(raw).hexdigest()
    assert identity['binary_sha256'] == hashlib.sha256(Path(PYTHON).read_bytes()).hexdigest()


def test_ffmpeg_retains_error_tail_while_speech_errors_remain_redacted(monkeypatch):
    actual = process.start_owned_process

    def launch(command, **options):
        return actual([PYTHON, '-I', '-c',
            "import sys;sys.stderr.write('synthetic diagnostic marker');raise SystemExit(7)"], **options)

    monkeypatch.setattr(ffmpeg, 'start_owned_process', launch)
    with pytest.raises(RuntimeError, match='synthetic diagnostic marker'):
        ffmpeg.run_ffmpeg(PYTHON, [])
    monkeypatch.setattr(engines, 'start_owned_process', launch)
    with pytest.raises(engines.EngineUnavailable, match='exit code 7') as error:
        engines._run([PYTHON, '-I', '-c', 'pass'])
    assert 'synthetic diagnostic marker' not in str(error.value)


def test_ffmpeg_duration_remains_owned_by_caller_cancellation(monkeypatch):
    ticks = [0.0]
    class Child:
        returncode = 0
        def poll(self):
            return 0 if ticks[0] > 400 * 86400 else None
    child = Child()
    closed = []
    monkeypatch.setattr(ffmpeg, 'start_owned_process', lambda *args, **kwargs: child)
    monkeypatch.setattr(ffmpeg, 'stop_owned_process', lambda selected: closed.append(selected))
    monkeypatch.setattr(ffmpeg, 'time', SimpleNamespace(
        monotonic=lambda: ticks[0], sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + 200 * 86400)))
    assert ffmpeg.run_ffmpeg(PYTHON, []) == ''
    assert ticks[0] > 365 * 86400 and closed == [child]


@pytest.mark.parametrize('interrupt', [KeyboardInterrupt, SystemExit])
def test_first_cleanup_status_interrupt_still_settles_an_event_only_owner(interrupt):
    event = threading.Event()
    reads, attempts = [], []

    class Status:
        def is_set(self):
            reads.append(True)
            if len(reads) == 1:
                raise interrupt('first status read interrupted')
            return event.is_set()
        def wait(self, timeout):
            return event.wait(timeout)

    class Owner:
        # Existing synchronous installer/trust adapters expose this event and
        # close; no extra quiescent property is required by the shared bridge.
        quiescent_event = Status()
        def close(self):
            attempts.append(True)
            event.set()
            return True

    error = AudioCleanupPending('synthetic startup cleanup', cleanup_owner=Owner())
    with pytest.raises(interrupt, match='first status read'):
        process.wait_for_cleanup(error)
    assert event.is_set() and attempts == [True] and len(reads) >= 3


def test_confirmed_family_cleanup_is_idempotent_without_resignalling_pid(monkeypatch):
    child = process.start_owned_process([PYTHON, '-I', '-c', 'pass'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert child.wait(timeout=8) == 0
        assert process.stop_owned_process(child) is True
        assert child._carr_family_quiescent is True
        def forbidden(*args, **kwargs):
            pytest.fail('A confirmed closed family was signalled again by PID.')
        if os.name == 'nt':
            monkeypatch.setattr(child, 'close', forbidden)
        else:
            monkeypatch.setattr(process.os, 'killpg', forbidden)
        assert process.stop_owned_process(child) is True
    finally:
        # The assertion above confirms cleanup before the injected second call.
        if not getattr(child, '_carr_family_quiescent', False):
            process.stop_owned_process(child)
