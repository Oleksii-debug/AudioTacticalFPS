"""Durable command updates survive a short Windows read/replace collision."""
import ctypes
import os
from pathlib import Path
import threading

import pytest

from carr_library import durable_state as state
from carr_library.audio import jobs


def _sharing_error():
    error = PermissionError('Synthetic Windows sharing violation')
    error.winerror = 32
    return error


def test_atomic_state_update_survives_reader_without_exposing_partial_bytes(tmp_path, monkeypatch):
    destination = tmp_path / 'control.json'
    state.atomic_json(destination, {'command': 'run'})
    original = destination.read_bytes()
    replace = jobs._replace_state_file
    attempts, timers = [], []
    released = threading.Event()
    closed = threading.Event()
    close_lock = threading.Lock()

    if os.name == 'nt':
        # A real Windows handle omitting FILE_SHARE_DELETE reproduces the
        # control.json collision observed in the native ASR CI run.
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(destination), 0x80000000, 3, None, 3, 128, None)
        assert handle != ctypes.c_void_p(-1).value

        def release():
            with close_lock:
                if not closed.is_set():
                    assert kernel.CloseHandle(handle)
                    closed.set()
            released.set()
    else:
        def release():
            released.set()

    def collide(source, target):
        try:
            if os.name != 'nt' and not released.is_set():
                raise _sharing_error()
            return replace(source, target)
        except PermissionError as exc:
            attempts.append(getattr(exc, 'winerror', None))
            assert Path(target).read_bytes() == original
            assert state.read_json(source) == {'command': 'pause', 'label': 'Пауза'}
            if not timers:
                timer = threading.Timer(0.10, release)
                timers.append(timer)
                timer.start()
            raise

    monkeypatch.setattr(jobs, '_replace_state_file', collide)
    try:
        state.atomic_json(destination, {'command': 'pause', 'label': 'Пауза'}, replace=True)
    finally:
        for timer in timers:
            timer.join(2)
        release()
    assert attempts and set(attempts) <= {5, 32, 33}
    assert released.is_set()
    assert state.read_json(destination) == {'command': 'pause', 'label': 'Пауза'}
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('windows_sharing', [False, True])
def test_permanent_replace_denial_keeps_old_state_and_cleans_own_stage(tmp_path, monkeypatch,
                                                                    windows_sharing):
    destination = tmp_path / 'control.json'
    state.atomic_json(destination, {'command': 'run'})
    original = destination.read_bytes()
    attempts = []

    def denied(source, target):
        attempts.append((source, target))
        raise _sharing_error() if windows_sharing else PermissionError('No write access')

    monkeypatch.setattr(jobs, '_replace_state_file', denied)
    with pytest.raises(PermissionError):
        state.atomic_json(destination, {'command': 'pause'}, replace=True)
    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.pending-*'))
    if not windows_sharing:
        assert len(attempts) == 1


def test_unrelated_publication_error_is_not_retried(tmp_path, monkeypatch):
    destination = tmp_path / 'control.json'
    state.atomic_json(destination, {'command': 'run'})
    original, attempts = destination.read_bytes(), []

    def failed(source, target):
        attempts.append((source, target))
        raise OSError('Storage device failure')

    monkeypatch.setattr(jobs, '_replace_state_file', failed)
    with pytest.raises(OSError, match='Storage device failure'):
        state.atomic_json(destination, {'command': 'pause'}, replace=True)
    assert len(attempts) == 1 and destination.read_bytes() == original
    assert not list(tmp_path.glob('.pending-*'))


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_windows_retry_budget_reuses_one_complete_stage(tmp_path, monkeypatch, winerror):
    destination = tmp_path / 'control.json'
    state.atomic_json(destination, {'command': 'run'})
    original, attempts, delays = destination.read_bytes(), [], []

    def denied(source, target):
        attempts.append((source, target))
        assert Path(target).read_bytes() == original
        assert state.read_json(source) == {'command': 'pause'}
        error = PermissionError('Synthetic persistent Windows access error')
        error.winerror = winerror
        raise error

    monkeypatch.setattr(jobs, '_replace_state_file', denied)
    monkeypatch.setattr(state.time, 'sleep', delays.append)
    with pytest.raises(PermissionError):
        state.atomic_json(destination, {'command': 'pause'}, replace=True)
    assert len(attempts) == 7 and len(set(attempts)) == 1
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16, 0.32]
    assert sum(delays) == pytest.approx(0.63)
    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.pending-*'))
