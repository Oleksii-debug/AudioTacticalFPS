"""Actual incremental-menu installation after File/Edit have been inserted.

Native widgets are represented only at their menu storage boundary. The real
installer, command specifications, pending registry and bound handlers execute;
native keyboard/geometry acceptance remains a separate Windows probe.
"""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui.backup_incremental_dialog import (
    IncrementalBackupActions, install_incremental_backup_actions,
)


class Menu:
    def __init__(self, caption):
        self.caption = caption
        self.original = SimpleNamespace(label="Existing " + caption)
        self.items = [self.original]

    def AppendSeparator(self):
        self.items.append(None)

    def Append(self, identifier, label):
        item = SimpleNamespace(identifier=identifier, label=label)
        self.items.append(item)
        return item


class Bar:
    def __init__(self, captions):
        self.menus = [Menu(caption) for caption in captions]
        self.reads = []

    def GetMenuCount(self):
        return len(self.menus)

    def GetMenuLabel(self, index):
        self.reads.append(("label", index))
        return self.menus[index].caption

    def GetMenu(self, index):
        self.reads.append(("menu", index))
        return self.menus[index]


class Frame:
    def __init__(self, bar):
        self.bar = bar
        self.bindings = []

    def GetMenuBar(self):
        return self.bar

    def Bind(self, event, handler, item):
        self.bindings.append((event, handler, item))

    def run_job(self, *args, **options):
        raise AssertionError("Installing menu commands must not start backup work.")


def _native_constants(monkeypatch):
    wx = SimpleNamespace(ID_ANY=-1, EVT_MENU=object())
    monkeypatch.setitem(sys.modules, "wx", wx)
    return wx


@pytest.mark.parametrize("captions", [
    ("&Library", "&File", "&Edit"),
    ("&File", "&Edit", "&Library", "&Tools"),
])
def test_incremental_commands_bind_exact_library_after_native_menu_reordering(
        monkeypatch, captions):
    wx = _native_constants(monkeypatch)
    bar = Bar(captions)
    frame = Frame(bar)
    target = next(menu for menu in bar.menus
                  if menu.caption.replace("&", "") == "Library")
    before = {id(menu): tuple(menu.items) for menu in bar.menus}

    actions = install_incremental_backup_actions(frame)

    assert type(actions) is IncrementalBackupActions
    assert frame.incremental_backup_actions is actions and actions.frame is frame
    assert target.items[:2] == [target.original, None]
    expected = [
        ("backup.incremental.create", "Create incremental backup...", "create"),
        ("backup.incremental.verify", "Verify incremental backup chain...", "verify"),
        ("backup.incremental.restore", "Restore incremental backup to a new folder...", "restore"),
    ]
    assert [item.label for item in target.items[2:]] == [row[1] for row in expected]
    assert len(frame.bindings) == len(frame._pending_keyboard_commands) == 3
    for (identity, label, method), binding, registration in zip(
            expected, frame.bindings, frame._pending_keyboard_commands):
        event, handler, item = binding
        spec, registered_item = registration
        assert event is wx.EVT_MENU and handler.__self__ is actions
        assert handler.__name__ == method and registered_item is item
        assert spec.command_id == identity and spec.label == label and spec.context == "main"
        assert item is target.items[2 + expected.index((identity, label, method))]
    for menu in bar.menus:
        if menu is not target:
            assert tuple(menu.items) == before[id(menu)]

    # Genuine bound methods retain their exact action routes, without invoking
    # any worker, picker, snapshot producer or restore during menu installation.
    routed = []
    monkeypatch.setattr(actions, "start", routed.append)
    for _, handler, _ in frame.bindings:
        handler()
    assert routed == ["create", "verify", "restore"]

    saved_items = tuple(target.items)
    saved_bindings = tuple(frame.bindings)
    saved_pending = tuple(frame._pending_keyboard_commands)
    assert install_incremental_backup_actions(frame) is actions
    assert tuple(target.items) == saved_items
    assert tuple(frame.bindings) == saved_bindings
    assert tuple(frame._pending_keyboard_commands) == saved_pending


@pytest.mark.parametrize("captions", [
    None,
    (),
    ("&File", "&Edit"),
    ("&File", "&Library", "Library"),
    ("&Library",) + tuple("Menu %d" % index for index in range(64)),
])
def test_missing_ambiguous_or_over_budget_library_menu_fails_before_any_mutation(
        monkeypatch, captions):
    _native_constants(monkeypatch)
    bar = None if captions is None else Bar(captions)
    frame = Frame(bar)
    before = None if bar is None else [tuple(menu.items) for menu in bar.menus]

    with pytest.raises(ValueError, match="Library menu"):
        install_incremental_backup_actions(frame)

    assert not frame.bindings
    assert not hasattr(frame, "_pending_keyboard_commands")
    assert not hasattr(frame, "incremental_backup_actions")
    if bar is not None:
        assert [tuple(menu.items) for menu in bar.menus] == before
        if not 0 < len(bar.menus) <= 64:
            assert not bar.reads
