"""Natural punctuation must not silently remove a thematic query concept."""
from hashlib import sha256

import pytest

from carr_library.search.thematic import Passage, search, search_hybrid


def passage(identity, raw):
    return Passage('A', identity, 'edition-' + identity, 'revision-' + identity,
                   identity, 1, 0, len(raw), raw, sha256(raw.encode()).hexdigest())


@pytest.mark.parametrize('query,matching,unrelated', [
    ('10:30', 'The departure is at 10:30.', 'The departure is at 10:45.'),
    ('railway: bridge', 'A railway bridge.', 'A stone bridge.'),
    ('Україна: history', 'Україна history.', 'Slovakia history.'),
    ('ratio 1:2', 'The ratio is 1:2.', 'The ratio is 1:3.'),
])
def test_punctuation_preserves_every_concept(query, matching, unrelated):
    pages = [passage('match', matching), passage('other', unrelated)]
    hits = search('A', pages, query)
    assert [hit['passage_id'] for hit in hits] == ['match']
    assert hits[0]['raw_quote'] == matching
    assert hits[0]['start'] == 0 and hits[0]['end'] == len(matching)
    # An unavailable semantic result must retain the same lexical baseline.
    assert search_hybrid('A', pages, query, semantic_ranker=lambda *_: []) == hits


def test_quoted_colon_phrase_is_not_split_into_additional_concepts():
    pages = [passage('match', 'Time 10:30 at the railway station.'),
             passage('other', 'Time 10:45 at the railway station.')]
    hit, = search('A', pages, '"10:30 at the railway"')
    assert hit['passage_id'] == 'match'
    assert hit['matched_terms'] == ('10:30 at the railway',)


@pytest.mark.parametrize('query', ['content:railway', 'filename:railway', 'CONTENT:railway'])
def test_existing_supported_prefixes_keep_their_upstream_behavior(query):
    hit, = search('A', [passage('match', 'A railway bridge.')], query)
    assert hit['matched_terms'] == ('railway',)


def test_unsupported_prefix_cannot_silently_broaden_the_query():
    # Unknown colon syntax is searchable source text, never an ignored filter.
    pages = [passage('match', 'The note says title:railway then bridge.'),
             passage('other', 'A railway bridge.')]
    hit, = search('A', pages, 'title:railway bridge')
    assert hit['passage_id'] == 'match'


def test_colon_terms_remain_quoted_fts_literals():
    pages = [passage('match', 'A railway bridge.'),
             passage('other', 'An unrelated garden.')]
    assert search('A', pages, 'railway:bridge OR "absent"') == []
