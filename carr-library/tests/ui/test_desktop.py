"""Desktop orchestration with real files/threads; native wx remains separate QA."""
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.desktop import DesktopController, import_files, import_summary, write_export


def test_export_utf8_atomic_replacement_and_storage_guard(tmp_path):
    private = tmp_path / 'library'
    private.mkdir()
    target = tmp_path / 'research.md'
    target.write_text('Old export', encoding='utf-8')
    write_export(target, 'Exact quotation: Україна\n', private_root=private)
    assert target.read_bytes() == 'Exact quotation: Україна\n'.encode('utf-8')
    assert tuple(tmp_path.glob('.carr-export-*')) == ()
    with pytest.raises(ValueError, match='outside'):
        write_export(private / 'library.sqlite', 'overwrite', private_root=private)
    with pytest.raises(ValueError, match='regular'):
        write_export(private.parent, 'overwrite', private_root=private)


def test_export_does_not_follow_symlink(tmp_path):
    original = tmp_path / 'original.txt'
    original.write_text('original', encoding='utf-8')
    destination = tmp_path / 'export.txt'
    destination.symlink_to(original)
    with pytest.raises(ValueError, match='regular'):
        write_export(destination, 'changed', private_root=tmp_path / 'data')
    assert original.read_text(encoding='utf-8') == 'original'


def test_import_batch_captures_scope_and_cancels_between_files():
    cancel = Event()
    service = Mock()

    def imported(workspace, path, *, title, authors, cancelled):
        assert (workspace, title, authors) == ('selected-library', None, ())
        assert not cancelled()
        cancel.set()
        return {'state': 'complete', 'citation_status': 'byte_verified'}

    service.import_file.side_effect = imported
    progress = Mock()
    results = import_files(service, 'selected-library', ('first.txt', 'second.txt'),
                           cancelled=cancel.is_set, progress=progress)
    service.import_file.assert_called_once()
    assert len(results) == 1
    assert 'Imported 1 of 2 files' in import_summary(results, total=2)
    progress.assert_called_once()


def test_import_batch_keeps_failure_and_review_states_truthful():
    service = Mock()
    service.import_file.side_effect = [ValueError('unsupported document'),
                                      {'state': 'complete', 'citation_status': 'review_required'}]
    results = import_files(service, 'workspace', ('bad.txt', 'scan.pdf'),
                           cancelled=lambda: False, progress=Mock())
    assert [row['state'] for row in results] == ['failed', 'complete']
    message = import_summary(results)
    assert '0 ready for verified text citation' in message
    assert '1 require source review' in message
    assert 'unsupported document' in message


def controller():
    ui = object.__new__(DesktopController)
    pending = Queue()
    ui.wx = SimpleNamespace(CallAfter=lambda fn, *args: pending.put((fn, args)))
    ui.frame = SimpleNamespace(busy=False, choice=Mock(), book=None, status=Mock(),
                               active=None, refresh_scopes=Mock())
    ui.worker, ui.cancelled, ui.closed = None, Event(), False
    ui.actions, ui.cancel_button = [], Mock()
    return ui, pending


def test_running_job_cancel_close_and_main_thread_completion():
    ui, pending = controller()
    started = Event()

    def work(cancelled, progress):
        started.set()
        assert ui.cancelled.wait(5), 'test cancellation was not delivered'
        assert cancelled()
        return 'cancelled'

    assert ui.start_job(work, lambda value: 'Import state: ' + value)
    assert started.wait(5)
    assert not ui.start_job(Mock(), Mock())
    close = SimpleNamespace(CanVeto=lambda: True, Veto=Mock(), Skip=Mock())
    assert ui.on_close(close)
    close.Veto.assert_called_once()
    close.Skip.assert_not_called()
    worker = ui.worker
    worker.join(5)
    assert not worker.is_alive()
    # The worker only schedules completion; it must not touch native controls.
    assert ui.frame.busy
    ui.frame.refresh_scopes.assert_not_called()
    finish, args = pending.get_nowait()
    finish(*args)
    assert ui.worker is None and not ui.frame.busy
    ui.frame.refresh_scopes.assert_called_once()
    ui.frame.status.SetValue.assert_called_with('Import state: cancelled')


def test_failed_background_job_restores_controls_and_reports_failure():
    ui, pending = controller()

    def fail(*unused):
        raise ValueError('source unavailable')

    ui.start_job(fail, Mock())
    worker = ui.worker
    worker.join(5)
    assert not worker.is_alive()
    finish, args = pending.get_nowait()
    finish(*args)
    assert not ui.frame.busy
    ui.cancel_button.Enable.assert_called_with(False)
    ui.frame.status.SetValue.assert_called_with('Operation failed: source unavailable')


