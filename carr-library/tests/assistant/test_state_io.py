"""Actual state handles, bounded publication and Assistant's retained JSON budget."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import durable_state
from carr_library.assistant import state


MOVE_CURRENT = r'''
import json, os, pathlib, sys
target = pathlib.Path(sys.argv[1])
try:
    os.replace(target, target.with_suffix('.moved'))
except OSError as error:
    print(json.dumps({'moved': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
print(json.dumps({'moved': True}))
'''


def move_in_child(path):
    child = subprocess.run(
        [sys.executable, '-I', '-B', '-X', 'utf8', '-c', MOVE_CURRENT, str(path)],
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert child.returncode in (0, 2), child.stderr
    return json.loads(child.stdout)


def test_assistant_snapshot_shares_delete_with_actual_child_and_closes_owned_handle(tmp_path, monkeypatch):
    path = tmp_path / 'стан.json'
    raw = b'{"status":"running","completed":0}'
    path.write_bytes(raw)
    with path.open('rb'):
        ordinary = move_in_child(path)
    if os.name == 'nt':
        assert ordinary['moved'] is False and ordinary['winerror'] in (5, 32, 33)
    else:
        assert ordinary == {'moved': True}
        path.with_suffix('.moved').replace(path)
    original, streams = state._open_json_file, []

    class Snapshot:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, maximum):
            # The independent process moves the actual currently-open name.
            # No writer/parser substitute or timing-based retry is involved.
            assert move_in_child(path) == {'moved': True}
            return self.stream.read(maximum)

    def open_snapshot(selected):
        stream = original(selected)
        streams.append(stream)
        assert not os.get_inheritable(stream.fileno())
        return Snapshot(stream)

    monkeypatch.setattr(state, '_open_json_file', open_snapshot)
    assert state.read_json(path) == {'status': 'running', 'completed': 0}
    assert len(streams) == 1 and streams[0].closed
    assert not path.exists() and path.with_suffix('.moved').read_bytes() == raw


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_assistant_publication_reuses_one_complete_stage_during_transient_denial(
        tmp_path, monkeypatch, winerror):
    path = tmp_path / 'job.json'
    previous = b'{"status":"running"}\n'
    path.write_bytes(previous)
    value = {'status': 'complete', 'text': 'Київ'}
    expected = (state.canonical(value) + '\n').encode('utf-8')
    replace, attempts, delays = os.replace, [], []

    def interrupted(source, target):
        stage = Path(source)
        assert Path(target) == path and stage.read_bytes() == expected
        assert path.read_bytes() == previous
        attempts.append((stage, stage.stat().st_ino))
        if len(attempts) <= 2:
            error = PermissionError('modeled observed Windows sharing denial')
            error.winerror = winerror
            raise error
        return replace(source, target)

    monkeypatch.setattr(durable_state.os, 'replace', interrupted)
    monkeypatch.setattr(durable_state.time, 'sleep', delays.append)
    state.atomic_json(path, value)
    assert len(attempts) == 3 and len(set(attempts)) == 1
    assert delays == [.01, .02]
    assert path.read_bytes() == expected and state.read_json(path) == value
    assert sorted(p.name for p in tmp_path.iterdir()) == ['job.json']


def test_exhausted_sharing_denial_is_bounded_and_preserves_previous_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / 'job.json'
    previous = b'{"status":"running"}\n'
    path.write_bytes(previous)
    attempts, delays = [], []

    def denied(source, target):
        assert Path(source).read_bytes() == b'{"status":"complete"}\n'
        assert path.read_bytes() == previous
        attempts.append(Path(source))
        error = PermissionError('continued access denial')
        error.winerror = 5
        raise error

    monkeypatch.setattr(durable_state.os, 'replace', denied)
    monkeypatch.setattr(durable_state.time, 'sleep', delays.append)
    with pytest.raises(PermissionError, match='continued access denial'):
        state.atomic_json(path, {'status': 'complete'})
    assert len(attempts) == 7 and len(set(attempts)) == 1
    assert delays == [.01, .02, .04, .08, .16, .32] and sum(delays) == pytest.approx(.63)
    assert path.read_bytes() == previous
    assert sorted(p.name for p in tmp_path.iterdir()) == ['job.json']


@pytest.mark.parametrize('winerror', [None, 87])
def test_unrelated_publication_error_never_retries_or_unlinks_old_state(tmp_path, monkeypatch, winerror):
    path = tmp_path / 'job.json'
    path.write_bytes(b'{"old":1}')
    attempts = []

    def denied(source, target):
        attempts.append(source)
        error = PermissionError('unrelated permission failure')
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr(durable_state.os, 'replace', denied)
    monkeypatch.setattr(durable_state.time, 'sleep',
                        lambda delay: pytest.fail('Unrelated failure was retried.'))
    with pytest.raises(PermissionError, match='unrelated permission failure'):
        state.atomic_json(path, {'new': 2})
    assert len(attempts) == 1 and path.read_bytes() == b'{"old":1}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['job.json']


def test_assistant_reader_retries_only_atomic_stat_open_replacement(tmp_path, monkeypatch):
    path, replacement = tmp_path / 'job.json', tmp_path / 'new.json'
    path.write_bytes(b'{"revision":0}')
    replacement.write_bytes(b'{"revision":1}')
    original, streams = state._open_json_file, []

    def replace_before_open(selected):
        if replacement.exists():
            os.replace(replacement, path)
        stream = original(selected)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', replace_before_open)
    assert state.read_json(path) == {'revision': 1}
    assert len(streams) == 2 and all(stream.closed for stream in streams)


def test_continuously_replaced_reader_is_bounded_and_closes_every_handle(tmp_path, monkeypatch):
    path, replacement = tmp_path / 'job.json', tmp_path / 'new.json'
    path.write_bytes(b'{"revision":0}')
    original, streams = state._open_json_file, []

    def replace_before_open(selected):
        replacement.write_bytes(b'{"revision":1}')
        os.replace(replacement, path)
        stream = original(selected)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', replace_before_open)
    with pytest.raises(ValueError, match='changed repeatedly'):
        state.read_json(path)
    assert len(streams) == 8 and all(stream.closed for stream in streams)
    assert path.read_bytes() == b'{"revision":1}'


@pytest.mark.parametrize('kind', ['file', 'ancestor', 'hardlink'])
def test_assistant_links_reject_before_any_payload_read(tmp_path, monkeypatch, kind):
    directory = tmp_path / 'outside'
    directory.mkdir()
    original = directory / 'job.json'
    original.write_bytes(b'{"private":"outside"}')
    selected = tmp_path / 'selected'
    if kind == 'file':
        selected.symlink_to(original)
    elif kind == 'ancestor':
        selected.symlink_to(directory, target_is_directory=True)
        selected /= 'job.json'
    else:
        os.link(original, selected)
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('Linked checkpoint reached a payload open.'))
    with pytest.raises(ValueError, match='links|regular'):
        state.read_json(selected)
    assert original.read_bytes() == b'{"private":"outside"}'


@pytest.mark.parametrize('grow', [False, True])
def test_assistant_size_limit_rechecks_the_actual_opened_object(tmp_path, monkeypatch, grow):
    path = tmp_path / 'job.json'
    path.write_bytes(b'{}' if grow else b'{"text":"too long"}')
    original, streams = state._open_json_file, []

    def open_snapshot(selected):
        if grow:
            selected.write_bytes(b'{"text":"too long"}')
        stream = original(selected)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', open_snapshot)
    with pytest.raises(ValueError, match='size'):
        state.read_json(path, maximum=8)
    assert len(streams) == int(grow) and all(stream.closed for stream in streams)


def test_assistant_live_growth_never_crosses_read_budget(tmp_path, monkeypatch):
    path = tmp_path / 'job.json'
    path.write_bytes(b'{}')
    original, streams = state._open_json_file, []

    class Grown:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, maximum):
            with path.open('ab') as writer:
                writer.write(b' ' * 20)
            assert maximum == 9
            return self.stream.read(maximum)

    def open_snapshot(selected):
        stream = original(selected)
        streams.append(stream)
        return Grown(stream)

    monkeypatch.setattr(state, '_open_json_file', open_snapshot)
    with pytest.raises(ValueError, match='budget'):
        state.read_json(path, maximum=8)
    assert len(streams) == 1 and streams[0].closed


@pytest.mark.parametrize('raw', [b'{broken', b'\xff', b'{"status":'])
def test_malformed_checkpoint_closes_the_actual_read_handle(tmp_path, monkeypatch, raw):
    path = tmp_path / 'job.json'
    path.write_bytes(raw)
    original, streams = state._open_json_file, []

    def open_snapshot(selected):
        stream = original(selected)
        streams.append(stream)
        return stream

    monkeypatch.setattr(state, '_open_json_file', open_snapshot)
    with pytest.raises((ValueError, UnicodeError)):
        state.read_json(path)
    assert len(streams) == 1 and streams[0].closed and path.read_bytes() == raw


@pytest.mark.parametrize('maximum', [True, 0, -1, 64 * 1024 * 1024 + 1])
def test_invalid_assistant_read_budget_rejects_before_open(tmp_path, monkeypatch, maximum):
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('Invalid budget reached payload access.'))
    with pytest.raises(ValueError, match='read budget'):
        state.read_json(tmp_path / 'absent.json', maximum=maximum)


def test_existing_assistant_64_mib_contract_is_not_reduced_to_generic_json_limit(tmp_path):
    assert state.MAX_CHECKPOINT_BYTES == 64 * 1024 * 1024
    path = tmp_path / 'larger-assistant.json'
    # A real checkpoint above the generic reader's limit proves that sharing
    # helpers did not silently narrow the existing Assistant API.
    value = {'text': 'x' * (durable_state.MAX_JSON_BYTES + 1)}
    state.atomic_json(path, value)
    assert durable_state.MAX_JSON_BYTES < path.stat().st_size < state.MAX_CHECKPOINT_BYTES
    assert state.read_json(path) == value


def test_assistant_write_budget_preserves_prior_state_before_creating_a_stage(tmp_path, monkeypatch):
    path = tmp_path / 'job.json'
    path.write_bytes(b'{"old":1}')
    monkeypatch.setattr(state, 'MAX_CHECKPOINT_BYTES', 16)
    with pytest.raises(ValueError, match='storage budget'):
        state.atomic_json(path, {'text': 'x' * 17})
    assert path.read_bytes() == b'{"old":1}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['job.json']
