"""Atomic status publication by the isolated synthetic ASR protocol fixture."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker():
    path = Path(__file__).with_name('fixtures') / 'protocol_worker.py'
    spec = importlib.util.spec_from_file_location('publication_protocol_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status(worker, tmp_path):
    path = tmp_path / 'job_status.json'
    worker.write(path, {'state': 'обробка', 'done': 0, 'failed': 0, 'total': 1})
    completed = {'state': 'завершено', 'done': 1, 'failed': 0, 'total': 1}
    return path, path.read_bytes(), completed


def _denied(winerror):
    error = PermissionError('Synthetic status publication denial')
    if winerror is not None:
        error.winerror = winerror
    return error


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_transient_windows_status_collision_retries_one_complete_stage(worker, tmp_path,
                                                                      monkeypatch, winerror):
    path, original, completed = _status(worker, tmp_path)
    attempts, delays = [], []
    replace = os.replace

    def collide(source, destination):
        attempts.append((source, destination))
        assert path.read_bytes() == original
        assert Path(source).read_bytes() == worker.raw(completed)
        if len(attempts) <= 2:
            raise _denied(winerror)
        return replace(source, destination)

    # Rebind fixture namespaces; never mutate process-global os/time functions.
    monkeypatch.setattr(worker, 'os', SimpleNamespace(replace=collide))
    monkeypatch.setattr(worker, 'time', SimpleNamespace(sleep=delays.append))
    worker.write(path, completed)
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert delays == [0.01, 0.02]
    assert json.loads(path.read_text(encoding='utf-8')) == completed
    assert not path.with_suffix('.json.tmp').exists()


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_permanent_windows_status_denial_is_bounded_and_keeps_old_state(worker, tmp_path,
                                                                      monkeypatch, winerror):
    path, original, completed = _status(worker, tmp_path)
    attempts, delays = [], []

    def denied(source, destination):
        attempts.append((source, destination))
        assert path.read_bytes() == original
        assert Path(source).read_bytes() == worker.raw(completed)
        raise _denied(winerror)

    monkeypatch.setattr(worker, 'os', SimpleNamespace(replace=denied))
    monkeypatch.setattr(worker, 'time', SimpleNamespace(sleep=delays.append))
    with pytest.raises(PermissionError):
        worker.write(path, completed)
    assert len(attempts) == 7 and len(set(attempts)) == 1
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16, 0.32]
    assert sum(delays) == pytest.approx(0.63)
    assert path.read_bytes() == original


@pytest.mark.parametrize('error', [_denied(None), _denied(87), OSError('Storage failure')],
                         ids=['ordinary-permission', 'unrelated-windows', 'storage'])
def test_unrelated_status_publication_errors_are_not_retried(worker, tmp_path, monkeypatch, error):
    path, original, completed = _status(worker, tmp_path)
    attempts, delays = [], []

    def failed(source, destination):
        attempts.append((source, destination))
        raise error

    monkeypatch.setattr(worker, 'os', SimpleNamespace(replace=failed))
    monkeypatch.setattr(worker, 'time', SimpleNamespace(sleep=delays.append))
    with pytest.raises(type(error)) as raised:
        worker.write(path, completed)
    assert raised.value is error
    assert len(attempts) == 1 and not delays
    assert path.read_bytes() == original


def _windows_reader(path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ | FILE_SHARE_WRITE deliberately excludes SHARE_DELETE.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 128, None)
    assert handle != ctypes.c_void_p(-1).value
    closed = False

    def close():
        nonlocal closed
        if not closed:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            closed = True
    return close


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows non-delete-sharing read handle.')
@pytest.mark.parametrize('release_reader', [True, False], ids=['transient-reader', 'permanent-reader'])
def test_native_windows_reader_preserves_atomic_status(worker, tmp_path, monkeypatch, release_reader):
    path, original, completed = _status(worker, tmp_path)
    close = _windows_reader(path)
    attempts, delays, rejected = [], [], []
    replace = os.replace

    def observed_replace(source, destination):
        attempts.append((source, destination))
        try:
            return replace(source, destination)
        except PermissionError as exc:
            rejected.append(getattr(exc, 'winerror', None))
            assert path.read_bytes() == original
            assert Path(source).read_bytes() == worker.raw(completed)
            if release_reader:
                # Release only after the real native failure. The unchanged
                # error must still reach the fixture's bounded retry policy.
                close()
            raise

    monkeypatch.setattr(worker, 'os', SimpleNamespace(replace=observed_replace))
    monkeypatch.setattr(worker, 'time', SimpleNamespace(sleep=delays.append))
    try:
        if release_reader:
            worker.write(path, completed)
            assert len(attempts) == 2 and len(rejected) == 1
            assert delays == [0.01]
            assert path.read_bytes() == worker.raw(completed)
            assert not path.with_suffix('.json.tmp').exists()
        else:
            with pytest.raises(PermissionError):
                worker.write(path, completed)
            assert len(attempts) == 7
            assert delays == [0.01, 0.02, 0.04, 0.08, 0.16, 0.32]
            assert path.read_bytes() == original
    finally:
        close()
    assert rejected and set(rejected) <= {5, 32, 33}
    assert len(set(attempts)) == 1
    assert sum(delays) <= 0.63
