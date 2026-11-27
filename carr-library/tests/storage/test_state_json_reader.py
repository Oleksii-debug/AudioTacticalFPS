"""Actual file-handle and child-process checks for concurrently published state."""
import json
import os
import subprocess
import sys
import threading

import pytest

from carr_library import durable_state as state


REPLACE_ONCE = r'''
import json, os, pathlib, sys
target = pathlib.Path(sys.argv[1])
staged = target.with_suffix('.next')
try:
    with staged.open('xb') as stream:
        stream.write(b'{"done":1}')
        stream.flush()
        os.fsync(stream.fileno())
    if sys.argv[2] == 'move-current':
        os.replace(target, target.with_suffix('.moved'))
    else:
        os.replace(staged, target)
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
else:
    print(json.dumps({'published': True}))
finally:
    staged.unlink(missing_ok=True)
'''


def replace_in_child(path, *, move_current=False):
    process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
                              REPLACE_ONCE, str(path),
                              'move-current' if move_current else 'replace'], capture_output=True,
                             text=True, encoding='utf-8', timeout=15)
    assert process.returncode in (0, 2), process.stderr
    return json.loads(process.stdout)


def test_production_json_reader_releases_coherent_snapshot_for_atomic_replacement(tmp_path, monkeypatch):
    target = tmp_path / 'стан.json'
    target.write_bytes(b'{"done":0}')
    # This negative control demonstrates the real Windows sharing collision.
    # POSIX permits replacement with either kind of reader, without a skip.
    with target.open('rb'):
        ordinary = replace_in_child(target)
    if os.name == 'nt':
        assert ordinary['published'] is False
        assert ordinary['winerror'] in (5, 32, 33)
    else:
        assert ordinary == {'published': True}
    target.write_bytes(b'{"done":0}')

    original, streams, results, errors = state._open_json_file, [], [], []
    reading, release = threading.Event(), threading.Event()

    class HeldSnapshot:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, maximum):
            reading.set()
            assert release.wait(10), 'The test did not release the live snapshot.'
            return self.stream.read(maximum)

    def hold_snapshot(path):
        stream = original(path)
        streams.append(stream)
        assert not os.get_inheritable(stream.fileno())
        return HeldSnapshot(stream)

    def read_snapshot():
        try:
            results.append(state.read_json(target))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(state, '_open_json_file', hold_snapshot)
    reader = threading.Thread(target=read_snapshot)
    reader.start()
    try:
        assert reading.wait(10), 'The production reader never opened its snapshot.'
        observed = replace_in_child(target)
        if os.name == 'nt':
            # Legacy MoveFileEx replacement can reject an open target even
            # when its handle permits delete sharing. It must remain atomic.
            assert observed['published'] is False and observed['winerror'] in (5, 32, 33)
        else:
            assert observed == {'published': True}
    finally:
        release.set()
        reader.join(timeout=15)
    assert not reader.is_alive() and errors == []
    assert results == [{'done': 0}]
    assert all(stream.closed for stream in streams)
    monkeypatch.setattr(state, '_open_json_file', original)
    assert replace_in_child(target) == {'published': True}
    assert state.read_json(target) == {'done': 1}


def test_production_handle_shares_delete_for_actual_child_name_move(tmp_path):
    target = tmp_path / 'state.json'
    target.write_bytes(b'{"done":0}')
    with target.open('rb'):
        ordinary = replace_in_child(target, move_current=True)
    if os.name == 'nt':
        assert ordinary['published'] is False and ordinary['winerror'] in (5, 32, 33)
    else:
        assert ordinary == {'published': True}
        target.with_suffix('.moved').replace(target)
    with state._open_json_file(target) as stream:
        assert not os.get_inheritable(stream.fileno())
        assert replace_in_child(target, move_current=True) == {'published': True}
        assert stream.read() == b'{"done":0}'
    assert stream.closed and not target.exists()
    assert target.with_suffix('.moved').read_bytes() == b'{"done":0}'


