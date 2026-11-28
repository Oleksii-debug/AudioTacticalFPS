"""Portable regressions for the actual Studio/BookBatch native drivers.

The extracted callbacks execute unchanged. These controls reproduce wxWidgets'
separate live-list and modal-result state; actual Windows controls remain a
separate required native gate.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import traceback

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'


def actual_functions(script_name, names, scope):
    path = SCRIPTS / script_name
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    selected = []
    for name in names:
        matches = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(matches) == 1, (script_name, name, len(matches))
        selected.append(matches[0])

    class HeldScope(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            return ast.copy_location(ast.Global(names=node.names), node)

    module = ast.Module(body=[HeldScope().visit(node) for node in selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), scope)
    return scope


def book_driver(*, checklist=True, apply_selection=True, list_count=1):
    checks, keys, callbacks, owned = [], [], [], []

    class ListBox:
        def __init__(self):
            self.selected = ()

        def GetCount(self):
            return 2

        def IsSelected(self, index):
            return index in self.selected

    class CheckListBox(ListBox):
        def IsChecked(self, index):
            return index in self.selected

    class Picker:
        def __init__(self):
            kind = CheckListBox if checklist else ListBox
            self.children = [kind() for _ in range(list_count)]
            self.cached, self.modal = (), True

        def GetChildren(self):
            return self.children

        def SetSelections(self, selected):
            if apply_selection:
                for child in self.children:
                    child.selected = tuple(selected)

        def GetSelections(self):
            # wxMultiChoiceDialog copies this only in TransferDataFromWindow.
            return self.cached

    picker = Picker()
    button = object()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def press(control):
        assert control is button and picker.modal
        assert picker.GetSelections() == ()
        keys.append('actual OK')
        picker.cached = tuple(index for index in range(2)
                              if picker.children[0].IsSelected(index))
        picker.modal = False

    def find(identity, parent):
        assert identity == 1 and parent is picker
        return button

    scope = dict(
        wx=SimpleNamespace(
            ListBox=ListBox, CheckListBox=CheckListBox, MultiChoiceDialog=Picker,
            ID_OK=1, FindWindowById=find,
            CallLater=lambda delay, callback: callbacks.append(callback)),
        monotonic=lambda: 1.0, deadline=10.0, key_pending=False, stage=2,
        registry=None, batch_dialog=None, child=None, reviewing=0,
        errors=[], finished=False, titled=lambda title: picker,
        own_modal=lambda window: owned.append(window), check=check,
        press=press, guarded=lambda callback: callback)
    actual_functions('windows_book_batch_smoke.py', ('live_book_selections', 'drive'), scope)
    return SimpleNamespace(drive=scope['drive'], picker=picker, keys=keys,
                           callbacks=callbacks, owned=owned, checks=checks, scope=scope)


@pytest.mark.parametrize('checklist', [True, False], ids=('native-checklist', 'native-list'))
def test_native_book_chooser_reads_live_selection_before_actual_ok(checklist):
    driver = book_driver(checklist=checklist)
    driver.drive()
    assert driver.owned == [driver.picker]
    assert driver.keys == ['actual OK'] and not driver.picker.modal
    assert driver.picker.GetSelections() == (0, 1)
    assert driver.scope['stage'] == 3 and len(driver.callbacks) == 1
    assert driver.checks == ['actual native library chooser selects two existing books']


@pytest.mark.parametrize('checklist', [True, False], ids=('native-checklist', 'native-list'))
def test_native_book_chooser_refuses_an_unapplied_live_selection_before_ok(checklist):
    driver = book_driver(checklist=checklist, apply_selection=False)
    with pytest.raises(AssertionError, match='selects two existing books'):
        driver.drive()
    assert not driver.keys and driver.picker.modal
    assert driver.picker.GetSelections() == () and driver.scope['stage'] == 2


@pytest.mark.parametrize('list_count', [0, 2], ids=('missing-list', 'ambiguous-lists'))
def test_native_book_chooser_refuses_missing_or_ambiguous_live_controls(list_count):
    driver = book_driver(list_count=list_count)
    with pytest.raises(AssertionError, match='no unique live list'):
        driver.drive()
    assert not driver.keys and driver.picker.modal
    assert driver.picker.GetSelections() == () and driver.scope['stage'] == 2


def history_driver(*, fault=None, list_count=1):
    expected = 'Revision 7; identity aaaaaaaaaaaa'
    keys, checks = [], []

    class ListBox:
        def __init__(self):
            self.selected = 0
            self.labels = ['Another revision'] * 5
            self.labels[4] = expected if fault != 'wrong-identity' else 'Revision 7; identity bbbbbbbbbbbb'

        def GetSelection(self):
            return self.selected

        def GetStringSelection(self):
            return self.labels[self.selected]

    class Picker:
        def __init__(self):
            self.children = [ListBox() for _ in range(list_count)]
            self.cached, self.modal = '', True

        def GetChildren(self):
            return self.children

        def SetSelection(self, index):
            if fault != 'wrong-selection':
                for child in self.children:
                    child.selected = index

        def GetStringSelection(self):
            # SetSelection does not update m_stringSelection before DoChoice.
            return self.cached

    picker = Picker()
    button = object()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def press(control):
        assert control is button and picker.modal and picker.GetStringSelection() == ''
        keys.append('actual OK')
        picker.cached = picker.children[0].GetStringSelection()
        picker.modal = False

    def find(identity, parent):
        assert identity == 1 and parent is picker
        return button

    scope = dict(
        wx=SimpleNamespace(ListBox=ListBox, ID_OK=1,
                           Window=SimpleNamespace(FindWindowById=find)),
        before_timeline=SimpleNamespace(revision=7, fingerprint='a' * 64),
        check=check, press=press)
    actual_functions('windows_studio_effects_smoke.py', ('choose_original_history',), scope)
    return SimpleNamespace(choose=scope['choose_original_history'], picker=picker,
                           keys=keys, checks=checks, expected=expected)


def test_native_history_checks_live_revision_while_modal_string_cache_is_empty():
    driver = history_driver()
    driver.choose(driver.picker)
    assert driver.keys == ['actual OK'] and not driver.picker.modal
    assert driver.picker.GetStringSelection() == driver.expected
    assert driver.checks == ['native history picker targets exact reviewed revision']


@pytest.mark.parametrize('fault', ['wrong-selection', 'wrong-identity'])
def test_native_history_refuses_wrong_live_index_or_exact_revision_identity(fault):
    driver = history_driver(fault=fault)
    with pytest.raises(AssertionError, match='targets exact reviewed revision'):
        driver.choose(driver.picker)
    assert not driver.keys and driver.picker.modal and driver.picker.GetStringSelection() == ''


@pytest.mark.parametrize('list_count', [0, 2], ids=('missing-list', 'ambiguous-lists'))
def test_native_history_refuses_missing_or_ambiguous_live_controls(list_count):
    driver = history_driver(list_count=list_count)
    with pytest.raises(AssertionError, match='no unique live list'):
        driver.choose(driver.picker)
    assert not driver.keys and driver.picker.modal and driver.picker.GetStringSelection() == ''


def abort_driver(*, native_question=False, fail_close=False):
    callbacks, events, exits = [], [], []
    state = dict(active=None, next=None, native_question=native_question)

    class Loop:
        def __init__(self, name):
            self.name = name

        def IsRunning(self):
            return state['active'] is self

        def IsMain(self):
            return self.name == 'main'

        def ScheduleExit(self, code):
            assert self.IsMain() and self.IsRunning()
            exits.append(code)

    main, outer_loop, inner_loop = (Loop(name) for name in ('main', 'outer', 'inner'))

    class Dialog:
        def __init__(self, name, handle, loop, parent_loop):
            self.name, self.handle, self.loop = name, handle, loop
            self.parent_loop, self.modal = parent_loop, True

        def IsModal(self):
            return self.modal

        def EndModal(self, code):
            assert code == 2 and self.modal and self.loop.IsRunning()
            if fail_close:
                raise RuntimeError('actual cleanup refusal')
            events.append(('EndModal', self.name))
            self.modal = False
            # EndModal asks its current loop to exit. The outer loop is resumed
            # only after this callback returns to the event dispatcher.
            state['next'] = self.parent_loop

    outer = Dialog('outer', 10, outer_loop, main)
    inner = Dialog('inner', 20, inner_loop, outer_loop)
    foreign = Dialog('foreign', 30, Loop('foreign'), main)
    state['active'] = inner_loop

    def dismiss():
        if not state['native_question']:
            return False
        events.append(('owned question', 'No'))
        state['native_question'] = False
        return True

    scope = dict(
        abort_pending=False, modal_loops=[(outer, 10, outer_loop), (inner, 20, inner_loop)],
        dismiss_owned_question=dismiss, errors=['original callback failure'],
        wx=SimpleNamespace(
            ID_CANCEL=2, GetTopLevelWindows=lambda: [foreign, outer, inner],
            CallAfter=lambda callback: callbacks.append(callback),
            CallLater=lambda delay, callback: callbacks.append(callback),
            EventLoopBase=SimpleNamespace(GetActive=lambda: state['active'])),
        user32=SimpleNamespace(IsWindow=lambda handle: handle in (10, 20, 30)),
        traceback=traceback, sys=sys)
    actual_functions('windows_book_batch_smoke.py', ('abort',), scope)

    def step():
        assert callbacks
        callbacks.pop(0)()
        if state['next'] is not None:
            state['active'], state['next'] = state['next'], None

    return SimpleNamespace(abort=scope['abort'], step=step, callbacks=callbacks,
                           events=events, exits=exits, scope=scope,
                           outer=outer, inner=inner, foreign=foreign)


def test_native_batch_abort_unwinds_one_running_owned_loop_per_event_turn():
    driver = abort_driver()
    driver.abort()
    driver.abort()
    assert len(driver.callbacks) == 1
    driver.step()
    assert driver.events == [('EndModal', 'inner')]
    assert driver.outer.modal and driver.foreign.modal
    driver.step()  # next-turn abort admission
    assert driver.events == [('EndModal', 'inner')]
    driver.step()
    assert driver.events == [('EndModal', 'inner'), ('EndModal', 'outer')]
    driver.step()
    driver.step()
    assert driver.exits == [1] and not driver.callbacks and driver.foreign.modal
    assert driver.scope['errors'] == ['original callback failure']


def test_native_batch_abort_answers_owned_question_before_any_wx_modal():
    driver = abort_driver(native_question=True)
    driver.abort()
    driver.step()
    assert driver.events == [('owned question', 'No')]
    assert driver.inner.modal and driver.outer.modal and driver.foreign.modal
    driver.step()
    driver.step()
    assert driver.events == [('owned question', 'No'), ('EndModal', 'inner')]
    assert driver.outer.modal and driver.foreign.modal and not driver.exits


def test_native_batch_abort_surfaces_cleanup_refusal_without_closing_suspended_parents(capsys):
    driver = abort_driver(fail_close=True)
    driver.abort()
    driver.step()
    assert not driver.events and not driver.exits and not driver.callbacks
    assert driver.inner.modal and driver.outer.modal and driver.foreign.modal
    assert driver.scope['errors'][0] == 'original callback failure'
    assert len(driver.scope['errors']) == 2
    assert 'actual cleanup refusal' in driver.scope['errors'][1]
    assert 'actual cleanup refusal' in capsys.readouterr().err
