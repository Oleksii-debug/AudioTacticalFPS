"""Real keymap manager and reader adapter with minimal native event/table doubles.

Native key conversion and OS delivery belong to scripts/windows_reader_keymap_smoke.py.
These checks target context routing, shared refresh, and callback lifetime.
"""
from itertools import count
import sys
from types import SimpleNamespace

import pytest

from carr_library.keybindings import Command, KeybindingStore, validate_bindings
from carr_library.ui import keyboard_settings, productivity

reading_shortcuts = keyboard_settings


class _Button:
    def __init__(self, name):
        self.name = self.label = name
        self.enabled = True
        self.destroyed = False

    def _alive(self):
        if self.destroyed:
            raise RuntimeError('Native control has been destroyed.')

    def GetName(self):
        self._alive()
        return self.name

    def SetLabel(self, label):
        self._alive()
        self.label = label

    def Disable(self):
        self.enabled = False

    def IsEnabled(self):
        self._alive()
        return self.enabled


class _Window:
    def __init__(self):
        self.events = {}
        self.table = ()
        self.destroyed = False

    def Bind(self, event, callback, *, id):
        self.events[id] = callback

    def Unbind(self, event, *, id):
        return self.events.pop(id, None) is not None

    def SetAcceleratorTable(self, table):
        assert not self.destroyed, 'A disposed window received new native accelerators.'
        self.table = table

    def key(self, shortcut):
        # The opaque key encoding is supplied by the fixture. No native input
        # is simulated here; this is deterministic event-to-command dispatch.
        matches = [entry for entry in self.table if entry[0] == shortcut]
        assert len(matches) <= 1, 'A context has duplicate effective shortcuts.'
        if matches:
            return self.events[matches[0][2]](None)
        return False


class _MenuItem:
    def __init__(self, label):
        self.label = label
    def GetItemLabel(self):
        return self.label
    def SetItemLabel(self, label):
        self.label = label


@pytest.fixture
def contexts(tmp_path, monkeypatch):
    identifiers = count(100)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        NewIdRef=lambda: next(identifiers), EVT_MENU=object(), AcceleratorTable=tuple,
        MessageBox=lambda *args: 1, YES=1, YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8))

    def entry(shortcut, identifier, wx):
        return shortcut, 0, int(identifier)

    monkeypatch.setattr(keyboard_settings, 'reader_accelerator', entry)
    commands = (
        Command('Library/New library', 'Library — New library', 'Ctrl+N'),
        Command('Academic/Academic library', 'Academic — Academic library', 'Ctrl+Shift+B'),
        Command('Library/Library tools', 'Library — Library tools', 'Ctrl+Shift+L'),
        Command('Help/Find a command', 'Help — Find a command', 'Ctrl+Shift+P'),
    )
    bindings = tuple(productivity.MenuBinding(command, _MenuItem(command.label), command.label)
                     for command in commands)
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), report=lambda value: None)
    manager = productivity.KeyboardActions(frame, bindings)
    frame.keyboard_actions = manager
    assert manager.reload() == ''
    store = KeybindingStore(tmp_path)
    readers = []
    for _ in range(2):
        calls = []
        reader = SimpleNamespace(parent=frame, closed=False, busy=False, dialog=_Window(), command_buttons={},
                                 controller=SimpleNamespace(desk=SimpleNamespace(library=frame.library)), calls=calls,
                                 dialogs=SimpleNamespace(choose=lambda *args: None), _error=lambda value: None)
        for command in manager.commands:
            if command.context == 'reader':
                reader.command_buttons[command.identity] = (
                    _Button(command.label),
                    lambda event, identity=command.identity, calls=calls: calls.append(identity))
        reader.keyboard = reading_shortcuts.ReaderKeyboardAdapter(reader, manager)
        readers.append(reader)
    yield SimpleNamespace(manager=manager, store=store, readers=readers, root=tmp_path)
    for reader in readers:
        reader.keyboard.close()

def test_context_reuse_dispatches_only_to_the_active_reader(contexts):
    manager = contexts.manager
    first, second = contexts.readers
    assert manager.shortcut('Academic/Academic library') == manager.shortcut('reader.bookmarks') == 'Ctrl+Shift+B'
    assert manager.shortcut('Library/Library tools') == manager.shortcut('reader.where') == 'Ctrl+Shift+L'

    assert first.dialog.key('Ctrl+Shift+B') is True
    assert first.dialog.key('Ctrl+Shift+L') is True
    assert first.dialog.key('Ctrl+N') is False  # Main command is not installed in the Reader.

    assert first.calls == ['reader.bookmarks', 'reader.where']
    assert second.calls == []


def test_remap_refreshes_all_open_readers_without_changing_other_context_defaults(contexts):
    manager = contexts.manager
    first, second = contexts.readers
    first.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})

    assert manager.shortcut('Academic/Academic library') == 'Ctrl+Shift+B'
    assert contexts.store.load() == {'reader.next': 'Ctrl+Shift+J'}
    for reader in (first, second):
        assert reader.dialog.key('Ctrl+PageDown') is False
        assert reader.dialog.key('Ctrl+Shift+J') is True
        assert reader.calls == ['reader.next']
        button = reader.command_buttons['reader.next'][0]
        assert button.GetName() == 'Next source location'
        assert button.label == 'Next source location (Ctrl+Shift+J)'
        assert 'Ctrl+Shift+J: Next source location.' in reader.keyboard.guide()
        assert 'Ctrl+PageDown' not in reader.keyboard.guide()


