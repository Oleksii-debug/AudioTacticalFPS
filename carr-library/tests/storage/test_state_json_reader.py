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



@pytest.mark.parametrize('raw', [b'\xffbroken', b'{"a":1,"a":2}', b'{"a":NaN}', b''])
def test_raw_snapshot_preserves_exact_invalid_bytes_for_explicit_cas_repair(tmp_path, raw):
    from hashlib import sha256
    target = tmp_path / 'reviewed-state.json'
    target.write_bytes(raw)
    captured, token = state.read_bytes_snapshot(target, max_bytes=8192)
    assert captured == raw and token == sha256(raw).hexdigest()
    with pytest.raises((ValueError, UnicodeError)):
        state.read_json_snapshot(target, max_bytes=8192)
    assert target.read_bytes() == raw


@pytest.mark.parametrize('kind', ['hardlink', 'over-budget'])
def test_raw_snapshot_does_not_bypass_existing_path_and_size_guards(tmp_path, monkeypatch, kind):
    selected = tmp_path / 'selected.json'
    selected.write_bytes(b'123456789')
    if kind == 'hardlink':
        os.link(selected, tmp_path / 'second-name.json')
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('Rejected state reached a payload open.'))
    with pytest.raises(ValueError):
        state.read_bytes_snapshot(selected, max_bytes=8)


# Optional initial absence is a separate observation, not a general exception
# fallback. These cases keep the original reader and real captured handles.
@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
def test_missing_snapshot_requires_explicit_boolean_opt_in(tmp_path, monkeypatch, reader_name):
    target = tmp_path / 'missing.json'
    guarded = []
    original = state.local_path
    def guard(path, **kwargs):
        guarded.append(kwargs.get('exists', False))
        return original(path, **kwargs)
    monkeypatch.setattr(state, 'local_path', guard)
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('An absent state reached a payload open.'))
    reader = getattr(state, reader_name)
    with pytest.raises(FileNotFoundError):
        reader(target)
    assert guarded == [True]
    guarded.clear()
    with pytest.raises(FileNotFoundError):
        reader(target, allow_missing=False)
    assert guarded == [True]
    guarded.clear()
    assert reader(target, allow_missing=True) == (None, None)
    assert guarded == [False] and not target.exists()


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
@pytest.mark.parametrize('value', [0, 1, None, 'true', [], {}],
                         ids=['zero', 'one', 'none', 'text', 'list', 'mapping'])
def test_missing_admission_rejects_non_boolean_before_path_access(tmp_path, monkeypatch, reader_name, value):
    def forbidden(*args, **kwargs):
        pytest.fail('A non-boolean missing-state option reached path admission.')
    monkeypatch.setattr(state, 'local_path', forbidden)
    monkeypatch.setattr(state, '_open_json_file', forbidden)
    with pytest.raises(ValueError, match='boolean'):
        getattr(state, reader_name)(tmp_path / 'state.json', allow_missing=value)


