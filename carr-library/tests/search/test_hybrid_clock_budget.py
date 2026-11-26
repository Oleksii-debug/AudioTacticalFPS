"""A coarse clock must not enlarge the budget delegated by hybrid search."""
from hashlib import sha256

from carr_library.search import thematic


def test_hybrid_handoff_preserves_budget_and_exact_source(monkeypatch):
    raw = 'Railway archives preserve exact evidence.'
    passage = thematic.Passage('workspace', 'work', 'edition', 'revision', 'page',
        1, 0, len(raw), raw, sha256(raw.encode()).hexdigest())
    # This valid tick reproduces a rounded remainder above the strict 60s limit.
    monkeypatch.setattr(thematic, 'monotonic', lambda: 123.456789)
    hit, = thematic.search_hybrid('workspace', (passage,), 'railway', timeout=60,
        semantic_ranker=lambda query, candidates: [key for key, _ in candidates])
    assert hit['raw_quote'] == raw
    assert hit['edition_id'] == 'edition'
    assert hit['revision_id'] == 'revision'
