"""Keyboard preference and save behavior, independent from native NVDA evidence."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.keybindings import (Command, KeybindingStore, decode_preferences,
    encode_preferences, normalize_shortcut, validate_bindings)
from carr_library.ui.productivity import KeyboardActions, MenuBinding, collect_commands, save_current


def commands():
    return (Command('File/Save', 'File — Save', 'Ctrl+S'),
            Command('Library/Open', 'Library — Open', 'Ctrl+O'),
            Command('Library/Exit', 'Library — Exit', 'Alt+F4', False))


@pytest.mark.parametrize(('value', 'expected'), [
    ('ctrl + shift + s', 'Ctrl+Shift+S'), ('f12', 'F12'),
    ('SHIFT+alt+f8', 'Alt+Shift+F8'), ('Ctrl+PageUp', 'Ctrl+PageUp'), ('', '')])
def test_shortcut_normalization(value, expected):
    assert normalize_shortcut(value) == expected


@pytest.mark.parametrize('value', ['S', 'Shift+S', 'Alt+F', 'Ctrl+Ctrl+O', 'Ctrl++S',
    'Ctrl+C', 'Ctrl+V', 'Ctrl+Z', 'Alt+F4', 'Ctrl+Alt+Delete', 'F25', 'Ctrl+Tab',
    'Ctrl+Shift+Tab', 'Win+R', 'Ctrl+Enter;shutdown', True, 'Ctrl+É'])
def test_typing_system_and_malformed_keys_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_shortcut(value)


def test_conflict_check_is_atomic_and_explains_owner():
    with pytest.raises(ValueError, match='already assigned to File'):
        validate_bindings(commands(), {'Library/Open': 'ctrl+s'})
    values = validate_bindings(commands(), {'File/Save': '', 'Library/Open': 'Ctrl+S'})
    assert values['File/Save'] == '' and values['Library/Open'] == 'Ctrl+S'
    with pytest.raises(ValueError, match='fixed system'):
        validate_bindings(commands(), {'Library/Exit': 'Ctrl+Q'})
    with pytest.raises(ValueError, match='unavailable'):
        validate_bindings(commands(), {'missing': 'Ctrl+Q'})


def test_preferences_survive_reopen_and_conflict_keeps_old_file(tmp_path):
    store = KeybindingStore(tmp_path)
    assert store.load() == {}
    expected = {'File/Save': 'Ctrl+Alt+S'}
    store.save(commands(), expected)
    before = store.path.read_bytes()
    assert KeybindingStore(tmp_path).load() == expected
    with pytest.raises(ValueError):
        store.save(commands(), {'File/Save': 'Ctrl+O'})
    assert store.path.read_bytes() == before


def test_preferences_reject_duplicate_fields_and_untrusted_data():
    with pytest.raises(ValueError):
        decode_preferences(b'{"version":1,"version":1,"kind":"carr-keybindings","overrides":{}}')
    with pytest.raises(ValueError):
        decode_preferences(b'{"version":true,"kind":"carr-keybindings","overrides":{}}')
    with pytest.raises(ValueError):
        encode_preferences({'File/Save': '$(anything)'})
    with pytest.raises(ValueError):
        encode_preferences({'File/Save\nFake': 'Ctrl+S'})


def test_preferences_atomic_write_failure_keeps_previous(tmp_path, monkeypatch):
    store = KeybindingStore(tmp_path)
    store.save(commands(), {})
    before = store.path.read_bytes()
    def full(*args):
        raise OSError('Disk full')
    monkeypatch.setattr('carr_library.keybindings.os.replace', full)
    with pytest.raises(OSError, match='Disk full'):
        store.save(commands(), {'File/Save': 'Ctrl+Alt+S'})
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob('.keybindings-*.tmp'))


def test_preferences_refuse_linked_target(tmp_path):
    import os
    target = tmp_path / 'valuable.json'
    target.write_bytes(encode_preferences({}))
    os.link(target, tmp_path / 'keybindings.json')
    with pytest.raises(ValueError):
        KeybindingStore(tmp_path).save(commands(), {'File/Save': 'Ctrl+Alt+S'})
    assert decode_preferences(target.read_bytes()) == {}


class Item:
    def __init__(self, label, child=None):
        self.label, self.child = label, child
    def IsSeparator(self):
        return self.label is None
    def GetItemLabel(self):
        return self.label
    def SetItemLabel(self, value):
        self.label = value
    def GetSubMenu(self):
        return self.child


def test_dynamic_menu_inventory_keeps_fixed_shortcut_and_nested_ids():
    menu = SimpleNamespace(GetMenuItems=lambda: [Item('&Save\tCtrl+S'), Item(None),
        Item('More', SimpleNamespace(GetMenuItems=lambda: [Item('&Exit\tAlt+F4')]))])
    bar = SimpleNamespace(GetMenuCount=lambda: 1, GetMenu=lambda _: menu, GetMenuLabel=lambda _: '&File')
    bindings = collect_commands(bar)
    assert [binding.command.identity for binding in bindings] == ['File/Save', 'File/More/Exit']
    assert not bindings[1].command.configurable


def keyboard(tmp_path):
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=Mock())
    bindings = [MenuBinding(command, Item(command.label + '\t' + command.shortcut), command.label)
                for command in commands()]
    return KeyboardActions(frame, bindings)


def test_keyboard_reload_invalid_file_falls_back_without_erasing_it(tmp_path):
    action = keyboard(tmp_path)
    action.save({'File/Save': 'Ctrl+Alt+S'})
    (tmp_path / 'keybindings.json').write_text('not JSON', encoding='utf-8')
    assert 'Default keyboard' in action.reload()
    assert action.bindings[0].item.label.endswith('\tCtrl+S')
    assert (tmp_path / 'keybindings.json').read_text(encoding='utf-8') == 'not JSON'


def test_failed_keymap_persistence_does_not_change_active_keys(tmp_path, monkeypatch):
    action = keyboard(tmp_path)
    action.reload()
    old = action.bindings[0].item.label
    def failed(*args):
        raise OSError('Read-only folder')
    monkeypatch.setattr(KeybindingStore, 'save', failed)
    with pytest.raises(OSError):
        action.save({'File/Save': 'Ctrl+Alt+S'})
    assert action.bindings[0].item.label == old
    assert action.overrides == {}


def test_save_routes_dirty_note_without_premature_success():
    panel = SimpleNamespace(_dirty=lambda: True, save_note=Mock())
    frame = SimpleNamespace(working=False, busy=False, evidence=panel,
                            report=Mock(), _restore_note_focus=Mock())
    assert save_current(frame)
    frame._restore_note_focus.assert_called_once()
    panel.save_note.assert_called_once()
    frame.report.assert_not_called()


def test_save_during_operation_keeps_draft_and_reports_no_success():
    panel = SimpleNamespace(_dirty=lambda: True, save_note=Mock())
    frame = SimpleNamespace(working=True, evidence=panel, report=Mock())
    assert not save_current(frame)
    panel.save_note.assert_not_called()
    assert 'running' in frame.report.call_args.args[0]


def test_save_no_draft_is_honest_about_modal_editors():
    frame = SimpleNamespace(working=False, evidence=None, report=Mock())
    assert save_current(frame)
    assert 'already saved' in frame.report.call_args.args[0]
    assert 'editors' in frame.report.call_args.args[0]