@pytest.mark.parametrize('kind', ['file', 'ancestor', 'hardlink'])
def test_linked_json_is_rejected_before_opening_any_payload(tmp_path, monkeypatch, kind):
    directory = tmp_path / 'outside'
    directory.mkdir()
    original = directory / 'state.json'
    original.write_bytes(b'{"private":"outside"}')
    selected = tmp_path / 'selected.json'
    if kind == 'file':
        selected.symlink_to(original)
    elif kind == 'ancestor':
        selected.symlink_to(directory, target_is_directory=True)
        selected /= 'state.json'
    else:
        os.link(original, selected)
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('Linked state reached a payload open.'))
    with pytest.raises(ValueError, match='links'):
        state.read_json(selected)
    assert original.read_bytes() == b'{"private":"outside"}'


def test_atomic_replacement_between_stat_and_open_is_retried(tmp_path, monkeypatch):
    target, staged = tmp_path / 'state.json', tmp_path / 'new.json'
    target.write_bytes(b'{"done":0}')
    staged.write_bytes(b'{"done":1}')
    original = state._open_json_file
    opened = []

    def replace_before_open(path):
        if staged.exists():
            os.replace(staged, target)
        stream = original(path)
        opened.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', replace_before_open)
    assert state.read_json(target) == {'done': 1}
    assert len(opened) == 2 and all(stream.closed for stream in opened)


def test_link_substituted_after_validation_is_never_decoded(tmp_path, monkeypatch):
    target, outside = tmp_path / 'state.json', tmp_path / 'outside.json'
    target.write_bytes(b'{"done":0}')
    outside.write_bytes(b'{"private":"outside"}')
    original = state._open_json_file

    def substitute(path):
        target.unlink()
        target.symlink_to(outside)
        return original(path)

    monkeypatch.setattr(state, '_open_json_file', substitute)
    monkeypatch.setattr(state.json, 'loads',
                        lambda *args, **kwargs: pytest.fail('A substituted link was decoded.'))
    with pytest.raises((ValueError, OSError)):
        state.read_json(target)
    assert outside.read_bytes() == b'{"private":"outside"}'


@pytest.mark.parametrize('payload', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{invalid'])
def test_invalid_json_still_fails_and_closes_its_read_handle(tmp_path, monkeypatch, payload):
    target = tmp_path / 'state.json'
    target.write_bytes(payload)
    original, streams = state._open_json_file, []

    def capture(path):
        stream = original(path)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', capture)
    with pytest.raises(ValueError):
        state.read_json(target)
    assert streams and all(stream.closed for stream in streams)
    assert target.read_bytes() == payload


@pytest.mark.parametrize('grow', [False, True])
def test_json_byte_limit_applies_before_and_after_open(tmp_path, monkeypatch, grow):
    target = tmp_path / 'state.json'
    target.write_bytes(b'{}' if grow else b'{"text":"too long"}')
    monkeypatch.setattr(state, 'MAX_JSON_BYTES', 8)
    original, streams = state._open_json_file, []

    def capture(path):
        if grow:
            path.write_bytes(b'{"text":"too long"}')
        stream = original(path)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', capture)
    with pytest.raises(ValueError, match='budget'):
        state.read_json(target)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize('stop_at', [1, 3], ids=['before-first-read', 'during-streamed-hash'])
def test_digest_cancellation_closes_actual_stream_and_preserves_complete_file(tmp_path, monkeypatch, stop_at):
    from hashlib import sha256
    target = tmp_path / 'completed-output.bin'
    original = b'a' * (3 * 1024 * 1024 + 17)
    target.write_bytes(original)
    stamp = target.stat().st_mtime_ns
    opened, read_bytes = [], []
    original_open = state.Path.open

    class Observed:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, maximum):
            value = self.stream.read(maximum)
            read_bytes.append(len(value))
            return value

    def observe(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == target:
            opened.append(stream)
            return Observed(stream)
        return stream

    polls = 0
    def cancelled():
        nonlocal polls
        polls += 1
        return polls >= stop_at

    with monkeypatch.context() as patches:
        patches.setattr(state.Path, 'open', observe)
        with pytest.raises(InterruptedError, match='verification cancelled'):
            state.digest_file(target, cancelled=cancelled)
    assert polls == stop_at and len(opened) == 1 and opened[0].closed
    assert sum(read_bytes) == (0 if stop_at == 1 else 2 * 1024 * 1024)
    assert target.read_bytes() == original and target.stat().st_mtime_ns == stamp
    assert state.digest_file(target) == sha256(original).hexdigest()
