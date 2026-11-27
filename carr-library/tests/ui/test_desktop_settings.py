"""Actual desktop settings handlers over thread-checked native-control stand-ins."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from carr_library.desktop_preferences import DesktopPreferenceStore, DesktopPreferences
from carr_library.ui.desktop_settings import show_desktop_settings

spec = spec_from_file_location('desktop_controller_fixture', Path(__file__).with_name('test_desktop_integration.py'))
fixtures = module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Widget:
    widgets = []
    def __init__(self, parent=None, *args, **kwargs):
        self.parent, self.owner = parent, get_ident()
        self.name = kwargs.get('label', next((value for value in args if isinstance(value, str)), ''))
        self.value = kwargs.get('value', '')
        self.choices, self.selection = list(kwargs.get('choices', ())), -1
        self.enabled, self.destroyed, self.handlers = True, False, {}
        self.identity, self.destroy_calls = len(self.widgets) + 400, 0
        self.widgets.append(self)
    def native(self):
        assert get_ident() == self.owner and not self.destroyed
    def GetId(self): self.native(); return self.identity
    def SetName(self, value): self.native(); self.name = value
    def SetValue(self, value): self.native(); self.value = value
    def ChangeValue(self, value): self.SetValue(value)
    def GetValue(self): self.native(); return self.value
    def SetSelection(self, value): self.native(); self.selection = value
    def GetSelection(self): self.native(); return self.selection
    def Enable(self, enabled): self.native(); self.enabled = enabled
    def SetFocus(self): self.native()
    def Add(self, *args, **kwargs): self.native()
    def SetSizer(self, *args): self.native()
    def SetEscapeId(self, *args): self.native()
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def Destroy(self):
        self.native()
        self.destroy_calls += 1
        self.destroyed = True
        if 'destroy' in self.handlers:
            self.handlers['destroy'](SimpleNamespace(GetId=lambda: self.identity, Skip=lambda: None))


@pytest.fixture
def ui(tmp_path, monkeypatch):
    Widget.widgets = []
    e = fixtures.desktop(tmp_path)
    e.owner.hotkeys.platform, e.owner.hotkeys.native = 'win32', fixtures.Native()
    e.driver = lambda window: None
    class Dialog(Widget):
        def ShowModal(self): self.native(); e.driver(self); return 2
    class Label(Widget): pass
    class Control(Widget): pass
    wx = SimpleNamespace(Dialog=Dialog, StaticText=Label, TextCtrl=Control, Choice=Control,
        CheckBox=Control, Button=Control, BoxSizer=Widget, ID_CANCEL=2, ID_ANY=-1,
        EVT_BUTTON='button', EVT_CHOICE='choice', EVT_WINDOW_DESTROY='destroy', VERTICAL=0, HORIZONTAL=0, EXPAND=0,
        ALL=0, LEFT=0, TOP=0, TE_READONLY=0, TE_MULTILINE=0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    def find(name):
        values = [widget for widget in Widget.widgets if isinstance(widget, Control) and widget.name == name]
        assert len(values) == 1, name
        return values[0]
    def press(name):
        button = find(name)
        assert button.enabled
        button.handlers['button'](None)
    e.find, e.press = find, press
    e.open = lambda: show_desktop_settings(e.frame, e.owner)
    yield e
    e.owner.close()


def test_native_handler_saves_exact_choices_then_explicitly_enables_and_releases(ui):
    e = ui
    def drive(window):
        assert not e.owner.hotkeys.native.calls and not e.owner.watch.enabled
        e.find('Global action').SetSelection(1)
        e.find('Global action').handlers['choice'](None)
        e.find('Global shortcut').ChangeValue('Ctrl+Alt+Shift+K')
        e.press('Save choices')
        saved = DesktopPreferenceStore(e.frame.library.root).load()
        assert saved.global_shortcuts == {'voice.clipboard': 'Ctrl+Alt+Shift+K'}
        assert not e.owner.hotkeys.native.calls
        e.press('Enable saved global shortcuts')
        assert e.owner.hotkeys.enabled and not e.find('Save choices').enabled
        e.press('Disable global shortcuts')
        assert not e.owner.hotkeys.enabled and not e.owner.hotkeys.native.keys
        e.press('Start clipboard watching')
        assert e.owner.watch.enabled and e.owner._read.call_count == 0
        e.press('Stop clipboard watching')
        assert not e.owner.watch.enabled and not e.owner.timer.running
    e.driver = drive
    e.open()


def test_invalid_assignment_and_unsaved_enable_leave_existing_file_and_keys_untouched(ui):
    e = ui
    def drive(window):
        e.find('Global action').SetSelection(1)
        e.find('Global action').handlers['choice'](None)
        e.find('Global shortcut').ChangeValue('Ctrl+Alt+V')
        e.press('Save choices')
        assert 'Ctrl+Shift' in e.find('Desktop integration status').value
        assert not e.owner.store.path.exists() and not e.owner.hotkeys.native.calls
        e.find('Global shortcut').ChangeValue('Ctrl+Shift+K')
        e.press('Enable saved global shortcuts')
        assert 'Save or reload' in e.find('Desktop integration status').value
        assert not e.owner.hotkeys.native.calls
    e.driver = drive
    e.open()


def test_unknown_saved_assignment_can_be_removed_without_granting_it_runtime_authority(ui):
    e = ui
    e.owner.store.save(DesktopPreferences(global_shortcuts={'future.command': 'Ctrl+Shift+K'}))
    e.owner.preferences = e.owner.store.load()
    def drive(window):
        choice = e.find('Global action')
        assert choice.choices[-1] == 'Unavailable saved action: future.command'
        choice.SetSelection(len(choice.choices) - 1)
        choice.handlers['choice'](None)
        e.press('Clear assignment'); e.press('Save choices')
        assert not e.owner.preferences.global_shortcuts and not e.owner.hotkeys.native.calls
    e.driver = drive
    e.open()


def test_scope_change_and_late_callback_do_not_write_or_touch_destroyed_controls(ui):
    e = ui
    callback = []
    def drive(window):
        callback.append(e.find('Save choices').handlers['button'])
        e.frame.active = ('different-workspace', None)
        e.press('Save choices')
        assert 'selection changed' in e.find('Desktop integration status').value
        assert not e.owner.store.path.exists()
    e.driver = drive
    e.open()
    for widget in Widget.widgets:
        widget.destroyed = True
    callback[0](None)
    assert not e.owner.store.path.exists() and not e.owner.hotkeys.native.calls


@pytest.mark.parametrize('callback_name', ['Save choices', 'Start clipboard watching'])
def test_actual_owner_destruction_revokes_queued_settings_handlers_and_skips_second_destroy(ui, callback_name):
    e = ui
    callbacks, dialogs = [], []
    def drive(window):
        dialogs.append(window)
        callbacks.append(e.find(callback_name).handlers['button'])
        window.Destroy()  # Wrapper is already invalid when its owner-ID event runs.
        for widget in Widget.widgets:
            widget.destroyed = True
        callbacks[0](None)
    e.driver = drive
    e.open()
    assert dialogs[0].destroy_calls == 1
    callbacks[0](None)
    assert not e.owner.store.path.exists()
    assert not e.owner.watch.enabled and not e.owner.hotkeys.native.calls


def test_child_destroy_notification_does_not_revoke_live_settings(ui):
    e = ui
    def drive(window):
        child = e.find('Global shortcut')
        window.handlers['destroy'](SimpleNamespace(GetId=lambda: child.identity, Skip=lambda: None))
        e.press('Save choices')
        assert e.owner.store.path.exists()
        assert 'Choices saved' in e.find('Desktop integration status').value
    e.driver = drive
    e.open()