def test_destroyed_frame_ignores_queued_completion():
    ui, _ = controller()
    ui.closed = True
    ui.finish_job(None, ValueError('failure'), Mock(), True)
    ui.frame.refresh_scopes.assert_not_called()
    ui.frame.status.SetValue.assert_not_called()


def test_actual_batch_import_persists_searchable_unicode_sources(tmp_path):
    from carr_library.application import Library

    library = Library(tmp_path / 'private-library')
    workspace = library.create_workspace('Synthetic desktop library')
    sources = []
    for name, text in [('First', 'Original history source. Історія.'),
                       ('Second', 'A different author and original history.')]:
        path = tmp_path / (name + '.txt')
        path.write_text(text, encoding='utf-8')
        sources.append(path)
    result = import_files(library, workspace.id, sources,
                          cancelled=lambda: False, progress=Mock())
    assert len(result) == 2 and all(row['state'] == 'complete' for row in result)
    assert all(row['citation_status'] == 'byte_verified' for row in result)
    hits = library.search(workspace.id, 'history')
    assert len(hits) == 2
    assert {hit.title for hit in hits} == {'Unknown title'}
    assert {hit.quote for hit in hits} == {'history'}


def test_backup_handler_requires_explicit_all_library_confirmation(tmp_path):
    ui, _ = controller()
    ui.library = Mock(root=tmp_path / 'current')
    ui.wx = SimpleNamespace(YES=1, YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8,
                            MessageBox=Mock(return_value=0))
    ui._new_directory = Mock(return_value=tmp_path / 'backup')
    ui.start_job = Mock()
    ui.backup_all()
    ui._new_directory.assert_not_called()
    ui.start_job.assert_not_called()
    ui.wx.MessageBox.return_value = ui.wx.YES
    ui.backup_all()
    operation, completed = ui.start_job.call_args.args
    assert ui.start_job.call_args.kwargs == {'refresh': False, 'cancellable': False}
    ui.library.backup.return_value = 'a' * 64
    checksum = operation(lambda: False, Mock())
    ui.library.backup.assert_called_once_with(tmp_path / 'backup', include_all_workspaces=True)
    ui._receipt = Mock(side_effect=lambda title, message: message)
    receipt = completed(checksum)
    assert str(tmp_path / 'backup') in receipt and 'a' * 64 in receipt
    assert 'pending import jobs are excluded' in receipt


def test_restore_uses_independent_receipt_and_keeps_current_session(tmp_path):
    ui, _ = controller()
    original_library = Mock(root=tmp_path / 'current')
    ui.library = original_library
    source = tmp_path / 'untrusted-backup'
    source.mkdir()
    (source / 'manifest.json').write_text('{"checksum":"untrusted"}', encoding='utf-8')
    dialog = Mock()
    dialog.ShowModal.return_value = 1
    dialog.GetPath.return_value = str(source)
    ui.wx = SimpleNamespace(ID_OK=1, DD_DEFAULT_STYLE=1, DD_DIR_MUST_EXIST=2,
                            DirDialog=Mock(return_value=dialog))
    checksum = 'b' * 64
    destination = tmp_path / 'restored'
    ui._form = Mock(return_value={'checksum': checksum.upper()})
    ui._new_directory = Mock(return_value=destination)
    ui.start_job = Mock()
    ui.restore_backup()
    operation, completed = ui.start_job.call_args.args
    assert ui.start_job.call_args.kwargs == {'refresh': False, 'cancellable': False}
    restored = SimpleNamespace(root=destination)
    original_library.restore.return_value = restored
    assert operation(lambda: False, Mock()) is restored
    original_library.restore.assert_called_once_with(source, destination, checksum)
    ui._receipt = Mock(side_effect=lambda title, message: message)
    receipt = completed(restored)
    assert ui.library is original_library
    assert str(destination) in receipt and str(original_library.root) in receipt
    assert checksum in receipt
    ui.frame.refresh_scopes.assert_not_called()


def test_invalid_restore_receipt_never_starts_work(tmp_path):
    ui, _ = controller()
    dialog = Mock()
    dialog.ShowModal.return_value = 1
    dialog.GetPath.return_value = str(tmp_path / 'backup')
    ui.wx = SimpleNamespace(ID_OK=1, DD_DEFAULT_STYLE=1, DD_DIR_MUST_EXIST=2,
                            DirDialog=Mock(return_value=dialog))
    ui._form = Mock(return_value={'checksum': 'not-a-checksum'})
    ui._new_directory = Mock()
    ui.start_job = Mock()
    ui.restore_backup()
    ui._new_directory.assert_not_called()
    ui.start_job.assert_not_called()
    assert '64-character SHA256' in ui.frame.status.SetValue.call_args.args[0]
