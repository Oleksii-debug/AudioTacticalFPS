"""Bounded JSON snapshots and native-handle ownership, without native claims."""
import ctypes
import os
from types import SimpleNamespace
import sys

import pytest

from carr_library import durable_state


class _ReadStream:
    def __init__(self, stream, before_read=None):
        self.stream, self.before_read = stream, before_read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def fileno(self):
        return self.stream.fileno()

    def read(self, count):
        if self.before_read is not None:
            self.before_read()
        return self.stream.read(count)


def _track_read(monkeypatch, before_read=None):
    original = durable_state._open_json_read
    streams = []

    def opened(path):
        stream = _ReadStream(original(path), before_read)
        streams.append(stream.stream)
        return stream

    monkeypatch.setattr(durable_state, '_open_json_read', opened)
    return streams


@pytest.mark.parametrize('payload', [b'{"same":1,"same":2}', b'{"n":NaN}',
                                    b'{"n":Infinity}', b'{', b'\xff'])
def test_json_parser_refusals_close_the_read_stream(tmp_path, monkeypatch, payload):
    path = tmp_path / 'state.json'
    path.write_bytes(payload)
    streams = _track_read(monkeypatch)
    with pytest.raises((ValueError, UnicodeDecodeError)):
        durable_state.read_json(path)
    assert len(streams) == 1 and streams[0].closed


def test_growing_json_is_bounded_and_closes_the_read_stream(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{}')
    monkeypatch.setattr(durable_state, 'MAX_JSON_BYTES', 32)

    def grow():
        with path.open('ab') as stream:
            stream.write(b' ' * 128)

    streams = _track_read(monkeypatch, grow)
    with pytest.raises(ValueError, match='grew beyond'):
        durable_state.read_json(path)
    assert len(streams) == 1 and streams[0].closed


def test_in_place_json_mutation_is_not_a_stable_snapshot(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{"value":1}')
    before = path.stat()

    def change():
        path.write_bytes(b'{"value":2}')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))

    streams = _track_read(monkeypatch, change)
    with pytest.raises(ValueError, match='changed while'):
        durable_state.read_json(path)
    assert streams[0].closed


def test_atomic_replacement_keeps_a_complete_open_snapshot(tmp_path, monkeypatch):
    path, stage = tmp_path / 'state.json', tmp_path / 'status.tmp'
    path.write_bytes(b'{"generation":1}')
    stage.write_bytes(b'{"generation":2}')
    with monkeypatch.context() as patch:
        streams = _track_read(patch, lambda: durable_state.replace_file(stage, path))
        assert durable_state.read_json(path) == {'generation': 1}
    assert len(streams) == 1 and streams[0].closed
    assert durable_state.read_json(path) == {'generation': 2}
    assert not stage.exists()


def test_oversized_json_is_rejected_before_open(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_bytes(b' ' * 33)
    monkeypatch.setattr(durable_state, 'MAX_JSON_BYTES', 32)
    streams = _track_read(monkeypatch)
    with pytest.raises(ValueError, match='configured JSON budget'):
        durable_state.read_json(path)
    assert streams == []


def test_hard_link_and_directory_state_are_rejected_before_open(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{}')
    os.link(path, tmp_path / 'second-name.json')
    streams = _track_read(monkeypatch)
    for candidate in (path, tmp_path):
        with pytest.raises(ValueError, match='regular local files'):
            durable_state.read_json(candidate)
    assert streams == []


@pytest.mark.parametrize('kind', ['reparse', 'hard-link', 'non-regular'])
def test_opened_file_must_remain_a_plain_file(tmp_path, monkeypatch, kind):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{}')
    real_fstat = os.fstat
    streams = _track_read(monkeypatch)

    def changed(descriptor):
        value = real_fstat(descriptor)
        return SimpleNamespace(st_mode=0 if kind == 'non-regular' else value.st_mode,
            st_file_attributes=0x400 if kind == 'reparse' else 0,
            st_nlink=2 if kind == 'hard-link' else value.st_nlink)

    monkeypatch.setattr(durable_state.os, 'fstat', changed)
    with pytest.raises(ValueError, match='regular local files'):
        durable_state.read_json(path)
    assert len(streams) == 1 and streams[0].closed


class _NativeCall:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


@pytest.mark.parametrize('failure', ['create', 'transfer', 'stream'])
def test_windows_handle_transfer_closes_exactly_its_current_owner(tmp_path, monkeypatch, failure):
    """Exercise failure ownership portably; the separate Windows gate uses WinAPI."""
    calls, closed_handles, closed_descriptors = [], [], []
    handle, descriptor = 0x12345678, 2147480000

    def create(*args):
        calls.append(args)
        return ctypes.c_void_p(-1).value if failure == 'create' else handle

    kernel = SimpleNamespace(CreateFileW=_NativeCall(create),
        CloseHandle=_NativeCall(lambda value: closed_handles.append(value) or 1))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kwargs: kernel, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda value: OSError('synthetic CreateFile failure'), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(os, 'O_BINARY', 0x8000, raising=False)
    monkeypatch.setattr(os, 'O_NOINHERIT', 0x80, raising=False)

    def transfer(value, flags):
        assert value == handle
        assert flags & os.O_BINARY and flags & os.O_NOINHERIT
        if failure == 'transfer':
            raise OSError('synthetic transfer failure')
        return descriptor

    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(open_osfhandle=transfer))

    def stream(value, mode):
        assert value == descriptor and mode == 'rb'
        raise OSError('synthetic stream failure')

    real_close = os.close

    def close(value):
        if value == descriptor:
            closed_descriptors.append(value)
        else:
            real_close(value)

    monkeypatch.setattr(os, 'fdopen', stream)
    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(OSError, match='synthetic'):
        durable_state._open_windows_read(tmp_path / 'state.json')
    assert closed_handles == ([handle] if failure == 'transfer' else [])
    assert closed_descriptors == ([descriptor] if failure == 'stream' else [])
    assert len(calls) == 1
    assert calls[0][1:] == (0x80000000, 0x7, None, 3, 0x00200000, None)
