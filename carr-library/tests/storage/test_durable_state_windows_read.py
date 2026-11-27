"""Actual Windows handle/child barriers: run as a strict, zero-skip Windows gate.

Non-Windows collection is explicitly unsupported evidence. Explicit legacy
cases establish why both delete sharing and native replacement are required.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import durable_state


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Requires actual Windows file sharing.')


def _replace_in_child(source, destination, *, native=False):
    code = ('import os, sys; os.replace(sys.argv[1], sys.argv[2])' if not native else
            'import sys; sys.path.insert(0, sys.argv[3]); '
            'from carr_library.windows_state import replace_file; '
            'replace_file(sys.argv[1], sys.argv[2])')
    return subprocess.run([sys.executable, '-I', '-B', '-c',
        code, str(source), str(destination), str(Path(durable_state.__file__).resolve().parents[1])],
        capture_output=True, text=True, timeout=10, check=False)


def test_windows_unshared_reader_reproduces_atomic_child_replace_denial(tmp_path):
    target, stage = tmp_path / 'state.json', tmp_path / 'status.tmp'
    target.write_bytes(b'{"generation":1}')
    stage.write_bytes(b'{"generation":2}')
    with target.open('rb') as stream:
        result = _replace_in_child(stage, target)
        assert result.returncode != 0
        assert 'PermissionError' in result.stderr and '[WinError ' in result.stderr
        assert stream.read() == b'{"generation":1}'
    assert target.read_bytes() == b'{"generation":1}' and stage.exists()
    # No retry occurred in the child. A separate call after release establishes
    # that the held read handle was the actual publication barrier.
    result = _replace_in_child(stage, target)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b'{"generation":2}' and not stage.exists()


def test_windows_json_reader_allows_atomic_child_replace_and_keeps_old_snapshot(tmp_path, monkeypatch):
    target, stage = tmp_path / 'state.json', tmp_path / 'status.tmp'
    old, new = {'generation': 1, 'title': 'Попередній знімок'}, {'generation': 2, 'title': 'Новий знімок'}
    target.write_bytes(durable_state.encode(old))
    stage.write_bytes(durable_state.encode(new))
    streams, child_results = [], []

    class HeldRead:
        def __init__(self, stream):
            self.stream = stream
            streams.append(stream)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            assert not self.stream.closed
            result = _replace_in_child(stage, target, native=True)
            child_results.append(result)
            assert result.returncode == 0, result.stderr
            return self.stream.read(count)

    with monkeypatch.context() as patch:
        if hasattr(durable_state, '_open_json_read'):
            original = durable_state._open_json_read
            patch.setattr(durable_state, '_open_json_read', lambda path: HeldRead(original(path)))
        else:
            # The identical regression executes the baseline production reader.
            original = Path.open

            def opened(path, *args, **kwargs):
                stream = original(path, *args, **kwargs)
                return HeldRead(stream) if path == target else stream

            patch.setattr(Path, 'open', opened)
        assert durable_state.read_json(target) == old
    assert len(child_results) == 1 and len(streams) == 1 and streams[0].closed
    assert durable_state.read_json(target) == new
    assert not stage.exists()
    target.unlink()  # The reader retained no live handle after its snapshot.


def _handle_is_open(handle):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetHandleInformation
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    query.restype = wintypes.BOOL
    flags = wintypes.DWORD()
    return bool(query(handle, ctypes.byref(flags)))


def test_windows_real_handle_is_closed_if_descriptor_transfer_fails(tmp_path, monkeypatch):
    import msvcrt

    target = tmp_path / 'state.json'
    target.write_bytes(b'{}')
    handles = []

    def fail_transfer(handle, flags):
        handles.append(handle)
        assert _handle_is_open(handle)
        raise OSError('synthetic CRT transfer refusal')

    monkeypatch.setattr(msvcrt, 'open_osfhandle', fail_transfer)
    with pytest.raises(OSError, match='transfer refusal'):
        durable_state.read_json(target)
    assert len(handles) == 1 and not _handle_is_open(handles[0])
    target.unlink()


def test_windows_real_descriptor_is_closed_if_stream_construction_fails(tmp_path, monkeypatch):
    import msvcrt

    target = tmp_path / 'state.json'
    target.write_bytes(b'{}')
    descriptors, handles = [], []

    def fail_stream(descriptor, mode):
        descriptors.append(descriptor)
        handles.append(msvcrt.get_osfhandle(descriptor))
        assert _handle_is_open(handles[-1])
        raise OSError('synthetic stream construction refusal')

    monkeypatch.setattr(os, 'fdopen', fail_stream)
    with pytest.raises(OSError, match='construction refusal'):
        durable_state.read_json(target)
    assert len(handles) == 1 and not _handle_is_open(handles[0])
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    target.unlink()


def test_windows_json_handle_is_not_inheritable(tmp_path):
    target = tmp_path / 'state.json'
    target.write_bytes(b'{}')
    with durable_state._open_json_read(target) as stream:
        assert not os.get_inheritable(stream.fileno())
        assert stream.read() == b'{}'


def test_windows_legacy_replace_still_refuses_a_delete_shared_target(tmp_path):
    """FILE_SHARE_DELETE alone cannot repair the MoveFileEx overwrite contract."""
    target, stage = tmp_path / 'state.json', tmp_path / 'status.tmp'
    target.write_bytes(b'old')
    stage.write_bytes(b'new')
    with durable_state._open_json_read(target) as stream:
        legacy = _replace_in_child(stage, target)
        assert legacy.returncode != 0 and 'PermissionError' in legacy.stderr
        assert stream.read() == b'old' and target.read_bytes() == b'old'
        replacement = _replace_in_child(stage, target, native=True)
        assert replacement.returncode == 0, replacement.stderr
        stream.seek(0)
        assert stream.read() == b'old' and target.read_bytes() == b'new'
    assert not stage.exists()
