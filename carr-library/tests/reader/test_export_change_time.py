"""Windows metadata, bounded byte verification and real edits at approval.

The unmocked file test runs on every platform. Native metadata alone can
collide; unchanged bytes must also participate in overwrite approval.
"""
import ctypes
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.research import export_file


def rewrite_preserving_mtime(target, replacement):
    before = target.stat()
    assert len(replacement) == before.st_size
    with target.open('r+b') as output:
        output.write(replacement)
        output.flush()
        os.fsync(output.fileno())
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)


@pytest.mark.parametrize('binary', [False, True])
def test_actual_same_length_edit_with_restored_mtime_is_not_overwritten(tmp_path, binary):
    target = tmp_path / 'selected'
    private = tmp_path / 'private'
    target.write_bytes(b'Original independent work')
    approval = export_file.capture_export_destination(target, private_root=private)
    if os.name == 'nt':
        assert len(approval.state) == 8 and approval.state[-2] > 0
    assert isinstance(approval.state[-1], bytes) and len(approval.state[-1]) == 32
    replacement = b'Modified independent work'
    rewrite_preserving_mtime(target, replacement)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == replacement
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_actual_edit_with_colliding_native_metadata_is_not_overwritten(tmp_path, monkeypatch, binary):
    target = tmp_path / 'selected'
    private = tmp_path / 'private'
    target.write_bytes(b'Original independent work')
    creation_time = target.stat().st_ctime_ns
    actual_state = export_file._file_state
    monkeypatch.setattr(export_file, '_file_state',
        lambda info: (*actual_state(info)[:-1], creation_time))
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)
    # Actual NTFS installed acceptance exposed equal ChangeTime for a rapid
    # same-length edit. The file bytes must still participate in approval.
    monkeypatch.setattr(export_file, '_windows_change_time', lambda _path: 123456789)
    approval = export_file.capture_export_destination(target, private_root=private)
    replacement = b'Modified independent work'
    rewrite_preserving_mtime(target, replacement)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == replacement
    assert not list(tmp_path.glob('.research-export-*'))