def test_opt_in_json_null_has_the_exact_byte_token_instead_of_missing_token(tmp_path):
    from hashlib import sha256
    target = tmp_path / 'null.json'
    raw = b' \r\nnull\t'
    target.write_bytes(raw)
    token = sha256(raw).hexdigest()
    assert state.read_bytes_snapshot(target, allow_missing=True) == (raw, token)
    assert state.read_json_snapshot(target, allow_missing=True) == (None, token)
    assert state.read_json_snapshot(target) == (None, token)
    assert target.read_bytes() == raw


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
@pytest.mark.parametrize('boundary', ['regular', 'open'])
def test_opt_in_positive_observation_does_not_hide_disappearance(
        tmp_path, monkeypatch, reader_name, boundary):
    target = tmp_path / 'admitted.json'
    target.write_bytes(b'{"accepted":true}')
    original_guard, original_regular, original_open = state.local_path, state.regular, state._open_json_file
    guarded, lost = [], []
    def guard(path, **kwargs):
        result = original_guard(path, **kwargs)
        guarded.append(kwargs.get('exists', False))
        return result
    def regular(path, **kwargs):
        # local_path has already finished the first full ancestor walk. The
        # initial non-following absence observation also precedes this call.
        if boundary == 'regular' and guarded and state.Path(path) == target and not lost:
            target.unlink()
            lost.append(boundary)
        return original_regular(path, **kwargs)
    def opened(path):
        if boundary == 'open':
            target.unlink()
            lost.append(boundary)
        return original_open(path)
    monkeypatch.setattr(state, 'local_path', guard)
    monkeypatch.setattr(state, 'regular', regular)
    monkeypatch.setattr(state, '_open_json_file', opened)
    with pytest.raises(FileNotFoundError):
        getattr(state, reader_name)(target, allow_missing=True)
    assert guarded == [False] and lost == [boundary]
    assert not target.exists()


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
def test_opt_in_inode_retry_cannot_default_after_the_file_disappears(tmp_path, monkeypatch, reader_name):
    target, replacement = tmp_path / 'state.json', tmp_path / 'replacement.json'
    target.write_bytes(b'{"old":true}')
    replacement.write_bytes(b'{"new":true}')
    original_guard, original_open = state.local_path, state._open_json_file
    guards, streams, reads = [], [], []
    def guard(path, **kwargs):
        guards.append(kwargs.get('exists', False))
        return original_guard(path, **kwargs)
    class Replaced:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def fileno(self): return self.stream.fileno()
        def read(self, maximum):
            reads.append(maximum)
            pytest.fail('A mismatched inode was read before a coherent retry.')
        def __exit__(self, *args):
            self.stream.close()
            target.unlink()  # Lose the admitted replacement before retry #2.
    def replaced(path):
        assert not streams
        os.replace(replacement, target)
        stream = original_open(path)
        streams.append(stream)
        return Replaced(stream)
    monkeypatch.setattr(state, 'local_path', guard)
    monkeypatch.setattr(state, '_open_json_file', replaced)
    with pytest.raises(FileNotFoundError):
        getattr(state, reader_name)(target, allow_missing=True)
    assert guards == [False, True]
    assert len(streams) == 1 and streams[0].closed and reads == []
    assert not target.exists()


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
def test_dangling_leaf_inserted_after_first_guard_is_not_absence(tmp_path, monkeypatch, reader_name):
    import stat
    target, missing = tmp_path / 'selected.json', tmp_path / 'unavailable.json'
    target.write_bytes(b'{"original":true}')
    original_guard, original_lstat = state.local_path, state.Path.lstat
    before = target.lstat()
    inserted = []
    class ReparseStat:
        # Windows symlink/reparse metadata is modeled explicitly: hosted
        # filesystem privilege is not evidence of accepting a dangling leaf.
        def __getattr__(self, name): return getattr(before, name)
        st_mode = stat.S_IFLNK | stat.S_IMODE(before.st_mode)
        st_file_attributes = getattr(before, 'st_file_attributes', 0) | 0x400
    def lstat(path, *args, **kwargs):
        if os.name == 'nt' and inserted and state.Path(path) == target:
            return ReparseStat()
        return original_lstat(path, *args, **kwargs)
    def guard(path, **kwargs):
        result = original_guard(path, **kwargs)
        if not inserted:
            target.unlink()
            if os.name != 'nt':
                target.symlink_to(missing)
            inserted.append(result)
        return result
    monkeypatch.setattr(state.Path, 'lstat', lstat)
    monkeypatch.setattr(state, 'local_path', guard)
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('A substituted dangling leaf reached a payload open.'))
    with pytest.raises(ValueError, match='without links'):
        getattr(state, reader_name)(target, allow_missing=True)
    assert inserted == [target] and not missing.exists()
    if os.name != 'nt':
        assert target.is_symlink() and os.path.lexists(target) and not target.exists()


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
@pytest.mark.parametrize('kind', ['permission', 'io'])
def test_initial_metadata_probe_propagates_non_enoent_errors_unchanged(
        tmp_path, monkeypatch, reader_name, kind):
    target = tmp_path / 'state.json'
    target.write_bytes(b'{"original":true}')
    failure = PermissionError('Metadata access denied.') if kind == 'permission' else OSError('Metadata I/O failed.')
    original_guard, original_lstat = state.local_path, state.Path.lstat
    guarded = []
    def guard(path, **kwargs):
        result = original_guard(path, **kwargs)
        guarded.append(result)
        return result
    def lstat(path, *args, **kwargs):
        if guarded and state.Path(path) == target:
            raise failure
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(state, 'local_path', guard)
    monkeypatch.setattr(state.Path, 'lstat', lstat)
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('A failed metadata probe reached a payload open.'))
    with pytest.raises(type(failure)) as caught:
        getattr(state, reader_name)(target, allow_missing=True)
    assert caught.value is failure and guarded == [target]
    assert target.read_bytes() == b'{"original":true}'


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
@pytest.mark.parametrize('kind', ['invalid-ancestor', 'hardlink'])
def test_allow_missing_keeps_real_ancestor_and_hardlink_guards(tmp_path, monkeypatch, reader_name, kind):
    target = tmp_path / 'state.json'
    if kind == 'invalid-ancestor':
        parent = tmp_path / 'not-a-directory'
        parent.write_bytes(b'retained parent')
        target = parent / 'missing.json'
    else:
        target.write_bytes(b'{"original":true}')
        os.link(target, tmp_path / 'other-name.json')
    monkeypatch.setattr(state, '_open_json_file',
                        lambda path: pytest.fail('Rejected state reached a payload open.'))
    with pytest.raises(ValueError, match='regular local'):
        getattr(state, reader_name)(target, allow_missing=True)
    if kind == 'invalid-ancestor':
        assert parent.read_bytes() == b'retained parent'
    else:
        assert target.read_bytes() == b'{"original":true}' and target.stat().st_nlink == 2


