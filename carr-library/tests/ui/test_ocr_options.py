"""Import language intent and screen-readable completion text (no GUI verdict)."""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui.ocr_options import choose_ocr_config


@pytest.mark.parametrize('choice,expected', [(0, 'eng'), (1, 'ukr'), (2, 'slk'), (3, 'eng+ukr+slk')])
def test_native_dialog_preserves_language_selection(monkeypatch, choice, expected):
    observed = {}
    class Dialog:
        def __init__(self, parent, message, title, labels):
            observed.update(message=message, title=title, labels=labels)
        def SetSelection(self, index):
            observed['initial'] = index
        def ShowModal(self):
            return 1
        def GetSelection(self):
            return choice
        def Destroy(self):
            observed['destroyed'] = True
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(SingleChoiceDialog=Dialog, ID_OK=1))
    result = choose_ocr_config(None, ['book.PDF'])
    assert result.languages == expected
    assert observed['title'] == 'Text recognition languages'
    assert observed['destroyed'] and observed['initial'] == 0


def test_cancel_prevents_import_and_closes_dialog(monkeypatch):
    observed = []
    dialog = SimpleNamespace(SetSelection=lambda index: None, ShowModal=lambda: 0,
                             Destroy=lambda: observed.append('closed'))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(SingleChoiceDialog=lambda *args: dialog, ID_OK=1))
    assert choose_ocr_config(None, ['scan.pdf']) is None
    assert observed == ['closed']


def test_text_import_needs_no_window(monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', None)
    assert choose_ocr_config(None, ['source.txt', 'report.docx']).languages == 'eng'
