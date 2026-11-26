"""Fallback disclosure is operation-local and does not require any model download."""
from types import SimpleNamespace

from carr_library.application import LibraryApp
from carr_library.semantic import OfflineSemanticRanker


class OverBudgetTokenizer:
    """Exercise only the fallback boundary; never impersonate model inference."""
    truncation = padding = None

    def encode(self, text, add_special_tokens=True):
        return SimpleNamespace(ids=list(range(129)), offsets=[(0, 0)] * 129,
                               special_tokens_mask=[1] * 129, overflowing=[])


def test_available_model_budget_fallback_is_reported_without_results(tmp_path):
    ranker = object.__new__(OfflineSemanticRanker)
    ranker.tokenizer = OverBudgetTokenizer()
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Fallback fixture')
    app.add_text(workspace, 'Document', 'A source about railway history.')
    messages = []
    assert app.search(workspace, 'botanical flowers', mode='semantic', on_status=messages.append) == ()
    assert len(messages) == 1
    assert 'Keyword search used.' in messages[0]
    assert 'Shorten the query' in messages[0]
    # No status is stored on the shared application or leaked to a later query.
    later = []
    assert app.search(workspace, 'railway', on_status=later.append)[0].verified
    assert later == []
