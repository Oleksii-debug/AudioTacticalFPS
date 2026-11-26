"""Independent candidate integration probe: real reader and citation code, fake wx.
CARR_UI_SOURCE points to reviewed PR24 search_panel.py; PYTHONPATH provides PR26/34.
This deliberately exposes a host contract gap, not a native NVDA test.
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
def candidate():
    path = Path(os.environ['CARR_UI_SOURCE'])
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SearchPanel')
    clip = Mock()
    clip.Open.return_value = clip.SetData.return_value = clip.Flush.return_value = True
    wx = NS(Panel=object, TheClipboard=clip, TextDataObject=lambda s: s)
    env = {'wx': wx}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), env)
    panel = object.__new__(env['SearchPanel'])
    identity = Identity('A', 'edition-1', 'a' * 64, 'r1')
    locator = Locator('physical_page', 3, 'iv', True)
    raw = 'Before: Київ\n'
    anchor = make_anchor(identity, locator, raw, 8, len(raw),
                         Metadata('Synthetic author', 'Synthetic title'), text_verified=True)
    state = {'raw': raw, 'missing': False}

    def load(i, l):
        if state['missing']:
            raise FileNotFoundError('sealed source missing')
        return i, l, state['raw'], True

    context = open_context('A', [anchor], load)
    page = context.page(0)
    hit = NS(workspace='A', ref='passage-1', revision='r1', quote=page.quote, citation=page.citation)
    panel.model = Mock()
    panel.model.hit.return_value = hit
    panel.grid, panel.status = Mock(), Mock()
    panel.grid.selected_rows.return_value = [0]
    # Minimal adapter to PR24's documented string-based More contract.
    panel.more_service = lambda w, ref, rev: open_context(w, [anchor], load).page(0).raw_text
    return panel, clip, context, load, state, hit


def test_healthy_copy_matches_verified_reader(candidate):
    p, clip, context, load, _, hit = candidate
    p.on_quote(None)
    clip.SetData.assert_called_once_with(context.clipboard('A', 0, 'quote', load))
    assert hit.quote == 'Київ\n'


@pytest.mark.parametrize('fault', ['drift', 'missing'])
@pytest.mark.parametrize('field', ['quote', 'citation'])
def test_copy_after_more_detects_unresolvable_source(candidate, fault, field):
    p, clip, context, load, state, _ = candidate
    if fault == 'drift':
        state['raw'] += ' changed'
    else:
        state['missing'] = True
    with pytest.raises((ValueError, FileNotFoundError)):
        context.clipboard('A', 0, field, load)
    p.on_more(None)
    p.status.ChangeValue.assert_called_with('Context unavailable or selection invalid')
    p.copy(field)
    # Once the sealed source is known unresolvable, do not report verified copy.
    clip.SetData.assert_not_called()
