"""Real menu installers share one registry; native event delivery is a separate gate."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.keyboard_preferences import KeymapStore
from carr_library.ui.academic_tools import install_academic_actions
from carr_library.ui.command_registry import CommandSpec
from carr_library.ui.keyboard_settings import register_menu_command
from carr_library.ui.productivity import install_productivity_actions


class Item:
    serial = 100
    def __init__(self, label):
        Item.serial += 1
        self.identity, self.label = Item.serial, label
    def GetId(self): return self.identity
    def GetItemLabel(self): return self.label
    def SetItemLabel(self, value): self.label = value
    def IsSeparator(self): return self.label is None
    def GetSubMenu(self): return None
    def IsEnabled(self): return True


class Menu:
    def __init__(self): self.items = []
    def Append(self, identity, label):
        item = Item(label)
        self.items.append(item)
        return item
    def AppendSeparator(self): self.items.append(Item(None))
    def GetMenuItems(self): return tuple(self.items)


class MenuBar:
    def __init__(self): self.entries = []
    def Append(self, menu, label): self.entries.append((label, menu))
    def Insert(self, index, menu, label): self.entries.insert(index, (label, menu))
    def GetMenuCount(self): return len(self.entries)
    def GetMenuLabel(self, index): return self.entries[index][0]
    def GetMenu(self, index): return self.entries[index][1]
    def IsEnabledTop(self, index): return True
    def FindMenu(self, name):
        return next((index for index, (label, _) in enumerate(self.entries)
                     if label.replace('&', '') == name), -1)


def frame(root):
    bar = MenuBar()
    for title in ('Library', 'Research', 'Academic', 'Reading', 'Help'):
        bar.Append(Menu(), '&' + title)
    library = bar.GetMenu(bar.FindMenu('Library'))
    library.Append(-1, 'Import books\tCtrl+I')
    research = bar.GetMenu(bar.FindMenu('Research'))
    research.Append(-1, 'Research Search Center\tCtrl+Alt+F')
    advanced = research.Append(-1, 'Build advanced query\tCtrl+Alt+Q')
    value = SimpleNamespace(menu_bar=bar, library=SimpleNamespace(root=root),
        report=Mock(), Bind=Mock(), IsEnabled=lambda: True, GetMenuBar=lambda: bar,
        working=False, busy=False, _closing=False)
    register_menu_command(value, CommandSpec('research.advanced_query',
                          'Build advanced query', 'main', 'Ctrl+Alt+Q'), advanced)
    install_academic_actions(value)
    install_productivity_actions(value)
    return value


@pytest.fixture
def native_double(monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        Menu=Menu, Dialog=object, ID_ANY=-1, EVT_MENU='menu',
        CallAfter=lambda function, *args: function(*args)))
    monkeypatch.setattr('carr_library.ui.keyboard_settings.accelerator_tuple',
                        lambda shortcut, identity: (0, 0, identity))


def test_reading_and_academic_actions_use_one_contextual_registry(tmp_path, native_double):
    value = frame(tmp_path)
    manager = value.keyboard_commands
    labels = [value.menu_bar.GetMenuLabel(i) for i in range(value.menu_bar.GetMenuCount())]
    assert labels[0] == '&File' and labels.count('&Options') == 1
    assert not hasattr(value, 'keyboard_actions')
    expected = {'file.save': 'Ctrl+S', 'file.save_all': 'Ctrl+Shift+S',
                'research.search_center': 'Ctrl+Alt+F', 'research.advanced_query': 'Ctrl+Alt+Q',
                'academic.compare_sources': 'Ctrl+Alt+M'}
    for identity, shortcut in expected.items():
        assert manager.shortcut(identity) == shortcut
        assert manager.bindings[identity].item.GetItemLabel().endswith('\t' + shortcut)
    assert 'reader.edit_copy' in manager.registry.specs
    assert install_productivity_actions(value) is manager
    assert value.menu_bar.GetMenuCount() == len(labels)
    value.evidence = SimpleNamespace(_dirty=lambda: True, save_note=Mock())
    value._restore_note_focus = Mock()
    saved_item = manager.bindings['file.save'].item
    callback = next(call.args[1] for call in value.Bind.call_args_list if call.args[2] is saved_item)
    callback(None)
    value.evidence.save_note.assert_called_once()
    value.report.assert_not_called()


def test_shared_assignments_reopen_without_legacy_or_context_conflicts(tmp_path, native_double):
    first = frame(tmp_path)
    overrides = {'file.save': 'Ctrl+Alt+S', 'reader.next': 'Ctrl+Alt+S'}
    first.keyboard_commands.apply_overrides(overrides)
    before = (tmp_path / 'keyboard-shortcuts.json').read_bytes()
    reopened = frame(tmp_path)
    manager = reopened.keyboard_commands
    assert not manager.load_notice
    assert manager.overrides == KeymapStore(tmp_path / 'keyboard-shortcuts.json').load() == overrides
    assert manager.bindings['file.save'].item.GetItemLabel().endswith('\tCtrl+Alt+S')
    labels = {key: binding.item.GetItemLabel() for key, binding in manager.bindings.items()}
    with pytest.raises(ValueError):
        manager.apply_overrides({'file.save': 'Ctrl+Alt+F'})
    assert (tmp_path / 'keyboard-shortcuts.json').read_bytes() == before
    assert {key: binding.item.GetItemLabel() for key, binding in manager.bindings.items()} == labels
