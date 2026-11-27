"""Shared-worker scope guards with actual local repository operations."""
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup
from carr_library.ui import incremental_backup_dialog as ui


class Frame:
    def __init__(self, library):
        self.library = library
        self._closing = False
        self.working = False
        self.cancel_event = Event()
        self.queued = []
        self.messages = []
        self.opened = []

    def report(self, message):
        self.messages.append(message)

    def run_job(self, title, operation, done):
        self.queued.append((title, operation, done))
        return self.queued[-1]

    def _open_data_root(self, path):
        self.opened.append(path)


@pytest.fixture
def frame(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Backup UI')
    app.add_text(workspace, 'Original', 'Railway original retained.')
    result = Frame(app)
    result.workspace = workspace
    result.outputs = []
    monkeypatch.setitem(sys.modules, 'carr_library.ui.desktop_app',
        SimpleNamespace(show_text=lambda parent, title, text: result.outputs.append((title, text))))
    return result


def select(monkeypatch, command, directories=(), texts=()):
    folders, inputs = iter(directories), iter(texts)
    monkeypatch.setattr(ui, '_action', lambda frame: command)
    monkeypatch.setattr(ui, '_directory', lambda frame, title: next(folders))
    monkeypatch.setattr(ui, '_text', lambda frame, title, prompt, value='': next(inputs))


def execute(frame):
    _, operation, done = frame.queued[-1]
    result = operation()
    done(result)
    return result


def test_ui_actions_use_real_canonical_repository_and_never_switch_library(frame, tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    select(monkeypatch, 'init', [tmp_path], ['repository'])
    ui.show_incremental_backup(frame)
    assert not repository.exists()
    assert execute(frame)['path'] == str(repository)
    select(monkeypatch, 'create', [repository], [''])
    ui.show_incremental_backup(frame)
    snapshot = execute(frame)
    assert snapshot['published']
    assert snapshot['snapshot_sha256'] in frame.outputs[-1][1]
    assert 'typed state' in frame.outputs[-1][1] and 'credentials are excluded' in frame.outputs[-1][1]
    select(monkeypatch, 'list', [repository])
    ui.show_incremental_backup(frame)
    assert execute(frame)[0]['payloads_verified'] is False
    assert 'not a verification' in frame.outputs[-1][1]
    select(monkeypatch, 'verify', [repository], [snapshot['snapshot_sha256']])
    ui.show_incremental_backup(frame)
    assert execute(frame)['payloads_verified']
    select(monkeypatch, 'materialize', [repository, tmp_path],
           [snapshot['snapshot_sha256'], 'ordinary-full'])
    ui.show_incremental_backup(frame)
    assert execute(frame)['published']
    select(monkeypatch, 'restore', [repository, tmp_path],
           [snapshot['snapshot_sha256'], 'restored'])
    ui.show_incremental_backup(frame)
    restored = execute(frame)
    assert restored['restored'] and frame.opened == []
    recovered = LibraryApp(Path(restored['path']))
    assert recovered.search(frame.workspace, 'Railway')[0].quote == 'Railway'
    assert frame.library.root == tmp_path / 'library'
    assert 'Open it explicitly' in frame.outputs[-1][1]


@pytest.mark.parametrize('stage', ['action', 'repository', 'parent', 'checksum', 'output_parent', 'output_name'])
def test_cancelled_selection_does_not_queue_or_write(frame, tmp_path, monkeypatch, stage):
    repository = tmp_path / 'repository'
    if stage == 'action':
        select(monkeypatch, None)
    elif stage == 'repository':
        select(monkeypatch, 'create', [None])
    elif stage == 'parent':
        select(monkeypatch, 'create', [repository], [None])
    elif stage == 'checksum':
        select(monkeypatch, 'restore', [repository], [None])
    elif stage == 'output_parent':
        select(monkeypatch, 'restore', [repository, None], ['a' * 64])
    else:
        select(monkeypatch, 'restore', [repository, tmp_path], ['a' * 64, None])
    ui.show_incremental_backup(frame)
    assert not frame.queued and not repository.exists() and not frame.outputs


@pytest.mark.parametrize('change', ['same_path_replacement', 'root_change', 'close', 'cancel'])
def test_queued_snapshot_checks_actual_library_scope_before_any_write(frame, tmp_path, monkeypatch, change):
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    select(monkeypatch, 'create', [repository], [''])
    ui.show_incremental_backup(frame)
    if change == 'same_path_replacement':
        frame.library = LibraryApp(frame.library.root)
    elif change == 'root_change':
        frame.library.root = tmp_path / 'other-library'
    elif change == 'close':
        frame._closing = True
    else:
        frame.cancel_event.set()
    with pytest.raises(InterruptedError):
        execute(frame)
    assert not list((repository / 'snapshots').iterdir())
    assert not frame.outputs


def test_late_scope_change_suppresses_old_ui_but_preserves_committed_result(frame, tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    select(monkeypatch, 'create', [repository], [''])
    ui.show_incremental_backup(frame)
    _, operation, done = frame.queued[-1]
    result = operation()
    frame.library = LibraryApp(frame.library.root)
    done(result)
    assert not frame.outputs
    assert incremental_backup.verify_snapshot(repository, result['snapshot_sha256'])['payloads_verified']


def test_late_cancel_still_displays_committed_snapshot_identity(frame, tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    select(monkeypatch, 'create', [repository], [''])
    ui.show_incremental_backup(frame)
    _, operation, done = frame.queued[-1]
    result = operation()
    frame.cancel_event.set()
    done(result)
    assert result['published'] and result['snapshot_sha256'] in frame.outputs[-1][1]


@pytest.mark.parametrize('invalid', ['../escape', 'a/b', 'NUL', ''])
def test_new_repository_folder_name_cannot_escape_explicit_parent(frame, tmp_path, monkeypatch, invalid):
    select(monkeypatch, 'init', [tmp_path], [invalid])
    ui.show_incremental_backup(frame)
    assert not frame.queued and frame.messages
    assert not (tmp_path.parent / 'escape').exists()


def test_invalid_independent_token_fails_before_queue(frame, tmp_path, monkeypatch):
    select(monkeypatch, 'verify', [tmp_path / 'repository'], ['not-a-checksum'])
    ui.show_incremental_backup(frame)
    assert not frame.queued and 'SHA256' in frame.messages[-1]


def test_native_entry_registers_one_stable_shared_command(frame, monkeypatch):
    events, registrations = [], []
    item = SimpleNamespace()
    menu = SimpleNamespace(Append=lambda identity, label: item)
    frame.menu_bar = SimpleNamespace(FindMenu=lambda label: 0, GetMenu=lambda index: menu)
    frame.Bind = lambda event, handler, selected: events.append((handler, selected))
    monkeypatch.setitem(sys.modules, 'wx',
                        SimpleNamespace(NOT_FOUND=-1, ID_ANY=-1, EVT_MENU=object()))
    from carr_library.ui import keyboard_settings
    monkeypatch.setattr(keyboard_settings, 'register_menu_command',
                        lambda parent, spec, selected: registrations.append((parent, spec, selected)))
    assert ui.install_incremental_backup(frame) is item
    assert len(events) == len(registrations) == 1
    assert registrations[0][1].command_id == 'library.incremental_backup'
    calls = []
    monkeypatch.setattr(ui, 'show_incremental_backup', lambda parent, event: calls.append(parent))
    events[0][0](None)
    assert calls == [frame]


def test_unknown_durability_and_cleanup_outcomes_remain_truthful():
    message = ui._result_text('create', dict(snapshot_sha256='a' * 64,
        backup_manifest_sha256='b' * 64, new_chunks=2, reused_chunks=4,
        new_bytes=123, reused_bytes=456, durability_confirmed=False))
    assert 'published' in message.casefold() and 'did not confirm durability' in message
    message = ui._result_text('restore', dict(path='/new-library', staging_cleanup_pending=True,
        retained_temporary_directory='/retained-private-copy'))
    assert 'operation completed' in message.casefold() and '/retained-private-copy' in message
