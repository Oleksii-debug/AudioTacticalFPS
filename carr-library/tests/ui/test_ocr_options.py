"""Import language intent and screen-readable completion text (no GUI verdict)."""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui.ocr_options import choose_ocr_config
from carr_library.ui import ocr_options
from carr_library.extract.page_ocr import Config


@pytest.fixture(autouse=True)
def installed_models(monkeypatch):
    monkeypatch.setattr(ocr_options, 'discover_ocr_languages',
        lambda: ('eng', 'ukr', 'slk', 'deu', 'script/Latin', 'custom_model'))


@pytest.mark.parametrize('choice,expected', [([0], 'eng'), ([1], 'ukr'), ([2], 'slk'),
    ([0, 1, 2], 'eng+ukr+slk'), ([3, 4], 'deu+script/Latin'), ([5], 'custom_model')])
def test_native_dialog_preserves_language_selection(monkeypatch, choice, expected):
    observed = {}
    class Dialog:
        def __init__(self, parent, message, title, labels):
            observed.update(message=message, title=title, labels=labels)
        def SetSelections(self, indices):
            observed['initial'] = indices
        def ShowModal(self):
            return 1
        def GetSelections(self):
            return choice
        def Destroy(self):
            observed['destroyed'] = True
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(MultiChoiceDialog=Dialog, ID_OK=1))
    result = choose_ocr_config(None, ['book.PDF'])
    assert result.languages == expected
    assert observed['title'] == 'Text recognition languages'
    assert observed['destroyed'] and observed['initial'] == [0]
    assert 'Space checks' in observed['message']
    assert 'Latin script (script/Latin)' in observed['labels']
    assert 'custom_model' in observed['labels']


def test_cancel_prevents_import_and_closes_dialog(monkeypatch):
    observed = []
    dialog = SimpleNamespace(SetSelections=lambda indices: None, ShowModal=lambda: 0,
                             Destroy=lambda: observed.append('closed'))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(MultiChoiceDialog=lambda *args: dialog, ID_OK=1))
    assert choose_ocr_config(None, ['scan.pdf']) is None
    assert observed == ['closed']


def test_text_import_needs_no_window(monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', None)
    monkeypatch.setattr(ocr_options, 'discover_ocr_languages', lambda: pytest.fail('No OCR query for text'))
    assert choose_ocr_config(None, ['source.txt', 'report.docx']).languages == 'eng'


@pytest.mark.parametrize('selection', [(), (-1,), (6,), (0, 0), (True,), ('0',)])
def test_invalid_or_empty_language_selection_is_rejected(monkeypatch, selection):
    observed = []
    dialog = SimpleNamespace(SetSelections=lambda values: None, ShowModal=lambda: 1,
        GetSelections=lambda: selection, Destroy=lambda: observed.append('closed'))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(MultiChoiceDialog=lambda *args: dialog, ID_OK=1))
    with pytest.raises(ValueError, match='Choose at least one'):
        choose_ocr_config(None, ['source.pdf'])
    assert observed == ['closed']


def test_missing_english_uses_present_model_and_preserves_config(monkeypatch):
    monkeypatch.setattr(ocr_options, 'discover_ocr_languages', lambda: ('deu', 'script/Latin'))
    observed = {}
    dialog = SimpleNamespace(SetSelections=lambda values: observed.update(initial=values),
        ShowModal=lambda: 1, GetSelections=lambda: [0, 1], Destroy=lambda: None)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(MultiChoiceDialog=lambda *args: dialog, ID_OK=1))
    result = choose_ocr_config(None, (), initial=Config(dpi=240, timeout=45), force=True)
    assert observed['initial'] == [0]
    assert result == Config(languages='deu+script/Latin', dpi=240, timeout=45)


def test_missing_engine_is_explicit_and_keeps_native_pdf_path(monkeypatch):
    monkeypatch.setattr(ocr_options, 'discover_ocr_languages', lambda: ())
    observed = {}
    def create(parent, message, title, labels):
        observed['labels'] = labels
        return SimpleNamespace(SetSelections=lambda values: None, ShowModal=lambda: 1,
            GetSelections=lambda: [0], Destroy=lambda: None)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(MultiChoiceDialog=create, ID_OK=1))
    assert choose_ocr_config(None, ['source.pdf']) == Config()
    assert observed['labels'] == ['No local OCR languages found; scanned text unavailable']


def test_invalid_selected_runtime_never_silently_falls_back(monkeypatch):
    def fail():
        raise ValueError('Portable OCR file checksum mismatch.')
    monkeypatch.setattr(ocr_options, 'discover_ocr_languages', fail)
    monkeypatch.setitem(sys.modules, 'wx', None)
    with pytest.raises(ValueError, match='checksum'):
        choose_ocr_config(None, ['source.pdf'])
