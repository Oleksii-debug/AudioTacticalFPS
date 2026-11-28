"""Live metadata denial retries with real owned child lifetimes."""
from pathlib import Path
from types import SimpleNamespace
import os
import sys
import time

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioError
from carr_library.audio import process as runner


def _denial(winerror=5):
    error = PermissionError('Synthetic native metadata refusal.')
    if winerror is not None:
        error.winerror = winerror
    return error


@pytest.fixture
def live_children(monkeypatch):
    assert runner._USE_WINDOWS_LIVE_FILE_STATS == (os.name == 'nt')
    monkeypatch.setattr(runner, '_USE_WINDOWS_LIVE_FILE_STATS', True)
    children = []
    spawn = runner.subprocess.Popen

    def own(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runner.subprocess, 'Popen', own)
    return children


def _waiting_code(release):
    return f'''
from pathlib import Path
import time
release = Path({str(release)!r})
deadline = time.monotonic() + 8
while not release.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Parent did not release the real worker.')
    time.sleep(.005)
'''


def _run(code, **kwargs):
    return runner.run_process(
        [str(Path(sys.executable).resolve()), '-I', '-B', '-c', code], **kwargs)


def _reaped(children):
    assert len(children) == 1
    assert children[0].poll() is not None


@pytest.mark.parametrize('outcome', ['recovered', 'disappeared', 'oversized'])
def test_live_refusal_requires_a_new_size_observation(
        tmp_path, monkeypatch, live_children, outcome):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'x' * (8192 if outcome == 'oversized' else 17))
    original = runner._live_file_info
    observations = []

    def observe(path):
        assert path == output
        observations.append(path)
        if len(observations) == 1:
            raise _denial()
        if outcome == 'disappeared':
            output.unlink(missing_ok=True)
        if outcome != 'oversized':
            release.write_bytes(b'Release the real child.')
        return original(path)

    monkeypatch.setattr(runner, '_live_file_info', observe)
    options = dict(watch=(output,), budget=AudioBudget(timeout=5, max_output_bytes=4096))
    if outcome == 'oversized':
        with pytest.raises(AudioError, match='output disk budget'):
            _run(_waiting_code(release), **options)
        assert not release.exists()
    else:
        result = _run(_waiting_code(release), **options)
        assert result.returncode == 0
        assert output.exists() == (outcome == 'recovered')
    assert len(observations) >= 2
    _reaped(live_children)


def test_persistent_live_denial_remains_a_failure_and_reaps_child(
        tmp_path, monkeypatch, live_children):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'unchanged')
    expected, calls = _denial(), []

    def observe(path):
        calls.append(path)
        raise expected

    monkeypatch.setattr(runner, '_live_file_info', observe)
    with pytest.raises(PermissionError) as caught:
        _run(_waiting_code(release), watch=(output,), budget=AudioBudget(timeout=5))
    assert caught.value is expected
    assert len(calls) == 11
    assert output.read_bytes() == b'unchanged' and not release.exists()
    _reaped(live_children)


@pytest.mark.parametrize('winerror', [None, 13, 32])
def test_other_metadata_denials_are_not_retried(
        tmp_path, monkeypatch, live_children, winerror):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'unchanged')
    expected, calls = _denial(winerror), []

    def observe(path):
        calls.append(path)
        raise expected

    monkeypatch.setattr(runner, '_live_file_info', observe)
    with pytest.raises(PermissionError) as caught:
        _run(_waiting_code(release), watch=(output,), budget=AudioBudget(timeout=5))
    assert caught.value is expected and calls == [output]
    assert not release.exists()
    _reaped(live_children)


def test_cancellation_interrupts_metadata_retry(
        tmp_path, monkeypatch, live_children):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'unchanged')
    calls = []

    def observe(path):
        calls.append(path)
        raise _denial()

    monkeypatch.setattr(runner, '_live_file_info', observe)
    with pytest.raises(AudioCancelled, match='cancelled'):
        _run(_waiting_code(release), watch=(output,), cancelled=lambda: bool(calls),
             budget=AudioBudget(timeout=5))
    assert calls == [output] and not release.exists()
    _reaped(live_children)


def test_existing_time_budget_interrupts_metadata_retry(
        tmp_path, monkeypatch, live_children):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'unchanged')
    calls = []
    monotonic = time.monotonic

    def clock():
        return monotonic() + (10 if calls else 0)

    def observe(path):
        calls.append(path)
        raise _denial()

    # Keep the real subprocess module's own clock and cleanup waits intact.
    monkeypatch.setattr(runner, 'time', SimpleNamespace(monotonic=clock, sleep=time.sleep))
    monkeypatch.setattr(runner, '_live_file_info', observe)
    with pytest.raises(AudioError, match='time allowance'):
        _run(_waiting_code(release), watch=(output,), budget=AudioBudget(timeout=5))
    assert calls == [output] and not release.exists()
    _reaped(live_children)


