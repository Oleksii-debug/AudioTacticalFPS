"""Real child protocols and retained cleanup for the existing audio adapters.

Synthetic PCM/protocol data here proves process ownership, not model inference.
The common Windows job and Linux process-group boundaries are exercised without
replacing Popen, its executable, or its output with a success stub.
"""
from contextlib import contextmanager
from pathlib import Path
import ctypes
import os
import select
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import cleanup, model_voices, process, render, voices


def command(code):
    return [sys.executable, '-I', '-B', '-c', code]


def invoke(kind, args, root, **kwargs):
    if kind == 'render':
        return render.run_process(args, timeout_seconds=10, **kwargs)
    if kind == 'voices':
        return voices._run(args, root, timeout_seconds=10, **kwargs)
    return model_voices._run(args, timeout=10, **kwargs)


@pytest.fixture
def manual_cleanup(monkeypatch):
    owners = []
    def retain(attempt, *, message, callbacks=()):
        owner = cleanup.CleanupOwner(attempt)
        for callback in callbacks:
            owner.add_quiescent_callback(callback)
        owners.append(owner)
        return cleanup.AudioCleanupPending(message, cleanup_owner=owner)
    monkeypatch.setattr(cleanup, 'pending_cleanup', retain)
    yield owners
    for owner in owners:
        owner.close()


@pytest.mark.parametrize('kind', ['render', 'voices', 'model'])
def test_real_compatibility_workers_use_the_existing_family_owner(tmp_path, monkeypatch, kind):
    start = process._start_process
    observed = []
    def launch(args, **kwargs):
        observed.append((tuple(args), kwargs['owned_tree']))
        return start(args, **kwargs)
    monkeypatch.setattr(process, '_start_process', launch)
    args = command('import sys;print("literal output");print("bounded diagnostic",file=sys.stderr)')
    result = invoke(kind, args, tmp_path)
    assert observed == [(tuple(args), True)]
    if kind == 'render':
        assert result[0].splitlines() == ['literal output']
        assert result[1].splitlines() == ['bounded diagnostic']
    elif kind == 'voices':
        assert set(result.splitlines()) == {'literal output', 'bounded diagnostic'}
    else:
        assert result.splitlines() == [b'literal output']


def test_model_file_protocol_keeps_literal_stdin_and_selected_diagnostic_stream(tmp_path):
    text = 'Справжні байти; $(never-a-command)\n'.encode('utf-8')
    args = command('import sys;data=sys.stdin.buffer.read();sys.stdout.buffer.write(data);'
                   'sys.stderr.buffer.write(data[::-1])')
    assert model_voices._run(args, data=text, cwd=tmp_path, timeout=10) == text
    assert model_voices._run(args, data=text, return_stderr=True, cwd=tmp_path, timeout=10) == text[::-1]


def test_voice_file_protocol_retains_explicit_nonzero_success_codes(tmp_path):
    args = command('import sys;print("reviewed enumeration");sys.exit(7)')
    assert voices._run(args, tmp_path, timeout_seconds=10, allowed_returncodes=(0, 7)).strip() == 'reviewed enumeration'
    with pytest.raises(RuntimeError, match='exit 7'):
        voices._run(args, tmp_path, timeout_seconds=10)
    with pytest.raises(model_voices.EngineUnavailable, match='exit code 7'):
        model_voices._run(args, timeout=10)


@pytest.mark.parametrize('kind', ['render', 'voices', 'model'])
def test_failed_native_cleanup_retains_real_log_handles_until_confirmed(tmp_path, monkeypatch, manual_cleanup, kind):
    start, stop = process._start_process, process._stop_owned
    selected = []
    admitted = threading.Event()
    def launch(args, **kwargs):
        child = start(args, **kwargs)
        selected.append((child, tuple(kwargs[key] for key in ('stdout', 'stderr'))))
        return child
    def uncertain(child):
        assert stop(child) is True
        if not admitted.is_set():
            raise RuntimeError('Synthetic uncertainty after actual child exit')
        return True
    monkeypatch.setattr(process, '_start_process', launch)
    monkeypatch.setattr(process, '_stop_owned', uncertain)
    with pytest.raises(cleanup.AudioCleanupPending) as caught:
        invoke(kind, command('print("completed native output")'), tmp_path)
    child, streams = selected[0]
    assert child.poll() == 0
    assert all(not stream.closed for stream in streams)
    assert not caught.value.cleanup_owner.quiescent
    admitted.set()
    assert caught.value.cleanup_owner.close()
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('failure', ['raises', 'not_ready'])
def test_required_cleanup_callbacks_are_retried_before_retained_stage_release(tmp_path, manual_cleanup, failure):
    completed, allowed = [], threading.Event()
    def first():
        completed.append('first')
        if not allowed.is_set():
            if failure == 'raises':
                raise BrokenPipeError('Synthetic callback failure after real process exit')
            return False
        return True
    def second():
        completed.append('second')
    with pytest.raises(cleanup.AudioCleanupPending) as caught:
        with cleanup.retained_temporary_directory(prefix='owned-', dir=tmp_path) as stage:
            marker = Path(stage) / 'retained.txt'
            marker.write_text('belongs to the still-pending callback')
            with process.owned_process(command('pass'), stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, quiescent_callbacks=(first, second)) as child:
                assert child.wait(timeout=10) == 0
    assert completed == ['first'] and marker.is_file()
    assert not caught.value.cleanup_owner.close()
    assert marker.is_file() and 'second' not in completed
    allowed.set()
    assert caught.value.cleanup_owner.close()
    assert completed.count('second') == 1 and not marker.exists()
    assert caught.value.cleanup_owner.close()
    assert completed.count('second') == 1


