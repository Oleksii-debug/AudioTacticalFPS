"""Owned-stop and uncertain-cleanup regressions; native faults are injected.

The real child and pipe tests run on each operating system. The fake kernel
tests exercise retained handle decisions and do not claim native Windows proof.
"""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from carr_library.audio.cleanup import CleanupOwner, retained_state_lock
from carr_library.audio.capture_types import AudioBudget, AudioCleanupPending, AudioError
from carr_library.audio import capture_process as process
from carr_library.audio.windows_job import JobProcess, _Accounting, _ProcessInfo, DWORD


def execute(code, **options):
    return process.run_process([str(Path(sys.executable).resolve()), '-I', '-c', code],
                               budget=AudioBudget(timeout=15, reserve_disk_bytes=0), **options)


def test_real_owned_stop_delivers_only_fixed_quit_command(tmp_path):
    ready = tmp_path / 'ready'
    result = execute(
        f"import pathlib,sys;pathlib.Path({str(ready)!r}).write_text('ready');"
        "data=sys.stdin.buffer.read();sys.stdout.buffer.write(data)",
        owned_tree=True, stop_requested=ready.exists)
    assert result.returncode == 0 and result.stopped is True
    assert result.stdout == b'q\n'


def test_explicit_diagnostic_exit_preserves_exit_code_and_does_not_bypass_budget():
    result = execute("import sys;sys.stderr.buffer.write(b'no capture devices');raise SystemExit(1)",
                     owned_tree=True, allow_failure=True)
    assert result.returncode == 1 and result.stopped is False
    assert result.stderr == b'no capture devices'
    with pytest.raises(AudioError, match='diagnostic'):
        process.run_process([str(Path(sys.executable).resolve()), '-I', '-c',
            "import os;os.write(2,b'x'*20000);raise SystemExit(1)"],
            owned_tree=True, allow_failure=True,
            budget=AudioBudget(max_log_bytes=1024, reserve_disk_bytes=0))


def test_ambient_ffreport_is_removed_without_mutating_selected_environment():
    selected = {**os.environ, 'FFREPORT': 'file=should-never-be-created'}
    original = dict(selected)
    result = execute("import os;print(os.environ.get('FFREPORT','absent'))", env=selected)
    assert result.stdout.strip() == b'absent' and selected == original


def test_runner_retains_failed_cleanup_and_delivers_quiescence_callback(monkeypatch):
    permitted = threading.Event()
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_terminate')
    actual = getattr(target, name)
    attempts = []

    def fail_until_released(child):
        attempts.append(child)
        if not permitted.is_set():
            raise OSError('injected native cleanup failure')
        actual(child)

    monkeypatch.setattr(target, name, fail_until_released)
    notified = threading.Event()
    owner = None
    try:
        with pytest.raises(AudioCleanupPending) as error:
            execute("print('completed child')", owned_tree=True)
        owner = error.value.cleanup_owner
        owner.add_quiescent_callback(notified.set)
        assert attempts and not owner.quiescent and not notified.is_set()
        assert owner.close() is False
        permitted.set()
        assert owner.quiescent_event.wait(3)
        assert notified.wait(3)
        assert owner.close() is True
        late = []
        owner.add_quiescent_callback(lambda: late.append('quiet'))
        assert late == ['quiet']
    finally:
        permitted.set()
        if owner is not None:
            owner.close()


def test_retained_state_lock_releases_only_after_cross_thread_quiescence(tmp_path):
    from filelock import FileLock, Timeout
    ready = threading.Event()
    owner = CleanupOwner(lambda: ready.is_set())
    error = AudioCleanupPending('fixture cleanup pending', cleanup_owner=owner)
    with pytest.raises(AudioCleanupPending):
        with retained_state_lock(tmp_path, 'runtime'):
            raise error
    competitor = FileLock(tmp_path / '.runtime.lock', timeout=0)
    with pytest.raises(Timeout):
        competitor.acquire()
    assert owner.close() is False
    ready.set()
    thread = threading.Thread(target=owner.close)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and owner.quiescent
    with competitor:
        pass


