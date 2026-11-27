"""One native manager with both published keymap producers, without silent conversion."""
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.command_registry import CommandSpec
from carr_library.ui.keyboard_settings import (
    KeyboardSettings, install_keyboard_settings, register_menu_command,
)


@pytest.fixture
def native_contract(tmp_path, monkeypatch):
    # Reuse authored controls, not a replacement product module or source path.
    old = runpy.run_path(str(Path(__file__).with_name('test_keyboard_settings.py')))
    wx = old['wx'].__wrapped__(monkeypatch)
    wx.CallAfter = lambda callback, *args: callback(*args)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    frame = old['Frame'](tmp_path / 'library')
    frame.IsBeingDeleted = lambda: False
    return old, wx, frame


def legacy_settings(frame, wx):
    value = KeyboardSettings(frame, wx_module=wx)
    frame.keyboard_settings = value
    return value


def test_existing_legacy_map_stays_active_and_writable_with_exact_f24(native_contract):
    old, wx, original = native_contract
    previous = legacy_settings(original, wx)
    previous.apply({'library/new library': 'Ctrl+F24'})
    path = original.library.root / 'state/ui/shortcuts.json'
    before = path.read_bytes()
    restarted = old['Frame'](original.library.root)
    manager = install_keyboard_settings(restarted)
    facade = restarted.keyboard_settings
    assert facade.manager is manager is restarted.keyboard_commands
    assert manager.store.legacy and manager.shortcut('library.new') == 'Ctrl+F24'
    assert path.read_bytes() == before
    assert not (original.library.root / 'keyboard-shortcuts.json').exists()
    facade.apply({'library/new library': 'F23'})
    value = json.loads(path.read_bytes())
    assert value == {'schema': 1, 'overrides': {'library/new library': 'F23'},
                     'originals': {'library/new library': 'Ctrl+N'}}
    assert manager.shortcut('library.new') == 'F23'


def test_explicit_canonical_command_keeps_its_legacy_f24_assignment(native_contract):
    old, wx, original = native_contract
    previous = legacy_settings(original, wx)
    previous.apply({'library/import books': 'F24'})
    path = original.library.root / 'state/ui/shortcuts.json'
    before = path.read_bytes()
    restarted = old['Frame'](original.library.root)
    item = restarted.menu_bar.GetMenu(1).items[1]
    register_menu_command(restarted, CommandSpec('library.import', 'Import books',
                                                default_shortcut='Ctrl+I'), item)
    manager = install_keyboard_settings(restarted)
    assert manager.shortcut('library.import') == 'F24'
    assert path.read_bytes() == before and manager.store.legacy


def test_canonical_file_wins_without_rewriting_existing_legacy_bytes(native_contract):
    old, wx, original = native_contract
    previous = legacy_settings(original, wx)
    previous.apply({'library/new library': 'F24'})
    legacy = original.library.root / 'state/ui/shortcuts.json'
    before = legacy.read_bytes()
    canonical = original.library.root / 'keyboard-shortcuts.json'
    KeymapStore(canonical).save({'library.new': 'Ctrl+Shift+K'}, previous.manager.registry)
    restarted = old['Frame'](original.library.root)
    manager = install_keyboard_settings(restarted)
    assert not manager.store.legacy and manager.store.path == canonical
    assert manager.shortcut('library.new') == 'Ctrl+Shift+K'
    manager.apply_overrides({'library.new': 'Ctrl+Alt+J'})
    assert json.loads(canonical.read_bytes()) == {
        'version': 1, 'bindings': {'library.new': 'Ctrl+Alt+J'}}
    assert legacy.read_bytes() == before


def test_conversion_requires_explicit_supported_draft_and_retains_old_bytes(native_contract):
    old, wx, frame = native_contract
    facade = legacy_settings(frame, wx)
    facade.apply({'library/new library': 'Ctrl+F24'})
    manager = facade.manager
    legacy = manager.store.path
    before = legacy.read_bytes()
    with pytest.raises(ValueError):
        manager.store.conversion_draft(manager.overrides, manager.registry)
    assert not (frame.library.root / 'keyboard-shortcuts.json').exists()
    assert legacy.read_bytes() == before
    draft = {'library.new': 'Ctrl+Shift+K', 'reader.next': 'Alt+PageDown'}
    reviewed = manager.store.conversion_draft(draft, manager.registry)
    token = manager.store.token
    assert manager.store.legacy and legacy.read_bytes() == before
    manager.convert_overrides(reviewed, expected_token=token)
    assert not manager.store.legacy
    assert json.loads(manager.store.path.read_bytes()) == {'version': 1, 'bindings': draft}
    assert legacy.read_bytes() == before
    assert facade.manager is manager and facade.overrides == {'library/new library': 'Ctrl+Shift+K'}


