"""Actual file-handle and child-process checks for concurrently published state."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

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
    os.replace(staged, target)
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
else:
    print(json.dumps({'published': True}))
finally:
    staged.unlink(missing_ok=True)
'''


def replace_in_child(path):
    process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
                              REPLACE_ONCE, str(path)], capture_output=True,
                             text=True, encoding='utf-8', timeout=15)
    assert process.returncode in (0, 2), process.stderr
    return json.loads(process.stdout)


def test_raw_replace_exposes_windows_open_destination_limit_without_corrupting_snapshot(tmp_path, monkeypatch):
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

    original = state._open_json_file
    observed, streams = [], []

    def publish_while_production_handle_is_open(path):
        stream = original(path)
        streams.append(stream)
        try:
            assert not os.get_inheritable(stream.fileno())
            observed.append(replace_in_child(path))
            if os.name == 'nt':
                # Actual PR163 Windows evidence returned WinError 5 here:
                # MoveFileExW cannot replace an open destination merely because
                # its handle grants FILE_SHARE_DELETE. The production writer's
                # bounded retry is exercised separately below.
                assert observed[-1]['published'] is False
                assert observed[-1]['winerror'] in (5, 32, 33)
            else:
                assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(state, '_open_json_file', publish_while_production_handle_is_open)
    # Either platform retains coherent old bytes on the already-open handle.
    assert state.read_json(target) == {'done': 0}
    assert len(observed) == 1
    assert all(stream.closed for stream in streams)
    monkeypatch.setattr(state, '_open_json_file', original)
    if os.name == 'nt':
        assert state.read_json(target) == {'done': 0}
        assert replace_in_child(target) == {'published': True}
    assert state.read_json(target) == {'done': 1}


PRODUCTION_REPLACE = r'''
from hashlib import sha256
import importlib.util, json, os, pathlib, sys
source, expected, target, observed = sys.argv[1:]
source, target, observed = map(pathlib.Path, (source, target, observed))
assert sha256(source.read_bytes()).hexdigest() == expected
spec = importlib.util.spec_from_file_location('tested_durable_state', source)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
assert pathlib.Path(state.__file__).resolve() == source.resolve()
assert sha256(source.read_bytes()).hexdigest() == expected
replace = state.os.replace
errors = []
def signal():
    if not observed.exists():
        observed.write_bytes(b'attempted')
def observed_replace(old, new):
    try:
        result = replace(old, new)
    except PermissionError as error:
        errors.append(getattr(error, 'winerror', None))
        signal()
        raise
    else:
        signal()
        return result
state.os.replace = observed_replace
try:
    state.atomic_json(target, {'done': 1}, replace=True)
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}), flush=True)
    raise SystemExit(2)
else:
    print(json.dumps({'published': True, 'errors': errors, 'module': str(source),
                      'sha256': expected}), flush=True)
'''


def test_production_atomic_writer_publishes_after_bounded_snapshot_reader_closes(tmp_path, monkeypatch):
    target, observed = tmp_path / 'стан.json', tmp_path / 'writer-attempted'
    state.atomic_json(target, {'done': 0})
    source = Path(state.__file__).resolve()
    expected = sha256(source.read_bytes()).hexdigest()
    original, streams, children = state._open_json_file, [], []

    def overlap_writer(path):
        stream = original(path)
        streams.append(stream)
        try:
            assert not os.get_inheritable(stream.fileno())
            # Bind the same product module imported by the parent. This also
            # works in the isolated installed-package gate without source-path
            # injection or assuming that the checkout is importable in a child.
            child = subprocess.Popen(
                [sys.executable, '-I', '-B', '-X', 'utf8', '-c', PRODUCTION_REPLACE,
                 str(source), expected, str(path), str(observed)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            children.append(child)
            deadline = time.monotonic() + 10
            while not observed.exists():
                if child.poll() is not None:
                    stdout, stderr = child.communicate(timeout=5)
                    pytest.fail(f'Writer exited before its atomic attempt: {stdout} {stderr}')
                if time.monotonic() >= deadline:
                    pytest.fail('Writer did not reach its atomic replacement within 10 seconds.')
                time.sleep(0.005)
            return stream
        except BaseException:
            stream.close()
            raise

    monkeypatch.setattr(state, '_open_json_file', overlap_writer)
    try:
        # The child has attempted publication while this production read handle
        # is still open. On Windows the retry publishes only after read_json has
        # read and closed it; POSIX can publish while the old snapshot is open.
        assert state.read_json(target) == {'done': 0}
        assert len(streams) == 1 and streams[0].closed
        assert len(children) == 1
        stdout, stderr = children[0].communicate(timeout=15)
        assert children[0].returncode == 0, (stdout, stderr)
        result = json.loads(stdout)
        assert result['published'] is True
        assert result['module'] == str(source) and result['sha256'] == expected
        if os.name == 'nt':
            assert result['errors'] and set(result['errors']) <= {5, 32, 33}
        else:
            assert result['errors'] == []
    finally:
        for stream in streams:
            stream.close()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
    monkeypatch.setattr(state, '_open_json_file', original)
    assert state.read_json(target) == {'done': 1}
    assert not list(tmp_path.glob('.pending-*'))


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
