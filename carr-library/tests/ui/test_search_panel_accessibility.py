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
    p.verified_copy_service = Mock(side_effect=lambda w, ref, rev, field: getattr(h, field))
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


def test_search_details_without_selection_event_and_repeated_search(panel):
    from dataclasses import replace
    p, h, _ = panel
    p.query, p.retrieval, p.result_mode = Mock(), Mock(), Mock()
    p.query.GetValue.return_value = 'quote'
    p.retrieval.GetSelection.return_value = p.result_mode.GetSelection.return_value = 0
    other = replace(h, title='Different title', quote='Новий текст', ref='other')
    p.search_service = Mock(side_effect=[[h], [other], []])
    # select_rows/focus_row are mocks: no wx selection event is emitted.
    for expected in (h, other):
        p.on_search(None)
        details = p.details.ChangeValue.call_args.args[0]
        for value in (expected.title, expected.author, expected.edition, expected.page,
                      expected.citation, expected.quote):
            assert value in details
        p.grid.focus_row.assert_called_with(0)
    p.on_search(None)
    assert p.details.ChangeValue.call_args.args[0] == ''
    p.status.SetFocus.assert_called_once()


def test_more_return_refreshes_details_without_selection_event(panel, monkeypatch):
    p, h, _ = panel
    wx = p.on_more.__func__.__globals__['wx']
    dialog, reader = Mock(), Mock()
    for name, value in dict(Dialog=Mock(return_value=dialog), TextCtrl=Mock(return_value=reader),
                            BoxSizer=Mock(), Button=Mock(), VERTICAL=1, TE_READONLY=2,
                            TE_MULTILINE=4, EXPAND=8, ALL=16, ID_CANCEL=32).items():
        monkeypatch.setattr(wx, name, value, raising=False)
    p.more_service = Mock(return_value='Original context: ' + h.quote)
    dialog.ShowModal.side_effect = lambda: p.details.ChangeValue('')
    p.on_more(None)
    p.grid.select_rows.assert_called_once_with([0])
    p.grid.focus_row.assert_called_once_with(0)
    assert h.quote in p.details.ChangeValue.call_args.args[0]
    assert h.citation in p.details.ChangeValue.call_args.args[0]
