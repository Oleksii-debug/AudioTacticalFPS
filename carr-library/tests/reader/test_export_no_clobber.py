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
        return writer(target, payload, private_root=private, **options)

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

    def competing_writer(*args, **kwargs):
        nonlocal checks
        state = inspect(*args, **kwargs)
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

    def create_after_final_check(*args, **kwargs):
        nonlocal checks
        state = inspect(*args, **kwargs)
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

    def create_after_final_check(*args, **kwargs):
        nonlocal checks
        state = inspect(*args, **kwargs)
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
        receipt = publish(require_absent=True)
        assert receipt.path == target and 'file is saved' in receipt.cleanup_warning
        assert 'does not need to be repeated' in receipt.cleanup_warning
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


def _cached_file_timestamps(monkeypatch):
    state = export_file._file_state
    # Windows may retain both timestamps for an equal-length in-place update.
    # Keep the actual identity, mode, link count and size while modelling that
    # metadata behavior with real changed bytes on every host platform.
    monkeypatch.setattr(export_file, '_file_state', lambda info: state(info)[:-2] + (0, 0))


def test_approved_content_change_with_identical_metadata_is_never_replaced(export, monkeypatch):
    target, _, publish = export
    target.write_bytes(b'Approved earlier bytes')
    _cached_file_timestamps(monkeypatch)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    target.write_bytes(b'Newer independent work')
    assert export_file._file_state(target.stat()) == approval.state

    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)

    assert target.read_bytes() == b'Newer independent work'
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('approved', (False, True))
def test_staged_export_rechecks_same_metadata_content(export, monkeypatch, approved):
    target, _, publish = export
    target.write_bytes(b'Approved earlier bytes')
    _cached_file_timestamps(monkeypatch)
    approval = (export_file.capture_export_destination(target,
        private_root=target.parent / 'private') if approved else None)
    fsync = os.fsync

    def change_after_staging(descriptor):
        fsync(descriptor)
        target.write_bytes(b'Newer independent work')

    monkeypatch.setattr(os, 'fsync', change_after_staging)
    with pytest.raises(ValueError, match='destination changed'):
        publish(approval=approval)

    assert target.read_bytes() == b'Newer independent work'
    assert not list(target.parent.glob('.research-export-*'))


