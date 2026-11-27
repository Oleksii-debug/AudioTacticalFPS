"""Canonical port of the real-file export regressions from 9c5b81b.

The unrelated alternate bibliography stack is not included.
"""
import os
from pathlib import Path

import pytest
from carr_library.research import export_file

@pytest.fixture(params=('text', 'binary'))
def export(request, tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    target = tmp_path / ('result.txt' if request.param == 'text' else 'result.bin')
    payload = 'Quoted text\r\nУкраїнський рядок.' if request.param == 'text' else b'\x00\x01binary\r\n'
    writer = export_file.write_export if request.param == 'text' else export_file.write_binary_export
    expected = payload.encode('utf-8') if isinstance(payload, str) else payload

    def publish(**options):
        return writer(target, payload, private_root=private, **options)

    return target, expected, publish


def test_create_only_export_publishes_exact_bytes_without_staging_residue(export):
    target, expected, publish = export
    receipt = publish(require_absent=True)
    assert receipt.path == target and receipt.cleanup_warning is None
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('broken_logger', [False, True])
def test_saved_export_survives_persistent_staging_cleanup_failure(export, monkeypatch, caplog, broken_logger):
    """Reuse 32a1029's real-file fault case with the Windows rename successor."""
    target, expected, publish = export
    unlink = os.unlink

    def locked_stage(path, *args, **kwargs):
        if Path(path).name.startswith('.research-export-') and target.exists():
            assert target.read_bytes() == expected
            raise PermissionError('Synthetic persistent cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', locked_stage)
    if broken_logger:
        def unavailable_log(*args):
            raise OSError('Synthetic unavailable log sink')
        monkeypatch.setattr(export_file._logger, 'warning', unavailable_log)
    try:
        receipt = publish(require_absent=True)
        assert receipt.path == target and target.read_bytes() == expected
        if os.name == 'nt':
            # Windows publishes in one move; there is no cleanup link to fail.
            assert receipt.cleanup_warning is None
            assert not list(target.parent.glob('.research-export-*'))
        else:
            assert 'file is saved' in receipt.cleanup_warning
            assert 'does not need to be repeated' in receipt.cleanup_warning
            if not broken_logger:
                assert receipt.cleanup_warning in caplog.text
            stage, = target.parent.glob('.research-export-*')
            assert os.path.samefile(stage, target)
    finally:
        for stage in target.parent.glob('.research-export-*'):
            unlink(stage)
    assert target.stat().st_nlink == 1
    assert target.read_bytes() == expected


def test_create_only_export_rejects_existing_file_without_touching_it(export):
    target, _, publish = export
    target.write_bytes(b'Previous work must survive')
    with pytest.raises(FileExistsError):
        publish(require_absent=True)
    assert target.read_bytes() == b'Previous work must survive'
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('explicit_policy', (False, True))
def test_file_created_after_final_destination_check_is_never_replaced(
        export, monkeypatch, explicit_policy):
    target, _, publish = export
    inspect = export_file._destination
    checks = 0

    def competing_writer(*args):
        nonlocal checks
        state = inspect(*args)
        checks += 1
        if checks == 2:
            # A real file appears after the writer's last destination snapshot,
            # so another preflight check cannot provide no-clobber semantics.
            target.write_bytes(b'Concurrent writer owns this file')
        return state

    monkeypatch.setattr(export_file, '_destination', competing_writer)
    with pytest.raises(FileExistsError):
        publish(**({'require_absent': True} if explicit_policy else {}))
    assert target.read_bytes() == b'Concurrent writer owns this file'
    assert not list(target.parent.glob('.research-export-*'))


def test_cancel_after_staging_leaves_no_destination_or_temporary(export):
    target, _, publish = export
    with pytest.raises(TimeoutError, match='cancelled'):
        publish(require_absent=True,
                cancelled=lambda: bool(list(target.parent.glob('.research-export-*'))))
    assert not target.exists()
    assert not list(target.parent.glob('.research-export-*'))


def test_explicit_existing_replacement_still_uses_supported_export_path(export):
    target, expected, publish = export
    target.write_bytes(b'Explicitly replaced old export')
    publish(require_absent=False)
    assert target.read_bytes() == expected
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('existing', (False, True))
def test_captured_destination_can_publish_only_its_unchanged_state(export, existing):
    target, expected, publish = export
    if existing:
        target.write_bytes(b'Explicitly selected existing file')
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    assert approval.require_absent is not existing

    publish(approval=approval)

    assert target.read_bytes() == expected
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('change', ('created', 'modified', 'replaced', 'removed'))
def test_captured_destination_rejects_changes_before_writer_starts(export, change):
    target, _, publish = export
    if change != 'created':
        target.write_bytes(b'Original approved file')
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    changed = b'Another writer owns these newer bytes'
    if change == 'removed':
        target.unlink()
    elif change == 'replaced':
        replacement = target.parent / 'replacement.tmp'
        replacement.write_bytes(changed)
        replacement.replace(target)
    else:
        target.write_bytes(changed)

    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)

    if change == 'removed':
        assert not target.exists()
    else:
        assert target.read_bytes() == changed
    assert not list(target.parent.glob('.research-export-*'))


