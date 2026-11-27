"""Existing desktop dispatcher performs every incremental header and byte read."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event, get_ident

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_incremental import BackupReference, create_incremental_backup
from carr_library.library_backup import create_backup
from carr_library.ui import backup_incremental_dialog as module
from carr_library.ui.backup_incremental_dialog import IncrementalBackupActions


class Frame:
    def __init__(self, app):
        self.library, self.working, self._closing = app, False, False
        self.cancel_event, self.queued, self.messages = Event(), [], []
    def run_job(self, label, work, done, failed=None):
        assert not self.working
        self.cancel_event.clear()
        self.working = True
        self.queued.append((label, work, done, failed))
    def report(self, message):
        self.messages.append(message)


class Dialogs:
    def __init__(self, folders, checksum):
        self.folders, self.saved_checksum, self.results = iter(folders), checksum, []
    def folder(self, _title):
        return next(self.folders)
    def checksum(self):
        return self.saved_checksum
    def show(self, title, message):
        self.results.append((title, message))


def finish_job(frame, index, *, worker=None):
    _, work, done, _failed = frame.queued[index]
    value = worker.submit(work).result(15) if worker is not None else work()
    # DesktopFrame finishes controls before dispatching on_done, which may
    # legitimately submit the next header/final operation on the same executor.
    frame.working = False
    done(value)
    return value


@pytest.fixture
def setup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Native backup choices')
    app.add_text(workspace, 'Retained source', 'Immutable quotation.', edition_id='original')
    base = tmp_path / 'full'
    checksum = create_backup(app, base)
    return app, workspace, base, checksum


def test_create_is_queued_on_existing_host_and_returns_exact_receipt(setup, tmp_path):
    app, _, base, checksum = setup
    frame = Frame(app)
    dialogs = Dialogs([base, tmp_path], checksum)
    actions = IncrementalBackupActions(frame, dialogs)
    actions.create()
    assert len(frame.queued) == 1 and not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    reference, required = finish_job(frame, 0)
    assert reference.sha256 == checksum and required is None
    assert len(frame.queued) == 2 and not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    receipt = finish_job(frame, 1)
    assert Path(receipt['path']).is_dir() and receipt['base_sha256'] == checksum
    assert dialogs.results[0][0] == 'Incremental backup complete'
    assert receipt['manifest_sha256'] in dialogs.results[0][1]
    assert 'not a standalone backup' in dialogs.results[0][1]
    assert not frame.messages


@pytest.mark.parametrize('action', ['verify', 'restore'])
def test_chain_dialog_selects_required_base_without_auto_discovery(setup, tmp_path, action):
    app, workspace, base, checksum = setup
    layer = create_incremental_backup(app, tmp_path / 'delta', base=BackupReference(base, checksum))
    folders = [layer.path, base, *([tmp_path] if action == 'restore' else [])]
    dialogs, frame = Dialogs(folders, layer.manifest_sha256), Frame(app)
    getattr(IncrementalBackupActions(frame, dialogs), action)()
    assert len(frame.queued) == 1
    assert finish_job(frame, 0)[1] == checksum
    assert len(frame.queued) == 2
    assert finish_job(frame, 1)[1] is None
    assert len(frame.queued) == 3
    result = finish_job(frame, 2)
    assert not frame.messages
    if action == 'restore':
        restored = LibraryApp(result['restored_root'])
        assert restored.edition_metadata(workspace, 'original').title == 'Retained source'
        assert 'require review' in dialogs.results[0][1]
    else:
        assert result['chain'] == [layer.manifest_sha256, checksum]


@pytest.mark.parametrize('change', ['instance', 'root', 'closing', 'cancelled'])
@pytest.mark.parametrize('phase', ['header', 'mutation'])
def test_queued_action_cannot_follow_changed_scope_or_cancellation(setup, tmp_path, change, phase):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    IncrementalBackupActions(frame, dialogs).create()
    assert len(frame.queued) == 1
    index = 0
    if phase == 'mutation':
        finish_job(frame, 0)
        index = 1
        assert len(frame.queued) == 2
    if change == 'instance':
        frame.library = LibraryApp(tmp_path / 'other')
    elif change == 'root':
        app.root = tmp_path / 'other-root'
    elif change == 'closing':
        frame._closing = True
    else:
        frame.cancel_event.set()
    with pytest.raises((ValueError, InterruptedError)):
        frame.queued[index][1]()
    assert not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    assert not dialogs.results


def test_cancelled_folder_or_wrong_checksum_never_queues_mutation(setup):
    app, _, base, checksum = setup
    first = Frame(app)
    IncrementalBackupActions(first, Dialogs([None], checksum)).create()
    assert not first.queued and not first.messages
    second = Frame(app)
    IncrementalBackupActions(second, Dialogs([base], '0' * 64)).create()
    assert len(second.queued) == 1
    _, work, _done, failed = second.queued[0]
    with pytest.raises(ValueError, match='digest mismatch') as caught:
        work()
    second.working = False
    failed(caught.value)
    assert len(second.queued) == 1 and any('digest mismatch' in message for message in second.messages)


def test_even_authenticated_header_io_stays_on_existing_background_worker(setup, tmp_path, monkeypatch):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    caller = get_ident()
    observed = []
    actual = module.required_base_sha256
    def inspect(reference):
        observed.append(get_ident())
        assert get_ident() != caller
        return actual(reference)
    monkeypatch.setattr(module, 'required_base_sha256', inspect)
    IncrementalBackupActions(frame, dialogs).create()
    assert observed == []
    with ThreadPoolExecutor(max_workers=1) as worker:
        finish_job(frame, 0, worker=worker)
        finish_job(frame, 1, worker=worker)
    assert len(observed) == 1 and observed[0] != caller
    assert dialogs.results[0][0] == 'Incremental backup complete'


@pytest.mark.parametrize('event', ['close', 'cancel', 'new-library'])
@pytest.mark.parametrize('picker', ['required-base', 'destination'])
def test_picker_return_rechecks_liveness_before_next_job_can_clear_cancellation(
        setup, tmp_path, event, picker):
    app, _, base, checksum = setup
    frame = Frame(app)
    if picker == 'required-base':
        layer = create_incremental_backup(app, tmp_path / 'delta', base=BackupReference(base, checksum))
        folders, selected, action = [layer.path, base], layer.manifest_sha256, 'verify'
    else:
        folders, selected, action = [base, tmp_path], checksum, 'create'
    class ChangedDialogs(Dialogs):
        count = 0
        def folder(self, title):
            value = super().folder(title)
            self.count += 1
            if self.count == 2:
                if event == 'close':
                    frame._closing = True
                elif event == 'cancel':
                    frame.cancel_event.set()
                else:
                    frame.library = LibraryApp(tmp_path / 'other-library')
            return value
    dialogs = ChangedDialogs(folders, selected)
    getattr(IncrementalBackupActions(frame, dialogs), action)()
    with pytest.raises(InterruptedError):
        finish_job(frame, 0)
    assert len(frame.queued) == 1 and not dialogs.results
    assert not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    if event == 'cancel':
        assert frame.cancel_event.is_set()


@pytest.mark.parametrize('event', ['close', 'cancel', 'new-library'])
def test_completed_header_cannot_open_another_picker_after_scope_change(setup, tmp_path, event):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base], checksum)
    IncrementalBackupActions(frame, dialogs).create()
    _, work, done, _ = frame.queued[0]
    value = work()
    frame.working = False
    if event == 'close':
        frame._closing = True
    elif event == 'cancel':
        frame.cancel_event.set()
    else:
        frame.library = LibraryApp(tmp_path / 'other-library')
    with pytest.raises(InterruptedError):
        done(value)
    assert len(frame.queued) == 1 and not dialogs.results


@pytest.mark.parametrize('event', ['cancel', 'new-library'])
def test_returned_committed_receipt_survives_late_cancel_or_selection_change(setup, tmp_path, event):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    IncrementalBackupActions(frame, dialogs).create()
    finish_job(frame, 0)
    _, work, done, _ = frame.queued[1]
    receipt = work()
    frame.working = False
    if event == 'cancel':
        frame.cancel_event.set()
    else:
        frame.library = LibraryApp(tmp_path / 'other-library')
    done(receipt)
    assert Path(receipt['path']).is_dir()
    assert dialogs.results[0][0] == 'Incremental backup complete'
    assert receipt['manifest_sha256'] in dialogs.results[0][1]
    if event == 'new-library':
        assert 'previously selected library' in dialogs.results[0][1]
    assert not frame.messages


def test_closed_frame_suppresses_result_controls_but_keeps_committed_backup(setup, tmp_path):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    IncrementalBackupActions(frame, dialogs).create()
    finish_job(frame, 0)
    _, work, done, _ = frame.queued[1]
    receipt = work()
    frame.working = False
    frame._closing = True
    done(receipt)
    assert Path(receipt['path']).is_dir() and not dialogs.results
