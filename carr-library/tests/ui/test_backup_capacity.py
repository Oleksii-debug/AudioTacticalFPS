"""Capacity choice and actual library lifetime on the existing host-worker seam."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library import library_backup
from carr_library.ui import backup_capacity as view


class Choice:
    selection = 0
    outcome = 1
    opened = []
    def __init__(self, *args):
        self.args = args
        self.initial = None
        self.destroyed = False
        self.name = ''
        type(self).opened.append(self)
    def SetName(self, value):
        self.name = value
    def SetSelection(self, value):
        self.initial = value
    def ShowModal(self):
        return self.outcome
    def GetSelection(self):
        return self.selection
    def Destroy(self):
        self.destroyed = True


@pytest.mark.parametrize('selection,expected', [(0, None), (1, 'large-source-v1')])
def test_actual_choice_logic_starts_ordinary_and_requires_selected_ok(monkeypatch, selection, expected):
    Choice.opened = []
    monkeypatch.setattr(Choice, 'selection', selection)
    monkeypatch.setattr(Choice, 'outcome', 1)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1, SingleChoiceDialog=Choice))
    assert view.choose_backup_capacity(object()) == (True, expected)
    assert Choice.opened[-1].initial == 0 and Choice.opened[-1].destroyed
    assert Choice.opened[-1].name == 'Complete-backup capacity'
    assert 'unfinished imports' in Choice.opened[-1].args[1]


def test_choice_cancel_is_not_ordinary_approval(monkeypatch):
    Choice.opened = []
    monkeypatch.setattr(Choice, 'outcome', 2)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1, SingleChoiceDialog=Choice))
    assert view.choose_backup_capacity(object()) == (False, None)
    assert Choice.opened[-1].destroyed


@pytest.mark.parametrize('selection', [True, -1, 2])
def test_invalid_native_selection_refuses_and_destroys_dialog(monkeypatch, selection):
    Choice.opened = []
    monkeypatch.setattr(Choice, 'outcome', 1)
    monkeypatch.setattr(Choice, 'selection', selection)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1, SingleChoiceDialog=Choice))
    with pytest.raises(ValueError, match='profile'):
        view.choose_backup_capacity(object())
    assert Choice.opened[-1].destroyed


def host(tmp_path):
    library = LibraryApp(tmp_path / 'source')
    workspace = library.create_workspace('Whole library backup')
    library.add_text(workspace, 'Original', 'Retained source remains unchanged.')
    calls, messages = [], []
    frame = SimpleNamespace(library=library, _closing=False, working=False,
                            cancel_event=threading.Event(), report=messages.append)
    def run(title, operation, done=None, failed=None):
        calls.append((operation, done, failed))
        return 'owned-host-future'
    frame.run_job = run
    return frame, calls, messages


def arrange(monkeypatch, tmp_path, *, restore=False):
    frame, calls, messages = host(tmp_path)
    outputs = []
    parent = tmp_path / 'outputs'
    parent.mkdir()
    monkeypatch.setattr(view, 'choose_backup_capacity', lambda frame: (True, 'large-source-v1'))
    monkeypatch.setattr(view, '_directory', lambda frame, title: parent)
    monkeypatch.setattr(view, '_show', lambda *args: outputs.append(args))
    return frame, calls, messages, outputs, parent


def test_cancelled_capacity_choice_has_no_picker_worker_or_destination(tmp_path, monkeypatch):
    frame, calls, messages = host(tmp_path)
    monkeypatch.setattr(view, 'choose_backup_capacity', lambda frame: (False, None))
    monkeypatch.setattr(view, '_directory', lambda *args: pytest.fail('Cancelled capacity opened a picker.'))
    before = set(tmp_path.iterdir())
    view.show_complete_backup(frame)
    assert calls == [] and messages == [] and set(tmp_path.iterdir()) == before


def test_real_backup_uses_host_worker_and_reports_committed_pending_inclusion(tmp_path, monkeypatch):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    assert view.show_complete_backup(frame) == 'owned-host-future'
    operation, done, _ = calls.pop()
    identity = operation()
    frame.cancel_event.set()  # A late UI cancel cannot retract the completed result.
    done(identity)
    target, = list(parent.iterdir())
    assert library_backup.verify_backup(target, identity)['pending']['version'] == 1
    assert len(shown) == 1 and identity in shown[0][2]
    assert 'unfinished imports' in shown[0][2] and 'excluded' not in shown[0][2]
    assert messages == []


@pytest.mark.parametrize('change', ['library-object', 'root-path', 'same-path-replaced', 'closing', 'cancel'])
def test_queued_scope_change_refuses_before_real_backup_and_suppresses_stale_callbacks(tmp_path, monkeypatch, change):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    original = frame.library
    view.show_complete_backup(frame)
    operation, done, failed = calls.pop()
    if change == 'library-object':
        frame.library = LibraryApp(original.root)
    elif change == 'root-path':
        original.root = tmp_path / 'other-root'
        original.root.mkdir()
    elif change == 'same-path-replaced':
        original.root.rename(tmp_path / 'retired-root')
        original.root.mkdir()
    elif change == 'closing':
        frame._closing = True
    else:
        frame.cancel_event.set()
    with pytest.raises(InterruptedError) as error:
        operation()
    assert list(parent.iterdir()) == []
    if change != 'cancel':
        done('never-committed')
        failed(error.value)
        assert shown == [] and messages == []


def test_cancel_inside_actual_copy_checkpoint_prevents_publication(tmp_path, monkeypatch):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    view.show_complete_backup(frame)
    operation, _, _ = calls.pop()
    actual = library_backup._copy_originals
    reached = []
    def stopped(*args, **kwargs):
        reached.append(True)
        frame.cancel_event.set()
        return actual(*args, **kwargs)
    monkeypatch.setattr(library_backup, '_copy_originals', stopped)
    with pytest.raises(InterruptedError):
        operation()
    assert reached == [True] and list(parent.iterdir()) == []
    with frame.library.read() as db:
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 1


def test_late_same_path_new_library_cannot_receive_actual_completed_backup(tmp_path, monkeypatch):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    view.show_complete_backup(frame)
    operation, done, failed = calls.pop()
    identity = operation()
    frame.library = LibraryApp(frame.library.root)
    done(identity)
    failed(RuntimeError('Old operation'))
    assert shown == [] and messages == []
    target, = list(parent.iterdir())
    assert library_backup.verify_backup(target, identity)['version'] == 4


def test_post_picker_scope_change_never_submits_worker(tmp_path, monkeypatch):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    def replaced(frame, title):
        frame.library = LibraryApp(frame.library.root)
        return parent
    monkeypatch.setattr(view, '_directory', replaced)
    view.show_complete_backup(frame)
    assert calls == [] and list(parent.iterdir()) == []


def test_real_restore_requires_fresh_destination_and_explicit_open_choice(tmp_path, monkeypatch):
    frame, calls, messages, shown, parent = arrange(monkeypatch, tmp_path)
    source = tmp_path / 'saved-backup'
    identity = frame.library.backup(source)
    selected = iter((source, parent))
    monkeypatch.setattr(view, '_directory', lambda *args: next(selected))
    monkeypatch.setattr(view, '_checksum', lambda *args: identity)
    opened = []
    frame._open_data_root = opened.append
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        YES_NO=1, NO_DEFAULT=2, ICON_QUESTION=4, YES=10,
        MessageBox=lambda *args: 0))
    view.show_complete_backup(frame, restore=True)
    operation, done, _ = calls.pop()
    restored = operation()
    assert restored.parent == parent and restored.is_dir()
    done(restored)
    assert opened == [] and len(shown) == 1
    assert (restored / 'library.sqlite').is_file()
    assert messages == []
