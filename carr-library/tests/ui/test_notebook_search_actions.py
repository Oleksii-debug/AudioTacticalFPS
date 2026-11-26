"""Real notebook-search handlers and worker threads; controlled native controls.

These assertions do not claim native focus or NVDA acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib.util
from pathlib import Path
from queue import Queue
import sys
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.anchored_export import SavedEvidence


class Control:
    def __init__(self, value=''):
        self.value, self.items, self.index = value, [], -1
        self.enabled, self.focused = True, False
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.index
    def SetSelection(self, index): self.index = index
    def Append(self, value): self.items.append(value)
    def Clear(self): self.items.clear(); self.index = -1
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def SetFocus(self): self.focused = True


@pytest.fixture
def ui():
    pending = Queue()
    clipboard = Mock()
    clipboard.Open.return_value = clipboard.SetData.return_value = clipboard.Flush.return_value = True
    wx = SimpleNamespace(Dialog=object, ID_OK=1, ID_CANCEL=2, TheClipboard=clipboard,
        TextDataObject=lambda text: text, CallAfter=lambda fn, *args: pending.put((fn, args)),
        CallLater=lambda delay, fn, *args: pending.put((fn, args)))
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/notebook_search.py'
    spec = importlib.util.spec_from_file_location('carr_library.ui._notebook_search_test', path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx}):
        spec.loader.exec_module(module)
    return module, pending, clipboard


def hit(topic='topic', identity='same-id', *, archived=False, note='A separate research note.'):
    source = Identity('library', 'edition', 'a' * 64, 'revision')
    metadata = Metadata('Synthetic author', 'Source title')
    first = make_anchor(source, Locator('physical_page', 2), 'First exact page.',
                        0, len('First exact page.'), metadata, text_verified=True)
    second = make_anchor(source, Locator('physical_page', 3), 'Second exact page.',
                         0, 12, metadata, text_verified=True)
    row = SavedEvidence(topic, identity, first, note, (first, second))
    return SimpleNamespace(evidence=row, topic_path=('Parent', topic), tags=('History',),
                           archived=archived, matched_fields=('quote', 'note'))


def result(*hits, total=None, truncated=False):
    return SimpleNamespace(hits=hits, total_matches=len(hits) if total is None else total,
                           evidence_scanned=75, topics_scanned=3, truncated=truncated)


@pytest.fixture
def dialog(ui):
    module, pending, _ = ui
    value = object.__new__(module.NotebookSearchDialog)
    value.library = SimpleNamespace(search_notebook=Mock(return_value=result(hit())),
                                    get_notebook_hit=Mock(return_value=hit()))
    value.frame = SimpleNamespace(library=value.library, active=('library', None), _closing=False)
    value.workspace, value.hits, value.open_target = 'library', (), None
    value._closed = value._busy = False
    value._epoch, value._cancel, value._future = 0, Event(), None
    value._executor = ThreadPoolExecutor(max_workers=1)
    for name in ('query', 'field', 'tag', 'archived', 'search', 'cancel', 'items', 'parts',
                 'quote', 'citation', 'note', 'status', 'copy', 'open'):
        setattr(value, name, Control())
    value.query.value = 'page'
    value.archived.value = False
    value.field.index = 0
    value.IsModal, value.EndModal, value.Hide = Mock(return_value=True), Mock(), Mock()
    yield value
    value.shutdown()
    value._executor.shutdown(wait=True, cancel_futures=True)


def finish(dialog, ui):
    dialog._future.exception(timeout=5)
    function, args = ui[1].get(timeout=5)
    function(*args)


def test_search_runs_in_worker_and_reports_honest_count_and_truncation(dialog, ui):
    thread = get_ident()
    def search(workspace, query, **options):
        assert get_ident() != thread
        assert workspace == 'library' and query == 'page'
        assert options['field'] == 'both' and options['tag'] is None
        assert options['include_archived'] is False and options['limit'] == 50
        assert options['timeout'] == 60 and not options['cancelled']()
        return result(hit(), total=60, truncated=True)
    dialog.library.search_notebook.side_effect = search
    dialog.on_search()
    assert dialog._busy and dialog.cancel.enabled and not dialog.search.enabled
    assert dialog.items.items == []
    finish(dialog, ui)
    assert not dialog._busy and not dialog.cancel.enabled
    assert 'Showing 1 of 60' in dialog.status.value
    assert '75 saved items in 3 topics' in dialog.status.value
    assert 'result limit' in dialog.status.value
    assert dialog.status.focused


def test_empty_query_requires_exact_tag_and_tag_only_is_explicit(dialog, ui):
    dialog.query.value = ''
    dialog.on_search()
    dialog.library.search_notebook.assert_not_called()
    assert 'Enter search words or an exact tag' in dialog.status.value
    dialog.tag.value = 'History'
    dialog.on_search()
    finish(dialog, ui)
    assert dialog.library.search_notebook.call_args.args == ('library', '')
    assert dialog.library.search_notebook.call_args.kwargs['tag'] == 'History'


def test_duplicate_evidence_ids_in_different_topics_are_distinct_results(dialog):
    dialog.show_results(result(hit('first'), hit('second')))
    assert len(dialog.items.items) == 2
    assert 'Topic: Parent / first' in dialog.items.items[0]
    assert 'Topic: Parent / second' in dialog.items.items[1]
    assert 'Source:' in dialog.items.items[0] and 'Matched quotation and note' in dialog.items.items[0]


def test_multi_page_quotes_citations_and_research_note_remain_separate(dialog):
    selected = hit()
    dialog.show_results(result(selected))
    assert dialog.quote.value == selected.evidence.anchors[0].quote
    assert 'PDF page 2' in dialog.citation.value
    dialog.parts.index = 1
    dialog.show_part()
    assert dialog.quote.value == selected.evidence.anchors[1].quote
    assert 'PDF page 3' in dialog.citation.value and 'PDF page 2' not in dialog.citation.value
    assert dialog.note.value == selected.evidence.note


def test_copy_freshly_verifies_composite_identity_and_never_copies_note(dialog, ui):
    selected = hit('chosen-topic')
    dialog.show_results(result(selected))
    fresh = SimpleNamespace(**{**vars(selected), 'matched_fields': ()})
    dialog.library.get_notebook_hit.return_value = fresh
    dialog.verify_selected('copy')
    ui[2].Open.assert_not_called()
    finish(dialog, ui)
    call = dialog.library.get_notebook_hit.call_args
    assert call.args == ('library', 'chosen-topic', 'same-id')
    assert call.kwargs['include_archived'] is False
    payload = ui[2].SetData.call_args.args[0]
    assert all(anchor.quote in payload for anchor in selected.evidence.anchors)
    assert 'PDF page 2' in payload and 'PDF page 3' in payload
    assert selected.evidence.note not in payload
    assert dialog.status.focused


@pytest.mark.parametrize('changed', ['source', 'note', 'selection'])
def test_stale_verification_or_selection_clears_untrusted_details(dialog, ui, changed):
    first, second = hit('first'), hit('second')
    dialog.show_results(result(first, second))
    if changed == 'source':
        dialog.library.get_notebook_hit.side_effect = ValueError('Retained source checksum mismatch.')
    elif changed == 'note':
        dialog.library.get_notebook_hit.return_value = hit('first', note='Saved elsewhere.')
    else:
        dialog.library.get_notebook_hit.return_value = first
    dialog.verify_selected('copy')
    if changed == 'selection':
        dialog.items.index = 1
    finish(dialog, ui)
    ui[2].Open.assert_not_called()
    assert dialog.hits == () and dialog.quote.value == dialog.citation.value == ''
    assert dialog.status.focused


def test_archive_is_explicit_read_only_and_copy_reauthorizes_it(dialog, ui):
    selected = hit(archived=True)
    dialog.archived.value = True
    dialog.library.get_notebook_hit.return_value = selected
    dialog.show_results(result(selected))
    assert 'Archived topic, read only' in dialog.items.items[0]
    assert not dialog.open.enabled and dialog.copy.enabled
    dialog.verify_selected('open')
    dialog.library.get_notebook_hit.assert_not_called()
    dialog.verify_selected('copy')
    finish(dialog, ui)
    assert dialog.library.get_notebook_hit.call_args.kwargs['include_archived'] is True
    assert dialog.open_target is None


def test_cancelled_worker_has_no_late_results_and_keeps_modal_close_available(dialog, ui):
    started, release = Event(), Event()
    def search(*args, cancelled, **kwargs):
        started.set()
        assert release.wait(3)
        assert cancelled()
        return result(hit())
    dialog.library.search_notebook.side_effect = search
    dialog.on_search()
    assert started.wait(3)
    dialog.on_cancel()
    release.set()
    finish(dialog, ui)
    assert dialog.hits == () and not dialog._busy
    assert 'cancelled' in dialog.status.value
    ui[2].Open.assert_not_called()


def test_close_does_not_wait_for_worker_or_queue_late_controls(dialog, ui):
    started, release = Event(), Event()
    def search(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return result(hit())
    dialog.library.search_notebook.side_effect = search
    dialog.on_search()
    assert started.wait(3)
    dialog.on_close()
    assert dialog._closed and dialog._cancel.is_set()
    dialog.EndModal.assert_called_once_with(ui[0].wx.ID_CANCEL)
    assert not dialog._future.done()
    release.set()
    dialog._future.result(timeout=3)
    assert ui[1].empty()


def test_scope_change_before_delivery_clears_results(dialog, ui):
    dialog.on_search()
    dialog.frame.active = ('other-library', None)
    finish(dialog, ui)
    assert not dialog.hits and dialog.quote.value == ''
    assert 'library changed' in dialog.status.value and dialog.status.focused


def test_open_uses_fresh_record_and_remembers_selected_source_part(dialog, ui):
    selected = hit()
    dialog.show_results(result(selected))
    dialog.parts.index = 1
    dialog.verify_selected('open')
    assert dialog.open_target is None
    finish(dialog, ui)
    assert dialog.open_target == (selected, 1)
    dialog.EndModal.assert_called_once_with(ui[0].wx.ID_OK)
    ui[2].Open.assert_not_called()


def test_refused_scope_switch_never_overwrites_draft_even_in_same_topic(ui):
    module = ui[0]
    selected = hit()
    panel = SimpleNamespace(refresh=Mock(), note=Control('unsaved draft'))
    frame = SimpleNamespace(library=object(), active=('library', 'topic'), working=False,
                            book=object(), evidence=panel, report=Mock(), _open_workspace=Mock())
    assert not module.open_notebook_hit(frame, frame.library, 'library', selected)
    frame._open_workspace.assert_called_once_with('library', 'topic')
    panel.refresh.assert_not_called()
    assert panel.note.value == 'unsaved draft'


def test_open_targets_exact_evidence_and_page_after_async_refresh(ui):
    module, pending, _ = ui
    selected = hit('destination')
    panel = SimpleNamespace(refresh=Mock(return_value=True), displayed=selected.evidence,
                            page_choice=Control(), show_page=Mock(), note=Control())
    book = SimpleNamespace(GetPageCount=lambda: 2, GetPage=lambda i: panel if i == 1 else object(),
                           ChangeSelection=Mock())
    frame = SimpleNamespace(library=object(), active=('library', 'old-topic'), working=False,
                            book=object(), evidence=None, report=Mock())
    def open_scope(workspace, topic):
        frame.active, frame.book, frame.evidence = (workspace, topic), book, panel
    frame._open_workspace = open_scope
    assert module.open_notebook_hit(frame, frame.library, 'library', selected, 1)
    panel.refresh.assert_called_once_with(evidence_id='same-id')
    callback, args = pending.get(timeout=1)
    callback(*args)
    book.ChangeSelection.assert_called_once_with(1)
    assert panel.page_choice.index == 1 and panel.note.focused


def test_late_navigation_callback_cannot_clear_or_focus_a_new_evidence_selection(ui):
    module, pending, _ = ui
    chosen, newer = hit('destination'), hit('destination', 'new-user-selection')
    panel = SimpleNamespace(refresh=Mock(return_value=True), displayed=chosen.evidence,
        _evidence_epoch=1, page_choice=Control(), show_page=Mock(), note=Control('new draft'),
        _clear_source=Mock())
    book = SimpleNamespace(GetPageCount=lambda: 1, GetPage=lambda i: panel, ChangeSelection=Mock())
    frame = SimpleNamespace(library=object(), active=('library', 'old-topic'), working=False,
                            book=object(), evidence=None, report=Mock())
    def open_scope(workspace, topic):
        frame.active, frame.book, frame.evidence = (workspace, topic), book, panel
    frame._open_workspace = open_scope
    assert module.open_notebook_hit(frame, frame.library, 'library', chosen)
    # The user starts a different selection before the queued focus callback.
    panel._evidence_epoch += 1
    panel.displayed = newer.evidence
    callback, args = pending.get(timeout=1)
    callback(*args)
    panel._clear_source.assert_not_called()
    book.ChangeSelection.assert_not_called()
    panel.show_page.assert_not_called()
    frame.report.assert_not_called()
    assert panel.note.value == 'new draft' and not panel.note.focused

