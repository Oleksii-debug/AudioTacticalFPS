"""Probe failure cleanup is testable without claiming native execution."""
import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest


_spec = importlib.util.spec_from_file_location('document_batch_native_probe',
    Path(__file__).resolve().parents[2] / 'scripts/windows_document_batch_smoke.py')
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def test_native_probe_uses_actual_installer_registry_identity_and_registered_menu_callback(tmp_path, monkeypatch):
    from carr_library.ui import document_batch, document_conversion
    from carr_library.ui.keyboard_settings import KeyboardSettings

    path = Path(__file__).resolve().parents[1] / 'ui/test_keyboard_settings.py'
    spec = importlib.util.spec_from_file_location('carr_batch_known_keyboard_fixture', path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    wx = fixture.wx.__wrapped__()
    monkeypatch.setitem(sys.modules, 'wx', wx)
    opened = []
    monkeypatch.setattr(document_batch, 'show_document_batches', lambda frame: opened.append(frame))
    frame = fixture.Frame(tmp_path / 'library')
    document_conversion.install_conversion_actions(frame)
    frame.keyboard_settings = KeyboardSettings(frame, wx_module=wx)
    commands = frame.keyboard_settings.commands

    # Reproduce the observed Windows failure: the native label keeps '&'.
    assert [row for row in commands if 'Document batches' in row.item.GetItemLabel()] == []
    binding = probe._document_batch_command(commands)
    assert binding.original_label == 'Document &batches — split, name and produce...'
    assert binding.title == 'Convert / Document batches — split, name and produce...'
    assert binding.menu_id in frame.bindings
    assert any(spec.command_id == 'conversion.batch' and item.GetId() == binding.menu_id
               for spec, item in frame._pending_keyboard_commands)
    frame.keyboard_settings.apply({**frame.keyboard_settings.overrides, binding.action: 'Ctrl+Shift+F10'})
    assert binding.item.GetItemLabel().endswith('\tCtrl+Shift+F10')
    assert (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_F10, binding.menu_id) in frame.table
    assert probe._document_batch_command(commands) is binding
    assert not opened
    frame.bindings[binding.menu_id](None)
    assert opened == [frame]


@pytest.mark.parametrize('count', [0, 2])
def test_native_probe_does_not_guess_a_missing_or_ambiguous_batch_command(count):
    command = SimpleNamespace(action='convert/document batches — split, name and produce')
    unrelated = SimpleNamespace(action='other/document batches — split, name and produce')
    with pytest.raises(AssertionError, match='exactly one registered'):
        probe._document_batch_command([unrelated, *([command] * count)])


@pytest.mark.parametrize('failure', ['none', 'timer', 'key', 'dialog', 'enumeration'])
def test_abort_retains_outer_loop_and_cleans_only_owned_native_dialogs(failure):
    calls, errors = [], []
    frame = SimpleNamespace(cancel_event=Event())
    foreign = object()
    class Dialog:
        def __init__(self, owner): self.owner = owner
        def GetParent(self): return self.owner
        def IsModal(self): return True
        def EndModal(self, code):
            calls.append(('close', self.owner))
            if failure == 'dialog': raise RuntimeError('close failed')
    owned, unrelated = Dialog(frame), Dialog(foreign)
    def windows():
        if failure == 'enumeration': raise RuntimeError('enumeration failed')
        return [owned, unrelated]
    def stop():
        calls.append('timer')
        if failure == 'timer': raise RuntimeError('timer failed')
    def release():
        calls.append('key')
        if failure == 'key': raise RuntimeError('key failed')
    loop = SimpleNamespace(ScheduleExit=lambda code: calls.append(('outer_exit', code)))
    app = SimpleNamespace(GetMainLoop=lambda: pytest.fail('The active nested loop must not replace the retained owner.'))
    wx = SimpleNamespace(Dialog=Dialog, GetTopLevelWindows=windows, ID_CANCEL=9)
    if failure == 'enumeration':
        with pytest.raises(RuntimeError, match='enumeration'):
            probe._abort(wx, app, frame, loop, SimpleNamespace(Stop=stop), release, errors, 'original failure')
    else:
        probe._abort(wx, app, frame, loop, SimpleNamespace(Stop=stop), release, errors, 'original failure')
    assert frame.cancel_event.is_set() and 'timer' in calls and 'key' in calls
    assert ('close', foreign) not in calls
    assert calls[-1] == ('outer_exit', 1) and errors[0] == 'original failure'
    if failure not in ('none', 'enumeration'):
        assert len(errors) == 2
