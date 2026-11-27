"""Native picker policy: explicit selection/cancellation and owned destruction."""
from types import SimpleNamespace
import sys

import pytest

from carr_library.ui.backup_capacity import choose_backup_capacity


@pytest.mark.parametrize('accepted,selection,expected', [
    (True, 0, (True, None)),
    (True, 1, (True, 'large-source-v1')),
    (False, 1, (False, None)),
], ids=['ordinary', 'large', 'cancel'])
def test_selected_contract_or_cancel_is_explicit(monkeypatch, accepted, selection, expected):
    class Dialog:
        destroyed = False
        def SetSelection(self, value):
            assert value == 0
        def ShowModal(self):
            return 1 if accepted else 2
        def GetSelection(self):
            assert accepted
            return selection
        def Destroy(self):
            self.destroyed = True
    dialog = Dialog()
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1,
        SingleChoiceDialog=lambda *args: dialog))
    assert choose_backup_capacity(object()) == expected
    assert dialog.destroyed


@pytest.mark.parametrize('selection', [-1, 2, True], ids=['none', 'outside', 'boolean'])
def test_invalid_selection_cannot_invent_backup_admission(monkeypatch, selection):
    dialog = SimpleNamespace(SetSelection=lambda value: None, ShowModal=lambda: 1,
        GetSelection=lambda: selection, destroyed=False)
    dialog.Destroy = lambda: setattr(dialog, 'destroyed', True)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1,
        SingleChoiceDialog=lambda *args: dialog))
    with pytest.raises(ValueError):
        choose_backup_capacity(object())
    assert dialog.destroyed