def test_legacy_save_cannot_drop_contextual_binding_or_overwrite_new_canonical_map(native_contract):
    old, wx, frame = native_contract
    facade = legacy_settings(frame, wx)
    facade.apply({'library/new library': 'F9'})
    manager = facade.manager
    legacy = manager.store.path
    before = legacy.read_bytes()
    with pytest.raises(ValueError, match='contextual'):
        manager.apply_overrides({'library.new': 'F9', 'reader.next': 'Alt+PageDown'})
    assert manager.overrides == {'library.new': 'F9'} and legacy.read_bytes() == before
    canonical = frame.library.root / 'keyboard-shortcuts.json'
    KeymapStore(canonical).save({'library.new': 'F12'}, manager.registry)
    independent = canonical.read_bytes()
    with pytest.raises(ValueError, match='appeared'):
        facade.apply({'library/new library': 'Ctrl+Shift+K'})
    assert canonical.read_bytes() == independent and legacy.read_bytes() == before
    assert manager.shortcut('library.new') == 'F9'


def test_change_root_keeps_legacy_api_producer_and_one_manager(native_contract, tmp_path):
    old, wx, frame = native_contract
    facade = legacy_settings(frame, wx)
    facade.apply({'library/new library': 'F9'})
    manager = facade.manager
    original = manager.store.path
    before = original.read_bytes()
    other = tmp_path / 'other-library'
    other.mkdir()
    frame.library = SimpleNamespace(root=other)
    assert facade.reload()
    facade.apply({'library/new library': 'F12'})
    assert facade.manager is manager is frame.keyboard_commands
    assert manager.store.legacy
    assert json.loads((other / 'state/ui/shortcuts.json').read_bytes())['overrides'] == {
        'library/new library': 'F12'}
    assert not (other / 'keyboard-shortcuts.json').exists()
    assert original.read_bytes() == before



@pytest.mark.parametrize('existing', [False, True])
def test_canonical_dialog_export_never_serializes_unsupported_legacy_keys(
        native_contract, tmp_path, existing):
    from carr_library.ui.keyboard_settings import KeyboardSettingsDialog
    from carr_library.keyboard_preferences import validate_keymap

    _old, wx, frame = native_contract
    facade = legacy_settings(frame, wx)
    facade.apply({'library/new library': 'Ctrl+F24'})
    manager = facade.manager
    legacy_path = manager.store.path
    legacy_bytes = legacy_path.read_bytes()
    original_overrides = dict(manager.overrides)
    target = tmp_path / 'exported-keymap.json'
    original = b'An independently retained export.'
    if existing:
        target.write_bytes(original)

    class Chooser:
        def __init__(self, *args, **kwargs):
            self.live = True
        def __bool__(self):
            return self.live
        def ShowModal(self):
            return wx.ID_OK
        def GetPath(self):
            assert self.live
            return str(target)
        def Destroy(self):
            assert self.live
            self.live = False

    wx.FileDialog = Chooser
    wx.ID_OK, wx.FD_SAVE = 1, 8
    wx.YES_NO, wx.NO_DEFAULT, wx.ICON_QUESTION, wx.YES = 32, 64, 128, 256
    wx.MessageBox = lambda *args: wx.YES
    dialog = object.__new__(KeyboardSettingsDialog)
    dialog.wx, dialog.manager, dialog.closed = wx, manager, False
    dialog.dialog = SimpleNamespace()
    dialog.draft = dict(manager.overrides)
    messages = []
    dialog._message = messages.append

    # This is the actual export handler, captured destination and writer.
    # A legacy-only F24 choice must not become unreadable canonical version1.
    dialog.export_map()
    assert 'not exported' in messages[-1].lower()
    assert target.read_bytes() == original if existing else not target.exists()
    assert manager.overrides == original_overrides
    assert manager.shortcut('library.new') == 'Ctrl+F24'
    assert legacy_path.read_bytes() == legacy_bytes
    assert manager.store.legacy
    assert not list(tmp_path.glob('.research-export-*'))

    # A separately reviewed supported draft can be exported and read by the
    # canonical producer without silently converting the saved legacy map.
    dialog.draft = {'library.new': 'Ctrl+Shift+K'}
    dialog.export_map()
    assert 'was exported' in messages[-1]
    assert validate_keymap(target.read_bytes()) == dialog.draft
    assert KeymapStore(target).load() == dialog.draft
    assert manager.overrides == original_overrides
    assert manager.shortcut('library.new') == 'Ctrl+F24'
    assert legacy_path.read_bytes() == legacy_bytes and manager.store.legacy
    assert not (frame.library.root / 'keyboard-shortcuts.json').exists()
