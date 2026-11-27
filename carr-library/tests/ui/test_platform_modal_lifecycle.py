"""Exercise Windows modal callback ordering without claiming native wx coverage."""
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


request_modal_escape = runpy.run_path(str(
    Path(__file__).resolve().parents[2] / 'scripts' / 'windows_platform_tools_smoke.py'
))['request_modal_escape']


class WindowsModalLifecycle:
    """wxMSW retains modal state until the active event callback returns."""

    def __init__(self):
        self.shown = self.modal = False
        self.code = 0
        self.end_calls = 0
        self.events = []

    def IsShown(self): return self.shown
    def IsModal(self): return self.modal
    def GetReturnCode(self): return self.code

    def EndModal(self, code):
        self.code, self.shown = code, False
        self.end_calls += 1
        # The event loop is asked to exit; its owning callback is still active.
        assert self.modal

    def ShowModal(self, callback):
        self.shown = self.modal = True
        callback()
        assert not self.shown, 'The callback must actually request a close.'
        self.modal = False
        return self.code

    def pump(self, predicate):
        if self.events:
            self.events.pop(0)()
        assert self.modal, 'Pumping a callback cannot unwind its owning modal loop.'
        return predicate()


def test_escape_observes_close_before_modal_callback_returns():
    dialog, evidence, keys = WindowsModalLifecycle(), [], []

    def press(key):
        keys.append(key)
        dialog.events.append(lambda: dialog.EndModal(42))

    def callback():
        evidence.append(request_modal_escape(dialog, SimpleNamespace(Char=press),
            escape_key=27, close_code=42, pump=dialog.pump))
        assert dialog.IsModal()  # This is why the old nested pump timed out.
        assert not dialog.IsShown()

    code = dialog.ShowModal(callback)
    assert evidence == [True] and keys == [27]
    assert code == 42 and not dialog.IsModal() and dialog.end_calls == 1


@pytest.mark.parametrize('behavior', ['ignored', 'wrong-code', 'hide-without-end'])
def test_escape_cannot_pass_for_an_ignored_or_incorrect_close(behavior):
    dialog = WindowsModalLifecycle()
    dialog.shown = dialog.modal = True

    def press(key):
        assert key == 27
        if behavior == 'wrong-code':
            dialog.events.append(lambda: dialog.EndModal(99))
        elif behavior == 'hide-without-end':
            dialog.events.append(lambda: setattr(dialog, 'shown', False))

    assert not request_modal_escape(dialog, SimpleNamespace(Char=press),
        escape_key=27, close_code=42, pump=dialog.pump)


@pytest.mark.parametrize('shown,modal', [(False, False), (False, True), (True, False)])
def test_escape_requires_a_live_visible_modal_window(shown, modal):
    dialog = WindowsModalLifecycle()
    dialog.shown, dialog.modal = shown, modal
    keys = []
    assert not request_modal_escape(dialog, SimpleNamespace(Char=keys.append),
        escape_key=27, close_code=42, pump=dialog.pump)
    assert keys == []
