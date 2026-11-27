"""Actual file-handle and child-process checks for concurrently published state."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import durable_state as state


REPLACE_ONCE = r'''
import hashlib, importlib.util, json, os, pathlib, sys
target = pathlib.Path(sys.argv[1])
module_path = pathlib.Path(sys.argv[2])
assert hashlib.sha256(module_path.read_bytes()).hexdigest() == sys.argv[3]
spec = importlib.util.spec_from_file_location('exact_durable_state', module_path)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)
if sys.argv[4] == 'legacy':
    # Reproduce the former MoveFileEx publisher independently of the corrected
    # production fallback; the rest of atomic_json still owns stage cleanup.
    state._replace_file = os.replace
try:
    state.atomic_json(target, {'done': 1}, replace=True)
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
else:
    print(json.dumps({'published': True}))
'''


def replace_in_child(path, *, legacy=False):
    process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
                              REPLACE_ONCE, str(path), str(Path(state.__file__).resolve()),
                              sha256(Path(state.__file__).read_bytes()).hexdigest(),
                              'legacy' if legacy else 'production'], capture_output=True,
                             text=True, encoding='utf-8', timeout=15)
    assert process.returncode in (0, 2), process.stderr
    return json.loads(process.stdout)


def test_production_json_reader_allows_actual_atomic_child_replacement(tmp_path, monkeypatch):
    target = tmp_path / 'стан 🌿.json'
    target.write_bytes(b'{"done":0}')
    # Raw os.replace is the legacy negative control. The production publisher
    # can succeed even with an ordinary reader on current Windows; requiring it
    # to fail would reject the fix before testing the shared reader below.
    # POSIX permits both replacements without a skip.
    with target.open('rb'):
        ordinary = replace_in_child(target, legacy=True)
    if os.name == 'nt':
        assert ordinary['published'] is False
        assert ordinary['winerror'] in (5, 32, 33)
        assert target.read_bytes() == b'{"done":0}'
        assert not list(tmp_path.glob('.pending-*'))
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
            assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(state, '_open_json_file', publish_while_production_handle_is_open)
    # The already-open snapshot remains coherent after the new state is published.
    assert state.read_json(target) == {'done': 0}
    assert observed == [{'published': True}]
    assert not list(tmp_path.glob('.pending-*'))
    assert all(stream.closed for stream in streams)
    monkeypatch.setattr(state, '_open_json_file', original)
    assert state.read_json(target) == {'done': 1}


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
