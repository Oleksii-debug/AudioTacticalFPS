"""Current native menu routing and ownership; actual wx dispatch has its own probe."""
from types import SimpleNamespace
import sys

import pytest

from carr_library.ui import productivity
from carr_library.ui.command_registry import CommandRegistry


class Item:
    def __init__(self, identity, label):
        self.identity, self.label = identity, label

    def GetId(self):
        return self.identity

    def GetItemLabel(self):
        return self.label


class Menu:
    def __init__(self):
        self.items = []

    def Append(self, identity, label):
        item = Item(len(self.items) + 1, label)
        self.items.append(item)
        return item

    def AppendSeparator(self):
        self.items.append(None)


class MenuBar:
    def __init__(self):
        self.entries = [('&Library', Menu()), ('&Reading', Menu())]

    def Insert(self, index, menu, label):
        self.entries.insert(index, (label, menu))

    def FindMenu(self, name):
        return next((index for index, (label, _) in enumerate(self.entries)
                     if label.replace('&', '') == name), -1)

    def GetMenu(self, index):
        return self.entries[index][1]


class Control:
    def __init__(self, parent, *, editable=True):
        self.parent, self.editable = parent, editable
        self.calls, self.value = [], 'Selected draft — Україна'
        self.capability = True

    def GetParent(self):
        return self.parent

    def IsEditable(self):
        return self.editable

    def CanUndo(self):
        return self.capability

    CanRedo = CanCut = CanCopy = CanPaste = CanUndo

    def Undo(self):
        self.calls.append('Undo')

    def Redo(self):
        self.calls.append('Redo')

    def Cut(self):
        self.calls.append('Cut')

    def Copy(self):
        self.calls.append('Copy')

    def Paste(self):
        self.calls.append('Paste')

    def SelectAll(self):
        self.calls.append('SelectAll')


@pytest.fixture
def host(monkeypatch):
    focus, calls, notices = [None], [], []
    frame = SimpleNamespace(menu_bar=MenuBar(), _closing=False, working=False, busy=False,
                            evidence=None, report=notices.append)
    frame.Bind = lambda event, handler, item: calls.append((item, handler))
    frame._restore_note_focus = lambda: None
    wx = SimpleNamespace(Menu=Menu, ID_ANY=-1, EVT_MENU=object(),
                         Window=SimpleNamespace(FindFocus=lambda: focus[0]))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    installed = []
    marker = object()
    def install(current):
        assert current is frame
        installed.append(current)
        return marker
    monkeypatch.setattr('carr_library.ui.keyboard_settings.install_keyboard_settings', install)
    return SimpleNamespace(frame=frame, focus=focus, calls=calls, notices=notices,
                           installed=installed, marker=marker)


def item_handler(host, caption):
    selected = [(item, handler) for item, handler in host.calls
                if item.label.split('\t', 1)[0].replace('&', '') == caption]
    assert len(selected) == 1
    return selected[0]


def test_installation_adds_one_edit_menu_and_recovery_to_existing_save_owner(host):
    previous = productivity.install_save_coordinator(host.frame)
    assert productivity.install_productivity_actions(host.frame) is host.marker
    assert host.frame._save_coordinator is previous
    assert tuple(previous.participants) == ('research.note',)
    assert [label for label, _ in host.frame.menu_bar.entries][:2] == ['&File', '&Edit']
    edit = host.frame.menu_bar.GetMenu(1)
    assert [item.label.replace('&', '') for item in edit.items] == [
        'Undo', 'Redo', 'Cut', 'Copy', 'Paste', 'Select all']
    assert item_handler(host, 'Recover research-note draft...')


def test_new_commands_reuse_semantic_registry_without_text_key_overrides(host):
    productivity.install_productivity_actions(host.frame)
    pending = host.frame._pending_keyboard_commands
    assert {spec.command_id for spec, _ in pending} == {
        'file.recover_note', 'edit.undo', 'edit.redo', 'edit.cut',
        'edit.copy', 'edit.paste', 'edit.select_all'}
    assert all(spec.context == 'main' and spec.default_shortcut == '' for spec, _ in pending)
    # Validate the actual product command schema, not a fake registry.
    registry = CommandRegistry(tuple(spec for spec, _ in pending))
    assert len(registry.commands()) == 7


def test_menu_events_read_current_focus_instead_of_captured_first_control(host):
    first, second = Control(host.frame), Control(host.frame)
    host.focus[0] = first
    productivity.install_productivity_actions(host.frame)
    _, handler = item_handler(host, 'Copy')
    host.focus[0] = second
    handler(object())
    assert first.calls == [] and second.calls == ['Copy']


@pytest.mark.parametrize('action', ['Undo', 'Redo', 'Cut', 'Copy', 'Paste', 'SelectAll'])
def test_edit_routes_only_the_explicit_native_action(host, action):
    target = Control(SimpleNamespace(GetParent=lambda: host.frame))
    host.focus[0] = target
    assert productivity.edit_current_text(host.frame, action)
    assert target.calls == [action] and host.notices == []


