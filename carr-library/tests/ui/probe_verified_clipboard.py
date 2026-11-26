"""Run against a combined PR24/26/34 tree; fake wx only, real sealed reader."""
import ast
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.reader.context import open_context


@pytest.fixture
def candidate():
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/search_panel.py'
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
    hit = NS(workspace='A', ref='passage-1', revision='r1', quote=page.quote, citation=page.citation,
             citation_verified=True, verification_status='Verified source text')
    panel.model = Mock()
    panel.model.workspace = 'A'
    panel.model.hit.return_value = hit
    panel.grid, panel.status = Mock(), Mock()
    panel.grid.selected_rows.return_value = [0]
    panel.more_service = lambda w, ref, rev: open_context(w, [anchor], load).page(0).raw_text

    def verified_copy(w, ref, rev, field):
        if (w, ref, rev) != ('A', 'passage-1', 'r1'):
            raise ValueError('identity mismatch')
        return context.clipboard(w, 0, field, load)

    panel.verified_copy_service = Mock(side_effect=verified_copy)
    return panel, clip, context, load, state, hit


@pytest.mark.parametrize('field', ['quote', 'citation'])
def test_healthy(candidate, field):
    p, clip, context, load, _, hit = candidate
    p.copy(field)
    clip.SetData.assert_called_once_with(context.clipboard('A', 0, field, load))
    p.verified_copy_service.assert_called_once_with('A', 'passage-1', 'r1', field)
    assert hit.quote == 'Київ\n'


@pytest.mark.parametrize('fault', ['drift', 'missing'])
@pytest.mark.parametrize('field', ['quote', 'citation'])
def test_unresolvable(candidate, fault, field):
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
    clip.Open.assert_not_called()
    clip.SetData.assert_not_called()


def test_scope_changed(candidate):
    p, clip, *_ = candidate
    p.model.workspace = 'B'
    p.copy('quote')
    p.verified_copy_service.assert_not_called()
    clip.Open.assert_not_called()


def test_mismatched_payload(candidate):
    p, clip, *_ = candidate
    p.verified_copy_service = Mock(return_value='different revision')
    p.copy('citation')
    clip.Open.assert_not_called()


def test_callback_missing(candidate):
    p, clip, *_ = candidate
    del p.verified_copy_service
    p.copy('quote')
    clip.Open.assert_not_called()


def test_scope_changes_during_verification(candidate):
    p, clip, _, _, _, hit = candidate
    def change(*args):
        p.model.workspace = 'B'
        return hit.quote
    p.verified_copy_service = change
    p.copy('quote')
    clip.Open.assert_not_called()
