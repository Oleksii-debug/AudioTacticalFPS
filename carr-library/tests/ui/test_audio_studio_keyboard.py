"""Current Studio menu registration and real keymap persistence.

The existing keyboard-manager menu doubles exercise semantic IDs and wiring.
Physical Windows key dispatch remains the native keyboard gate's responsibility.
"""
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.audio_studio import install_audio_studio_actions
from carr_library.ui.builtin_keyboard_commands import builtin_commands
from carr_library.ui.command_registry import CommandRegistry, CommandSpec
from carr_library.ui.keyboard_settings import install_keyboard_settings
from tests.ui.test_keyboard_manager import DynamicMenuBar, Frame, Menu


def frame_with_menu(tmp_path, monkeypatch):
    frame = Frame()
    frame.library = SimpleNamespace(root=tmp_path)
    frame.menu = DynamicMenuBar([('&Library', Menu(frame.items), True)])
    frame.menu_bar = frame.menu
    frame.bound = []
    frame.Bind = lambda event, handler, item: frame.bound.append((event, handler, item))
    fake_wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object(),
                              CallAfter=lambda callback, *args: callback(*args))
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda shortcut, identifier: (0, 0, identifier))
    return frame


def test_startup_studio_semantics_bind_the_existing_actions_and_defaults(tmp_path, monkeypatch):
    frame = frame_with_menu(tmp_path, monkeypatch)
    actions = install_audio_studio_actions(frame)
    pending = frame._pending_keyboard_commands
    assert [spec.command_id for spec, _ in pending] == ['audio.studio', 'audio.prepare_book']
    assert [spec.default_shortcut for spec, _ in pending] == ['Ctrl+Alt+A', '']
    assert [handler for _, handler, _ in frame.bound] == [actions.open, actions.from_selected_book]
    assert [item for _, _, item in frame.bound] == [item for _, item in pending]
    assert frame.audio_studio_actions is actions
    manager = install_keyboard_settings(frame)
    assert not frame._pending_keyboard_commands
    assert manager.available('audio.studio') and manager.available('audio.prepare_book')
    assert manager.shortcut('audio.studio') == 'Ctrl+Alt+A'
    assert manager.shortcut('audio.prepare_book') == ''
    assert 'Open Audio Studio' in manager.help_text()


@pytest.mark.parametrize('timing', ['startup', 'late'])
def test_saved_studio_keys_survive_startup_and_late_real_manager_registration(tmp_path, monkeypatch, timing):
    frame = frame_with_menu(tmp_path, monkeypatch)
    specs = (
        CommandSpec('audio.studio', 'Open Audio Studio...', 'main', 'Ctrl+Alt+A'),
        CommandSpec('audio.prepare_book', 'Prepare selected book for narration...', 'main', ''),
    )
    registry = CommandRegistry((*builtin_commands(), *specs))
    saved = {'audio.studio': 'Ctrl+Alt+Shift+A', 'audio.prepare_book': 'Ctrl+Alt+Shift+B'}
    path = tmp_path / 'keyboard-shortcuts.json'
    KeymapStore(path).save(saved, registry)
    original = path.read_bytes()
    if timing == 'late':
        manager = install_keyboard_settings(frame)
        assert manager._pending_saved == saved
        install_audio_studio_actions(frame)
    else:
        install_audio_studio_actions(frame)
        manager = install_keyboard_settings(frame)
    assert manager.overrides == saved
    assert manager._pending_saved is None
    assert manager.load_notice == ''
    assert path.read_bytes() == original
    assert manager.available('audio.studio') and manager.available('audio.prepare_book')
    assert manager.bindings['audio.studio'].item.GetItemLabel().endswith('\tCtrl+Alt+Shift+A')
    assert manager.bindings['audio.prepare_book'].item.GetItemLabel().endswith('\tCtrl+Alt+Shift+B')
    manager.refresh()
    assert manager.overrides == saved
    assert path.read_bytes() == original


def test_studio_semantic_binding_survives_caption_and_native_menu_id_changes(tmp_path, monkeypatch):
    first = frame_with_menu(tmp_path, monkeypatch)
    install_audio_studio_actions(first)
    manager = install_keyboard_settings(first)
    manager.apply_overrides({'audio.studio': 'Ctrl+Alt+Shift+A'})
    original = manager.store.path.read_bytes()
    original_id = manager.bindings['audio.studio'].item.GetId()

    reopened = frame_with_menu(tmp_path, monkeypatch)
    install_audio_studio_actions(reopened)
    pending = reopened._pending_keyboard_commands
    next(item for spec, item in pending if spec.command_id == 'audio.studio').SetItemLabel(
        'Local performance workspace\tCtrl+Alt+A')
    restored = install_keyboard_settings(reopened)
    assert restored.bindings['audio.studio'].item.GetId() != original_id
    assert restored.overrides == {'audio.studio': 'Ctrl+Alt+Shift+A'}
    assert restored.bindings['audio.studio'].item.GetItemLabel() == (
        'Local performance workspace\tCtrl+Alt+Shift+A')
    assert restored.store.path.read_bytes() == original
