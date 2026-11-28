"""Queued desktop backup actions call real snapshots and reject stale scope."""
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_incremental import BackupReference, create_incremental_backup
from carr_library.library_backup import create_backup
from carr_library.ui.backup_incremental_dialog import IncrementalBackupActions


class Frame:
    def __init__(self, app):
        self.library, self.working, self._closing = app, False, False
        self.cancel_event, self.queued, self.messages = Event(), [], []
    def run_job(self, label, work, done):
        self.queued.append((label, work, done))
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
    _, work, done = frame.queued[0]
    receipt = work()
    assert Path(receipt['path']).is_dir() and receipt['base_sha256'] == checksum
    done(receipt)
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
    _, work, done = frame.queued[0]
    result = work()
    done(result)
    assert not frame.messages
    if action == 'restore':
        restored = LibraryApp(result['restored_root'])
        assert restored.edition_metadata(workspace, 'original').title == 'Retained source'
        assert 'require review' in dialogs.results[0][1]
    else:
        assert result['chain'] == [layer.manifest_sha256, checksum]


@pytest.mark.parametrize('change', ['instance', 'root', 'closing', 'cancelled'])
def test_queued_action_cannot_follow_changed_scope_or_cancellation(setup, tmp_path, change):
    app, _, base, checksum = setup
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    IncrementalBackupActions(frame, dialogs).create()
    assert len(frame.queued) == 1
    if change == 'instance':
        frame.library = LibraryApp(tmp_path / 'other')
    elif change == 'root':
        app.root = tmp_path / 'other-root'
    elif change == 'closing':
        frame._closing = True
    else:
        frame.cancel_event.set()
    with pytest.raises((ValueError, InterruptedError)):
        frame.queued[0][1]()
    assert not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    assert not dialogs.results


def test_cancelled_folder_or_wrong_checksum_never_queues_work(setup):
    app, _, base, checksum = setup
    first = Frame(app)
    IncrementalBackupActions(first, Dialogs([None], checksum)).create()
    assert not first.queued and not first.messages
    second = Frame(app)
    IncrementalBackupActions(second, Dialogs([base], '0' * 64)).create()
    assert not second.queued and any('digest mismatch' in message for message in second.messages)