def test_large_existing_file_requires_new_filename_without_reading_it(tmp_path, monkeypatch):
    target = tmp_path / 'selected'
    original = b'Independent existing work'
    target.write_bytes(original)
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', 8)
    file_os = SimpleNamespace(**vars(os))
    file_os.read = Mock(side_effect=AssertionError('No content read is allowed above the bound'))
    monkeypatch.setattr(export_file, 'os', file_os)
    with pytest.raises(ValueError, match='too large.*Choose a new filename'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    file_os.read.assert_not_called()
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('failure', ['deadline', 'read_error'])
def test_unverifiable_content_closes_handle_and_keeps_existing_file(tmp_path, monkeypatch, failure):
    target = tmp_path / 'selected'
    original = b'Independent existing work'
    target.write_bytes(original)
    opened = []
    file_os = SimpleNamespace(**vars(os))
    real_open = os.open

    def track_open(*args):
        descriptor = real_open(*args)
        opened.append(descriptor)
        return descriptor

    file_os.open = track_open
    if failure == 'deadline':
        times = iter((0.0, 1.0))
        monkeypatch.setattr(export_file, 'monotonic', lambda: next(times))
        expected, message = ValueError, 'quickly enough.*Choose a new filename'
    else:
        file_os.read = Mock(side_effect=OSError('Read unavailable'))
        expected, message = OSError, 'Read unavailable'
    monkeypatch.setattr(export_file, 'os', file_os)
    with pytest.raises(expected, match=message):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_size_bound_never_prevents_creation_at_a_new_filename(tmp_path, monkeypatch, binary):
    target = tmp_path / 'new-output'
    private = tmp_path / 'private'
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', 4)
    approval = export_file.capture_export_destination(target, private_root=private)
    writer = export_file.write_binary_export if binary else export_file.write_export
    writer(target, b'Complete larger export' if binary else 'Complete larger export',
           private_root=private, approval=approval)
    assert target.read_bytes() == b'Complete larger export'
    assert not list(tmp_path.glob('.research-export-*'))


def test_file_growing_during_content_check_is_preserved(tmp_path, monkeypatch):
    target = tmp_path / 'selected'
    original = b'Independent existing work'
    target.write_bytes(original)
    file_os = SimpleNamespace(**vars(os))
    real_read = os.read
    changed = False

    def grow_after_read(*args):
        nonlocal changed
        block = real_read(*args)
        if not changed:
            changed = True
            with target.open('ab') as stream:
                stream.write(b' plus a new paragraph')
        return block

    file_os.read = grow_after_read
    monkeypatch.setattr(export_file, 'os', file_os)
    with pytest.raises(ValueError, match='destination changed'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    assert target.read_bytes() == original + b' plus a new paragraph'
    assert not list(tmp_path.glob('.research-export-*'))


class NativeFunction:
    def __init__(self, callback):
        self.callback, self.calls = callback, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


@pytest.fixture
def native_api(monkeypatch):
    native = SimpleNamespace(handle=0x100000123 if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x123,
                             query_ok=1, close_ok=1, attributes=0x80, change_time=123456789012345678)

    def query(_handle, _kind, pointer, _size):
        info = ctypes.cast(pointer, ctypes.POINTER(export_file._WindowsFileBasicInfo)).contents
        info.ChangeTime = native.change_time
        info.FileAttributes = native.attributes
        return native.query_ok

    native.create = NativeFunction(lambda *args: native.handle)
    native.query = NativeFunction(query)
    native.close = NativeFunction(lambda *args: native.close_ok)
    native.load = Mock(return_value=SimpleNamespace(CreateFileW=native.create,
        GetFileInformationByHandleEx=native.query, CloseHandle=native.close))
    export_file._windows_file_api.cache_clear()
    monkeypatch.setattr(ctypes, 'WinDLL', native.load, raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    try:
        yield native
    finally:
        export_file._windows_file_api.cache_clear()


def test_native_query_uses_metadata_access_and_pointer_width_handles(tmp_path, native_api):
    target = tmp_path / 'selected'
    assert export_file._windows_change_time(target) == native_api.change_time
    native_api.load.assert_called_once_with('kernel32', use_last_error=True)
    assert native_api.create.calls == [(str(target), 0x80, 7, None, 3, 0x00200000, None)]
    assert native_api.create.restype is ctypes.c_void_p
    assert native_api.query.argtypes[0] is ctypes.c_void_p
    assert native_api.close.argtypes == [ctypes.c_void_p]
    handle, kind, _buffer, size = native_api.query.calls[0]
    assert (handle, kind, size) == (native_api.handle, 0, 40)
    assert ctypes.sizeof(export_file._WindowsFileBasicInfo) == 40
    assert export_file._WindowsFileBasicInfo.ChangeTime.offset == 24
    assert export_file._WindowsFileBasicInfo.FileAttributes.offset == 32
    assert native_api.close.calls == [(native_api.handle,)]


def test_failed_native_open_does_not_query_or_close_an_invalid_handle(tmp_path, native_api):
    native_api.handle = ctypes.c_void_p(-1).value
    with pytest.raises(OSError, match='CreateFileW.*Windows error 5'):
        export_file._windows_change_time(tmp_path / 'selected')
    assert not native_api.query.calls and not native_api.close.calls


def test_failed_native_query_closes_its_handle_without_falling_back(tmp_path, native_api):
    native_api.query_ok = 0
    with pytest.raises(OSError, match='GetFileInformationByHandleEx.*Windows error 5'):
        export_file._windows_change_time(tmp_path / 'selected')
    assert native_api.close.calls == [(native_api.handle,)]


@pytest.mark.parametrize('attributes', [0x400, 0x10])
def test_native_reparse_point_or_directory_is_rejected_and_closed(tmp_path, native_api, attributes):
    native_api.attributes = attributes
    with pytest.raises(ValueError, match='regular file without links or reparse points'):
        export_file._windows_change_time(tmp_path / 'selected')
    assert native_api.close.calls == [(native_api.handle,)]


@pytest.mark.parametrize('change_time', [0, -1])
def test_missing_native_change_time_is_rejected_and_closed(tmp_path, native_api, change_time):
    native_api.change_time = change_time
    with pytest.raises(ValueError, match='usable Windows change time'):
        export_file._windows_change_time(tmp_path / 'selected')
    assert native_api.close.calls == [(native_api.handle,)]


def test_native_close_failure_does_not_return_an_approval(tmp_path, native_api):
    native_api.close_ok = 0
    with pytest.raises(OSError, match='CloseHandle.*Windows error 5'):
        export_file._windows_change_time(tmp_path / 'selected')
    assert native_api.close.calls == [(native_api.handle,)]


@pytest.mark.parametrize('change', ['modified', 'replaced'])
def test_destination_stat_identity_is_rechecked_around_native_query(tmp_path, monkeypatch, change):
    target = tmp_path / 'selected'
    target.write_bytes(b'Original work')
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)

    def query(path):
        if change == 'replaced':
            replacement = tmp_path / 'replacement'
            replacement.write_bytes(b'Independent work')
            replacement.replace(path)
        else:
            path.write_bytes(b'Independent work')
        return 12345678

    monkeypatch.setattr(export_file, '_windows_change_time', query)
    with pytest.raises(ValueError, match='destination changed'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    assert target.read_bytes() == b'Independent work'


@pytest.mark.parametrize('binary', [False, True])
def test_native_query_error_preserves_existing_file_without_staging(tmp_path, monkeypatch, binary):
    target = tmp_path / 'selected'
    target.write_bytes(b'Existing independent work')
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)
    monkeypatch.setattr(export_file, '_windows_change_time',
                        Mock(side_effect=OSError('Native metadata unavailable')))
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(OSError, match='Native metadata unavailable'):
        writer(target, b'new export' if binary else 'new export', private_root=tmp_path / 'private')
    assert target.read_bytes() == b'Existing independent work'
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_windows_creation_time_model_requires_native_change_time(tmp_path, monkeypatch, binary):
    target = tmp_path / 'selected'
    private = tmp_path / 'private'
    target.write_bytes(b'Original independent work')
    creation_time = target.stat().st_ctime_ns
    actual_state = export_file._file_state
    # Python 3.11 on Windows exposes creation time as ctime. Keep that field
    # constant over real in-place writes, while modeling the native change time.
    monkeypatch.setattr(export_file, '_file_state',
        lambda info: (*actual_state(info)[:-1], creation_time))
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)
    changes = iter((100, 100, 200))
    monkeypatch.setattr(export_file, '_windows_change_time', lambda _path: next(changes), raising=False)
    approval = export_file.capture_export_destination(target, private_root=private)
    replacement = b'Modified independent work'
    rewrite_preserving_mtime(target, replacement)
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == replacement
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('binary', [False, True])
def test_windows_change_time_rejects_same_bytes_restored_after_approval(tmp_path, monkeypatch, binary):
    """A content ABA is visible only in native metadata in this Windows model."""
    target, private = tmp_path / 'selected', tmp_path / 'private'
    original = b'Original independent work'
    target.write_bytes(original)
    creation_time = target.stat().st_ctime_ns
    file_state = export_file._file_state
    monkeypatch.setattr(export_file, '_file_state',
        lambda info: (*file_state(info)[:-1], creation_time))
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)
    change_time = [123456789]
    monkeypatch.setattr(export_file, '_windows_change_time', lambda _path: change_time[0])
    approval = export_file.capture_export_destination(target, private_root=private)
    rewrite_preserving_mtime(target, b'Modified independent work')
    rewrite_preserving_mtime(target, original)
    change_time[0] += 1
    assert target.read_bytes() == original
    observed = export_file.capture_export_destination(target, private_root=private)
    assert observed.state[:-2] == approval.state[:-2]
    assert observed.state[-1] == approval.state[-1]
    assert observed.state[-2] != approval.state[-2]
    writer = export_file.write_binary_export if binary else export_file.write_export
    with pytest.raises(ValueError, match='destination changed'):
        writer(target, b'new export' if binary else 'new export',
               private_root=private, approval=approval)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ['capture', 'before_stage', 'before_commit'])
