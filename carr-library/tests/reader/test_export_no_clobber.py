"""Real export publication must preserve files created after caller preflight."""
import os
from pathlib import Path
import subprocess
import sys

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
        writer(target, payload, private_root=private, **options)

    return target, expected, publish


def test_create_only_export_publishes_exact_bytes_without_staging_residue(export):
    target, expected, publish = export
    publish(require_absent=True)
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))


def test_create_only_export_rejects_existing_file_without_touching_it(export):
    target, _, publish = export
    target.write_bytes(b'Previous work must survive')
    with pytest.raises(FileExistsError):
        publish(require_absent=True)
    assert target.read_bytes() == b'Previous work must survive'
    assert not list(target.parent.glob('.research-export-*'))


def test_file_created_after_final_destination_check_is_never_replaced(export, monkeypatch):
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
        publish(require_absent=True)
    assert target.read_bytes() == b'Concurrent writer owns this file'
    assert not list(target.parent.glob('.research-export-*'))


def test_default_new_destination_never_replaces_a_last_moment_competing_file(export, monkeypatch):
    target, _, publish = export
    inspect = export_file._destination
    checks = 0

    def create_after_final_check(*args):
        nonlocal checks
        state = inspect(*args)
        checks += 1
        if checks == 2:
            target.write_bytes(b'Concurrent writer after the final default-policy check')
        return state

    monkeypatch.setattr(export_file, '_destination', create_after_final_check)
    with pytest.raises(FileExistsError):
        publish()
    assert target.read_bytes() == b'Concurrent writer after the final default-policy check'
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


def test_published_export_survives_transient_staging_cleanup_failure(export, monkeypatch):
    target, expected, publish = export
    unlink = os.unlink
    failures = []

    def fail_first_stage_unlink(path, *args, **kwargs):
        if Path(path).name.startswith('.research-export-') and not failures:
            assert target.read_bytes() == expected
            failures.append(path)
            raise PermissionError('Synthetic transient staging lock')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', fail_first_stage_unlink)
    publish(require_absent=True)

    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))
    # Windows renames without replacement and has no post-publication unlink.
    # The portable hard-link path retries its actual transient cleanup failure.
    assert len(failures) == (0 if os.name == 'nt' else 1)


def test_permanent_cleanup_failure_reports_published_bytes_truthfully(export, monkeypatch):
    target, expected, publish = export
    unlink = os.unlink
    attempts = []

    def lock_published_stage(path, *args, **kwargs):
        if Path(path).name.startswith('.research-export-'):
            assert target.read_bytes() == expected
            attempts.append(path)
            raise PermissionError('Synthetic persistent staging lock')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', lock_published_stage)
    if os.name == 'nt':
        publish(require_absent=True)
        assert not attempts
        assert not list(target.parent.glob('.research-export-*'))
    else:
        with pytest.raises(OSError, match='Export was published'):
            publish(require_absent=True)
        assert len(attempts) == 2
        stage, = target.parent.glob('.research-export-*')
        assert os.path.samefile(stage, target)
        unlink(stage)
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1


