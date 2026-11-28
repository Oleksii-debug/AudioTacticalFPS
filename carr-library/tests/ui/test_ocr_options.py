"""Import language intent and screen-readable completion text (no GUI verdict)."""
import sys
from types import SimpleNamespace

import pytest

from carr_library.ui.ocr_options import choose_ocr_config
from carr_library.ui import ocr_language_dialog
from carr_library.extract.page_ocr import Config


@pytest.mark.parametrize('expected,profile', [('eng', 'installed'), ('ukr', 'installed'),
    ('slk', 'installed'), ('eng+ukr+slk', 'installed'), ('script/Latin+fra', 'installed'),
    ('eng+script/Latin', 'fast'), ('script/Cyrillic', 'best')])
def test_dialog_adapter_preserves_complete_model_selection(monkeypatch, expected, profile):
    observed = {}
    class Dialog:
        result = Config(languages=expected, dpi=200, timeout=45, orientation=True, model_profile=profile)
        def ShowModal(self):
            return 1
        def Destroy(self):
            observed['destroyed'] = True
    def create(parent, initial, *, preset_root):
        observed.update(parent=parent, initial=initial, root=preset_root)
        return Dialog()
    monkeypatch.setattr(ocr_language_dialog, 'create_ocr_dialog', create)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1))
    result = choose_ocr_config(None, ['book.PDF'])
    assert result == Dialog.result
    assert observed['destroyed'] and observed['initial'] is None


def test_cancel_prevents_import_and_closes_dialog(monkeypatch):
    observed = []
    dialog = SimpleNamespace(ShowModal=lambda: 0, result=Config(), Destroy=lambda: observed.append('closed'))
    monkeypatch.setattr(ocr_language_dialog, 'create_ocr_dialog', lambda *args, **kwargs: dialog)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_OK=1))
    assert choose_ocr_config(None, ['scan.pdf']) is None
    assert observed == ['closed']


def test_text_import_needs_no_window(monkeypatch):
    monkeypatch.setitem(sys.modules, 'wx', None)
    assert choose_ocr_config(None, ['source.txt', 'report.docx']).languages == 'eng'
