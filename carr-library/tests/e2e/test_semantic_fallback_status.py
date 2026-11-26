"""Fallback disclosure is operation-local and does not require any model download."""
from types import SimpleNamespace

import pytest

import carr_library.application as application
import carr_library.semantic as semantic
from carr_library.application import LibraryApp
from carr_library.search import semantic_windows, thematic
from carr_library.semantic import OfflineSemanticRanker


class OverBudgetTokenizer:
    """Exercise only the fallback boundary; never impersonate model inference."""
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        return SimpleNamespace(ids=list(range(129)), offsets=[(0, 0)] * 129,
                               special_tokens_mask=[1] * 129, overflowing=[])


@pytest.mark.parametrize('clock', [None, 4.001])
def test_available_model_budget_fallback_is_reported_without_results(tmp_path, monkeypatch, clock):
    ranker = object.__new__(OfflineSemanticRanker)
    ranker.tokenizer = OverBudgetTokenizer()
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Fallback fixture')
    app.add_text(workspace, 'Document', 'A source about railway history.')
    if clock is not None:
        # Use the actual semantic snapshot and fallback path with a coarse
        # clock in every layer, without initializing model inference.
        for module in (application, semantic, semantic_windows, thematic):
            monkeypatch.setattr(module, 'monotonic', lambda: clock)
    messages = []
    assert app.search(workspace, 'botanical flowers', mode='semantic', on_status=messages.append) == ()
    assert len(messages) == 1
    assert 'Keyword search used.' in messages[0]
    assert 'Shorten the query' in messages[0]
    # No status is stored on the shared application or leaked to a later query.
    later = []
    assert app.search(workspace, 'railway', on_status=later.append)[0].verified
    assert later == []


def test_constant_clock_preserves_nested_semantic_budget_without_fallback(tmp_path, monkeypatch):
    class BoundaryTokenizer:
        """One token per character, solely to exercise adapter budget handoffs."""
        truncation = padding = None

        def encode(self, text, add_special_tokens=True):
            offsets = [(i, i + 1) for i in range(len(text))]
            return SimpleNamespace(ids=list(range(len(offsets) + 2)),
                offsets=[(0, 0), *offsets, (0, 0)],
                special_tokens_mask=[1, *([0] * len(offsets)), 1], overflowing=[])

    ranker = object.__new__(OfflineSemanticRanker)
    ranker.tokenizer = BoundaryTokenizer()
    observed = []

    def inspect_budget(query, candidates, *, cancelled, timeout):
        # This is a synthetic adapter boundary, not model inference.
        assert candidates and not cancelled()
        assert 0 < timeout <= 60
        observed.append(timeout)
        return []

    monkeypatch.setattr(ranker, 'rank', inspect_budget)
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Budget handoff fixture')
    app.add_text(workspace, 'Document', 'A source about railway history.')
    for module in (application, semantic, semantic_windows, thematic):
        monkeypatch.setattr(module, 'monotonic', lambda: 4.001)
    messages = []
    hits = app.search(workspace, 'railway', mode='semantic', on_status=messages.append)
    assert len(hits) == 1 and hits[0].verified and 'railway' in hits[0].quote
    assert observed == [60.0]
    assert messages == []