def test_real_process_exit_after_publication_preserves_output_and_windows_replacement(export):
    target, expected, publish = export
    script = '''
import os
from pathlib import Path
import sys
from carr_library.research import export_file

primitive = 'rename' if os.name == 'nt' else 'link'
original = getattr(os, primitive)
def exit_after_publication(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(74)
setattr(os, primitive, exit_after_publication)
payload = bytes.fromhex(sys.argv[3])
writer = export_file.write_export if sys.argv[4] == 'text' else export_file.write_binary_export
writer(Path(sys.argv[1]), payload.decode('utf-8') if sys.argv[4] == 'text' else payload,
       private_root=Path(sys.argv[2]), require_absent=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(target),
                             str(target.parent / 'private'), expected.hex(),
                             'text' if target.suffix == '.txt' else 'binary'],
                            capture_output=True, timeout=20, check=False)
    assert result.returncode == 74, result.stderr.decode('utf-8', errors='replace')
    assert target.read_bytes() == expected
    stages = list(target.parent.glob('.research-export-*'))
    if os.name == 'nt':
        assert stages == []
        assert target.stat().st_nlink == 1
    else:
        # The portable hard-link fallback cannot atomically remove its second
        # name on process death. Preserve this explicit limitation and reject
        # replacement until this test removes its own known residual link.
        stage, = stages
        assert os.path.samefile(stage, target)
        with pytest.raises(ValueError, match='hard links'):
            publish()
        assert target.read_bytes() == expected
        stage.unlink()
    publish()
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))


def test_create_only_primitive_failure_preserves_absence_and_cleans_stage(export, monkeypatch):
    target, _, publish = export
    calls = []

    def refuse_publication(*args, **kwargs):
        calls.append(args)
        raise OSError('Synthetic unsupported no-replacement operation')

    monkeypatch.setattr(os, 'rename' if os.name == 'nt' else 'link', refuse_publication)
    with pytest.raises(OSError, match='unsupported no-replacement'):
        publish(require_absent=True)
    assert len(calls) == 1
    assert not target.exists()
    assert not list(target.parent.glob('.research-export-*'))


def test_competing_file_at_actual_publication_is_not_replaced(export, monkeypatch):
    target, _, publish = export
    primitive = 'rename' if os.name == 'nt' else 'link'
    original = getattr(os, primitive)
    calls = []

    def publish_after_competitor(*args, **kwargs):
        calls.append(args)
        target.write_bytes(b'Other writer at the actual filesystem operation')
        return original(*args, **kwargs)

    monkeypatch.setattr(os, primitive, publish_after_competitor)
    with pytest.raises(FileExistsError):
        publish(require_absent=True)
    assert len(calls) == 1
    assert target.read_bytes() == b'Other writer at the actual filesystem operation'
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))



def _same_size_timestamp_collision(target, monkeypatch):
    """Real changed bytes/restored mtime, with Windows creation-time semantics."""
    original = target.stat()
    state = export_file._file_state
    identity = original.st_dev, original.st_ino

    def windows_ctime(info):
        result = state(info)
        if result[0] == identity:
            # POSIX ctime cannot be reset by os.utime; Windows st_ctime is the
            # creation time in the tested Python runtime and may remain equal.
            return (*result[:-1], original.st_ctime_ns)
        return result

    monkeypatch.setattr(export_file, '_file_state', windows_ctime)

    def change(raw):
        assert len(raw) == original.st_size
        target.write_bytes(raw)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))

    return change


def test_approved_same_size_changed_bytes_survive_colliding_timestamps(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'Approved earlier bytes')
    change = _same_size_timestamp_collision(target, monkeypatch)
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    changed = b'Newer independent work'
    change(changed)
    assert export_file._destination(target, approval.private_root)[2] == approval.state

    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)

    assert target.read_bytes() == changed
    assert not list(target.parent.glob('.research-export-*'))


def test_final_approved_byte_recheck_preserves_same_size_change_during_staging(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'Approved earlier bytes')
    change = _same_size_timestamp_collision(target, monkeypatch)
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    original_sync = export_file.os.fsync
    changed = b'Newer independent work'

    def competing_save(descriptor):
        original_sync(descriptor)
        change(changed)
        assert export_file._destination(target, approval.private_root)[2] == approval.state

    monkeypatch.setattr(export_file.os, 'fsync', competing_save)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)

    assert target.read_bytes() == changed
    assert not list(target.parent.glob('.research-export-*'))


def test_empty_existing_file_has_a_content_bound_approval(export):
    from hashlib import sha256
    target, expected, publish = export
    target.write_bytes(b'')
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    assert not approval.require_absent and approval.content_sha256 == sha256(b'').hexdigest()
    publish(approval=approval)
    assert target.read_bytes() == expected


def test_existing_approval_without_a_digest_never_falls_back_to_metadata(export):
    from dataclasses import replace
    target, _, publish = export
    target.write_bytes(b'Existing approved work')
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=replace(approval, content_sha256=None))
    assert target.read_bytes() == b'Existing approved work'
    assert not list(target.parent.glob('.research-export-*'))


def _observe_export_reads(monkeypatch, after_read=lambda size, raw: None):
    """Wrap real reader handles without replacing their I/O or identity."""
    fdopen = export_file.os.fdopen
    streams, requests = [], []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            assert 0 < size <= export_file.HASH_BLOCK_BYTES
            raw = self.stream.read(size)
            requests.append((size, len(raw)))
            after_read(size, raw)
            return raw
        def __enter__(self):
            return self
        def __exit__(self, *error):
            self.stream.close()

    def opened(descriptor, *args, **kwargs):
        stream = fdopen(descriptor, *args, **kwargs)
        streams.append(stream)
        return Reader(stream)

    monkeypatch.setattr(export_file.os, 'fdopen', opened)
    return streams, requests


def test_capture_size_cap_rejects_before_opening_oversized_existing_bytes(tmp_path, monkeypatch):
    target = tmp_path / 'oversized.txt'
    with target.open('wb') as stream:
        stream.truncate(export_file.MAX_APPROVAL_BYTES + 1)

    def forbidden(*args, **kwargs):
        pytest.fail('Oversized replacement approval opened the existing file.')

    monkeypatch.setattr(export_file.os, 'open', forbidden)
    with pytest.raises(ValueError, match='Choose a new filename'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    assert target.stat().st_size == export_file.MAX_APPROVAL_BYTES + 1
    assert not list(tmp_path.glob('.research-export-*'))


def test_capture_deadline_stops_between_bounded_reads_and_closes_its_handle(tmp_path, monkeypatch):
    target = tmp_path / 'slow.txt'
    original = b'X' * (export_file.HASH_BLOCK_BYTES * 3)
    target.write_bytes(original)
    clock = [0.0]
    monkeypatch.setattr(export_file, 'monotonic', lambda: clock[0])
    def slow_read(size, raw):
        if raw:
            clock[0] = export_file.APPROVAL_HASH_SECONDS + 1
    streams, reads = _observe_export_reads(monkeypatch, slow_read)

    with pytest.raises(TimeoutError, match='Choose a new filename'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private')

    assert reads == [(export_file.HASH_BLOCK_BYTES, export_file.HASH_BLOCK_BYTES)]
    assert streams and all(stream.closed for stream in streams)
    assert target.read_bytes() == original


def test_capture_cancellation_is_checked_between_chunks_without_retaining_an_open_file(tmp_path, monkeypatch):
    from threading import Event
    target = tmp_path / 'cancelled.txt'
    original = b'X' * (export_file.HASH_BLOCK_BYTES * 3)
    target.write_bytes(original)
    stopped = Event()
    streams, reads = _observe_export_reads(monkeypatch, lambda size, raw: stopped.set())

    with pytest.raises(TimeoutError, match='destination was not changed'):
        export_file.capture_export_destination(target, private_root=tmp_path / 'private', cancelled=stopped.is_set)

    assert reads == [(export_file.HASH_BLOCK_BYTES, export_file.HASH_BLOCK_BYTES)]
    assert streams and all(stream.closed for stream in streams)
    assert target.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('cancelled,exception', [(lambda: True, TimeoutError), (None, ValueError)])
def test_invalid_or_pre_cancelled_capture_never_inspects_a_destination(tmp_path, monkeypatch, cancelled, exception):
    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled/invalid approval accessed the filesystem.')
    monkeypatch.setattr(export_file, '_destination', forbidden)
    with pytest.raises(exception):
        export_file.capture_export_destination(tmp_path / 'output.txt', private_root=tmp_path / 'private',
                                               cancelled=cancelled)


@pytest.mark.parametrize('phase', ['before-staging', 'after-staging'])
def test_approved_export_cancellation_during_content_verification_preserves_old_file(export, monkeypatch, phase):
    from threading import Event
    target, _, publish = export
    original = b'X' * (export_file.HASH_BLOCK_BYTES * 3)
    target.write_bytes(original)
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    stopped = Event()
    def cancel_read(size, raw):
        if raw and (phase == 'before-staging' or list(target.parent.glob('.research-export-*'))):
            stopped.set()
    streams, reads = _observe_export_reads(monkeypatch, cancel_read)

    with pytest.raises(TimeoutError, match='destination was not changed'):
        publish(approval=approval, cancelled=stopped.is_set)

    assert stopped.is_set() and reads
    assert streams and all(stream.closed for stream in streams)
    assert target.read_bytes() == original
    assert not list(target.parent.glob('.research-export-*'))


def test_existing_approval_reader_handles_are_closed_before_windows_replacement(export, monkeypatch):
    target, expected, publish = export
    target.write_bytes(b'Existing selected work')
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    streams, _ = _observe_export_reads(monkeypatch)
    replace = export_file.os.replace

    def closed_before_replace(source, destination):
        assert len(streams) == 2 and all(stream.closed for stream in streams)
        return replace(source, destination)

    monkeypatch.setattr(export_file.os, 'replace', closed_before_replace)
    publish(approval=approval)
    assert target.read_bytes() == expected


def test_existing_file_swapped_between_metadata_and_read_is_not_approved(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'Approved earlier bytes')
    approval = export_file.capture_export_destination(target, private_root=target.parent / 'private')
    replacement = target.parent / 'competing.tmp'
    replacement.write_bytes(b'Newer independent work')
    original_open = export_file.os.open
    swaps = []

    def swap_before_read(path, flags, *args, **kwargs):
        if Path(path) == target and not swaps:
            os.replace(replacement, target)
            swaps.append(True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(export_file.os, 'open', swap_before_read)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert swaps and target.read_bytes() == b'Newer independent work'
    assert not list(target.parent.glob('.research-export-*'))



def test_windows_filename_execute_bits_do_not_break_handle_identity(tmp_path, monkeypatch):
    target = tmp_path / 'export.cmd'
    target.write_bytes(b'Approved plain text; this file is never executed.')
    os.chmod(target, target.stat().st_mode | 0o111)
    identity = target.stat().st_dev, target.stat().st_ino
    actual_fstat = export_file.os.fstat

    class HandleState:
        def __init__(self, value):
            self.value = value
            self.st_mode = value.st_mode & ~0o111
        def __getattr__(self, name):
            return getattr(self.value, name)

    def windows_handle_mode(descriptor):
        value = actual_fstat(descriptor)
        return HandleState(value) if (value.st_dev, value.st_ino) == identity else value

    # CPython Windows path stat adds executable-extension bits; fstat does not.
    # Model that documented source behavior on POSIX too, preserving real I/O.
    monkeypatch.setattr(export_file.os, 'fstat', windows_handle_mode)
    approval = export_file.capture_export_destination(target, private_root=tmp_path / 'private')
    export_file.write_export(target, 'Updated plain text', private_root=tmp_path / 'private', approval=approval)
    assert target.read_bytes() == b'Updated plain text'
    assert not list(tmp_path.glob('.research-export-*'))