@pytest.mark.parametrize('kind', ['missing', 'foreign', 'cycle'])
def test_foreign_or_cyclic_focus_cannot_receive_main_window_edit(host, kind):
    target = None if kind == 'missing' else Control(object())
    if kind == 'cycle':
        target.parent = target
    host.focus[0] = target
    assert not productivity.edit_current_text(host.frame, 'Cut')
    assert target is None or target.calls == []
    assert 'this window' in host.notices[-1]


@pytest.mark.parametrize('action', ['Copy', 'SelectAll'])
def test_readonly_native_selection_allows_copy_and_select_all(host, action):
    target = Control(host.frame, editable=False)
    host.focus[0] = target
    assert productivity.edit_current_text(host.frame, action)
    assert target.calls == [action]


@pytest.mark.parametrize('action', ['Undo', 'Redo', 'Cut', 'Paste'])
def test_readonly_native_selection_refuses_mutating_actions(host, action):
    target = Control(host.frame, editable=False)
    host.focus[0] = target
    previous = target.value
    assert not productivity.edit_current_text(host.frame, action)
    assert target.calls == [] and target.value == previous
    assert 'read only' in host.notices[-1]


@pytest.mark.parametrize('action', ['Undo', 'Redo', 'Cut', 'Copy', 'Paste'])
def test_current_control_capability_refusal_keeps_its_text(host, action):
    target = Control(host.frame)
    target.capability = False
    host.focus[0] = target
    assert not productivity.edit_current_text(host.frame, action)
    assert target.calls == [] and 'current selection' in host.notices[-1]


def test_destroyed_native_control_cannot_retarget_another_window(host):
    current, other = Control(host.frame), Control(host.frame)
    def destroyed():
        host.focus[0] = other
        raise RuntimeError('deleted native window')
    current.GetParent = destroyed
    host.focus[0] = current
    assert not productivity.edit_current_text(host.frame, 'Paste')
    assert current.calls == other.calls == []
    assert 'no longer available' in host.notices[-1]


@pytest.mark.parametrize('route', ['editing', 'recovery'])
def test_closing_host_never_invokes_a_text_or_recovery_action(host, route):
    target, recovered = Control(host.frame), []
    host.focus[0] = target
    host.frame.evidence = SimpleNamespace(recover_draft=lambda: recovered.append(True))
    host.frame._closing = True
    result = (productivity.edit_current_text(host.frame, 'Cut') if route == 'editing'
              else productivity.recover_current_note(host.frame))
    assert result is False and target.calls == recovered == []


def test_recovery_route_uses_the_current_existing_panel_without_saving(host):
    previous, current, writes = [], [], []
    host.frame.evidence = SimpleNamespace(recover_draft=lambda: previous.append(True))
    productivity.install_productivity_actions(host.frame)
    _, handler = item_handler(host, 'Recover research-note draft...')
    host.frame.evidence = SimpleNamespace(recover_draft=lambda: current.append(True),
                                         save_note=lambda: writes.append(True))
    handler(object())
    assert current == [True] and previous == writes == []


@pytest.mark.parametrize('panel', [None, SimpleNamespace()])
def test_recovery_without_selected_evidence_reports_actionable_status(host, panel):
    host.frame.evidence = panel
    assert not productivity.recover_current_note(host.frame)
    assert 'Open the saved evidence' in host.notices[-1]


@pytest.mark.parametrize('action', ['Destroy', 'SetValue', '__class__'])
def test_unknown_edit_action_cannot_gain_arbitrary_method_access(host, action):
    target = Control(host.frame)
    host.focus[0] = target
    with pytest.raises(ValueError, match='native text-editing action'):
        productivity.edit_current_text(host.frame, action)
    assert target.calls == []


def test_adding_recovery_route_keeps_durable_save_all_waiting_for_confirmation(host):
    from carr_library.ui.save_all import SaveResult
    dirty, callbacks = [True], []
    host.frame.evidence = SimpleNamespace(
        _dirty=lambda: dirty[0], save_note=lambda *, completed: callbacks.append(completed),
        recover_draft=lambda: None)
    productivity.install_productivity_actions(host.frame)
    _, handler = item_handler(host, 'Save all changes')
    handler(object())
    assert host.frame._save_coordinator.busy and len(callbacks) == 1
    assert host.notices == []
    dirty[0] = False
    callbacks[0](SaveResult(True))
    assert not host.frame._save_coordinator.busy
    assert host.notices[-1] == 'All 1 open drafts saved.'


def test_reinstall_retains_existing_routes_and_coordinator_without_duplicate_menus(host):
    productivity.install_productivity_actions(host.frame)
    before = (tuple(host.frame.menu_bar.entries), tuple(host.calls),
              tuple(host.frame._pending_keyboard_commands), host.frame._save_coordinator)
    assert productivity.install_productivity_actions(host.frame) is host.marker
    assert (tuple(host.frame.menu_bar.entries), tuple(host.calls),
            tuple(host.frame._pending_keyboard_commands), host.frame._save_coordinator) == before
    assert len(host.installed) == 2
