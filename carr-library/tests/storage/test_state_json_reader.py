"""Actual file-handle and child-process checks for concurrently published state."""
import json
import os
import subprocess
import sys

import pytest

from carr_library.audio import jobs as state


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


def test_production_json_reader_allows_actual_atomic_child_replacement(tmp_path, monkeypatch):
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
            assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream

    monkeypatch.setattr(state, '_open_json_file', publish_while_production_handle_is_open)
    # The already-open snapshot remains coherent after the new state is published.
    assert state.read_json(target) == {'done': 0}
    assert observed == [{'published': True}]
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
    with pytest.raises(ValueError):
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
    original, streams = state._open_json_file, []

    def capture(path):
        if grow:
            path.write_bytes(b'{"text":"too long"}')
        stream = original(path)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', capture)
    with pytest.raises(ValueError, match='too large'):
        state.read_json(target, limit=8)
    assert all(stream.closed for stream in streams)


def test_repeated_open_races_are_bounded_and_close_every_handle(tmp_path, monkeypatch):
    target = tmp_path / 'state.json'
    target.write_bytes(b'{"done":0}')
    original, streams = state._open_json_file, []
    def replace_each_time(path):
        staged = path.with_suffix('.next')
        staged.write_bytes(b'{"done":1}')
        os.replace(staged, path)
        stream = original(path)
        streams.append(stream)
        return stream
    monkeypatch.setattr(state, '_open_json_file', replace_each_time)
    with pytest.raises(ValueError, match='repeatedly'):
        state.read_json(target)
    assert len(streams) == 8 and all(stream.closed for stream in streams)


def test_in_place_mutation_during_read_is_rejected_before_json_decoding(tmp_path, monkeypatch):
    from contextlib import contextmanager
    target = tmp_path / 'state.json'
    target.write_bytes(b'{"done":0}')
    original = state._open_json_file
    @contextmanager
    def mutate_during_read(path):
        with original(path) as stream:
            class Reader:
                def fileno(self):
                    return stream.fileno()
                def read(self, count):
                    data = stream.read(count)
                    with path.open('ab') as writer:
                        writer.write(b' ')
                    return data
            yield Reader()
    monkeypatch.setattr(state, '_open_json_file', mutate_during_read)
    monkeypatch.setattr(state.json, 'loads',
                        lambda *args, **kwargs: pytest.fail('Changed state must not be decoded.'))
    with pytest.raises(ValueError, match='changed in place'):
        state.read_json(target)


def test_strict_status_snapshot_does_not_block_child_publication_and_retains_identity_check(tmp_path, monkeypatch):
    from carr_library.audio import model_manifest
    target = tmp_path / 'job_status.json'
    target.write_bytes(b'{"done":0}')
    original, streams, observed = model_manifest._open_json_file, [], []
    def replace_while_open(path):
        stream = original(path)
        streams.append(stream)
        try:
            observed.append(replace_in_child(path))
            assert observed[-1] == {'published': True}
        except BaseException:
            stream.close()
            raise
        return stream
    monkeypatch.setattr(model_manifest, '_open_json_file', replace_while_open)
    # Status/manifest callers keep their existing exact current-path contract.
    # An observational status read may retry, but cannot make the writer fail.
    with pytest.raises(model_manifest.ModelManifestError, match='changed'):
        model_manifest._read_json_snapshot(target, max_bytes=1024)
    assert observed == [{'published': True}] and all(stream.closed for stream in streams)
    monkeypatch.setattr(model_manifest, '_open_json_file', original)
    value, digest = model_manifest._read_json_snapshot(target, max_bytes=1024)
    import hashlib
    assert value == {'done': 1} and digest == hashlib.sha256(target.read_bytes()).hexdigest()