def test_child_exit_during_live_refusal_uses_fresh_final_lstat(
        tmp_path, monkeypatch, live_children):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'original')
    live_calls, final_calls = [], []
    lstat = Path.lstat

    def observe(path):
        live_calls.append(path)
        output.write_bytes(b'fresh final bytes')
        release.write_bytes(b'Exit before the retry.')
        live_children[0].wait(timeout=5)
        raise _denial()

    def final(path, *args, **kwargs):
        if path == output:
            assert live_children[0].poll() is not None
            final_calls.append(path)
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(runner, '_live_file_info', observe)
    monkeypatch.setattr(Path, 'lstat', final)
    result = _run(_waiting_code(release), watch=(output,), budget=AudioBudget(timeout=6))
    assert result.returncode == 0 and live_calls == [output] and final_calls
    assert output.read_bytes() == b'fresh final bytes'
    _reaped(live_children)


def test_final_lstat_denial_is_never_retried(
        tmp_path, monkeypatch, live_children):
    output, release = tmp_path / 'output', tmp_path / 'release'
    output.write_bytes(b'original')
    expected, final_calls = _denial(), []
    original, lstat = runner._live_file_info, Path.lstat

    def observe(path):
        info = original(path)
        release.write_bytes(b'Exit before strict final validation.')
        live_children[0].wait(timeout=5)
        return info

    def final(path, *args, **kwargs):
        if path == output:
            assert live_children[0].poll() is not None
            final_calls.append(path)
            raise expected
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(runner, '_live_file_info', observe)
    monkeypatch.setattr(Path, 'lstat', final)
    with pytest.raises(PermissionError) as caught:
        _run(_waiting_code(release), watch=(output,), budget=AudioBudget(timeout=6))
    assert caught.value is expected and final_calls == [output]
    _reaped(live_children)


def test_real_open_file_deletion_is_observed_without_cached_fallback(
        tmp_path, monkeypatch, live_children):
    output = tmp_path / 'retiring-lock'
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    original = runner._live_file_info
    observations, native_errors, metadata_outcomes, final_observations = [], [], [], []
    original_lstat = Path.lstat
    code = f'''
from pathlib import Path
import ctypes, os, time
output, ready, release = Path({str(output)!r}), Path({str(ready)!r}), Path({str(release)!r})
if os.name == 'nt':
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    create = api.CreateFileW
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    create.restype = ctypes.c_void_p
    delete = api.DeleteFileW
    delete.argtypes, delete.restype = [ctypes.c_wchar_p], ctypes.c_int32
    close = api.CloseHandle
    close.argtypes, close.restype = [ctypes.c_void_p], ctypes.c_int32
    handle = create(str(output), 0x40010000, 7, None, 2, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    def finish():
        if not close(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not delete(str(output)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        finish()
        raise
else:
    handle = open(output, 'wb')
    output.unlink()
    finish = handle.close
try:
    ready.write_bytes(b'Actual open file has been marked for deletion.')
    deadline = time.monotonic() + 8
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Parent did not observe the retiring file.')
        time.sleep(.005)
finally:
    finish()
'''

    def observe(path):
        assert path == output
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise AssertionError('The real child did not stage its open deletion.')
            time.sleep(.005)
        observations.append(path)
        try:
            info = original(path)
            assert info.st_size == 0
            metadata_outcomes.append('present')
            return info
        except FileNotFoundError:
            metadata_outcomes.append('absent')
            raise
        except PermissionError as error:
            metadata_outcomes.append('access-denied')
            native_errors.append(getattr(error, 'winerror', None))
            raise
        finally:
            release.write_bytes(b'Parent has completed its actual metadata query.')

    def final(path, *args, **kwargs):
        if path == output:
            assert live_children[0].poll() is not None
            final_observations.append(path)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(runner, '_live_file_info', observe)
    monkeypatch.setattr(Path, 'lstat', final)
    result = _run(code, watch=(output,), budget=AudioBudget(timeout=8))
    assert result.returncode == 0 and observations and final_observations
    assert not output.exists() and ready.exists() and release.exists()
    assert metadata_outcomes and set(metadata_outcomes) <= {'present', 'absent', 'access-denied'}
    if os.name == 'nt':
        # The real filesystem may still expose fresh attributes or may already
        # report absence. A transient denial is permitted, never required.
        # The other eleven cases deterministically enforce every retry/control
        # boundary; this case proves actual native deletion and final lstat.
        assert set(native_errors) <= {5}
    else:
        assert native_errors == [] and set(metadata_outcomes) == {'absent'}
    _reaped(live_children)
