"""Portable checks of the actual native backup chooser driver; no wx claim."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def chooser_driver(*, selected=1, cancel=False, move_selection=True,
                   list_count=1, initial_live=0):
    script = Path(__file__).resolve().parents[2] / 'scripts/windows_backup_capacity_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    main = [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'main']
    assert len(main) == 1
    commands = [node for node in main[0].body
                if isinstance(node, ast.FunctionDef) and node.name == 'command']
    assert len(commands) == 1
    functions = [node for node in commands[0].body
                 if isinstance(node, ast.FunctionDef) and node.name == 'visit']
    assert len(functions) == 1
    visit = functions[0]
    # Execute the unchanged native visit body with its closure held in this scope.
    # Only nonlocal declarations become globals; no control-flow rewrite.
    visit.body = [ast.copy_location(ast.Global(names=node.names), node)
                  if isinstance(node, ast.Nonlocal) else node for node in visit.body]
    keys, callbacks, checks = [], [], []
    frame = object()

    class ListBox:
        def __init__(self):
            self.selected = initial_live

        def GetSelection(self):
            return self.selected

    class Dialog:
        def IsShown(self):
            return self.modal

        def IsModal(self):
            return self.modal

        def GetParent(self):
            return frame

        def GetHandle(self):
            return 613

    class Choice(Dialog):
        def __init__(self):
            self.modal, self.cached = True, 0
            self.lists = [ListBox() for _ in range(list_count)]

        def GetTitle(self):
            return 'Complete-backup capacity'

        def GetName(self):
            return self.GetTitle()

        def GetSelection(self):
            # wxWidgets3.2.8 stores m_selection, updated from the list only at OK.
            return self.cached

        def FindWindowById(self, identity):
            return identity

    class TextEntry(Dialog):
        pass

    dialog = Choice()
    state = dict(choice=False, selection_key=False, text=False, result=False,
                 question=False, key_pending=False, text_input=False)
    loop = SimpleNamespace(IsMain=lambda: False, IsRunning=lambda: True)

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def press(control, key=32):
        if isinstance(control, ListBox):
            assert key == 317
            keys.append(('list', key))
            if move_selection:
                control.selected = 1
        else:
            keys.append((control, key))
            assert control in (1, 2)
            if control == 1:
                dialog.cached = dialog.lists[0].GetSelection()
            dialog.modal = False

    wx = SimpleNamespace(
        Dialog=Dialog, SingleChoiceDialog=Choice, TextEntryDialog=TextEntry,
        ListBox=ListBox, ID_OK=1, ID_CANCEL=2, WXK_DOWN=317,
        GetTopLevelWindows=lambda: [dialog],
        EventLoopBase=SimpleNamespace(GetActive=lambda: loop))
    scope = dict(
        wx=wx, monotonic=lambda: 1.0, until=10.0, owned_question=lambda: None,
        state=state, frame=frame, expected_result='Backup complete', selected=selected,
        cancel_choice=cancel, completed=False, question_until=None, text_until=None,
        result_until=None, checksum=None, own_modal=lambda window: None,
        descendants=lambda window: iter(window.lists), visited=set(), check=check,
        press=press, later=lambda delay, callback: callbacks.append(callback),
        user32=SimpleNamespace(IsWindow=lambda handle: handle == 613),
        modal_loops=[(dialog, 613, loop)])
    module = ast.fix_missing_locations(ast.Module(body=[visit], type_ignores=[]))
    exec(compile(module, str(script), 'exec'), scope)
    return SimpleNamespace(visit=scope['visit'], dialog=dialog, state=state,
                           keys=keys, callbacks=callbacks, checks=checks)


@pytest.mark.parametrize('selected', [0, 1], ids=('ordinary', 'large'))
def test_native_capacity_uses_live_list_then_requires_actual_modal_acceptance(selected):
    driver = chooser_driver(selected=selected)
    driver.visit()
    if selected:
        assert driver.dialog.GetSelection() == 0
        assert driver.dialog.lists[0].GetSelection() == 1
        assert driver.keys == [('list', 317)] and not driver.state['choice']
        driver.callbacks.pop(0)()
    assert driver.state['choice'] and not driver.dialog.IsModal()
    assert driver.dialog.GetSelection() == selected
    assert driver.keys == ([('list', 317)] if selected else []) + [(1, 32)]
    assert driver.checks == ['capacity dialog starts with the ordinary allowance']


def test_native_capacity_refuses_a_down_key_that_did_not_change_the_live_list():
    driver = chooser_driver(move_selection=False)
    driver.visit()
    with pytest.raises(AssertionError, match='Native keyboard did not select'):
        driver.callbacks.pop(0)()
    assert driver.keys == [('list', 317)] and driver.dialog.IsModal()
    assert not driver.state['choice'] and driver.dialog.GetSelection() == 0


def test_native_capacity_refuses_ambiguous_embedded_lists_before_any_key():
    driver = chooser_driver(list_count=2)
    with pytest.raises(AssertionError, match='no unique list'):
        driver.visit()
    assert not driver.keys and not driver.callbacks and driver.dialog.IsModal()


def test_native_capacity_default_requires_actual_ordinary_list_not_cached_result_only():
    driver = chooser_driver(initial_live=1)
    with pytest.raises(AssertionError, match='starts with the ordinary allowance'):
        driver.visit()
    assert driver.dialog.GetSelection() == 0
    assert not driver.keys and not driver.callbacks and driver.dialog.IsModal()


def test_native_capacity_cancel_does_not_select_or_accept_any_allowance():
    driver = chooser_driver(cancel=True)
    driver.visit()
    assert driver.keys == [(2, 32)] and not driver.dialog.IsModal()
    assert driver.dialog.GetSelection() == driver.dialog.lists[0].GetSelection() == 0
    assert driver.state['choice'] and not driver.state['selection_key']
