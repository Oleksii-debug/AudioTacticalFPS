"""Real SQLite search handoffs preserve a budget on a coarse monotonic clock.

The deterministic ranker/tokenizer below exercise orchestration only. No
native model is loaded, and these tests make no relevance-quality claim.
"""
from types import SimpleNamespace

import pytest

import carr_library.application as application
import carr_library.semantic as semantic
from carr_library.search import semantic_windows, thematic


class CharacterTokenizer:
    truncation = None
    padding = None

    def encode(self, text, add_special_tokens=True):
        return SimpleNamespace(
            ids=list(range(len(text) + 2)),
            offsets=[(0, 0)] + [(i, i + 1) for i in range(len(text))] + [(0, 0)],
            special_tokens_mask=[1] + [0] * len(text) + [1],
            overflowing=[],
        )


@pytest.fixture
def research(tmp_path, monkeypatch):
    ranker = object.__new__(semantic.OfflineSemanticRanker)
    ranker.tokenizer = CharacterTokenizer()
    calls = []

    def rank(query, candidates, *, timeout, cancelled, cache):
        calls.append(timeout)
        assert 0 < timeout <= 60
        assert cache is app.semantic_cache
        return [identity for identity, _ in candidates]

    ranker.rank = rank
    app = application.LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Nested clock boundary')
    app.add_text(workspace, 'Source', 'Railway history and archives.')
    # Keep the public operation budget exact to isolate the nested handoffs.
    # Their unchanged valid clock tick makes
    # (61.001 + 60.0) - 61.001 round to 60.00000000000001.
    monkeypatch.setattr(application, 'monotonic', lambda: 100.0)
    for module in (semantic, semantic_windows, thematic):
        monkeypatch.setattr(module, 'monotonic', lambda: 61.001)
    return app, workspace, calls


def test_semantic_handoffs_keep_maximum_budget_and_source_exact_copy(research):
    app, workspace, calls = research
    hit, = app.search(workspace, 'history', mode='semantic', timeout=60.0)
    assert calls and all(0 < timeout <= 60.0 for timeout in calls)
    assert hit.verified
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert hit.quote in 'Railway history and archives.' and hit.quote


def test_semantic_fallback_keeps_the_same_valid_budget(research):
    app, workspace, calls = research
    messages = []
    hit, = app.search(workspace, 'history ' * 20, mode='semantic', timeout=60.0,
                      on_status=messages.append)
    assert not calls, 'An oversized query must use the lexical fallback'
    assert messages and 'Keyword search used' in messages[0]
    assert hit.verified and 'history' in hit.quote
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote


def test_catalog_to_thematic_handoff_does_not_expand_budget(research):
    app, workspace, calls = research
    with app.read(workspace) as db:
        hit, = thematic.search_catalog(db, workspace, 'history', timeout=60.0)
    assert hit['workspace_id'] == workspace and 'history' in hit['raw_quote']
    assert not calls
