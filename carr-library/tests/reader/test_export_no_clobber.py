"""Real export publication must preserve files created after caller preflight."""
import _compression
from hashlib import sha256
from importlib.util import cache_from_source
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


def test_real_process_exit_after_publication_preserves_output_and_windows_replacement(export, monkeypatch):
    target, expected, publish = export
    script = '''
from hashlib import sha256
import os
from pathlib import Path
import sys
from carr_library.research import export_file

assert sha256(Path(export_file.__file__).read_bytes()).hexdigest() == sys.argv[5]
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
    # A fresh child does not inherit its parent's -B. Exercise the real export
    # with a stale, pre-existing stdlib cache and require it to stay sealed.
    cache_root = target.parent / 'sealed-runtime-cache'
    with monkeypatch.context() as context:
        context.setattr(sys, 'pycache_prefix', str(cache_root))
        compression_cache = Path(cache_from_source(_compression.__file__))
    compression_cache.parent.mkdir(parents=True)
    sealed_cache = b'Synthetic stale sealed _compression bytecode cache'
    compression_cache.write_bytes(sealed_cache)
    environment = os.environ.copy()
    environment.pop('PYTHONDONTWRITEBYTECODE', None)
    environment['PYTHONPYCACHEPREFIX'] = str(cache_root)
    environment['PYTHONPATH'] = str(Path(export_file.__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, '-B', '-c', script, str(target),
                             str(target.parent / 'private'), expected.hex(),
                             'text' if target.suffix == '.txt' else 'binary',
                             sha256(Path(export_file.__file__).read_bytes()).hexdigest()],
                            capture_output=True, timeout=20, check=False, env=environment)
    assert result.returncode == 74, result.stderr.decode('utf-8', errors='replace')
    assert compression_cache.read_bytes() == sealed_cache
    assert list(cache_root.rglob('*.pyc')) == [compression_cache]
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


def _keep_windows_creation_time(monkeypatch, target):
    """Only ctime is modeled; actual files, sizes, mtimes and identities remain real."""
    before = target.stat()
    original = export_file._file_state
    monkeypatch.setattr(export_file, '_file_state',
        lambda info: (*original(info)[:-1], before.st_ctime_ns))
    return before


@pytest.mark.parametrize('when', ('before_writer', 'after_staging'))
def test_same_size_changed_bytes_with_original_timestamps_are_not_replaced(export, monkeypatch, when):
    from contextlib import contextmanager

    target, _, publish = export
    approved, newer = b'Approved earlier bytes', b'Newer independent work'
    assert len(approved) == len(newer) == 22
    target.write_bytes(approved)
    before = _keep_windows_creation_time(monkeypatch, target)
    metadata = export_file._file_state(before)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')

    def change_content():
        target.write_bytes(newer)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert export_file._file_state(target.stat()) == metadata
        assert target.read_bytes() == newer

    if when == 'before_writer':
        change_content()
    else:
        original_temporary = export_file.NamedTemporaryFile

        @contextmanager
        def change_after_staging(*args, **kwargs):
            with original_temporary(*args, **kwargs) as stream:
                yield stream
            change_content()

        monkeypatch.setattr(export_file, 'NamedTemporaryFile', change_after_staging)

    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)
    assert target.read_bytes() == newer
    assert not list(target.parent.glob('.research-export-*'))


def _observe_destination_reads(monkeypatch, after_read=lambda *args: None):
    """Observe real unbuffered reads without substituting filesystem bytes."""
    original = os.fdopen
    sessions = []

    class Read:
        def __init__(self, source, session):
            self.source, self.session = source, session
        def __enter__(self):
            self.source.__enter__()
            return self
        def __exit__(self, *args):
            return self.source.__exit__(*args)
        def read(self, count):
            assert 0 < count <= 1024 * 1024
            result = self.source.read(count)
            self.session['requested'].append(count)
            self.session['returned'].append(len(result))
            after_read(self.session, result)
            return result

    def observe(descriptor, *args, **kwargs):
        source = original(descriptor, *args, **kwargs)
        session = {'index': len(sessions), 'descriptor': descriptor,
                   'source': source, 'requested': [], 'returned': []}
        sessions.append(session)
        return Read(source, session)

    monkeypatch.setattr(os, 'fdopen', observe)
    return sessions


def _assert_fingerprint_descriptors_closed(sessions):
    for session in sessions:
        assert session['source'].closed
        with pytest.raises(OSError):
            os.fstat(session['descriptor'])


@pytest.mark.parametrize('change_tail', (False, True))
def test_full_existing_content_is_streamed_and_unsampled_tail_changes_are_detected(export, monkeypatch, change_tail):
    target, expected, publish = export
    size = 2 * 1024 * 1024 + 37
    original_bytes = b'A' * size
    target.write_bytes(original_bytes)
    before = _keep_windows_creation_time(monkeypatch, target)
    sessions = _observe_destination_reads(monkeypatch)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    if change_tail:
        with target.open('r+b') as stream:
            stream.seek(-1, os.SEEK_END)
            stream.write(b'Z')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        with pytest.raises(ValueError, match='destination changed'):
            publish(approval=approval)
    else:
        publish(approval=approval)
    _assert_fingerprint_descriptors_closed(sessions)
    assert len(sessions) >= (2 if change_tail else 3)
    assert all(sum(row['returned']) == size and sum(row['requested']) == size + 1
               for row in sessions)
    assert target.read_bytes() == (original_bytes[:-1] + b'Z' if change_tail else expected)
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('when', ('capture', 'after_staging'))
@pytest.mark.parametrize('change', ('grow', 'shorten', 'rewrite'))
def test_destination_mutating_during_hash_is_rejected_without_publication(export, monkeypatch, when, change):
    target, _, publish = export
    original_bytes = b'A' * (1024 * 1024 + 37)
    target.write_bytes(original_bytes)
    before = target.stat()
    approval = (export_file.capture_export_destination(target, private_root=target.parent / 'private')
                if when == 'after_staging' else None)
    selected_read = 1 if when == 'after_staging' else 0
    changed = []

    def mutate(session, block):
        if session['index'] != selected_read or len(session['returned']) != 1:
            return
        assert block
        with target.open('r+b') as stream:
            if change == 'grow':
                stream.seek(0, os.SEEK_END)
                stream.write(b'additional newer bytes')
            elif change == 'shorten':
                stream.truncate(1024 * 1024)
            else:
                stream.write(b'B')
        # Make the in-read mutation observable on coarse-timestamp filesystems
        # too. The separate equal-stat cases exercise content-only identity.
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        changed.append(target.read_bytes())

    sessions = _observe_destination_reads(monkeypatch, mutate)
    with pytest.raises(ValueError, match='destination changed'):
        if when == 'capture':
            export_file.capture_export_destination(target, private_root=target.parent / 'private')
        else:
            publish(approval=approval)
    _assert_fingerprint_descriptors_closed(sessions)
    assert len(changed) == 1 and target.read_bytes() == changed[0]
    assert all(sum(row['returned']) <= len(original_bytes) + 1 for row in sessions)
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('when', ('before_staging', 'after_staging'))
@pytest.mark.parametrize('failure', ('cancel', 'read_error'))
def test_hash_cancellation_and_read_failure_preserve_destination_and_close_descriptors(export, monkeypatch, when, failure):
    target, _, publish = export
    original_bytes = b'Keep existing bytes.\n' * 100_000
    target.write_bytes(original_bytes)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    selected_read = 1 if when == 'after_staging' else 0
    cancelled, checkpoints = [], []

    def stop(session, block):
        if session['index'] == selected_read and len(session['returned']) == 1:
            assert block
            checkpoints.append(bool(list(target.parent.glob('.research-export-*'))))
            if failure == 'cancel':
                cancelled.append(True)
            else:
                raise OSError('Synthetic destination read failure')

    sessions = _observe_destination_reads(monkeypatch, stop)
    exception, message = (TimeoutError, 'cancelled') if failure == 'cancel' else (OSError, 'read failure')
    with pytest.raises(exception, match=message):
        publish(approval=approval, cancelled=lambda: bool(cancelled))
    _assert_fingerprint_descriptors_closed(sessions)
    assert checkpoints == [when == 'after_staging']
    assert target.read_bytes() == original_bytes
    assert not list(target.parent.glob('.research-export-*'))


def test_private_destination_is_rejected_before_opening_content(export, monkeypatch):
    target, _, publish = export
    private = target.parent / 'private'
    original = private / 'original.txt'
    original.write_bytes(b'Private retained bytes must not be read by export hashing')
    attempts = []

    def must_not_open(*args, **kwargs):
        attempts.append(args)
        raise AssertionError('A private export destination must be rejected before opening content.')

    monkeypatch.setattr(os, 'open', must_not_open)
    with pytest.raises(ValueError, match='outside the private library folder'):
        export_file.capture_export_destination(original, private_root=private)
    with pytest.raises(ValueError, match='outside the private library folder'):
        export_file.write_export(original, 'Forbidden replacement', private_root=private)
    assert not attempts
    assert original.read_bytes() == b'Private retained bytes must not be read by export hashing'


def test_raced_private_hardlink_is_rejected_before_any_descriptor_read(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'An ordinary external file')
    private = target.parent / 'private' / 'retained.txt'
    private.write_bytes(b'Private retained content')
    original_open, changed = os.open, []

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == target and not changed:
            target.unlink()
            os.link(private, target)
            changed.append(True)
        return original_open(path, flags, *args, **kwargs)

    sessions = _observe_destination_reads(monkeypatch)
    monkeypatch.setattr(os, 'open', replace_before_open)
    with pytest.raises(ValueError, match='destination changed'):
        publish()
    _assert_fingerprint_descriptors_closed(sessions)
    assert changed == [True] and all(not row['requested'] for row in sessions)
    assert target.samefile(private)
    assert private.read_bytes() == b'Private retained content'
    assert not list(target.parent.glob('.research-export-*'))


def test_raced_nonregular_destination_never_blocks_or_reads_content(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'An ordinary external file')
    original_open, changed = os.open, []

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == target and not changed:
            target.unlink()
            if hasattr(os, 'mkfifo'):
                os.mkfifo(target)
                # Fail before a blocking open if this safety flag is removed.
                assert flags & os.O_NONBLOCK
            else:
                target.mkdir()
            changed.append(True)
        return original_open(path, flags, *args, **kwargs)

    sessions = _observe_destination_reads(monkeypatch)
    monkeypatch.setattr(os, 'open', replace_before_open)
    with pytest.raises((ValueError, OSError)):
        publish()
    _assert_fingerprint_descriptors_closed(sessions)
    assert changed == [True] and all(not row['requested'] for row in sessions)
    assert target.is_fifo() if hasattr(os, 'mkfifo') else target.is_dir()
    assert not list(target.parent.glob('.research-export-*'))