def test_reset_and_unassign_remove_old_accelerators_in_every_open_reader(contexts):
    first, second = contexts.readers
    first.keyboard.apply_overrides({'reader.next': ''})
    for reader in (first, second):
        assert reader.dialog.key('Ctrl+PageDown') is False
        assert reader.command_buttons['reader.next'][0].label == 'Next source location'
        assert reader.keyboard.invoke('reader.next') is True  # Button/palette remains available.

    second.keyboard.apply_overrides({})
    for reader in (first, second):
        assert reader.dialog.key('Ctrl+PageDown') is True
        assert reader.calls == ['reader.next', 'reader.next']


def test_conflicting_remap_preserves_persisted_keymap_and_all_active_native_tables(contexts):
    first, second = contexts.readers
    first.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
    before = contexts.store.path.read_bytes()
    tables = [reader.dialog.table for reader in (first, second)]
    labels = [reader.command_buttons['reader.next'][0].label for reader in (first, second)]

    with pytest.raises(ValueError, match='already assigned'):
        second.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J',
                                         'reader.previous': 'Ctrl+Shift+J'})

    assert contexts.store.path.read_bytes() == before
    assert contexts.manager.overrides == {'reader.next': 'Ctrl+Shift+J'}
    assert [reader.dialog.table for reader in (first, second)] == tables
    assert [reader.command_buttons['reader.next'][0].label for reader in (first, second)] == labels
    assert first.calls == second.calls == []


@pytest.mark.parametrize('blocked', ['busy', 'disabled', 'closed', 'destroyed'])
def test_accelerator_callback_cannot_bypass_command_availability(contexts, blocked):
    reader = contexts.readers[0]
    button = reader.command_buttons['reader.next'][0]
    if blocked == 'busy':
        reader.busy = True
    elif blocked == 'disabled':
        button.enabled = False
    elif blocked == 'closed':
        reader.closed = True
    else:
        button.destroyed = True

    assert reader.dialog.key('Ctrl+PageDown') is False
    assert reader.calls == []


def test_disposed_reader_drops_bound_and_queued_callbacks_and_is_not_refreshed(contexts):
    first, second = contexts.readers
    identifier = next(entry[2] for entry in first.dialog.table if entry[0] == 'Ctrl+PageDown')
    queued_callback = first.dialog.events[identifier]
    original_table = first.dialog.table
    first.keyboard.close()
    first.dialog.destroyed = True
    for button, _ in first.command_buttons.values():
        button.destroyed = True

    assert first.dialog.events == {}
    assert queued_callback(None) is False
    second.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})

    assert first.dialog.table == original_table
    assert first.calls == []
    assert second.dialog.key('Ctrl+Shift+J') is True
    assert second.calls == ['reader.next']


@pytest.mark.parametrize('blocked', ['busy', 'closed'])
def test_reader_launcher_and_settings_do_not_open_from_an_unavailable_context(contexts, monkeypatch, blocked):
    reader = contexts.readers[0]
    setattr(reader, blocked, True)
    monkeypatch.setattr(reader.dialogs, 'choose',
                        lambda *args, **kwargs: pytest.fail('Unavailable reader opened its command launcher.'))
    monkeypatch.setattr(contexts.manager, 'change',
                        lambda *args, **kwargs: pytest.fail('Unavailable reader opened keyboard settings.'))

    reader.keyboard.commands()
    reader.keyboard.settings()
    assert reader.calls == []


def test_reader_edit_and_reset_preserve_main_choices_and_main_reset_preserves_reader(contexts):
    manager = contexts.manager
    manager.save({'Library/New library': 'Ctrl+Alt+Shift+J'})
    reader = contexts.readers[0]
    reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
    assert contexts.store.load() == {'Library/New library': 'Ctrl+Alt+Shift+J', 'reader.next': 'Ctrl+Shift+J'}
    manager.reset()
    assert contexts.store.load() == {'reader.next': 'Ctrl+Shift+J'}
    manager.save({**manager.overrides, 'Library/New library': 'Alt+F8'})
    reader.keyboard.apply_overrides({})
    assert contexts.store.load() == {'Library/New library': 'Alt+F8'}


def test_same_context_reserved_editing_and_nvda_keys_leave_file_and_tables_unchanged(contexts):
    reader = contexts.readers[0]
    reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
    before = contexts.store.path.read_bytes()
    tables = [item.dialog.table for item in contexts.readers]
    for shortcut in ('J', 'Shift+J', 'Insert+X', 'CapsLock+X', 'Ctrl+C', 'Ctrl+Shift+C',
                     'Ctrl+Left', 'Ctrl+Shift+Home', 'Alt+F4', 'Ctrl+Alt+N'):
        with pytest.raises(ValueError):
            reader.keyboard.apply_overrides({'reader.next': shortcut})
        assert contexts.store.path.read_bytes() == before
        assert [item.dialog.table for item in contexts.readers] == tables
    assert all('Ctrl+Alt+N' not in item.keyboard.guide() for item in contexts.readers)