@pytest.mark.parametrize('raw', [
    b'{"a":1,"a":2}', b'{"a":NaN}', b'Infinity', b'-Infinity', b'{invalid', b'\xff',
], ids=['duplicate', 'nan', 'infinity', 'negative-infinity', 'malformed', 'invalid-utf8'])
def test_allow_missing_json_keeps_strict_parsing_and_closes_actual_handle(tmp_path, monkeypatch, raw):
    target = tmp_path / 'state.json'
    target.write_bytes(raw)
    original_open, streams = state._open_json_file, []
    def capture(path):
        stream = original_open(path)
        streams.append(stream)
        return stream
    monkeypatch.setattr(state, '_open_json_file', capture)
    with pytest.raises(ValueError):
        state.read_json_snapshot(target, allow_missing=True)
    assert len(streams) == 1 and streams[0].closed
    assert target.read_bytes() == raw


@pytest.mark.parametrize('reader_name', ['read_bytes_snapshot', 'read_json_snapshot'])
@pytest.mark.parametrize('boundary', ['before-open', 'opened-size', 'read-growth'])
def test_allow_missing_keeps_all_byte_budget_boundaries(tmp_path, monkeypatch, reader_name, boundary):
    target = tmp_path / 'state.json'
    larger = b'{"larger":"actual growing file"}'
    target.write_bytes(larger if boundary == 'before-open' else b'{}')
    original_open, streams, reads = state._open_json_file, [], []
    class Observed:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, maximum):
            reads.append(maximum)
            if boundary == 'read-growth':
                target.write_bytes(larger)
            return self.stream.read(maximum)
    def capture(path):
        if boundary == 'opened-size':
            target.write_bytes(larger)
        stream = original_open(path)
        streams.append(stream)
        return Observed(stream)
    monkeypatch.setattr(state, '_open_json_file', capture)
    with pytest.raises(ValueError, match='budget'):
        getattr(state, reader_name)(target, max_bytes=8, allow_missing=True)
    assert len(streams) == (0 if boundary == 'before-open' else 1)
    assert all(stream.closed for stream in streams)
    assert reads == ([9] if boundary == 'read-growth' else [])
    assert target.read_bytes() == larger
