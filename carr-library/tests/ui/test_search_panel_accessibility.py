"""Headless handler regressions; native wx and NVDA are separate acceptance."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from carr_library.ui.search_model import Hit, SearchModel


@pytest.fixture
def panel():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/search_panel.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SearchPanel')
    clip = Mock()
    clip.Open.return_value = clip.SetData.return_value = clip.Flush.return_value = True
    wx = SimpleNamespace(Panel=object, TheClipboard=clip, TextDataObject=lambda value: value)
    env = {'wx': wx, 'SearchModel': SearchModel}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), env)
    p = object.__new__(env['SearchPanel'])
    p.model = SearchModel()
    p.model.set_workspace('library')
    h = Hit('library', 'ref', 'revision', 'Title A', 'Автор Б', 'Edition C',
            'Printed page unknown; physical 2', 'Точний текст\n“quote”', 'Citation token')
    p.model.replace('library', [h])
    p.grid, p.details, p.status = Mock(), Mock(), Mock()
    p.grid.selected_rows.return_value = [0]
    return p, h, clip


def test_readable_details_preserve_source_metadata_and_quote(panel):
    p, h, _ = panel
    p.on_selection(Mock())
    assert p.details.ChangeValue.call_args.args[0] == (
        f'Title: {h.title}\nAuthor: {h.author}\nEdition: {h.edition}\nPage: {h.page}'
        f'\nCitation: {h.citation}\n\nQuotation:\n{h.quote}')


def test_selection_loss_clears_details(panel):
    p, _, _ = panel
    p.grid.selected_rows.return_value = []
    p.on_selection(Mock())
    p.details.ChangeValue.assert_called_once_with('')


@pytest.mark.parametrize('field', ['quote', 'citation'])
@pytest.mark.parametrize('persisted', [True, False])
def test_clipboard_status_and_exact_payload(panel, field, persisted):
    p, h, clip = panel
    clip.Flush.return_value = persisted
    p.copy(field)
    clip.SetData.assert_called_once_with(getattr(h, field))
    clip.Close.assert_called_once()
    p.status.SetFocus.assert_called_once()
    message = p.status.ChangeValue.call_args.args[0]
    if persisted:
        assert message == 'Copied ' + field
    else:
        assert 'after exit' in message and message != 'Copied ' + field


def test_flush_exception_closes_clipboard(panel):
    p, _, clip = panel
    clip.Flush.side_effect = RuntimeError('flush failed')
    p.on_quote(None)
    clip.Close.assert_called_once()
    assert 'failed' in p.status.ChangeValue.call_args.args[0].lower()
