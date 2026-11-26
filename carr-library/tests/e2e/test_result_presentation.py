"""Actual database/source/copy/save checks for both desktop presentation modes."""
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.search.presentation import SHORT_CHARS, THEMATIC_CHARS, presentation_span


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Presentation acceptance')
    return app, workspace


def test_sentence_and_expanded_context_share_copy_save_and_more_offsets(library):
    app, workspace = library
    target = 'The historian described Straße Cafe\u0301 beside the Ukrainian city Київ.'
    page = ('Earlier context about the community. ' * 30 + '\r\n' + target +
            '\r\n' + 'Following context about this source. ' * 30)
    imported = app.add_text(workspace, 'Unicode original', page + '\fFOREIGN PAGE TEXT.',
                            authors=('Author One',), edition='Original')
    topic = app.create_topic(workspace, 'Evidence')
    short, = app.search(workspace, 'strasse café', result_mode='short')
    expanded, = app.search(workspace, 'strasse café', result_mode='thematic')
    assert short.quote == target
    assert len(short.quote) <= SHORT_CHARS
    assert short.quote in expanded.quote and len(expanded.quote) > len(short.quote)
    assert len(expanded.quote) <= THEMATIC_CHARS
    assert 'FOREIGN PAGE TEXT' not in expanded.quote
    assert 'Segment 1' in short.page and short.page == expanded.page
    assert short.revision == expanded.revision == imported['id']

    for hit in (short, expanded):
        context = app.context(workspace, hit.ref, hit.revision)
        anchor = context.pages[0].anchor
        assert context.pages[0].raw_text == page
        assert page[anchor.start:anchor.end] == hit.quote
        assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
        assert app.copy(workspace, hit.ref, hit.revision, 'citation') == hit.citation
        app.save_hit(workspace, topic, hit.ref, hit.revision)

    reopened = LibraryApp(app.root)
    assert {row.anchor.quote for row in reopened.evidence(workspace, topic)} == {
        short.quote, expanded.quote}
    exported = reopened.export_topic(workspace, topic, format='text')
    exported_quotes = {json.loads(line.removeprefix('Exact quotation: '))
                       for line in exported.splitlines() if line.startswith('Exact quotation: ')}
    assert exported_quotes == {short.quote, expanded.quote}
    assert app.assets.resolve(imported['asset']).read_bytes() == (
        page + '\fFOREIGN PAGE TEXT.').encode('utf-8')


@pytest.mark.parametrize('retrieval', ['thematic', 'semantic'])
def test_lexical_page_or_model_fallback_focuses_on_relevant_tail(library, retrieval):
    app, workspace = library
    prefix = 'Botanical reports discuss flowers and leaf shapes. ' * 160
    target = 'The distinct railway archive explains industrial transport history.'
    page = prefix + target + ' This adjacent sentence belongs to the same source.'
    app.add_text(workspace, 'Long original', page)
    for result_mode, bound in [('short', SHORT_CHARS), ('thematic', THEMATIC_CHARS)]:
        hit, = app.search(workspace, 'distinct railway archive', mode=retrieval,
                         result_mode=result_mode)
        assert 'distinct railway archive' in hit.quote
        assert hit.quote in page and len(hit.quote) <= bound
        assert app.context(workspace, hit.ref, hit.revision).pages[0].anchor.start > 6000
        assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote


@pytest.mark.parametrize('mode', ['short', 'thematic'])
@pytest.mark.parametrize('prefix', [
    'a ' * 32760, 'a ' * 32768, 'a \t\n' * 20000, 'a ' * 1000, '\u0457 ' * 1000,
], ids=['threshold-plus-tail', 'long-ascii', 'collapsed-whitespace',
        'short-ascii', 'unicode'])
def test_long_logical_source_focus_keeps_original_offsets(mode, prefix):
    passage = 'Railway\t  archives.'
    raw = 'Intro. ' + prefix + '\n\n' + passage + '\n\n' + 'z ' * 1000
    start = len('Intro. ')
    hit = dict(start=start, end=len(raw), raw_quote=raw[start:],
               matched_terms=('railway', 'archives'))
    a, b = presentation_span(raw, hit, 'railway archives', mode)
    assert 0 <= a <= raw.index(passage) < raw.index(passage) + len(passage) <= b <= len(raw)
    assert passage in raw[a:b]
    assert b - a <= (SHORT_CHARS if mode == 'short' else THEMATIC_CHARS)