class Kernel:
    def __init__(self, failure):
        self.failure, self.active, self.closed = failure, 1, []

    def QueryInformationJobObject(self, job, kind, pointer, size, returned):
        if self.failure == 'query':
            return 0
        ctypes.cast(pointer, ctypes.POINTER(_Accounting)).contents.ActiveProcesses = self.active
        return 1

    def TerminateJobObject(self, job, code):
        if self.failure == 'terminate':
            return 0
        self.active = 0
        return 1

    def WaitForSingleObject(self, handle, timeout):
        return 0x102 if self.failure == 'wait' else 0

    def GetExitCodeProcess(self, handle, pointer):
        if self.failure == 'exit-code':
            return 0
        ctypes.cast(pointer, ctypes.POINTER(DWORD)).contents.value = 0
        return 1

    def CloseHandle(self, handle):
        if self.failure == ('close-root' if handle == 101 else 'close-job'):
            return 0
        self.closed.append(handle)
        return 1


@pytest.mark.parametrize('failure', ['query', 'terminate', 'wait', 'exit-code', 'close-root', 'close-job'])
def test_failed_native_cleanup_keeps_unconfirmed_job_handle(monkeypatch, failure):
    monkeypatch.setattr(ctypes, 'WinError', lambda code: OSError('injected Win32 failure'), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    child = JobProcess.__new__(JobProcess)
    child.args, child.returncode = ['fixture-not-executed'], None
    child._api, child._process, child._job = Kernel(failure), _ProcessInfo(), 202
    child._process.hProcess = 101
    child._cleanup_lock = threading.Lock()
    with pytest.raises((OSError, subprocess.TimeoutExpired)):
        child.close()
    assert child._job == 202 and 202 not in child._api.closed
    if failure != 'close-job':
        assert child._process.hProcess == 101
    child._api.failure = None
    assert child.close() is True
    assert child._job is None and child._process.hProcess is None
    assert child._api.closed.count(101) == 1 and child._api.closed.count(202) == 1
    assert child.close() is True


def test_cleanup_callbacks_do_not_erase_quiescence_or_skip_other_callbacks():
    owner = CleanupOwner(lambda: True)
    called = []
    owner.add_quiescent_callback(lambda: (_ for _ in ()).throw(OSError('notification failed')))
    owner.add_quiescent_callback(lambda: called.append('released'))
    assert owner.close() is True and owner.quiescent
    assert called == ['released']


@pytest.mark.parametrize('boundary', ['construct', 'start'])
def test_reader_thread_admission_failure_still_reaps_actual_child(monkeypatch, boundary):
    children = []
    popen = process.subprocess.Popen
    thread = process.threading.Thread

    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def fail(*args, **kwargs):
        raise RuntimeError('injected reader thread admission failure')

    monkeypatch.setattr(process.subprocess, 'Popen', launch)
    if boundary == 'construct':
        monkeypatch.setattr(process.threading, 'Thread', fail)
    else:
        monkeypatch.setattr(thread, 'start', fail)
    with pytest.raises(RuntimeError, match='reader thread admission'):
        execute("import time;time.sleep(30)")
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed



def test_both_ambient_diagnostic_writers_are_removed_without_mutating_environment():
    selected = {**os.environ, 'FFREPORT': 'file=must-not-exist', 'SSLKEYLOGFILE': 'must-not-exist'}
    original = dict(selected)
    result = execute("import os; print(os.environ.get('FFREPORT')); print(os.environ.get('SSLKEYLOGFILE'))",
                     env=selected)
    assert result.stdout.splitlines() == [b'None', b'None'] and selected == original


def test_shared_owner_discard_mode_requires_no_drain_thread_on_windows(tmp_path):
    # This portable case has a separate real native branch, never a skipped
    # Windows assertion presented as a portable native pass.
    marker = tmp_path / 'completed'
    args = [str(Path(sys.executable).resolve()), '-I', '-B', '-c',
            "import os,pathlib; os.write(1,b'x'*4000000); os.write(2,b'y'*4000000); "
            f"pathlib.Path({str(marker)!r}).write_text('complete')"]
    if os.name == 'nt':
        child = JobProcess(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert child.stdin is child.stdout is child.stderr is None
            assert child.wait(timeout=15) == 0
        finally:
            assert child.close() is True
        assert marker.read_text() == 'complete'
    else:
        # The backend is unavailable explicitly; it must not silently launch a
        # less protected substitute on another platform.
        with pytest.raises(RuntimeError, match='native Windows'):
            JobProcess(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert not marker.exists()


@pytest.mark.parametrize('options', [
    {'stdin_pipe': 1}, {'stdout': None}, {'stderr': None},
    {'stdout': subprocess.STDOUT}, {'stderr': 3},
])
def test_shared_owner_rejects_implicit_or_unowned_standard_handles(options):
    with pytest.raises(ValueError, match='explicit'):
        JobProcess([str(Path(sys.executable).resolve()), '-I', '-c', 'pass'], **options)



def test_explicit_streams_keep_caller_ownership_and_require_readonly_input(tmp_path):
    from carr_library.audio.windows_job import _stream
    path = tmp_path / 'input.bin'
    path.write_bytes(b'owned selected input')
    with path.open('rb') as incoming:
        assert _stream(incoming, input_stream=True) == incoming.fileno()
        with pytest.raises(ValueError, match='writable'):
            _stream(incoming)
        assert incoming.read() == b'owned selected input'
    with path.open('r+b') as mutable:
        with pytest.raises(ValueError, match='read-only'):
            _stream(mutable, input_stream=True)
        assert _stream(mutable) == mutable.fileno()
    with pytest.raises(ValueError, match='open regular binary'):
        _stream(incoming)
    with path.open('w', encoding='utf-8') as text:
        with pytest.raises(ValueError, match='binary'):
            _stream(text)


def test_native_shared_owner_uses_only_explicit_caller_streams(tmp_path):
    incoming = tmp_path / 'input.bin'
    outgoing = tmp_path / 'stdout.bin'
    errors = tmp_path / 'stderr.bin'
    incoming.write_bytes(b'captured input')
    args = [str(Path(sys.executable).resolve()), '-I', '-B', '-c',
            "import sys; value=sys.stdin.buffer.read(); sys.stdout.buffer.write(value); "
            "sys.stderr.buffer.write(b'explicit diagnostics')"]
    with incoming.open('rb') as selected, outgoing.open('w+b') as result, errors.open('w+b') as diagnostic:
        if os.name == 'nt':
            child = JobProcess(args, stdin_file=selected, stdout=result, stderr=diagnostic)
            try:
                assert child.stdin is child.stdout is child.stderr is None
                assert child.wait(timeout=15) == 0
            finally:
                assert child.close()
            result.seek(0)
            diagnostic.seek(0)
            assert result.read() == b'captured input'
            assert diagnostic.read() == b'explicit diagnostics'
        else:
            with pytest.raises(RuntimeError, match='native Windows'):
                JobProcess(args, stdin_file=selected, stdout=result, stderr=diagnostic)
        assert not selected.closed and not result.closed and not diagnostic.closed
        selected.seek(0)
        assert selected.read() == b'captured input'
        result.write(b'caller still owns its stream')
    assert incoming.read_bytes() == b'captured input'


def test_shared_owner_never_combines_interactive_and_file_input(tmp_path):
    path = tmp_path / 'input.bin'
    path.write_bytes(b'explicit')
    with path.open('rb') as stream:
        with pytest.raises(ValueError, match='one explicit stdin'):
            JobProcess([str(Path(sys.executable).resolve())], stdin_pipe=True, stdin_file=stream)
