"""Real export publication must preserve files created after caller preflight."""
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


def test_create_only_export_rejects_existing_file_without_touching_it(export):
    target, _, publish = export
    target.write_bytes(b'Previous work must survive')
    with pytest.raises(FileExistsError):
        publish(require_absent=True)
    assert target.read_bytes() == b'Previous work must survive'
    assert not list(target.parent.glob('.research-export-*'))


@pytest.mark.parametrize('explicit_create_only', [False, True])
def test_file_created_after_final_destination_check_is_never_replaced(export, monkeypatch, explicit_create_only):
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
        publish(**({'require_absent': True} if explicit_create_only else {}))
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
