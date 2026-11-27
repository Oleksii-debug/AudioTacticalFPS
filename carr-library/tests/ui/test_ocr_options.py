"""Import entrypoint delegates to the shared installed/managed model selector."""
from unittest.mock import Mock

import pytest

from carr_library.extract.page_ocr import Config
from carr_library.ui.ocr_options import choose_ocr_config


@pytest.mark.parametrize('profile,languages', [
    ('installed', 'eng'), ('installed', 'ukr+slk'),
    ('fast', 'eng+script/Latin'), ('best', 'script/Cyrillic'),
])
def test_import_uses_selected_exact_profile_and_model_order(monkeypatch, profile, languages):
    result = Config(languages=languages, model_profile=profile)
    chooser = Mock(return_value=result)
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    parent = object()
    assert choose_ocr_config(parent, ['book.PDF']) is result
    chooser.assert_called_once_with(parent)


def test_cancel_prevents_import(monkeypatch):
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', lambda parent: None)
    assert choose_ocr_config(None, ['scan.pdf']) is None


def test_text_import_needs_no_window(monkeypatch):
    chooser = Mock(side_effect=AssertionError('Text-only imports must not open OCR settings'))
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    assert choose_ocr_config(None, ['source.txt', 'report.docx', 'book.epub']) == Config()
    chooser.assert_not_called()
