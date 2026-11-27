"""Keyboard export keeps destination hashing and publication off the GUI thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.keybindings import Command, encode_preferences
from carr_library.ui.productivity import KeyboardActions, MenuBinding


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'library'
    root.mkdir()
    output = tmp_path / 'keyboard.json'
    output.write_bytes(b'An existing user file.')
    queue, selected_styles = [], []
    host = SimpleNamespace(library=SimpleNamespace(root=root), active=('workspace', 'topic'),
        working=False, busy=False, _closing=False, cancel_event=Event(), report=Mock())
    def run_job(title, operation, done):
        host.cancel_event.clear()
        queue.append((title, operation, done))
    host.run_job = Mock(side_effect=run_job)
    item = SimpleNamespace(GetItemLabel=lambda: 'Save\tCtrl+S', SetItemLabel=Mock())
    command = Command('File/Save', 'File — Save', 'Ctrl+S')
    keyboard = KeyboardActions(host, [MenuBinding(command, item, 'Save')])
    keyboard.overrides = {'File/Save': 'Ctrl+Shift+S'}
    host.keyboard_actions = keyboard
    class Picker:
        def __init__(self, *args, **kwargs):
            selected_styles.append(kwargs['style'])
        def ShowModal(self): return 1
        def GetPath(self): return str(output)
        def Destroy(self): pass
    wx = SimpleNamespace(FileDialog=Picker, ID_OK=1, FD_SAVE=2, FD_OPEN=4, FD_FILE_MUST_EXIST=8,
        FD_OVERWRITE_PROMPT=16, YES=32, NO=64, YES_NO=128, NO_DEFAULT=256, ICON_QUESTION=512,
        MessageBox=Mock(return_value=32))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return SimpleNamespace(host=host, keyboard=keyboard, output=output, queue=queue, wx=wx,
                           styles=selected_styles)


def run_capture(setup):
    assert len(setup.queue) == 1
    title, operation, done = setup.queue.pop()
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(operation).result(timeout=10)
    return result, done


def test_hash_and_write_run_on_workers_and_confirmation_runs_on_ui(setup, monkeypatch):
    from carr_library.ui import productivity
    capture, write = productivity.capture_export_destination, productivity.write_binary_export
    main, threads, approvals = get_ident(), [], []
    def observed_capture(*args, **kwargs):
        threads.append(('capture', get_ident()))
        result = capture(*args, **kwargs)
        approvals.append(result)
        return result
    def observed_write(*args, **kwargs):
        threads.append(('write', get_ident()))
        assert kwargs['approval'] is approvals[0]
        return write(*args, **kwargs)
    monkeypatch.setattr(productivity, 'capture_export_destination', observed_capture)
    monkeypatch.setattr(productivity, 'write_binary_export', observed_write)
    setup.wx.MessageBox.side_effect = lambda *args: (
        threads.append(('confirm', get_ident())) or setup.wx.YES)
    before = setup.output.read_bytes()
    setup.keyboard.transfer(save=True)
    assert threads == [] and setup.wx.MessageBox.call_count == 0
    assert setup.styles == [setup.wx.FD_SAVE]
    approval, completed = run_capture(setup)
    assert setup.output.read_bytes() == before
    completed(approval)
    assert len(setup.queue) == 1 and setup.wx.MessageBox.call_count == 1
    assert setup.output.read_bytes() == before
    title, operation, done = setup.queue.pop()
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(operation).result(timeout=10)
    done(result)
    assert setup.output.read_bytes() == encode_preferences(setup.keyboard.overrides)
    assert [name for name, _ in threads] == ['capture', 'confirm', 'write']
    assert threads[0][1] != main and threads[1][1] == main and threads[2][1] != main
    assert setup.host.report.call_args.args == ('Keyboard shortcuts exported.',)


def test_declined_replacement_never_queues_writer(setup):
    before = setup.output.read_bytes()
    setup.wx.MessageBox.return_value = setup.wx.NO
    setup.keyboard.transfer(save=True)
    approval, completed = run_capture(setup)
    completed(approval)
    assert not setup.queue and setup.output.read_bytes() == before
    assert 'cancelled' in setup.host.report.call_args.args[0]


@pytest.mark.parametrize('change', ['cancel', 'library', 'active', 'preferences'])
def test_modal_confirmation_cannot_clear_a_stop_or_scope_change(setup, change):
    before = setup.output.read_bytes()
    def change_scope(*args):
        if change == 'cancel':
            setup.host.cancel_event.set()
        elif change == 'library':
            setup.host.library = SimpleNamespace(root=setup.host.library.root)
        elif change == 'active':
            setup.host.active = ('another', 'topic')
        else:
            setup.keyboard.overrides = {}
        return setup.wx.YES
    setup.wx.MessageBox.side_effect = change_scope
    setup.keyboard.transfer(save=True)
    approval, completed = run_capture(setup)
    with pytest.raises((InterruptedError, ValueError)):
        completed(approval)
    assert not setup.queue and setup.output.read_bytes() == before


def test_destination_change_after_confirmation_rejects_the_old_approval(setup):
    setup.keyboard.transfer(save=True)
    approval, completed = run_capture(setup)
    completed(approval)
    competitor = b'Newer third-party file that must survive.'
    setup.output.write_bytes(competitor)
    title, operation, done = setup.queue.pop()
    with ThreadPoolExecutor(max_workers=1) as worker:
        with pytest.raises(ValueError, match='changed'):
            worker.submit(operation).result(timeout=10)
    assert setup.output.read_bytes() == competitor
    assert not setup.host.report.called


def test_absent_destination_remains_create_only_without_overwrite_dialog(setup):
    setup.output.unlink()
    setup.keyboard.transfer(save=True)
    approval, completed = run_capture(setup)
    assert approval.require_absent
    completed(approval)
    setup.wx.MessageBox.assert_not_called()
    setup.output.write_bytes(b'New file created while the export was queued.')
    title, operation, done = setup.queue.pop()
    with ThreadPoolExecutor(max_workers=1) as worker:
        with pytest.raises((ValueError, FileExistsError)):
            worker.submit(operation).result(timeout=10)
    assert setup.output.read_bytes() == b'New file created while the export was queued.'


def test_cancel_during_capture_is_seen_by_shared_hash_reader(setup):
    setup.keyboard.transfer(save=True)
    setup.host.cancel_event.set()
    with pytest.raises(InterruptedError):
        run_capture(setup)
    setup.wx.MessageBox.assert_not_called()
    assert setup.output.read_bytes() == b'An existing user file.'
