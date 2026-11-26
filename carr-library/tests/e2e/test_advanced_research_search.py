"""Academic queries keep the live application's source and scope guarantees."""
import json

import pytest

from carr_library.application import LibraryApp


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'research')
    return app, app.create_workspace('Academic research')


def test_boolean_results_can_be_copied_saved_and_exported_without_changing_source(library):
    app, workspace = library
    source = 'Historical memory shaped national identity in Київ. This is the original account.'
    imported = app.add_text(workspace, 'A history source', source, authors=('First Author',))
    app.add_text(workspace, 'Excluded source', 'Historical memory shaped national identity during war.')
    other = app.create_workspace('A separate private library')
    app.add_text(other, 'Foreign source', source)
    query = '"historical memory" AND ("national identity" OR sovereignty) AND NOT war'
    hit, = app.search(workspace, query, mode='advanced', result_mode='short')
    assert hit.workspace == workspace and hit.verified
    assert hit.revision == imported['id']
    assert hit.quote in source and 'Historical memory' in hit.quote
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    context = app.context(workspace, hit.ref, hit.revision).pages[0]
    assert context.raw_text == source
    assert source[context.anchor.start:context.anchor.end] == hit.quote
    topic = app.create_topic(workspace, 'Research question')
    app.save_hit(workspace, topic, hit.ref, hit.revision)
    reopened = LibraryApp(app.root)
    evidence, = reopened.evidence(workspace, topic)
    assert evidence.anchor.quote == hit.quote
    exported = reopened.export_topic(workspace, topic, format='text')
    quotes = [json.loads(line.removeprefix('Exact quotation: '))
              for line in exported.splitlines() if line.startswith('Exact quotation: ')]
    assert quotes == [hit.quote]


def test_near_conditions_share_one_segment_and_accept_either_order(library):
    app, workspace = library
    app.add_text(workspace, 'Close', 'memory one two identity')
    app.add_text(workspace, 'Reverse', 'identity beside memory')
    app.add_text(workspace, 'Distant', 'memory one two three identity')
    app.add_text(workspace, 'Split', 'memory\fidentity')
    hits = app.search(workspace, 'memory NEAR/2 identity', mode='advanced', result_mode='short')
    assert {hit.title for hit in hits} == {'Close', 'Reverse'}
    for hit in hits:
        assert 'memory' in hit.quote and 'identity' in hit.quote
        assert '\f' not in hit.quote
        assert app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote


def test_existing_modes_keep_operator_words_literal(library):
    app, workspace = library
    app.add_text(workspace, 'Literal text', 'A source actually says war OR peace.')
    app.add_text(workspace, 'War', 'This source concerns war.')
    app.add_text(workspace, 'Peace', 'This source concerns peace.')
    assert {hit.title for hit in app.search(workspace, 'war OR peace')} == {'Literal text'}
    assert {hit.title for hit in app.search(workspace, 'war OR peace', mode='advanced')} == {
        'Literal text', 'War', 'Peace'}
    assert {hit.title for hit in app.search(workspace, 'war OR peace', mode='terms')} == {'Literal text'}


def test_unicode_phrase_matches_keep_original_clusters_and_bytes(library):
    app, workspace = library
    raw = 'Original Cafe\u0301 records describe Straße and Київ.'
    imported = app.add_text(workspace, 'Unicode', raw)
    hit, = app.search(workspace, '"café" AND strasse AND Київ', mode='advanced', result_mode='short')
    assert hit.quote == raw
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == raw
    assert app.assets.resolve(imported['asset']).read_bytes() == raw.encode('utf-8')


def test_advanced_search_reuses_filters_and_live_collection_authority(library):
    app, workspace = library
    collection = app.create_collection(workspace, 'Chosen sources')
    imported = app.add_text(workspace, 'Chosen source', 'memory and identity are discussed.',
                            authors=('Olena Researcher',), language='uk', edition='First',
                            collections=(collection,))
    app.add_text(workspace, 'Outside collection', 'memory and identity are discussed.',
                 authors=('Olena Researcher',), language='uk', edition='First')
    hit, = app.search(workspace, 'memory AND identity', mode='advanced', collection=collection,
                     title='Chosen', author='Olena', language='uk', edition='First')
    assert hit.title == 'Chosen source'
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, imported['edition'])
    with pytest.raises(ValueError, match='left the selected collection'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


@pytest.mark.parametrize('query', ['memory OR', '(memory AND identity', 'NOT memory'])
def test_invalid_advanced_query_cannot_publish_or_evict_selections(library, query):
    app, workspace = library
    app.add_text(workspace, 'Source', 'memory and identity')
    old, = app.search(workspace, 'memory')
    selections = dict(app._selections)
    with pytest.raises(ValueError):
        app.search(workspace, query, mode='advanced')
    assert app._selections == selections
    assert app.copy(workspace, old.ref, old.revision, 'quote') == 'memory'


def test_cancellation_cannot_publish_partial_advanced_selections(library):
    app, workspace = library
    app.add_text(workspace, 'Source', 'memory and identity')
    old, = app.search(workspace, 'memory')
    selections = dict(app._selections)
    with pytest.raises((TimeoutError, InterruptedError)):
        app.search(workspace, 'memory AND identity', mode='advanced', cancelled=lambda: True)
    assert app._selections == selections
    assert app.copy(workspace, old.ref, old.revision, 'quote') == 'memory'


def test_advanced_search_checks_retained_original_before_publishing_hit(library):
    app, workspace = library
    imported = app.add_text(workspace, 'Source', 'memory and identity')
    app.assets.resolve(imported['asset']).write_bytes(b'Changed original source.')
    with pytest.raises(ValueError):
        app.search(workspace, 'memory AND identity', mode='advanced')
    assert not app._selections