def protocol_engine(root):
    engine = object.__new__(model_voices.DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=root)
    engine.timeout = 10
    return engine


_PCM_REPLY = ('import sys,json,base64;json.loads(sys.stdin.readline());'
              'print(json.dumps({"pcm":base64.b64encode(b"\\x01\\x00"*80).decode()}),flush=True)')


def test_reply_reader_must_exit_before_error_file_and_stage_are_released(tmp_path, monkeypatch, manual_cleanup):
    held, release = threading.Event(), threading.Event()
    real_thread = threading.Thread
    readers = []
    def thread(*, target, **kwargs):
        def run():
            target()
            held.set()
            assert release.wait(10), 'test must release its owned reply reader'
        value = real_thread(target=run, **kwargs)
        readers.append(value)
        return value
    monkeypatch.setattr(model_voices, 'threading',
                        SimpleNamespace(Thread=thread, Event=threading.Event))
    errors = []
    actual_temporary_file = model_voices.tempfile.TemporaryFile
    def temporary(*args, **kwargs):
        value = actual_temporary_file(*args, **kwargs)
        errors.append(value)
        return value
    monkeypatch.setattr(model_voices, 'tempfile', SimpleNamespace(TemporaryFile=temporary))
    owner = None
    try:
        with pytest.raises(cleanup.AudioCleanupPending) as caught:
            with cleanup.retained_temporary_directory(prefix='reply-', dir=tmp_path) as stage:
                output = Path(stage) / 'result.wav'
                protocol_engine(tmp_path)._stream(command(_PCM_REPLY), {'text': 'fixture'}, output, lambda: False)
        owner = caught.value.cleanup_owner
        assert held.is_set() and readers[0].is_alive()
        assert output.is_file() and errors and not errors[0].closed
        assert not owner.quiescent
        release.set()
        readers[0].join(timeout=2)
        assert not readers[0].is_alive() and owner.close()
        assert errors[0].closed and not output.exists()
    finally:
        release.set()
        for reader in readers:
            reader.join(timeout=2)
        if owner is not None:
            owner.close()


def test_failed_stdin_close_still_joins_reply_reader_and_remains_retryable(tmp_path, monkeypatch, manual_cleanup):
    original = model_voices.owned_process
    selected, failures = [], []
    class ClosingInput:
        def __init__(self, stream):
            self.stream, self.failed = stream, False
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def close(self):
            self.stream.close()
            if not self.failed:
                self.failed = True
                failures.append('flush')
                raise BrokenPipeError('Synthetic final stdin flush failure')
    @contextmanager
    def wrapped(*args, **kwargs):
        with original(*args, **kwargs) as child:
            child.stdin = ClosingInput(child.stdin)
            selected.append(child)
            yield child
    monkeypatch.setattr(model_voices, 'owned_process', wrapped)
    with pytest.raises(cleanup.AudioCleanupPending) as caught:
        with cleanup.retained_temporary_directory(prefix='flush-', dir=tmp_path) as stage:
            output = Path(stage) / 'result.wav'
            protocol_engine(tmp_path)._stream(command(_PCM_REPLY), {'text': 'fixture'}, output, lambda: False)
    assert failures == ['flush'] and output.is_file()
    assert selected[0].stdin.closed and selected[0].stdout.closed
    assert caught.value.cleanup_owner.close()
    assert not output.exists()


def test_optional_voice_discovery_cannot_hide_a_pending_process_owner(tmp_path, monkeypatch, manual_cleanup):
    from carr_library.audio.cleanup import retained_temporary_directory
    allowed = threading.Event()
    owner = cleanup.CleanupOwner(lambda: allowed.is_set())
    stages = []
    @contextmanager
    def staged(*, prefix, dir):
        with retained_temporary_directory(prefix=prefix, dir=tmp_path) as path:
            stages.append(Path(path))
            yield path
    def unavailable(*args, **kwargs):
        raise cleanup.AudioCleanupPending('Synthetic discovery cleanup', cleanup_owner=owner)
    monkeypatch.setattr(voices, 'retained_temporary_directory', staged)
    monkeypatch.setattr(voices, '_executable', lambda value, fallback: Path(sys.executable) if fallback == 'espeak-ng' else None)
    monkeypatch.setattr(voices, '_run', unavailable)
    with pytest.raises(cleanup.AudioCleanupPending):
        voices.VoiceRegistry({'include_sapi': False, 'ffmpeg': False}).list_voices()
    assert stages and all(path.exists() for path in stages)
    allowed.set()
    assert owner.close()
    assert all(not path.exists() for path in stages)


