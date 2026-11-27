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
from carr_library.audio.models import AudioBudget, AudioCleanupPending, AudioError
from carr_library.audio import process
from carr_library.audio.windows_job import JobProcess, _ProcessIds, _ProcessInfo, DWORD


def execute(code, **options):
    return process.run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', code],
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
        process.run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c',
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
    target, name = (JobProcess, 'close') if os.name == 'nt' else (process, '_wait_posix_group')
    actual = getattr(target, name)
    attempts = []

    def fail_until_released(*args, **kwargs):
        attempts.append(args[0])
        if not permitted.is_set():
            raise OSError('injected native cleanup failure')
        return actual(*args, **kwargs)

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


def test_renderer_stage_survives_uncertain_native_cleanup_until_owned_exit(tmp_path, monkeypatch):
    from carr_library.audio import dsp
    permitted = threading.Event()
    owner = CleanupOwner(lambda: permitted.is_set())
    staged = []
    def uncertain(args, **options):
        path = Path(args[-1])
        path.write_bytes(b'Native worker still owns this exact staging file')
        staged.append(path)
        raise AudioCleanupPending('Injected native stop failure', cleanup_owner=owner)
    runtime = dsp.AudioRuntime(tmp_path, ffmpeg=sys.executable, ffprobe=sys.executable,
                               budget=AudioBudget(reserve_disk_bytes=0))
    # This test injects the native render boundary only, not discovery or real
    # encoding. A separate actual-FFmpeg suite qualifies those operations.
    runtime._capabilities = {'filters': [], 'encoders': ['pcm_s16le'], 'muxers': ['wav']}
    monkeypatch.setattr(dsp, 'run_process', uncertain)
    with pytest.raises(AudioCleanupPending):
        runtime.silence(tmp_path / 'result.wav', duration=.1)
    assert staged[0].read_bytes().startswith(b'Native worker still owns')
    assert not (tmp_path / 'result.wav').exists()
    assert owner.close() is False and staged[0].exists()
    permitted.set()
    thread = threading.Thread(target=owner.close)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and owner.quiescent
    assert not staged[0].parent.exists()


class Kernel:
    """Finite exact-root fixture for the current retained member-handle owner."""
    def __init__(self, failure):
        self.failure, self.active, self.closed = failure, 1, []

    def GetProcessId(self, handle):
        assert handle == 101
        return 701

    def QueryInformationJobObject(self, job, kind, pointer, size, returned):
        assert job == 202 and kind == 3
        if self.failure == 'query':
            return 0
        value = ctypes.cast(pointer, ctypes.POINTER(_ProcessIds)).contents
        value.assigned = value.count = self.active
        if self.active:
            value.identities[0] = 701
        return 1

    def TerminateProcess(self, handle, code):
        assert handle == 101 and code == 1
        if self.failure == 'terminate':
            return 0
        self.active = 0
        return 1

    def TerminateJobObject(self, *args):
        pytest.fail('A bulk stop cannot erase membership before exact-handle capture.')

    def WaitForSingleObject(self, handle, timeout):
        assert handle == 101
        return 0x102 if self.failure == 'wait' or self.active else 0

    def GetExitCodeProcess(self, handle, pointer):
        assert handle == 101
        if self.failure == 'exit-code':
            return 0
        ctypes.cast(pointer, ctypes.POINTER(DWORD)).contents.value = 0
        return 1

    def CloseHandle(self, handle):
        assert handle in (101, 202)
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
    child._member_handles = {}
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


def test_completion_waits_for_real_retained_leases_and_callbacks_registered_during_drain(tmp_path):
    from filelock import FileLock, Timeout
    from carr_library.audio import cleanup
    from carr_library.audio.cleanup import pending_cleanup, retained_temporary_directory

    native_ready = threading.Event()
    callback_entered = threading.Event()
    release_callback = threading.Event()
    confirmed = []

    def attempt():
        if not native_ready.is_set():
            return False
        confirmed.append('native completed')
        return True

    error = pending_cleanup(attempt, message='Authored pending native owner.')
    owner = error.cleanup_owner

    def held_callback():
        callback_entered.set()
        if not release_callback.wait(5):
            raise TimeoutError('The authored callback was not released.')

    owner.add_quiescent_callback(held_callback)
    competitor = FileLock(tmp_path / '.drain.lock', timeout=0)
    late = []
    try:
        with pytest.raises(AudioCleanupPending):
            with retained_state_lock(tmp_path, 'drain'):
                with retained_temporary_directory(prefix='retained-', dir=tmp_path) as temporary:
                    staged = Path(temporary)
                    (staged / 'owned.bin').write_bytes(b'owned until the callback completes')
                    raise error
        native_ready.set()
        assert callback_entered.wait(3)
        assert not owner.quiescent_event.is_set()
        assert owner.close() is False
        assert staged.is_dir()
        with cleanup._RETAINED_LOCK:
            assert owner in cleanup._RETAINED
        with pytest.raises(Timeout):
            competitor.acquire()

        def registered_during_drain():
            with competitor:
                late.append((staged.exists(), owner.quiescent))

        owner.add_quiescent_callback(registered_during_drain)
        release_callback.set()
        assert owner.quiescent_event.wait(3)
        assert owner.close() is True
        assert confirmed == ['native completed']
        assert late == [(False, False)]
        assert not staged.exists()
        with competitor:
            pass
        with cleanup._RETAINED_LOCK:
            assert owner not in cleanup._RETAINED
    finally:
        native_ready.set()
        release_callback.set()
        if not owner.quiescent_event.wait(5):
            owner.close()
        assert owner.quiescent


@pytest.mark.parametrize('interruption', [KeyboardInterrupt, SystemExit])
def test_interrupted_callback_drain_retries_without_repeating_native_cleanup(tmp_path, interruption):
    from filelock import FileLock, Timeout

    native, notifications, interrupted = [], [], []
    owner = CleanupOwner(lambda: native.append('confirmed'))
    owner.add_quiescent_callback(lambda: notifications.append('before'))

    def interrupt_once():
        interrupted.append('called')
        if len(interrupted) == 1:
            raise interruption('Authored retained-callback interruption.')
        notifications.append('retried')

    owner.add_quiescent_callback(interrupt_once)
    owner.add_quiescent_callback(lambda: notifications.append('after'))
    competitor = FileLock(tmp_path / '.interrupted-drain.lock', timeout=0)
    error = AudioCleanupPending('Authored pending native owner.', cleanup_owner=owner)
    try:
        with pytest.raises(AudioCleanupPending):
            with retained_state_lock(tmp_path, 'interrupted-drain'):
                raise error
        with pytest.raises(interruption):
            owner.close()
        assert not owner.quiescent and not owner.quiescent_event.is_set()
        assert native == ['confirmed']
        assert notifications == ['before']
        with pytest.raises(Timeout):
            competitor.acquire()
        owner.add_quiescent_callback(lambda: notifications.append('new'))
        assert owner.close() is True
        assert native == ['confirmed']
        assert interrupted == ['called', 'called']
        assert notifications == ['before', 'retried', 'after', 'new']
        with competitor:
            pass
        assert owner.close() is True and native == ['confirmed']
    finally:
        owner.close()
