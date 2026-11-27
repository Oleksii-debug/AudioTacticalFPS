"""Independent PR24 handler retest using real PR26/34 source verification.
Run with CARR_UI_SOURCE pointing at PR24 search_panel.py and PYTHONPATH
containing exact PR26 anchors.py and PR34 context.py. Native wx is mocked.
"""
import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context


@pytest.fixture
def fixture():
    path = Path(os.environ['CARR_UI_SOURCE'])
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SearchPanel')
    clip = Mock()
    clip.Open.return_value = clip.SetData.return_value = clip.Flush.return_value = True
    dialog, reader = Mock(), Mock()
    wx = NS(Panel=object, TheClipboard=clip, TextDataObject=lambda s: s,
            Dialog=Mock(return_value=dialog), TextCtrl=Mock(return_value=reader),
            BoxSizer=Mock(), Button=Mock(), VERTICAL=1, TE_READONLY=2,
            TE_MULTILINE=4, EXPAND=8, ALL=16, ID_CANCEL=32)
    env = {'wx': wx}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    panel = object.__new__(env['SearchPanel'])
    identity = Identity('A', 'edition-1', 'a' * 64, 'r1')
    locator = Locator('physical_page', 3, 'iv', True)
    raw = 'Before: Київ\n'
    anchor = make_anchor(identity, locator, raw, 8, len(raw),
                         Metadata('Synthetic author', 'Synthetic title'), text_verified=True)
    state = {'raw': raw, 'missing': False, 'verified': True, 'loaded': identity}

    def load(i, l):
        if state['missing']:
            raise FileNotFoundError('sealed source missing')
        return state['loaded'], l, state['raw'], state['verified']

    context = open_context('A', [anchor], load)
    page = context.page(0)
    hit = NS(workspace='A', ref='passage-1', revision='r1', quote=page.quote,
             citation=page.citation, title='Synthetic title', author='Synthetic author',
             edition='edition-1', page=locator.display())
    panel.model, panel.grid, panel.status, panel.details = Mock(), Mock(), Mock(), Mock()
    panel.model.workspace = 'A'
    panel.model.hit.return_value = hit
    panel.model.row_count.return_value = 1
    panel.grid.selected_rows.return_value = [0]

    def resolve(w, ref, rev):
        if (w, ref, rev) != ('A', 'passage-1', 'r1'):
            raise ValueError('incorrect source request')
        return open_context(w, [anchor], load)

    panel.more_service = lambda w, ref, rev: resolve(w, ref, rev).page(0).raw_text
    panel.verified_copy_service = Mock(side_effect=lambda w, ref, rev, field:
                                      resolve(w, ref, rev).clipboard(w, 0, field, load))
    return panel, clip, state, hit, dialog, reader


@pytest.mark.parametrize('field', ['quote', 'citation'])
def test_healthy_historical_copy(fixture, field):
    p, clip, _, hit, *_ = fixture
    p.copy(field)
    p.verified_copy_service.assert_called_once_with('A', 'passage-1', 'r1', field)
    clip.SetData.assert_called_once_with(getattr(hit, field))
    clip.Close.assert_called_once()
    p.status.ChangeValue.assert_called_once_with('Copied ' + field)
    p.status.SetFocus.assert_called_once()


@pytest.mark.parametrize('field', ['quote', 'citation'])
@pytest.mark.parametrize('fault', ['drift', 'missing', 'unverified', 'wrong_revision'])
def test_bad_source_cannot_copy(fixture, field, fault):
    p, clip, state, *_ = fixture
    if fault == 'drift':
        state['raw'] += ' changed'
    elif fault == 'missing':
        state['missing'] = True
    elif fault == 'unverified':
        state['verified'] = False
    else:
        state['loaded'] = Identity('A', 'edition-1', 'a' * 64, 'r2')
    p.on_more(None)
    p.status.ChangeValue.assert_called_with('Context unavailable or selection invalid')
    p.copy(field)
    clip.Open.assert_not_called()
    clip.SetData.assert_not_called()
    assert 'Nothing copied' in p.status.ChangeValue.call_args.args[0]
    p.status.SetFocus.assert_called()


def test_workspace_changes_during_verification(fixture):
    p, clip, _, hit, *_ = fixture
    original = p.verified_copy_service.side_effect

    def switch(*args):
        value = original(*args)
        p.model.workspace = 'B'
        return value

    p.verified_copy_service.side_effect = switch
    p.on_quote(None)
    clip.Open.assert_not_called()
    assert 'Nothing copied' in p.status.ChangeValue.call_args.args[0]


def test_stale_displayed_value(fixture):
    p, clip, _, hit, *_ = fixture
    hit.quote = 'not the source quote'
    p.on_quote(None)
    clip.Open.assert_not_called()
    assert 'Nothing copied' in p.status.ChangeValue.call_args.args[0]


def test_first_result_details_without_selection_event(fixture):
    p, _, _, hit, *_ = fixture
    p.query, p.retrieval, p.result_mode = Mock(), Mock(), Mock()
    p.query.GetValue.return_value = 'Київ'
    p.retrieval.GetSelection.return_value = 0
    p.result_mode.GetSelection.return_value = 0
    p.search_service = Mock(return_value=[hit])
    p.on_search(None)
    p.grid.select_rows.assert_called_once_with([0])
    p.grid.focus_row.assert_called_once_with(0)
    text = p.details.ChangeValue.call_args.args[0]
    for value in (hit.title, hit.author, hit.edition, hit.page, hit.quote, hit.citation):
        assert value in text


def test_more_returns_to_same_result(fixture):
    p, _, _, hit, dialog, reader = fixture
    p.on_more(None)
    reader.SetInsertionPoint.assert_called_once_with(0)
    reader.SetFocus.assert_called_once()
    dialog.ShowModal.assert_called_once()
    dialog.Destroy.assert_called_once()
    p.grid.select_rows.assert_called_once_with([0])
    p.grid.focus_row.assert_called_once_with(0)
    assert hit.quote in p.details.ChangeValue.call_args.args[0]
