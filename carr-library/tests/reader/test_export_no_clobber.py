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
        return writer(target, payload, private_root=private, **options)

    return target, expected, publish


def test_create_only_export_publishes_exact_bytes_without_staging_residue(export):
    target, expected, publish = export
    receipt = publish(require_absent=True)
    assert receipt.path == target and receipt.cleanup_warning is None
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
    assert 'exists' in capsys.readouterr().err
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


@pytest.mark.parametrize('existing', (False, True))
def test_captured_destination_can_publish_only_its_unchanged_state(export, existing):
    target, expected, publish = export
    if existing:
        target.write_bytes(b'Explicitly selected existing file')
    approval = export_file.capture_export_destination(target,
        private_root=target.parent / 'private')
    assert approval.require_absent is not existing

    receipt = publish(approval=approval)

    assert receipt.path == target and receipt.cleanup_warning is None
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
        receipt = publish(require_absent=True)
        assert receipt.path == target and receipt.cleanup_warning is None
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


def test_create_only_export_recovers_transient_staging_cleanup(export, monkeypatch):
    target, expected, publish = export
    unlink = os.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if Path(path).name.startswith('.research-export-') and not failed:
            # Inject only after actual atomic publication on the portable
            # hard-link path. Windows rename has no cleanup unlink.
            assert target.read_bytes() == expected
            failed = True
            raise PermissionError('Synthetic transient cleanup failure')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, 'unlink', fail_once)
    receipt = publish(require_absent=True)
    assert receipt.path == target and receipt.cleanup_warning is None
    assert failed is (os.name != 'nt')
    assert target.read_bytes() == expected
    assert target.stat().st_nlink == 1
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('broken_logger', [False, True])
def test_create_only_export_returns_saved_receipt_after_permanent_staging_cleanup_error(
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
            # Atomic Windows rename leaves no staging link to clean.
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


def test_cli_reports_saved_with_cleanup_warning_without_requesting_repeat(
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
