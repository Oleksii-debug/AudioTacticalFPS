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


def checksum_driver(*, text_count=1, typed_text=None, input_succeeds=True, cached=''):
    # Reuse the exact scoped main -> command -> visit extraction above.
    driver = chooser_driver(selected=0)
    scope = driver.visit.__globals__
    wx, frame = scope['wx'], scope['frame']
    clock, focus, typed = [1.0], [None], []
    checksum = 'a123456789bcdef0' * 4

    class TextCtrl:
        def __init__(self):
            self.value = ''

        def SetFocus(self):
            focus[0] = self

        def GetValue(self):
            return self.value

    class TextEntry(wx.Dialog):
        def __init__(self):
            self.modal, self.cached = True, cached
            self.controls = [TextCtrl() for _ in range(text_count)]

        def GetTitle(self):
            return 'Verify backup'

        def GetValue(self):
            # wxWidgets3.2.8 copies the control to m_value in the OK handler.
            return self.cached

        def Raise(self):
            pass

        def FindWindowById(self, identity):
            return identity

    dialog = TextEntry()
    def native_text(value):
        assert type(value) is bytes
        typed.append(value)
        if input_succeeds:
            dialog.controls[0].value = value.decode('ascii') if typed_text is None else typed_text
        return input_succeeds

    def press(control, key=32):
        assert control == wx.ID_OK
        driver.keys.append((control, key))
        dialog.cached = dialog.controls[0].GetValue()
        dialog.modal = False

    wx.TextEntryDialog, wx.TextCtrl = TextEntry, TextCtrl
    wx.GetTopLevelWindows = lambda: [dialog]
    wx.Window = SimpleNamespace(FindFocus=lambda: focus[0])
    scope.update(
        checksum=checksum, expected_result='Restore complete',
        monotonic=lambda: clock[0], descendants=lambda window: iter(window.controls),
        press=press, simulator=SimpleNamespace(Text=native_text),
        user32=SimpleNamespace(
            SetForegroundWindow=lambda handle: True,
            GetForegroundWindow=lambda: dialog.GetHandle(),
            IsWindow=lambda handle: handle == dialog.GetHandle()),
        modal_loops=[(dialog, dialog.GetHandle(), wx.EventLoopBase.GetActive())])
    driver.state['choice'] = True
    return SimpleNamespace(visit=driver.visit, dialog=dialog, state=driver.state,
                           keys=driver.keys, callbacks=driver.callbacks, checks=driver.checks,
                           typed=typed, clock=clock, checksum=checksum)


def test_native_backup_checksum_observes_live_text_before_modal_value_is_committed():
    driver = checksum_driver()
    driver.visit()
    assert driver.typed == [driver.checksum.encode('ascii')]
    assert driver.dialog.controls[0].GetValue() == driver.checksum
    assert driver.dialog.GetValue() == '' and driver.dialog.IsModal()
    assert driver.state['text_input'] and not driver.state['text'] and not driver.keys
    driver.callbacks.pop(0)()
    assert not driver.dialog.IsModal() and driver.state['text']
    assert driver.dialog.GetValue() == driver.checksum and driver.keys == [(1, 32)]
    assert driver.checks == ['native checksum entry preserves the exact retained manifest identity']


def test_native_backup_checksum_rejects_wrong_live_text_even_if_cached_value_matches():
    expected = 'a123456789bcdef0' * 4
    driver = checksum_driver(typed_text='b' * 64, cached=expected)
    driver.visit()
    assert driver.dialog.GetValue() == expected
    assert driver.dialog.controls[0].GetValue() != expected
    driver.clock[0] = 7.0
    with pytest.raises(AssertionError, match='Native checksum does not equal'):
        driver.callbacks.pop(0)()
    assert driver.dialog.IsModal() and not driver.state['text'] and not driver.keys


@pytest.mark.parametrize('text_count', [0, 2])
def test_native_backup_checksum_refuses_missing_or_ambiguous_live_control(text_count):
    driver = checksum_driver(text_count=text_count)
    with pytest.raises(AssertionError, match='no unique text control'):
        driver.visit()
    assert not driver.typed and not driver.keys and driver.dialog.IsModal()


def test_native_backup_checksum_never_accepts_failed_native_keyboard_input():
    driver = checksum_driver(input_succeeds=False)
    with pytest.raises(AssertionError, match='Native checksum input failed'):
        driver.visit()
    assert driver.typed == [driver.checksum.encode('ascii')]
    assert not driver.state['text_input'] and not driver.state['text']
    assert not driver.keys and driver.dialog.GetValue() == '' and driver.dialog.IsModal()
