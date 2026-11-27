"""Actual file-handle and child-process checks for concurrently published state."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.audio import jobs as state


REPLACE_ONCE = r'''
import json, os, pathlib, sys
target = pathlib.Path(sys.argv[1])
staged = target.with_suffix('.next')
root = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root))
from carr_library.audio import jobs
assert pathlib.Path(jobs.__file__).resolve().is_relative_to(root / 'carr_library')
try:
    if sys.argv[3] == 'legacy':
        with staged.open('xb') as stream:
            stream.write(b'{"done":1}')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
    else:
        jobs.atomic_json(target, {'done': 1})
except OSError as error:
    print(json.dumps({'published': False, 'winerror': getattr(error, 'winerror', None)}))
    raise SystemExit(2)
else:
    print(json.dumps({'published': True}))
finally:
    staged.unlink(missing_ok=True)
'''


def replace_in_child(path, *, legacy=False):
    process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c',
                              REPLACE_ONCE, str(path), str(Path(state.__file__).resolve().parents[2]),
                              'legacy' if legacy else 'production'], capture_output=True,
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
            if os.name == 'nt':
                # FILE_SHARE_DELETE alone cannot repair MoveFileExW semantics.
                # The actual production writer must use FileRenameInfoEx.
                legacy = replace_in_child(path, legacy=True)
                assert legacy == {'published': False, 'winerror': 5}
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


@pytest.mark.parametrize('kind', ['other_directory', 'source_link', 'source_alias',
                                 'target_link', 'target_alias', 'target_directory'])
def test_json_publication_rejects_unsafe_paths_without_changing_any_bytes(tmp_path, kind):
    source, target, outside = (tmp_path / name for name in ('stage.json', 'state.json', 'outside.json'))
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    outside.write_bytes(b'{"outside":1}')
    if kind == 'other_directory':
        directory = tmp_path / 'other'
        directory.mkdir()
        source = source.rename(directory / source.name)
    elif kind == 'source_link':
        source.unlink()
        source.symlink_to(outside)
    elif kind == 'source_alias':
        os.link(source, tmp_path / 'source-alias.json')
    elif kind == 'target_link':
        target.unlink()
        target.symlink_to(outside)
    elif kind == 'target_alias':
        os.link(target, tmp_path / 'target-alias.json')
    else:
        target.unlink()
        target.mkdir()
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with pytest.raises(ValueError):
        state.replace_json_file(source, target)
    assert {path: path.read_bytes() for path in before} == before
    assert source.exists()


def _native_publisher(monkeypatch, source, target, action, *, error=5):
    """Exercise real descriptor ownership and native wire layout with a fake syscall."""
    import ctypes
    from types import SimpleNamespace
    source_identity = state._replacement_identity(source.stat())
    target_identity = state._replacement_identity(target.stat())
    directory = target.parent.stat()
    directory_identity = (directory.st_dev, directory.st_ino)
    descriptors, native_calls, fallback_calls = [], [], []
    original_replace = os.replace
    original_descriptor = state._windows_descriptor
    native_kernel = ctypes.WinDLL('kernel32', use_last_error=True) if os.name == 'nt' else None
    if os.name == 'nt':
        import msvcrt
        get_handle = msvcrt.get_osfhandle
    else:
        get_handle = lambda fd: fd

    def open_descriptor(path, access, *, directory=False, share=0x7):
        fd = (original_descriptor(path, access, directory=directory, share=share)
              if os.name == 'nt' else os.open(path, os.O_RDONLY))
        descriptors.append((fd, path, access, directory, share, get_handle(fd)))
        return fd

    def error_value(code):
        exception = OSError(code, 'synthetic native rename failure')
        exception.winerror = code
        return exception

    class NativeRename:
        def __call__(self, handle, information_class, pointer, size):
            request = pointer._obj
            encoded = ctypes.string_at(ctypes.addressof(request) + type(request).FileName.offset,
                                       request.FileNameLength)
            native_calls.append((handle, information_class, request.Flags,
                                 request.RootDirectory, encoded.decode('utf-16-le'), size))
            return action(original_replace)

    def fallback(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return original_replace(*args, **kwargs)

    def unchanged():
        info = target.parent.stat()
        if ((info.st_dev, info.st_ino) != directory_identity
                or state._replacement_identity(source.stat()) != source_identity
                or state._replacement_identity(target.stat()) != target_identity):
            raise ValueError('JSON publication paths changed before replacement.')

    monkeypatch.setattr(state, '_windows_descriptor', open_descriptor)
    if os.name != 'nt':
        monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(get_osfhandle=get_handle))
    class Kernel:
        SetFileInformationByHandle = NativeRename()
        def __getattr__(self, name):
            return getattr(native_kernel, name)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: Kernel(), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: error, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', error_value, raising=False)
    monkeypatch.setattr(state.os, 'replace', fallback)

    def publish():
        try:
            state._replace_json_windows(source, target, source_identity, directory_identity, unchanged)
        finally:
            for fd, *_ in descriptors:
                with pytest.raises(OSError):
                    os.fstat(fd)
    return publish, descriptors, native_calls, fallback_calls


def test_native_json_request_pins_directory_and_uses_utf16_posix_rename(tmp_path, monkeypatch):
    source, target = tmp_path / 'stage.json', tmp_path / 'Стан-🎧.json'
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    def action(rename):
        rename(source, target)
        return True
    publish, descriptors, calls, fallback = _native_publisher(monkeypatch, source, target, action)
    publish()
    assert target.read_bytes() == b'{"new":1}' and not source.exists()
    assert len(calls) == 1 and not fallback
    assert descriptors[0][2:5] == (0xA0, True, 0x3)
    assert descriptors[1][2:5] == (0x80010000, False, 0x7)
    assert calls[0][:5] == (descriptors[1][5], 22, 0x3, descriptors[0][5], target.name)


@pytest.mark.parametrize('error', [5, 32, 33, 112])
def test_native_json_failure_never_unlinks_or_falls_back(tmp_path, monkeypatch, error):
    source, target = tmp_path / 'stage.json', tmp_path / 'state.json'
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    publish, _, calls, fallback = _native_publisher(monkeypatch, source, target,
                                                   lambda rename: False, error=error)
    with pytest.raises(OSError) as failure:
        publish()
    assert failure.value.winerror == error and len(calls) == 1 and not fallback
    assert source.read_bytes() == b'{"new":1}' and target.read_bytes() == b'{"old":1}'


@pytest.mark.parametrize('error', [1, 50, 87, 120])
def test_unsupported_native_json_uses_only_unchanged_atomic_fallback(tmp_path, monkeypatch, error):
    source, target = tmp_path / 'stage.json', tmp_path / 'state.json'
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    publish, _, calls, fallback = _native_publisher(monkeypatch, source, target,
                                                   lambda rename: False, error=error)
    publish()
    assert len(calls) == len(fallback) == 1
    assert not source.exists() and target.read_bytes() == b'{"new":1}'


@pytest.mark.parametrize('changed', ['source', 'destination'])
def test_unsupported_native_json_does_not_overwrite_changed_paths(tmp_path, monkeypatch, changed):
    source, target = tmp_path / 'stage.json', tmp_path / 'state.json'
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    selected = source if changed == 'source' else target
    def action(rename):
        selected.write_bytes(b'{"concurrent":1}')
        return False
    publish, _, calls, fallback = _native_publisher(monkeypatch, source, target, action, error=50)
    with pytest.raises(ValueError, match='changed'):
        publish()
    assert len(calls) == 1 and not fallback
    assert source.exists() and selected.read_bytes() == b'{"concurrent":1}'


@pytest.mark.parametrize('changed', ['source', 'directory'])
def test_native_json_refuses_replaced_open_identity_before_rename(tmp_path, monkeypatch, changed):
    parent = tmp_path / 'state'
    parent.mkdir()
    source, target = parent / 'stage.json', parent / 'state.json'
    source.write_bytes(b'{"new":1}')
    target.write_bytes(b'{"old":1}')
    publish, _, calls, fallback = _native_publisher(monkeypatch, source, target,
                                                   lambda rename: pytest.fail('Unsafe native rename'))
    original = state._windows_descriptor
    def race(path, access, **kwargs):
        if changed == 'directory' and kwargs.get('directory'):
            parent.rename(tmp_path / 'original')
            parent.mkdir()
        elif changed == 'source' and path == source:
            source.write_bytes(b'{"concurrent":1}')
        return original(path, access, **kwargs)
    monkeypatch.setattr(state, '_windows_descriptor', race)
    with pytest.raises(ValueError, match='changed while opening'):
        publish()
    assert not calls and not fallback


def test_atomic_json_failed_publication_preserves_previous_and_removes_only_own_stage(tmp_path, monkeypatch):
    target, unrelated = tmp_path / 'state.json', tmp_path / 'unrelated.json'
    target.write_bytes(b'{"old":1}')
    unrelated.write_bytes(b'{"keep":1}')
    def fail(source, destination):
        assert Path(destination) == target
        assert json.loads(Path(source).read_bytes()) == {'new': 1}
        raise OSError('synthetic native failure')
    monkeypatch.setattr(state, 'replace_json_file', fail)
    with pytest.raises(OSError, match='native failure'):
        state.atomic_json(target, {'new': 1})
    assert target.read_bytes() == b'{"old":1}' and unrelated.read_bytes() == b'{"keep":1}'
    assert set(tmp_path.iterdir()) == {target, unrelated}