def test_old_main_navigation_override_is_preserved_without_allowing_reader_editing_capture():
    commands = (Command('Main/Existing', 'Existing main command', 'Ctrl+Left'),
                Command('reader.test', 'Reader command', '', context='reader'))
    assert validate_bindings(commands, {})['Main/Existing'] == 'Ctrl+Left'
    with pytest.raises(ValueError, match='native text'):
        validate_bindings(commands, {'reader.test': 'Ctrl+Left'})


def test_reader_choice_cannot_disable_main_palette_on_reload(contexts):
    contexts.readers[0].keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+P', 'reader.commands': ''})
    contexts.manager.reload()
    assert contexts.manager.shortcut('Help/Find a command') == 'Ctrl+Shift+P'
    assert contexts.manager.shortcut('reader.next') == 'Ctrl+Shift+P'
    assert 'Help/Find a command' not in contexts.store.load()


def test_stale_root_rejects_pending_reader_dispatch_and_preferences(contexts, tmp_path):
    reader = contexts.readers[0]
    contexts.manager.frame.library = SimpleNamespace(root=tmp_path / 'another-library')
    assert not reader.dialog.key('Ctrl+PageDown')
    with pytest.raises(ValueError, match='current main window'):
        reader.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})
    assert not contexts.store.path.exists() and reader.calls == []


def test_standalone_reader_keeps_default_keys_but_cannot_overwrite_shared_main_preferences(contexts):
    first = contexts.readers[0]
    first.keyboard.close()
    first.parent = SimpleNamespace()
    first.keyboard = reading_shortcuts.ReaderKeyboardAdapter(first)
    assert first.dialog.key('Ctrl+PageDown')
    assert not first.keyboard.available('reader.settings')
    with pytest.raises(ValueError, match='main window'):
        first.keyboard.apply_overrides({})
    assert not contexts.store.path.exists()


def test_canonical_import_is_read_only_until_reviewed_save_and_preserves_current_choices(contexts):
    import json
    raw = json.dumps({'version': 1, 'bindings': {
        'reader.next': 'Ctrl+Shift+J', 'library.new': 'Alt+F8'}}).encode()
    legacy = contexts.root / 'keyboard-shortcuts.json'
    legacy.write_bytes(raw)
    contexts.manager.save({'Academic/Academic library': 'Ctrl+Alt+Shift+B'})
    current = contexts.store.path.read_bytes()
    preview = keyboard_settings.read_shortcut_import(legacy, contexts.manager.commands, contexts.manager.overrides)
    assert preview == {'Academic/Academic library': 'Ctrl+Alt+Shift+B',
                       'Library/New library': 'Alt+F8', 'reader.next': 'Ctrl+Shift+J'}
    assert legacy.read_bytes() == raw and contexts.store.path.read_bytes() == current
    contexts.manager.save(preview)
    assert legacy.read_bytes() == raw
    assert contexts.readers[0].dialog.key('Ctrl+Shift+J')
    assert 'available for review' in contexts.manager.reload()


@pytest.mark.parametrize('bindings', (
    {'unknown.module': 'Alt+F8'},
    {'library.new': 'Alt+F8'},
    {'reader.next': 'Ctrl+N', 'reader.previous': 'Ctrl+N'},
    {'reader.next': 'Insert+N'},
))
def test_canonical_unavailable_conflicting_or_protected_keys_never_replace_current_file(contexts, bindings):
    import json
    contexts.manager.save({'Library/New library': 'Ctrl+Alt+Shift+N'})
    before = contexts.store.path.read_bytes()
    raw = json.dumps({'version': 1, 'bindings': bindings}).encode()
    with pytest.raises(ValueError):
        keyboard_settings.migrate_canonical_shortcuts(raw, contexts.manager.commands, contexts.manager.overrides)
    assert contexts.store.path.read_bytes() == before


def test_canonical_duplicate_fields_and_noninteger_version_are_rejected(contexts):
    for raw in (b'{"version":1,"bindings":{"reader.next":"Alt+F8","reader.next":"Alt+F9"}}',
                b'{"version":true,"bindings":{}}'):
        with pytest.raises(ValueError):
            keyboard_settings.migrate_canonical_shortcuts(raw, contexts.manager.commands)


def test_reader_native_key_conversion_uses_exact_modifier_and_navigation_codes():
    wx = SimpleNamespace(ACCEL_CTRL=2, ACCEL_ALT=1, ACCEL_SHIFT=4, WXK_PAGEDOWN=367, WXK_F1=340)
    assert keyboard_settings.reader_accelerator('Ctrl+PageDown', 71, wx) == (2, 367, 71)
    assert keyboard_settings.reader_accelerator('Ctrl+Shift+J', 72, wx) == (6, ord('J'), 72)
    assert keyboard_settings.reader_accelerator('F1', 73, wx) == (0, 340, 73)
