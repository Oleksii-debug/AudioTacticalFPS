"""Reviewed acquisition command rename preserves existing shortcut preferences."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.keybindings import Command, KeybindingStore, encode_preferences
from carr_library.ui.productivity import KeyboardActions, MenuBinding

OLD = 'Academic/DOI lookup and open-access sources'
NEW = 'Academic/DOI and ISBN lookup and open-access sources'
SAVE = 'File/Save'


def commands(*, old=False):
    return (Command(OLD if old else NEW, 'Academic acquisition'),
            Command(SAVE, 'Save', 'Ctrl+S'))


def actions(root, *, old=False):
    host = SimpleNamespace(library=SimpleNamespace(root=root), report=Mock())
    bindings = []
    for command in commands(old=old):
        item = SimpleNamespace(label=command.label)
        item.GetItemLabel = lambda item=item: item.label
        item.SetItemLabel = lambda value, item=item: setattr(item, 'label', value)
        bindings.append(MenuBinding(command, item, command.label))
    return KeyboardActions(host, bindings, dialogs=SimpleNamespace(show=Mock()))


def test_active_reload_migrates_only_the_renamed_command_without_rewriting(tmp_path):
    old = {OLD: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'}
    store = KeybindingStore(tmp_path)
    store.save(commands(old=True), old)
    before = store.path.read_bytes()
    current = actions(tmp_path)
    notice = current.reload()
    assert current.overrides == {NEW: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'}
    assert 'renamed commands' in notice and 'unchanged until you save' in notice
    assert store.path.read_bytes() == before
    current.save(current.overrides)
    assert store.load() == current.overrides and OLD not in store.load()


def test_alias_is_not_applied_when_the_new_command_is_unavailable(tmp_path):
    KeybindingStore(tmp_path).save(commands(old=True), {OLD: 'Ctrl+Shift+D'})
    original = actions(tmp_path, old=True)
    original.reload()
    assert original.overrides == {OLD: 'Ctrl+Shift+D'}


@pytest.mark.parametrize('same', [True, False])
def test_equal_old_new_entries_converge_and_conflicts_fail_closed(tmp_path, same):
    path = tmp_path / 'keybindings.json'
    path.write_bytes(encode_preferences({OLD: 'Ctrl+Shift+D',
        NEW: 'Ctrl+Shift+D' if same else 'Ctrl+Shift+Y', SAVE: 'Ctrl+Alt+S'}))
    before = path.read_bytes()
    current = actions(tmp_path)
    notice = current.reload()
    if same:
        assert current.overrides == {NEW: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'}
    else:
        assert current.overrides == {} and 'Conflicting saved shortcuts' in notice
    assert path.read_bytes() == before


@pytest.mark.parametrize('same', [True, False])
def test_older_shortcut_schema_applies_the_same_exact_alias(tmp_path, same):
    path = tmp_path / 'state' / 'ui' / 'shortcuts.json'
    path.parent.mkdir(parents=True)
    old, new = OLD.casefold(), NEW.casefold()
    path.write_text(json.dumps({'schema': 1,
        'overrides': {old: 'Ctrl+Shift+D', new: 'Ctrl+Shift+D' if same else 'Ctrl+Shift+Y',
                      SAVE.casefold(): 'Ctrl+Alt+S'},
        'originals': {old: '', new: '', SAVE.casefold(): 'Ctrl+S'}}), encoding='utf-8')
    before = path.read_bytes()
    current = actions(tmp_path)
    notice = current.reload()
    if same:
        assert current.overrides == {NEW: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'}
    else:
        assert current.overrides == {} and 'Conflicting previous shortcut' in notice
    assert path.read_bytes() == before and not (tmp_path / 'keybindings.json').exists()


def test_import_previews_and_saves_current_identity_without_modifying_import_source(tmp_path, monkeypatch):
    source = tmp_path / 'imported.json'
    raw = encode_preferences({OLD: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'})
    source.write_bytes(raw)
    root = tmp_path / 'private'
    root.mkdir()
    current = actions(root)
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return 1
        def GetPath(self): return str(source)
        def Destroy(self): pass
    wx = SimpleNamespace(FileDialog=Picker, ID_OK=1, FD_SAVE=2, FD_OPEN=4,
        FD_FILE_MUST_EXIST=8, YES=32, YES_NO=64, NO_DEFAULT=128, ICON_QUESTION=256,
        MessageBox=Mock(return_value=32))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    current.transfer(save=False)
    assert current.overrides == {NEW: 'Ctrl+Shift+D', SAVE: 'Ctrl+Alt+S'}
    assert KeybindingStore(root).load() == current.overrides
    assert source.read_bytes() == raw
    assert 'Academic acquisition' in current.dialogs.show.call_args.args[2]
    wx.MessageBox.assert_called_once()
