"""Exercise the real settings closures with named widgets and explicit pickers."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.ui.keyboard_settings import KeyboardSettings

# Share exact test fixtures without changing production package search paths.
_spec = importlib.util.spec_from_file_location('keymap_dialog_fixtures', Path(__file__).with_name('test_keymap_interchange.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
Frame, frame, wx = _fixtures.Frame, _fixtures.frame, _fixtures.wx
_dialog_wx = _fixtures._menu_fixtures._dialog_wx
keymap, state_path = _fixtures.keymap, _fixtures.state_path


def dialog_fixture(wx, interaction, selections=(), *, on_picker=lambda: None):
    _dialog_wx(wx, interaction)
    wx.ID_OK, wx.FD_OPEN, wx.FD_FILE_MUST_EXIST, wx.FD_SAVE = 1, 2, 4, 8
    queue, seen = list(selections), []
    class Picker:
        def __init__(self, parent, title, **kwargs):
            self.selection = queue.pop(0)
            self.path = None if self.selection is None else str(self.selection)
            seen.append((title, kwargs['style']))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self):
            on_picker()
            return wx.ID_CANCEL if self.path is None else wx.ID_OK
        def GetPath(self): return self.path
    wx.FileDialog = Picker
    return seen


def named(dialog):
    return {control.name: control for control in dialog.controls if control.name}


def press(controls, label, wx):
    controls[label].handlers[wx.EVT_BUTTON](None)


def test_native_closures_preview_apply_full_map_undo_and_cancel(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9', 'help/keyboard guide': ''})
    before = state_path(controller).read_bytes()
    source = keymap(tmp_path / 'source.json', {'file/save': 'Ctrl+I'})
    def interact(dialog):
        controls = named(dialog)
        assert dialog.escape == wx.ID_CANCEL
        access = [control.label.split('&', 1)[1][0].casefold() for control in dialog.controls if '&' in control.label]
        assert len(access) == len(set(access)), 'Dialog access keys must identify one control each.'
        assert not controls['Apply reviewed import'].enabled and not controls['Undo last change'].enabled
        press(controls, 'Preview keymap import...', wx)
        assert controls['Apply reviewed import'].enabled
        assert 'Default made unassigned' in controls['Shortcut settings status'].value
        assert controls['Shortcut settings status'].focused
        assert state_path(controller).read_bytes() == before
        press(controls, 'Apply reviewed import', wx)
        assert controller.overrides == {'file/save': 'Ctrl+I', 'library/import books': '',
                                        'library/new library': 'F9', 'help/keyboard guide': ''}
        assert not controls['Apply reviewed import'].enabled and controls['Undo last change'].enabled
        press(controls, 'Undo last change', wx)
        assert state_path(controller).read_bytes() == before
        assert not controls['Undo last change'].enabled
        press(controls, 'Preview keymap import...', wx)
        assert controls['Apply reviewed import'].enabled
        # Closing cancels this second preview, leaving the previously restored map.
    seen = dialog_fixture(wx, interact, [source, source])
    controller.show()
    assert state_path(controller).read_bytes() == before
    assert seen == [('Preview keymap import', wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)] * 2


def test_reset_and_selected_edit_each_have_complete_map_undo(frame, wx):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'library/new library': 'F9', 'library/import books': ''})
    original = state_path(controller).read_bytes()
    def interact(dialog):
        controls = named(dialog)
        press(controls, 'Reset all shortcuts to original keys', wx)
        assert controller.overrides == {}
        press(controls, 'Undo last change', wx)
        assert state_path(controller).read_bytes() == original
        controls['Command to change'].SetSelection(0)
        controls['Command to change'].handlers[wx.EVT_CHOICE](None)
        controls['Keyboard shortcut; empty means unassigned'].ChangeValue('F12')
        press(controls, 'Apply', wx)
        assert controller.overrides['file/save'] == 'F12'
        press(controls, 'Undo last change', wx)
        assert state_path(controller).read_bytes() == original
    dialog_fixture(wx, interact)
    controller.show()


@pytest.mark.parametrize('change', ['key', 'selection', 'source'])
def test_stale_editor_or_file_cannot_apply_preceding_import(frame, wx, tmp_path, change):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'source.json', {'file/save': 'F9'})
    def interact(dialog):
        controls = named(dialog)
        press(controls, 'Preview keymap import...', wx)
        if change == 'key':
            key = controls['Keyboard shortcut; empty means unassigned']
            key.ChangeValue('F12'); key.handlers[wx.EVT_TEXT](SimpleNamespace(Skip=lambda: None))
        elif change == 'selection':
            choice = controls['Command to change']
            choice.SetSelection(2); choice.handlers[wx.EVT_CHOICE](SimpleNamespace())
        else:
            source.write_bytes(source.read_bytes().replace(b'F9', b'F8'))
        # Direct invocation also verifies the handler cannot bypass disabled controls.
        press(controls, 'Apply reviewed import', wx)
        assert not controls['Apply reviewed import'].enabled
        assert 'not applied' in controls['Shortcut settings status'].value
        assert not state_path(controller).exists()
    dialog_fixture(wx, interact, [source])
    controller.show()


def test_conflicting_review_explains_recognized_commands_but_never_enables_apply(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'bad.json', {'file/save': 'F9', 'old/removed': 'F8'},
                    {'file/save': 'Ctrl+S', 'old/removed': ''})
    def interact(dialog):
        controls = named(dialog)
        press(controls, 'Preview keymap import...', wx)
        assert 'File / Save [file/save]' in controls['Shortcut settings status'].value
        assert 'Unavailable command: old/removed' in controls['Shortcut settings status'].value
        assert not controls['Apply reviewed import'].enabled
        press(controls, 'Apply reviewed import', wx)
        assert not state_path(controller).exists()
    dialog_fixture(wx, interact, [source])
    controller.show()


@pytest.mark.parametrize('operation', ['Preview keymap import...', 'Export keymap...'])
def test_cancelled_file_picker_leaves_saved_and_preview_state_unchanged(frame, wx, tmp_path, operation):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'source.json', {'file/save': 'F9'})
    def interact(dialog):
        controls = named(dialog)
        press(controls, 'Preview keymap import...', wx)
        reviewed = controls['Shortcut settings status'].value
        press(controls, operation, wx)
        assert controls['Apply reviewed import'].enabled
        assert controls['Shortcut settings status'].value == reviewed
        assert not state_path(controller).exists()
    dialog_fixture(wx, interact, [source, None])
    controller.show()


@pytest.mark.parametrize('operation', ['Preview keymap import...', 'Export keymap...'])
def test_file_picker_cannot_retarget_a_reloaded_library_object(frame, wx, tmp_path, operation):
    controller = KeyboardSettings(frame, wx_module=wx)
    source = keymap(tmp_path / 'source.json', {'file/save': 'F9'})
    destination = tmp_path / 'export.json'
    def changed_library():
        frame.library = SimpleNamespace(root=frame.library.root)
        controller.reload()
    def interact(dialog):
        controls = named(dialog)
        press(controls, operation, wx)
        assert 'library or commands changed' in controls['Shortcut settings status'].value
        assert not state_path(controller).exists() and not destination.exists()
    dialog_fixture(wx, interact, [source if operation.startswith('Preview') else destination], on_picker=changed_library)
    controller.show()


def test_actual_native_export_handler_never_replaces_selected_file(frame, wx, tmp_path):
    controller = KeyboardSettings(frame, wx_module=wx)
    controller.apply({'file/save': 'F9'})
    destination = tmp_path / 'export.json'
    def interact(dialog):
        controls = named(dialog)
        press(controls, 'Export keymap...', wx)
        raw = destination.read_bytes()
        assert raw == state_path(controller).read_bytes()
        assert 'exported to a new file' in controls['Shortcut settings status'].value
        press(controls, 'Export keymap...', wx)
        assert destination.read_bytes() == raw
        assert 'already exists' in controls['Shortcut settings status'].value
    seen = dialog_fixture(wx, interact, [destination, destination])
    controller.show()
    assert seen == [('Export keymap to a new file', wx.FD_SAVE)] * 2


def test_preceding_undo_and_callbacks_cannot_overwrite_newer_changes_or_closed_dialog(frame, wx):
    controller = KeyboardSettings(frame, wx_module=wx)
    retained = []
    def interact(dialog):
        controls = named(dialog)
        retained.append(controls)
        controls['Keyboard shortcut; empty means unassigned'].ChangeValue('F9')
        press(controls, 'Apply', wx)
        other = KeyboardSettings(Frame(frame.library.root), wx_module=wx)
        other.apply({'file/save': 'F12'})
        press(controls, 'Undo last change', wx)
        assert 'Another window changed' in controls['Shortcut settings status'].value
    dialog_fixture(wx, interact)
    controller.show()
    raw = state_path(controller).read_bytes()
    for name in ('Apply', 'Reset all shortcuts to original keys', 'Undo last change',
                 'Preview keymap import...', 'Apply reviewed import', 'Export keymap...'):
        press(retained[0], name, wx)
    assert state_path(controller).read_bytes() == raw