@pytest.mark.parametrize('retrieval', ['thematic', 'semantic'])
@pytest.mark.parametrize('result_mode', ['short', 'thematic'])
def test_long_ascii_source_search_copy_and_context_use_exact_tail(library, retrieval, result_mode):
    app, workspace = library
    target = 'The railway\t  archives preserve the original transport history.'
    page = 'Unrelated botanical material.\r\n\t' * 2500 + target + '\n\nTrailing material.'
    assert page.isascii() and len(page) >= 65536
    imported = app.add_text(workspace, 'Long logical source', page + '\fEXCLUDED OTHER SEGMENT.')
    messages = []
    hit, = app.search(workspace, 'railway archives', mode=retrieval,
                     result_mode=result_mode, on_status=messages.append)
    assert hit.verified and hit.revision == imported['id']
    assert target in hit.quote and 'EXCLUDED OTHER SEGMENT' not in hit.quote
    assert len(hit.quote) <= (SHORT_CHARS if result_mode == 'short' else THEMATIC_CHARS)
    context = app.context(workspace, hit.ref, hit.revision)
    assert len(context.pages) == 1 and context.pages[0].raw_text == page
    anchor = context.pages[0].anchor
    assert anchor.start > 65536 and page[anchor.start:anchor.end] == hit.quote
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.copy(workspace, hit.ref, hit.revision, 'citation') == hit.citation
    if retrieval == 'semantic':
        assert messages == ['Local model unavailable; keyword search used.']
    else:
        assert messages == []


def test_exact_legacy_offsets_remain_available_to_existing_callers(library):
    app, workspace = library
    app.add_text(workspace, 'Source', 'A meaningful railway quotation in a sentence.')
    assert app.search(workspace, 'railway quotation')[0].quote == 'railway quotation'
    assert app.search(workspace, 'railway quotation', result_mode=None)[0].quote == 'railway quotation'
    assert app.search(workspace, 'railway quotation', result_mode='short')[0].quote == (
        'A meaningful railway quotation in a sentence.')


@pytest.mark.parametrize('query', ['railway-history', 'railway/history', 'railway.history',
                                  '"railway history"'])
def test_native_fts_phrase_punctuation_does_not_expand_short_quote_to_full_page(library, query):
    app, workspace = library
    target = 'The railway history is recorded here.'
    raw = 'Unrelated context. ' * 500 + target
    app.add_text(workspace, 'Long punctuated-query source', raw)
    short, = app.search(workspace, query, mode='thematic', result_mode='short')
    expanded, = app.search(workspace, query, mode='thematic', result_mode='thematic')
    assert short.quote == target
    assert target in expanded.quote and len(expanded.quote) <= THEMATIC_CHARS
    assert app.copy(workspace, short.ref, short.revision, 'quote') == target


@pytest.mark.parametrize('query', ['"railway history"', 'railway-history', 'railway/history'])
def test_native_phrase_focus_preserves_order_and_adjacent_token_positions(library, query):
    app, workspace = library
    wrong = 'History railway mentions are reversed here.'
    right = 'The railway ' + ('-' * 80) + ' history is the actually matching ordered phrase.'
    raw = wrong + ' Context. ' * 500 + right
    app.add_text(workspace, 'Phrase order source', raw)
    for mode in ('short', 'thematic'):
        hit, = app.search(workspace, query, mode='thematic', result_mode=mode)
        assert right in hit.quote and wrong not in hit.quote
        assert app.context(workspace, hit.ref, hit.revision).pages[0].anchor.start > 3000
        assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote


def test_native_repeated_token_phrase_requires_distinct_adjacent_occurrences(library):
    app, workspace = library
    wrong = 'A single railway word cannot satisfy the phrase.'
    right = 'The railway railway sequence is the matching repetition.'
    raw = wrong + ' Context. ' * 500 + right
    app.add_text(workspace, 'Repeated phrase source', raw)
    hit, = app.search(workspace, '"railway railway"', mode='thematic', result_mode='short')
    assert hit.quote == right and wrong not in hit.quote


