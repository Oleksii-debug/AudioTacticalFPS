"""Required actual Windows read-handle sharing and atomic status publication."""
import ctypes
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

assert os.name == 'nt', 'This gate requires actual Windows file-sharing handles.'

# Reuse the portable fixture and source-byte assertions by exact test-file path;
# installed/importlib mode must not depend on pytest adding sibling directories.
_helpers_path = Path(__file__).resolve().parents[1] / 'transcription' / 'test_protocol_fixture.py'
_helpers_spec = importlib.util.spec_from_file_location('carr_protocol_fixture_helpers', _helpers_path)
assert _helpers_spec is not None and _helpers_spec.loader is not None
_helpers = importlib.util.module_from_spec(_helpers_spec)
_helpers_spec.loader.exec_module(_helpers)
worker = _helpers.worker
_status = _helpers._status


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
    assert sum(delays) == pytest.approx(0.01 if release_reader else 0.63)
