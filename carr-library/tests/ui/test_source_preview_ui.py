"""Headless preview UI behavior; these tests do not claim native NVDA acceptance."""
import ast
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.reader.context import open_context
from carr_library.reader.source_preview import SourcePreview, open_pdf_preview
from carr_library.ui.page_context_dialog import PageSession, show_page_context
from carr_library.ui.search_model import Hit, SearchModel


@pytest.fixture
def preview():
    original = b'%PDF-synthetic UI fixture'
    identity = Identity('library', 'edition', sha256(original).hexdigest(), 'revision')
    raw = 'Unverified Café source\nSecond line.'
    return open_pdf_preview('library', identity, Locator('physical_page', 3),
                            raw, 0, 21, Metadata('Author', 'Source title'),
                            stored_digest=digest(raw), original_bytes=original)


@pytest.fixture
def panel(preview):
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/search_panel.py'
    cls = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
               if isinstance(node, ast.ClassDef) and node.name == 'SearchPanel')
    clipboard = Mock()
    wx = SimpleNamespace(Panel=object, TheClipboard=clipboard,
                         TextDataObject=lambda value: value)
    env = {'wx': wx, 'SearchModel': SearchModel}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), env)
    widget = object.__new__(env['SearchPanel'])
    widget.model = SearchModel()
    widget.model.set_workspace('library')
    page = preview.page(0)
    hit = Hit('library', 'ref', 'revision', page.metadata.title, page.metadata.author,
              'Unknown edition', page.page_label, page.quote, page.citation,
              citation_verified=False, verification_status=page.verification_status)
    widget.model.replace('library', (hit,))
    widget.grid, widget.status, widget.details = Mock(), Mock(), Mock()
    widget.grid.selected_rows.return_value = [0]
    widget.verified_actions = [Mock(), Mock(), Mock()]
    widget.more_button = Mock()
    widget.verified_copy_service, widget.save_evidence_service = Mock(), Mock()
    return widget, hit, clipboard


def test_existing_hit_construction_and_explicit_preview_column(panel):
    widget, hit, _ = panel
    old = Hit('library', 'ref', 'revision', 'Title', 'Author', 'Edition',
              'Page', 'Exact text', 'Citation')
    assert old.citation_verified is True
    assert old.verification_status == 'Verified source text'
    columns = {column.name: column.label for column in widget.model.columns()}
    assert columns['quote'] == 'Excerpt'
    assert columns['verification_status'] == 'Verification'
    assert widget.model.cell_text(0, 'verification_status') == hit.verification_status
    assert widget.model.cell_text(0, 'quote') == hit.quote


@pytest.mark.parametrize('changes', [
    {'citation_verified': 1}, {'verification_status': ''},
    {'verification_status': 'Verified source text'},
])
def test_model_rejects_ambiguous_preview_status(panel, changes):
    widget, hit, _ = panel
    with pytest.raises(ValueError, match='Invalid'):
        widget.model.replace('library', (replace(hit, **changes),))
    assert widget.model.row_count() == 0


def test_preview_details_keep_more_available_and_disable_verified_actions(panel):
    widget, hit, _ = panel
    widget._refresh_details()
    details = widget.details.ChangeValue.call_args.args[0]
    assert hit.verification_status in details and hit.quote in details
    assert 'Excerpt:' in details and 'Quotation:' not in details
    widget.more_button.Enable.assert_called_with(True)
    for action in widget.verified_actions:
        action.Enable.assert_called_with(False)
    verified = replace(hit, citation_verified=True, verification_status='Verified source text')
    widget.model.replace('library', (verified,))
    widget._refresh_details()
    for action in widget.verified_actions:
        action.Enable.assert_called_with(True)
    widget.grid.selected_rows.return_value = []
    widget._refresh_details()
    widget.more_button.Enable.assert_called_with(False)
    assert widget.details.ChangeValue.call_args.args[0] == ''


@pytest.mark.parametrize('action', ['quote', 'citation', 'save'])
def test_preview_never_calls_verified_copy_or_save_services(panel, action):
    widget, _, clipboard = panel
    if action == 'save':
        widget.on_save_evidence(None)
    else:
        widget.copy(action)
    widget.verified_copy_service.assert_not_called()
    widget.save_evidence_service.assert_not_called()
    clipboard.Open.assert_not_called()
    assert 'Unverified extraction preview' in widget.status.ChangeValue.call_args.args[0]
    widget.status.SetFocus.assert_called_once()


def test_more_preview_reloads_exact_selection_and_bounds(preview):
    selection = ('library', 'ref', 'revision')
    loader = Mock(return_value=preview)
    session = PageSession(selection, lambda: selection, loader)
    page, count = session.visit(0)
    assert page == preview.page(0) and count == 1
    assert session.citation_verified is False
    assert not hasattr(page, 'anchor')
    for position in (-1, 1, True):
        with pytest.raises(IndexError):
            session.visit(position)
    assert session.position == 0
    assert loader.call_count == 4


@pytest.mark.parametrize('change', ['identity', 'locator', 'raw', 'quote', 'metadata'])
def test_more_rejects_preview_identity_or_content_drift(preview, change):
    selection = ('library', 'ref', 'revision')
    state = SimpleNamespace(context=preview)
    session = PageSession(selection, lambda: selection, lambda *args: state.context)
    session.visit(0)
    page = preview.page(0)
    if change == 'identity':
        changed = replace(page, identity=replace(page.identity, edition_id='other-edition'))
    elif change == 'locator':
        changed = replace(page, locator=Locator('physical_page', 4))
    elif change == 'raw':
        raw = page.raw_text + '\nChanged source'
        changed = replace(page, raw_text=raw, raw_sha256=digest(raw))
    elif change == 'quote':
        changed = replace(page, start=1)
    else:
        changed = replace(page, metadata=Metadata('Other author', 'Source title'))
    state.context = SourcePreview((changed,))
    with pytest.raises(ValueError, match='anchors changed'):
        session.visit(0)