def _exit_handle(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        def ready():
            result = kernel.WaitForSingleObject(handle, 0)
            assert result in (0, 258), 'The exact retained process handle must remain valid.'
            return result == 0
        def close():
            assert kernel.CloseHandle(handle)
        return ready, close
    descriptor = os.pidfd_open(pid, 0)
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    def ready():
        rows = poller.poll(0)
        return bool(rows and rows[0][1] & (select.POLLIN | select.POLLHUP))
    return ready, lambda: os.close(descriptor)


def test_exited_leader_does_not_release_a_live_owned_descendant(tmp_path):
    marker = tmp_path / 'descendant.txt'
    child = ('import os,pathlib,time;'
             'p=pathlib.Path(' + repr(str(marker)) + ');'
             'q=p.with_suffix(".tmp");q.write_text(str(os.getpid()));os.replace(q,p);'
             'time.sleep(30)')
    leader = ('import pathlib,subprocess,sys,time;'
              'p=pathlib.Path(' + repr(str(marker)) + ');'
              'subprocess.Popen([sys.executable,"-I","-B","-c",' + repr(child) + ']);'
              '\nwhile not p.exists(): time.sleep(.01)\n')
    ready = close = None
    try:
        with process.owned_process(command(leader), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as owned:
            assert owned.wait(timeout=10) == 0
            ready, close = _exit_handle(int(marker.read_text()))
            assert not ready(), 'The descendant must still be alive after the original leader exits.'
        assert ready(), 'Cleanup must prove the exact descendant exited before returning.'
        assert owned.poll() == 0
    finally:
        if close is not None:
            close()


def test_owned_group_cleanup_preserves_an_unrelated_real_process(tmp_path):
    unrelated = subprocess.Popen(command('import time;time.sleep(30)'),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with process.owned_process(command('pass'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as owned:
            assert owned.wait(timeout=10) == 0
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


def _modeled_group(monkeypatch):
    """Model only native error returns; actual group exit is tested above."""
    from unittest.mock import Mock
    value = object.__new__(process._PosixGroupProcess)
    value._process = SimpleNamespace(pid=4242, wait=Mock(return_value=0))
    value._members = {}
    value._lock = threading.RLock()
    value._observed = 0
    value._disposal_error = None
    value._group_quiet = value._reaped = value._closed = False
    value.poll = Mock(return_value=0)
    value._stat = Mock(return_value=(4242, 4242, 73))
    native = SimpleNamespace(killpg=Mock(), close=Mock())
    monkeypatch.setattr(process, 'os', native)
    monkeypatch.setattr(process, 'signal', SimpleNamespace(SIGKILL=9, pidfd_send_signal=Mock()))
    return value, native


def test_failed_group_inventory_stops_only_pinned_group_but_never_acknowledges_quiescence(monkeypatch):
    value, native = _modeled_group(monkeypatch)
    def inaccessible(deadline):
        raise PermissionError('Synthetic native group inventory denial')
    value._inventory = inaccessible
    with pytest.raises(PermissionError, match='inventory denial'):
        value.close()
    native.killpg.assert_called_once_with(4242, 9)
    value._process.wait.assert_not_called()
    assert not value._closed and not value._group_quiet


def test_changed_group_leader_identity_never_signals_a_recycled_group(monkeypatch):
    value, native = _modeled_group(monkeypatch)
    value._stat.return_value = (8181, 8181, 100)
    def changed(deadline):
        raise RuntimeError('Synthetic lost original leader identity')
    value._inventory = changed
    with pytest.raises(RuntimeError, match='original leader'):
        value.close()
    native.killpg.assert_not_called()
    value._process.wait.assert_not_called()
    assert not value._closed


def test_uncertain_group_handle_disposal_never_closes_a_reused_number_or_signals_reaped_group(monkeypatch):
    value, native = _modeled_group(monkeypatch)
    value._group_quiet = value._reaped = True
    value._members[(4243, 74)] = 91
    native.close.side_effect = OSError('Synthetic retained pidfd close failure')
    with pytest.raises(RuntimeError, match='disposed with certainty'):
        value.close()
    assert value._members == {} and not value._closed
    native.close.assert_called_once_with(91)
    native.killpg.assert_not_called()
    value._process.wait.assert_not_called()
    # The same integer may now identify an unrelated open file. A retry must
    # retain the explicit uncertainty without closing or signalling it.
    native.close.side_effect = None
    with pytest.raises(RuntimeError, match='Close the application'):
        value.close()
    native.close.assert_called_once_with(91)
    native.killpg.assert_not_called()
    value._process.wait.assert_not_called()
