"""Search-only normalization must not normalize evidence or conflate marks."""
from hashlib import sha256
from unicodedata import normalize

import pytest
from carr_library.search.thematic import Passage, search


def page(key, raw, start=0, end=None):
    return Passage('A', 'w-' + key, 'e-' + key, 'r-' + key, key, 2,
                   start, len(raw) if end is None else end, raw,
                   sha256(raw.encode('utf-8')).hexdigest())


@pytest.mark.parametrize('word', ['Café', 'Україна', 'ľudová', 'Ελλάδα'])
@pytest.mark.parametrize('form', ['NFC', 'NFD'])
def test_canonical_equivalence_keeps_raw_offsets(word, form):
    source_word = normalize(form, word)
    raw = 'Preface.\n' + source_word + ' passage.\nEnd.'
    start = raw.index('\n') + 1
    end = raw.index('\nEnd.')
    p = page('one', raw, start, end)
    other = normalize('NFD' if form == 'NFC' else 'NFC', word)
    h, = search('A', [p], '"' + other + ' passage"')
    assert h['raw_quote'] == raw[start:end]
    assert (h['start'], h['end'], h['page_sha256']) == (start, end, p.page_sha256)
    assert h['revision_id'] == p.revision_id


def test_combining_marks_not_discarded():
    # Distinct Devanagari words; a vowel mark must not disappear during indexing.
    pages = [page('right', 'का'), page('wrong', 'कि')]
    hits = search('A', pages, 'का')
    assert [h['passage_id'] for h in hits] == ['right']
    assert [h['passage_id'] for h in search('A', pages, 'कि')] == ['wrong']


def test_accent_distinction_and_alias_punctuation():
    pages = [page('accent', 'Café'), page('plain', 'Cafe'), page('alias', 'Gertsen exile')]
    assert [h['passage_id'] for h in search('A', pages, 'Café')] == ['accent']
    assert [h['passage_id'] for h in search('A', pages, 'Herzen, exile ***')] == ['alias']


def test_no_match_from_normalization_outside_span():
    raw = 'Café\nother passage'
    p = page('one', raw, raw.index('\n') + 1)
    assert search('A', [p], 'Cafe\u0301') == []
