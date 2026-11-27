"""Portable regressions for the actual Book probe; no native-runtime claim."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import traceback

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/windows_book_audio_smoke.py'


def functions(*names):
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'), filename=str(SCRIPT))
    found = []
    for name in names:
        matches = [node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == name]
        assert len(matches) == 1, name
        found.append(matches[0])

    class CapturedCells(ast.NodeTransformer):
        def visit_Nonlocal(self, node):
            # Execute these actual closures with explicit captured cells.
            return ast.copy_location(ast.Global(names=node.names), node)

    return ast.fix_missing_locations(CapturedCells().visit(
        ast.Module(body=found, type_ignores=[])))


def unwind_driver(*, controlled=False, question=False, fail=False):
    after, later, calls = [], [], []
    errors = [] if controlled else ['actual publication failure']
    active = [None]
    main = SimpleNamespace(IsMain=lambda: True,
                           ScheduleExit=lambda result: calls.append(('main exit', result)))

    class Loop:
        def IsRunning(self):
            return active[0] is self

        def IsMain(self):
            return False

    class Dialog:
        def __init__(self, name, handle):
            self.name, self.handle = name, handle
            self.modal, self.loop = True, Loop()

        def IsModal(self):
            return self.modal

        def EndModal(self, result):
            assert self.loop.IsRunning(), 'A suspended owner must never be ended.'
            calls.append((self.name, result))
            if fail and self.name == 'measured child':
                raise RuntimeError('owned child refused modal exit')
            self.modal = False
            # ShowModal has not returned yet: tests explicitly advance the loop.

    parent, child = Dialog('Book parent', 101), Dialog('measured child', 102)
    active[0] = child.loop
    native_question = [question]

    def dismiss_owned_question():
        if native_question[0]:
            native_question[0] = False
            calls.append(('owned No',))
            return True
        return False

    wx = SimpleNamespace(ID_CANCEL=2,
        CallAfter=lambda callback: after.append(callback),
        CallLater=lambda delay, callback: later.append((delay, callback)),
        EventLoopBase=SimpleNamespace(GetActive=lambda: active[0]))
    environment = dict(wx=wx, user32=SimpleNamespace(IsWindow=lambda handle: handle in (101, 102)),
        errors=errors, modal_loops=[(parent, parent.handle, parent.loop),
                                   (child, child.handle, child.loop)],
        unwind_pending=False, dismiss_owned_question=dismiss_owned_question,
        traceback=traceback, sys=sys)
    exec(compile(functions('unwind'), str(SCRIPT), 'exec'), environment)
    return SimpleNamespace(environment=environment, after=after, later=later, calls=calls,
        errors=errors, active=active, main=main, parent=parent, child=child,
        unwind=environment['unwind'])


def next_delayed_turn(driver):
    assert len(driver.later) == 1 and driver.later[0][0] == 25
    driver.later.pop(0)[1]()
    assert len(driver.after) == 1
    driver.after.pop(0)()


def test_failed_publication_unwinds_each_resumed_owned_modal_on_later_turn():
    driver = unwind_driver()
    driver.unwind()
    driver.unwind()  # Concurrent failure reports coalesce while the closure is queued.
    assert len(driver.after) == 1 and driver.calls == []
    driver.after.pop(0)()
    assert driver.calls == [('measured child', 2)]
    assert not driver.child.modal and driver.parent.modal
    assert driver.active[0] is driver.child.loop

    driver.active[0] = driver.parent.loop  # The child's actual ShowModal returns.
    next_delayed_turn(driver)
    assert driver.calls == [('measured child', 2), ('Book parent', 2)]
    assert not driver.parent.modal
    driver.active[0] = driver.main  # The parent's actual ShowModal returns.
    next_delayed_turn(driver)
    assert driver.calls[-1] == ('main exit', 1)
    assert not driver.after and not driver.later
    assert driver.errors == ['actual publication failure']


def test_controlled_failure_closes_only_running_child_and_keeps_parent_alive():
    driver = unwind_driver(controlled=True)
    driver.unwind()
    driver.after.pop(0)()
    assert driver.calls == [('measured child', 2)]
    assert not driver.child.modal and driver.parent.modal
    assert not driver.after and not driver.later and not driver.errors


def test_error_unwind_waits_for_owned_question_before_any_wx_modal():
    driver = unwind_driver(question=True)
    driver.unwind()
    driver.after.pop(0)()
    assert driver.calls == [('owned No',)]
    assert driver.child.modal and driver.parent.modal
    next_delayed_turn(driver)  # Native question yielded to the captured child loop.
    assert driver.calls == [('owned No',), ('measured child', 2)]
    assert not driver.child.modal and driver.parent.modal


def test_modal_exit_error_remains_visible_without_ending_suspended_parent(capsys):
    driver = unwind_driver(fail=True)
    driver.unwind()
    driver.after.pop(0)()
    assert driver.calls == [('measured child', 2)]
    assert driver.child.modal and driver.parent.modal
    assert len(driver.errors) == 2 and 'owned child refused modal exit' in driver.errors[-1]
    assert 'FAIL: native Book cleanup' in capsys.readouterr().err
    assert not driver.after and not driver.later


def publication_observer(operation):
    outcome = {}
    environment = dict(publication_outcome=outcome, actual_publish_duration=operation,
                       json=json, traceback=traceback)
    exec(compile(functions('observe_publication', 'observed_publish_duration'), str(SCRIPT), 'exec'),
         environment)
    return environment['observed_publish_duration'], outcome


def test_publication_observer_preserves_actual_arguments_and_returned_receipt(capsys):
    arguments, options, calls = (object(), object()), {'cancelled': object()}, []
    receipt = {'status': 'completed', 'id': 'actual-result', 'retained': object()}

    def operation(*received, **keywords):
        calls.append((received, keywords))
        return receipt

    observed, outcome = publication_observer(operation)
    assert observed(*arguments, **options) is receipt
    assert calls == [(arguments, options)]
    assert outcome == {'started': True, 'returned': True,
                       'receipt_status': 'completed', 'receipt_id': 'actual-result'}
    assert 'actual measured Book publication' in capsys.readouterr().out


@pytest.mark.parametrize('kind', [RuntimeError, InterruptedError, KeyboardInterrupt])
def test_publication_observer_preserves_original_error_and_bounded_diagnostic(kind, capsys):
    failure, calls = kind('original failure ' + 'x' * 20000), []

    def operation(*arguments, **options):
        calls.append((arguments, options))
        raise failure

    observed, outcome = publication_observer(operation)
    with pytest.raises(kind) as raised:
        observed('original-plan', maximum_ms=500)
    assert raised.value is failure and calls == [(('original-plan',), {'maximum_ms': 500})]
    assert outcome['started'] is True and 'returned' not in outcome
    assert len(outcome['error']) <= len(kind.__name__) + 2 + 4096
    assert 0 < len(outcome['traceback']) <= 16384
    assert 'actual measured Book publication' in capsys.readouterr().out