@pytest.mark.parametrize('field,value', [('workspace', 'other'), ('revision', 'other')])
def test_more_rejects_preview_outside_selection(preview, field, value):
    selection = ('library', 'ref', 'revision')
    page = preview.page(0)
    other = SourcePreview((replace(page, identity=replace(page.identity, **{field: value})),))
    with pytest.raises(ValueError, match='identity mismatch'):
        PageSession(selection, lambda: selection, lambda *args: other).visit(0)


def test_more_checks_selection_before_and_after_preview_load(preview):
    selection = ('library', 'ref', 'revision')
    state = SimpleNamespace(selection=selection)

    def load(*args):
        state.selection = ('other', 'ref', 'revision')
        return preview

    with pytest.raises(ValueError, match='selection changed'):
        PageSession(selection, lambda: state.selection, load).visit(0)
    forbidden = Mock()
    with pytest.raises(ValueError, match='Selection changed'):
        PageSession(selection, lambda: state.selection, forbidden).visit(0)
    forbidden.assert_not_called()


def test_more_rejects_preview_becoming_verified_mid_visit(preview):
    selection = ('library', 'ref', 'revision')
    page = preview.page(0)
    anchor = make_anchor(page.identity, page.locator, page.raw_text, page.start,
                          page.end, page.metadata, text_verified=True)
    verified = open_context('library', (anchor,),
                            lambda identity, locator: (identity, locator, page.raw_text, True))
    loader = Mock(side_effect=[preview, verified])
    session = PageSession(selection, lambda: selection, loader)
    session.visit(0)
    with pytest.raises(ValueError, match='anchors changed'):
        session.visit(0)


def test_more_native_adapter_labels_preview_without_copy_controls(preview, monkeypatch):
    selection = ('library', 'ref', 'revision')
    status, reader, previous, following, back, dialog = [Mock() for _ in range(6)]
    wx = SimpleNamespace(Dialog=Mock(return_value=dialog), BoxSizer=Mock(), StaticText=Mock(),
                         TextCtrl=Mock(side_effect=[status, reader]),
                         Button=Mock(side_effect=[previous, following, back]),
                         VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8,
                         TE_READONLY=16, TE_MULTILINE=32, ID_CANCEL=64, EVT_BUTTON=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    show_page_context(None, selection, lambda: selection, lambda *args: preview)
    dialog.SetTitle.assert_called_with('Unverified extraction preview')
    reader.SetName.assert_called_with('Unverified extracted text')
    reader.ChangeValue.assert_called_with(preview.page(0).raw_text)
    assert 'Source review is required' in status.ChangeValue.call_args.args[0]
    status.SetFocus.assert_called_once()
    previous.Enable.assert_called_with(False)
    following.Enable.assert_called_with(False)
    assert wx.Button.call_count == 3
    dialog.Destroy.assert_called_once()


def test_preview_search_requires_explicit_source_choice_and_clears_old_hits(panel):
    widget, preview_hit, _ = panel
    verified_hit = replace(preview_hit, citation_verified=True, verification_status='Verified source text')
    widget.query, widget.retrieval, widget.result_mode, widget.source_mode = [Mock() for _ in range(4)]
    widget.query.GetValue.return_value = 'Café'
    widget.retrieval.GetSelection.return_value = widget.result_mode.GetSelection.return_value = 0
    widget.source_mode.GetSelection.return_value = 0
    widget.search_service = Mock(return_value=(verified_hit,))
    widget.preview_search_service = Mock(return_value=(preview_hit,))
    widget.on_search(None)
    widget.search_service.assert_called_once_with('library', 'Café', 'literal', 'short', 100)
    widget.preview_search_service.assert_not_called()
    assert widget.model.hit(0).citation_verified is True

    widget.source_mode.GetSelection.return_value = 1
    widget.on_source_mode(None)
    assert widget.model.row_count() == 0
    assert widget.details.ChangeValue.call_args.args[0] == ''
    for action in widget.verified_actions:
        action.Enable.assert_called_with(False)
    widget.on_search(None)
    widget.preview_search_service.assert_called_once_with('library', 'Café', 'literal', 'short', 100)
    assert widget.model.hit(0).citation_verified is False
    widget.copy('quote')
    widget.on_save_evidence(None)
    widget.verified_copy_service.assert_not_called()
    widget.save_evidence_service.assert_not_called()


def test_unknown_source_choice_fails_closed(panel):
    widget, _, _ = panel
    widget.query, widget.retrieval, widget.result_mode, widget.source_mode = [Mock() for _ in range(4)]
    widget.query.GetValue.return_value = 'Café'
    widget.source_mode.GetSelection.return_value = 2
    widget.search_service = Mock()
    widget.preview_search_service = Mock()
    widget.on_search(None)
    widget.search_service.assert_not_called()
    widget.preview_search_service.assert_not_called()
    assert widget.model.row_count() == 0
    assert widget.status.ChangeValue.call_args.args[0] == 'Search failed. No results retained.'


def test_preview_rejects_offset_drift_when_identical_text_occurs_twice(preview):
    selection = ('library', 'ref', 'revision')
    original = preview.page(0)
    raw = 'same same'
    first = replace(original, raw_text=raw, raw_sha256=digest(raw), start=0, end=4)
    second = replace(first, start=5, end=9)
    assert first.quote == second.quote
    loader = Mock(side_effect=[SourcePreview((first,)), SourcePreview((second,))])
    session = PageSession(selection, lambda: selection, loader)
    session.visit(0)
    with pytest.raises(ValueError, match='anchors changed'):
        session.visit(0)