def test_native_change_time_rechecks_an_actual_byte_aba_during_hashing(
        tmp_path, monkeypatch, phase):
    target, private = tmp_path / 'selected', tmp_path / 'private'
    original = b'Original independent work'
    target.write_bytes(original)
    creation_time = target.stat().st_ctime_ns
    actual_state = export_file._file_state
    monkeypatch.setattr(export_file, '_file_state',
        lambda info: (*actual_state(info)[:-1], creation_time))
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = 'nt'
    monkeypatch.setattr(export_file, 'os', windows_os)
    change_time = [123456789]
    monkeypatch.setattr(export_file, '_windows_change_time', lambda _path: change_time[0])
    approval = (None if phase == 'capture' else
                export_file.capture_export_destination(target, private_root=private))
    actual_read = os.read
    reads = [0]
    trigger = 2 if phase == 'before_commit' else 1

    def edit_and_restore_after_read(descriptor, length):
        block = actual_read(descriptor, length)
        if block:
            reads[0] += 1
            if reads[0] == trigger:
                rewrite_preserving_mtime(target, b'Modified independent work')
                rewrite_preserving_mtime(target, original)
                change_time[0] += 1
        return block

    windows_os.read = edit_and_restore_after_read
    with pytest.raises(ValueError, match='destination changed'):
        if phase == 'capture':
            export_file.capture_export_destination(target, private_root=private)
        else:
            export_file.write_binary_export(target, b'new export',
                private_root=private, approval=approval)
    assert reads[0] == trigger
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))
