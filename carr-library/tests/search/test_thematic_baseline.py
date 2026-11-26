from dataclasses import replace
from hashlib import sha256
import pytest
from carr_library.search.thematic import Passage, search


def passage(key, text, work=None, workspace='history', start=0, end=None):
    return Passage(workspace, work or key, 'edition-'+key, 'revision-'+key,
                   key, 3, start, len(text) if end is None else end,
                   text, sha256(text.encode()).hexdigest())


def test_gold_and_alias():
    pages = [passage('a', 'Gertsen discussed political exile.'),
             passage('b', 'Herzen wrote about exile in Europe.'),
             passage('c', 'Herzen studied botany.'),
             passage('d', 'An unrelated account of exile.')]
    hits = search('history', pages, 'Herzen exile')
    assert {h['passage_id'] for h in hits} == {'a', 'b'}
    assert any('Gertsen' in h['matched_terms'] for h in hits)
    assert search('history', pages, 'quantum mechanics') == []
    # Synthetic recall@2=2/2, reciprocal rank=1 (not a held-out benchmark).


def test_exact_unicode_span_and_phrase():
    text = 'Передмова.\nМіст через річку.\nAfterword.'
    p = passage('u', text, start=text.index('М'), end=text.index('\nAfter'))
    hits = search('history', [p], '"Міст через"')
    assert len(hits) == 1
    h = hits[0]
    assert h['raw_quote'] == text[h['start']:h['end']] == 'Міст через річку.'
    assert h['revision_id'] == p.revision_id and h['page_sha256'] == p.page_sha256
    assert search('history', [p], 'Afterword') == []


def test_scope_rejects_foreign_and_drift():
    p = passage('a', 'railway bridge')
    foreign = passage('b', 'railway bridge', workspace='private')
    with pytest.raises(ValueError, match='foreign'):
        search('history', [p, foreign], 'railway')
    with pytest.raises(ValueError, match='unverified'):
        search('history', [replace(p, raw_page='modified')], 'railway')
    with pytest.raises(ValueError, match='duplicate'):
        search('history', [p, p], 'railway')
    assert search('history', [p], 'railway') == search('history', [p], 'railway')


def test_work_diversity_stable():
    a = passage('a', 'railway railway railway', work='same')
    b = passage('b', 'railway railway', work='same')
    c = passage('c', 'railway history', work='other')
    hits = search('history', [a, b, c], 'railway', limit=2)
    assert {h['work_id'] for h in hits} == {'same', 'other'}
    assert hits == search('history', [c, b, a], 'railway', limit=2)


def test_empty_and_punctuation():
    p = passage('a', 'O’Neil and railway')
    assert search('history', [p], '') == []
    assert search('history', [p], '***') == []
    assert search('history', [p], 'O’Neil')[0]['raw_quote'] == p.raw_page
    assert search('history', [p], 'railway OR "absent"') == []
    with pytest.raises(ValueError):
        search('', [p], 'railway')
