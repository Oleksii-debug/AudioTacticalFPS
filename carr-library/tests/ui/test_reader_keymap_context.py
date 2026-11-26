"""Real keymap manager and reader adapter with minimal native event/table doubles.

Native key conversion and OS delivery belong to native_reader_keymap_smoke.py.
These checks target context routing, shared refresh, and callback lifetime.
"""
from itertools import count
import sys
from types import SimpleNamespace

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui import keyboard_settings, reading_shortcuts


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


@pytest.fixture
def contexts(tmp_path, monkeypatch):
    identifiers = count(100)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        NewIdRef=lambda: next(identifiers), EVT_MENU=object(), AcceleratorTable=tuple))

    def entry(shortcut, identifier):
        return shortcut, 0, int(identifier)

    monkeypatch.setattr(keyboard_settings, 'accelerator_tuple', entry)
    monkeypatch.setattr(reading_shortcuts, 'accelerator_tuple', entry)
    store = KeymapStore(tmp_path / 'keyboard-shortcuts.json')
    manager = keyboard_settings.KeyboardManager(store=store)
    readers = []
    for _ in range(2):
        calls = []
        reader = SimpleNamespace(closed=False, busy=False, dialog=_Window(), command_buttons={},
                                 controller=SimpleNamespace(desk=SimpleNamespace(
                                     library=SimpleNamespace(root=tmp_path))), calls=calls)
        for command in manager.registry.commands('reader'):
            reader.command_buttons[command.command_id] = (
                _Button(command.label),
                lambda event, identity=command.command_id, calls=calls: calls.append(identity))
        reader.keyboard = reading_shortcuts.ReaderKeyboardAdapter(reader, manager)
        readers.append(reader)
    yield SimpleNamespace(manager=manager, store=store, readers=readers)
    for reader in readers:
        reader.keyboard.close()


def test_context_reuse_dispatches_only_to_the_active_reader(contexts):
    manager = contexts.manager
    first, second = contexts.readers
    assert manager.shortcut('academic.open') == manager.shortcut('reader.bookmarks') == 'Ctrl+Shift+B'
    assert manager.shortcut('library.tools') == manager.shortcut('reader.where') == 'Ctrl+Shift+L'

    assert first.dialog.key('Ctrl+Shift+B') is True
    assert first.dialog.key('Ctrl+Shift+L') is True
    assert first.dialog.key('Ctrl+N') is False  # Main command is not installed in the Reader.

    assert first.calls == ['reader.bookmarks', 'reader.where']
    assert second.calls == []


def test_remap_refreshes_all_open_readers_without_changing_other_context_defaults(contexts):
    manager = contexts.manager
    first, second = contexts.readers
    first.keyboard.apply_overrides({'reader.next': 'Ctrl+Shift+J'})

    assert manager.shortcut('academic.open') == 'Ctrl+Shift+B'
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

    with pytest.raises(ValueError, match='both'):
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
    monkeypatch.setattr(reading_shortcuts, 'show_command_launcher',
                        lambda *args, **kwargs: pytest.fail('Unavailable reader opened its command launcher.'))
    monkeypatch.setattr(reading_shortcuts, 'show_keyboard_settings',
                        lambda *args, **kwargs: pytest.fail('Unavailable reader opened keyboard settings.'))

    reader.keyboard.commands()
    reader.keyboard.settings()
    assert reader.calls == []
