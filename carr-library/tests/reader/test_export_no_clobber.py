"""Real export publication must preserve files created after caller preflight."""
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