def test_native_phrase_focus_maps_composed_index_back_to_decomposed_original(library):
    app, workspace = library
    wrong = 'History Café words are in the wrong order.'
    right = 'Cafe\u0301 ' + ('-' * 20) + ' history is the exact original source.'
    raw = wrong + ' Context. ' * 500 + right
    app.add_text(workspace, 'Unicode phrase source', raw)
    hit, = app.search(workspace, '"café history"', mode='thematic', result_mode='short')
    assert hit.quote == right and 'Cafe\u0301' in hit.quote
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == right


def test_repeated_matches_in_same_sentence_do_not_duplicate_presented_quote(library):
    app, workspace = library
    app.add_text(workspace, 'Source', 'Railway history records railway builders and railway stations.')
    assert len(app.search(workspace, 'railway')) == 3
    hit, = app.search(workspace, 'railway', result_mode='short')
    assert hit.quote == 'Railway history records railway builders and railway stations.'


def test_all_words_on_one_page_never_stitches_distant_passages(library):
    app, workspace = library
    first = 'The railway records begin here.'
    raw = first + ' Background context. ' * 250 + 'The private archive ends here.'
    app.add_text(workspace, 'Source', raw)
    hit, = app.search(workspace, 'railway archive', mode='terms', result_mode='short')
    assert hit.quote == first and hit.quote in raw
    assert 'archive' in app.context(workspace, hit.ref, hit.revision).pages[0].raw_text
    assert 'archive' not in hit.quote


def test_expanded_selection_keeps_collection_authorization(library):
    app, workspace = library
    collection = app.create_collection(workspace, 'Restricted selection')
    imported = app.add_text(workspace, 'Source', 'This source has railway evidence.', collections=(collection,))
    hit, = app.search(workspace, 'railway', collection=collection, result_mode='thematic')
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, imported['edition'])
    with pytest.raises(ValueError, match='left the selected collection'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_invalid_presentation_and_cancellation_do_not_return_results(library):
    app, workspace = library
    app.add_text(workspace, 'Source', 'Railway evidence.')
    with pytest.raises(ValueError, match='Short quotations'):
        app.search(workspace, 'railway', result_mode='invented')
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', result_mode='short', cancelled=lambda: True)


def test_cancellation_during_result_materialization_is_honored(library, monkeypatch):
    app, workspace = library
    app.add_text(workspace, 'Source', 'Railway evidence.')
    stopped = False
    original = app._loaders

    def loaders(*args, **kwargs):
        nonlocal stopped
        value = original(*args, **kwargs)
        stopped = True
        return value

    monkeypatch.setattr(app, '_loaders', loaders)
    with pytest.raises(TimeoutError):
        app.search(workspace, 'railway', result_mode='short', cancelled=lambda: stopped)


def test_semantic_window_is_not_truncated_just_to_shorten_the_display():
    # Synthetic ranked window boundary; this does not claim model inference.
    raw = 'Opening material. ' * 40 + 'Source window content. ' * 40 + 'Closing material. ' * 40
    start = len('Opening material. ' * 40)
    end = start + len('Source window content. ' * 40)
    hit = {'start': start, 'end': end, 'raw_quote': raw[start:end]}
    for mode in ('short', 'thematic'):
        a, b = presentation_span(raw, hit, 'a semantic paraphrase', mode)
        assert a <= start < end <= b
        assert raw[start:end] in raw[a:b]


def test_excerpt_edges_do_not_split_combining_sequences_or_joiner():
    raw = ('e\u0301 ' * 250) + 'railway' + (' 👩\u200d🔬 ' * 250)
    start = raw.index('railway')
    hit = {'start': start, 'end': start + 7, 'raw_quote': 'railway',
           'match_spans': ((start, start + 7),)}
    for mode in ('short', 'thematic'):
        a, b = presentation_span(raw, hit, 'railway', mode)
        assert raw[a:b].count('railway') == 1
        assert raw[a] not in ('\u0301', '\u200d')
        assert not a or raw[a - 1] != '\u200d'
        assert raw[b - 1] != '\u200d'
        assert b == len(raw) or raw[b] not in ('\u0301', '\u200d')