def test_captured_absence_forces_atomic_no_clobber_even_without_policy_flag(export, monkeypatch):
    target, _, publish = export
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    inspect = export_file._destination
    checks = 0

    def create_after_final_check(*args):
        nonlocal checks
        state = inspect(*args)
        checks += 1
        if checks == 2:
            target.write_bytes(b'Late writer at the publication boundary')
        return state

    monkeypatch.setattr(export_file, '_destination', create_after_final_check)
    with pytest.raises(FileExistsError):
        publish(approval=approval)
    assert target.read_bytes() == b'Late writer at the publication boundary'
    assert not list(target.parent.glob('.research-export-*'))


def test_destination_approval_cannot_be_reused_for_another_path(export):
    target, _, publish = export
    approval = export_file.capture_export_destination(target.parent / 'another.txt',
        private_root=target.parent / 'private')
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert not target.exists()
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('when', ('queued', 'staging'))
def test_existing_approval_rejects_same_size_edit_with_unchanged_observed_times(
        export, monkeypatch, when):
    target, _, publish = export
    target.write_bytes(b'Original selected content')
    original = target.stat()
    if os.name != 'nt':
        # Python's Windows st_ctime is creation time. Keep that observation on
        # POSIX too, while inode, size, mode, links and mtime remain real. This
        # makes the regression exercise the Windows failure on every runner.
        actual_state = export_file._file_state
        monkeypatch.setattr(export_file, '_file_state',
                            lambda info: actual_state(info)[:-1] + (original.st_ctime_ns,))
    approval = export_file.capture_export_destination(
        target, private_root=target.parent / 'private')
    newer = b'Independently saved data!'
    assert len(newer) == original.st_size

    def modify():
        target.write_bytes(newer)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert export_file._file_state(target.stat()) == approval.state

    if when == 'queued':
        modify()
    else:
        actual_sync = os.fsync

        def sync_then_modify(fd):
            actual_sync(fd)
            modify()

        monkeypatch.setattr(os, 'fsync', sync_then_modify)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert target.read_bytes() == newer
    assert not list(target.parent.glob('.research-export-*'))


def test_existing_approval_rejects_oversized_target_without_reading(export, monkeypatch):
    target, _, _ = export
    previous = b'Unchanged existing export'
    target.write_bytes(previous)
    monkeypatch.setattr(export_file, 'MAX_APPROVAL_BYTES', len(previous) - 1)

    def forbidden_open(*args, **kwargs):
        pytest.fail('An oversized overwrite candidate was read.')

    with monkeypatch.context() as reading:
        reading.setattr(os, 'open', forbidden_open)
        with pytest.raises(ValueError, match='size limit.*new filename'):
            export_file.capture_export_destination(
                target, private_root=target.parent / 'private')
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


def test_existing_target_moved_aside_during_final_hash_is_not_replaced(export, monkeypatch):
    target, _, publish = export
    previous = b'Original selected content'
    newer = b'Independent replacement'
    target.write_bytes(previous)
    approval = export_file.capture_export_destination(
        target, private_root=target.parent / 'private')
    digest = export_file._existing_digest
    reads = 0
    saved = target.with_name('saved-original')

    def move_after_read(*args):
        nonlocal reads
        result = digest(*args)
        reads += 1
        if reads == 2:
            target.rename(saved)
            target.write_bytes(newer)
        return result

    monkeypatch.setattr(export_file, '_existing_digest', move_after_read)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert saved.read_bytes() == previous
    assert target.read_bytes() == newer
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('reason', ('cancelled', 'deadline'))
@pytest.mark.parametrize('stage', ('approval', 'queued', 'staging'))
def test_existing_file_hash_can_stop_between_bounded_reads(
        export, monkeypatch, reason, stage):
    target, _, publish = export
    previous = b'Previous export\r\n' * 100_000
    target.write_bytes(previous)
    approval = export_file.capture_export_destination(
        target, private_root=target.parent / 'private')
    stopped = False
    clock = 0.0
    inspected = []
    opened = 0
    actual_open = os.fdopen

    class ObservedFile:
        def __init__(self, stream, interrupt):
            self.stream, self.interrupt = stream, interrupt

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            nonlocal stopped, clock
            assert 0 < size <= 1024 * 1024
            data = self.stream.read(size)
            if self.interrupt:
                inspected.append(len(data))
                stopped = reason == 'cancelled'
                clock = export_file.APPROVAL_TIMEOUT + 1
            return data

    def observed_open(fd, *args, **kwargs):
        nonlocal opened
        opened += 1
        return ObservedFile(actual_open(fd, *args, **kwargs),
                            opened == (2 if stage == 'staging' else 1))

    monkeypatch.setattr(os, 'fdopen', observed_open)
    monkeypatch.setattr(export_file, 'monotonic', lambda: clock)
    message = 'cancelled' if reason == 'cancelled' else 'too long.*new filename'
    with pytest.raises(TimeoutError, match=message):
        if stage == 'approval':
            export_file.capture_export_destination(
                target, private_root=target.parent / 'private', cancelled=lambda: stopped)
        else:
            publish(approval=approval, cancelled=lambda: stopped)
    assert inspected == [1024 * 1024]
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