def test_existing_content_inspection_accepts_exact_byte_budget(export, monkeypatch):
    target, expected, publish = export
    previous = b'original'
    target.write_bytes(previous)
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', len(previous))
    monkeypatch.setattr(export_file, '_HASH_BLOCK_BYTES', 3)
    read = os.read
    requests = []

    def bounded_read(descriptor, size):
        requests.append(size)
        return read(descriptor, size)

    monkeypatch.setattr(os, 'read', bounded_read)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    assert approval.content_sha256 == sha256(previous).hexdigest()
    publish(approval=approval)

    assert requests and max(requests) <= 3
    assert target.read_bytes() == expected
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ('approval', 'write'))
def test_oversized_existing_target_is_rejected_before_open(export, monkeypatch, phase):
    target, _, publish = export
    previous = b'oversized'
    target.write_bytes(previous)
    monkeypatch.setattr(export_file, '_MAX_OVERWRITE_BYTES', len(previous) - 1)
    opened = os.open

    def reject_read(path, *args, **kwargs):
        if Path(path) == target:
            pytest.fail('An oversized target must not be opened or hashed.')
        return opened(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', reject_read)
    with pytest.raises(ValueError, match='too large to verify'):
        if phase == 'approval':
            export_file.capture_export_destination(target, private_root=target.parent / 'private')
        else:
            publish()
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('phase', ('approval', 'initial', 'final'))
def test_existing_content_deadline_preserves_file_and_cleans_stage(export, monkeypatch, phase):
    target, _, publish = export
    previous = b'Original content must remain intact'
    target.write_bytes(previous)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    clock = [0.0]
    monkeypatch.setattr(export_file, 'monotonic', lambda: clock[0])
    read = os.read

    def delayed_read(descriptor, size):
        result = read(descriptor, size)
        if phase != 'final' or list(target.parent.glob('.research-export-*')):
            clock[0] += export_file._OVERWRITE_CHECK_SECONDS + 1
        return result

    monkeypatch.setattr(os, 'read', delayed_read)
    with pytest.raises(ValueError, match='quickly enough.*Choose a new filename'):
        if phase == 'approval':
            export_file.capture_export_destination(target, private_root=target.parent / 'private')
        else:
            publish(approval=approval)
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


def test_cancel_during_final_content_inspection_preserves_file(export, monkeypatch):
    target, _, publish = export
    previous = b'Original content must remain intact'
    target.write_bytes(previous)
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    cancelled = False
    read = os.read

    def cancel_after_read(descriptor, size):
        nonlocal cancelled
        result = read(descriptor, size)
        if list(target.parent.glob('.research-export-*')):
            cancelled = True
        return result

    monkeypatch.setattr(os, 'read', cancel_after_read)
    with pytest.raises(TimeoutError, match='Export cancelled; the destination was not changed'):
        publish(approval=approval, cancelled=lambda: cancelled)
    assert cancelled
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


def test_existing_file_growth_during_inspection_cannot_exceed_captured_budget(export, monkeypatch):
    target, _, _ = export
    previous = b'Original content'
    target.write_bytes(previous)
    monkeypatch.setattr(export_file, '_HASH_BLOCK_BYTES', 4)
    read = os.read
    total = 0

    def grow_after_read(descriptor, size):
        nonlocal total
        result = read(descriptor, size)
        total += len(result)
        with target.open('ab') as stream:
            stream.write(b'competing writer')
        return result

    monkeypatch.setattr(os, 'read', grow_after_read)
    with pytest.raises(ValueError, match='destination changed'):
        export_file.capture_export_destination(target, private_root=target.parent / 'private')
    assert total <= len(previous) + 1
    assert target.read_bytes().startswith(previous)
    assert not list(target.parent.glob('.research-export-*'))


def test_unreadable_existing_content_never_authorizes_replacement(export, monkeypatch):
    target, _, publish = export
    previous = b'Original content must remain intact'
    target.write_bytes(previous)
    opened = os.open

    def deny_read(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError('Synthetic denied read permission')
        return opened(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', deny_read)
    with pytest.raises(PermissionError, match='denied read permission'):
        publish()
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('binary', (False, True))
@pytest.mark.parametrize('phase', ('approval', 'initial', 'final'))
def test_parent_redirect_into_private_library_is_rejected_before_digest_reads(
        tmp_path, monkeypatch, binary, phase):
    private, folder = tmp_path / 'private', tmp_path / 'exports'
    private.mkdir()
    folder.mkdir()
    target = folder / 'selected-export'
    previous = b'Synthetic approved bytes'
    target.write_bytes(previous)
    approval = (export_file.capture_export_destination(target, private_root=private)
                if phase != 'approval' else None)
    opened, read = os.open, os.read
    redirected, private_reads = [], []

    def redirect_inside_open(path, flags, *args, **kwargs):
        if (Path(path) == target and not redirected
                and (phase != 'final' or list(folder.glob('.research-export-*')))):
            # Renaming the parent preserves the actual file's identity and
            # timestamps on POSIX too. O_NOFOLLOW does not protect ancestors.
            folder.rename(private / 'moved-exports')
            folder.symlink_to(private / 'moved-exports', target_is_directory=True)
            redirected.append(True)
        return opened(path, flags, *args, **kwargs)

    def inspect_read(descriptor, count):
        if redirected:
            private_reads.append(count)
        return read(descriptor, count)

    monkeypatch.setattr(os, 'open', redirect_inside_open)
    monkeypatch.setattr(os, 'read', inspect_read)
    with pytest.raises(ValueError, match='symbolic links|private library folder|reparse'):
        if phase == 'approval':
            export_file.capture_export_destination(target, private_root=private)
        else:
            writer = export_file.write_binary_export if binary else export_file.write_export
            writer(target, b'New export' if binary else 'New export', private_root=private,
                   approval=approval)

    assert redirected == [True] and not private_reads
    assert folder.is_symlink()
    moved = private / 'moved-exports'
    assert (moved / target.name).read_bytes() == previous
    assert not list(moved.glob('.research-export-*'))



def test_create_only_export_recovers_transient_staging_cleanup(export, monkeypatch):
    target, expected, publish = export
    unlink = os.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if Path(path).name.startswith('.research-export-') and not failed:
            # Inject only after actual publication. Windows no-replace rename
            # avoids this separate cleanup operation entirely.
            assert target.read_bytes() == expected
            failed = True
            raise PermissionError('Synthetic transient cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', fail_once)
    receipt = publish(require_absent=True)
    assert failed is (os.name != 'nt')
    assert receipt.path == target and receipt.cleanup_warning is None
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('broken_logger', [False, True])
def test_create_only_cleanup_cannot_reverse_completed_export(
        export, monkeypatch, caplog, broken_logger):
    target, expected, publish = export
    unlink = os.unlink

    def locked_stage(path, *args, **kwargs):
        if Path(path).name.startswith('.research-export-'):
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
        assert receipt.path == target
        assert target.read_bytes() == expected
        if os.name == 'nt':
            # No staging link needs cleanup after Windows no-replace rename.
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
        # Remove only this test's own staging link with the real OS operation.
        for stage in target.parent.glob('.research-export-*'):
            unlink(stage)
    assert target.stat().st_nlink == 1
    assert target.read_bytes() == expected


def test_cli_reports_completed_export_when_staging_cleanup_is_denied(
        tmp_path, monkeypatch, capsys):
    import json

    from carr_library.application import LibraryApp
    from carr_library.bibliography_cli import main
    from carr_library.ui.academic_library_dialog import AcademicController

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Publication receipt')
    controller = AcademicController(app, workspace)
    reference = controller.create(dict(title='Synthetic source', authors=('Doe, Jane',), year='2020'))
    destination = tmp_path / 'bibliography.txt'
    unlink = os.unlink

    def locked_stage(path, *args, **kwargs):
        if Path(path).name.startswith('.research-export-'):
            raise PermissionError('Synthetic persistent cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', locked_stage)
    try:
        result = main(['format', '--data-dir', str(app.root), '--workspace', workspace,
                       '--reference', reference.id, '--output', str(destination)])
        report = json.loads(capsys.readouterr().out)
        assert result == 0 and report['result'] == 'SAVED'
        assert 'Synthetic source' in destination.read_text(encoding='utf-8')
        if os.name == 'nt':
            assert not any('cleanup' in warning for warning in report['warnings'])
            assert not list(tmp_path.glob('.research-export-*'))
        else:
            assert any('file is saved' in warning for warning in report['warnings'])
            stage, = tmp_path.glob('.research-export-*')
            assert os.path.samefile(stage, destination)
    finally:
        for stage in tmp_path.glob('.research-export-*'):
            unlink(stage)


def test_cli_without_overwrite_preserves_file_created_during_formatting(tmp_path, monkeypatch, capsys):
    from carr_library.application import LibraryApp
    from carr_library.bibliography_cli import main
    from carr_library.citations.bibliography_workbench import BibliographyWorkbench
    from carr_library.ui.academic_library_dialog import AcademicController

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Export audit')
    controller = AcademicController(app, workspace)
    reference = controller.create(dict(title='Synthetic source', authors=('Doe, Jane',), year='2020'))
    destination = tmp_path / 'bibliography.txt'
    render = BibliographyWorkbench.preview

    def format_then_competing_write(workbench, **options):
        preview = render(workbench, **options)
        destination.write_bytes(b'Created by another program during rendering')
        return preview

    monkeypatch.setattr(BibliographyWorkbench, 'preview', format_then_competing_write)
    result = main(['format', '--data-dir', str(app.root), '--workspace', workspace,
                   '--reference', reference.id, '--output', str(destination)])
    assert result == 1
    assert 'destination changed' in capsys.readouterr().err
    assert destination.read_bytes() == b'Created by another program during rendering'
    assert controller.get(reference.id) == reference
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('change', ('metadata', 'workspace-archive'))
@pytest.mark.parametrize('existing', (False, True))
def test_staged_bibliography_reauthorizes_metadata_and_scope_before_publication(
        tmp_path, change, existing):
    from carr_library.application import LibraryApp
    from carr_library.citations.bibliography_workbench import BibliographyWorkbench
    from carr_library.ui.academic_library_dialog import AcademicController

    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Export freshness audit')
    controller = AcademicController(app, workspace)
    reference = controller.create(dict(title='Original metadata title', authors=('Doe, Jane',), year='2020'))
    workbench = BibliographyWorkbench(controller)
    preview = workbench.preview(identity=reference.id, style='ieee')
    destination = tmp_path / 'bibliography.txt'
    original_bytes = b'Previously exported work must survive'
    if existing:
        destination.write_bytes(original_bytes)
    changed = False

    def change_after_real_staging():
        nonlocal changed
        if not changed and list(tmp_path.glob('.research-export-*')):
            # The pre-render and pre-writer checks already passed. Change the
            # database only once the real payload has reached a staging file.
            changed = True
            if change == 'metadata':
                controller.update(reference, {'title': 'New current metadata title'})
            else:
                app.archive_workspace(workspace)
        return False

    with pytest.raises(ValueError):
        workbench.export(preview, destination, cancelled=change_after_real_staging,
                         require_absent=not existing)

    assert changed, 'The metadata/scope change must happen after the payload is staged.'
    if existing:
        assert destination.read_bytes() == original_bytes
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob('.research-export-*'))
    if change == 'metadata':
        assert controller.get(reference.id).title == 'New current metadata title'
    else:
        with pytest.raises(ValueError):
            controller.get(reference.id)
