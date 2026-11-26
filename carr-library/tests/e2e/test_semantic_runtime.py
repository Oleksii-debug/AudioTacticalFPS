"""Actual pinned MiniLM inference, enabled with CARR_TEST_MODEL_DIR.

The model is prepared explicitly by the build operator; these tests do not
download files. No fixture score is presented as a held-out relevance study.
"""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from carr_library.application import LibraryApp
from carr_library.semantic import APPROVED_MANIFEST, OfflineSemanticRanker, verify_model_bundle
from carr_library.search.semantic_windows import source_windows, TOKEN_LIMIT


@pytest.fixture(scope='module')
def ranker():
    directory = os.environ.get('CARR_TEST_MODEL_DIR')
    if not directory:
        pytest.skip('Set CARR_TEST_MODEL_DIR to the verified offline MiniLM bundle.')
    with patch('socket.socket.connect', side_effect=AssertionError('Network is forbidden during inference')), \
            patch('socket.create_connection', side_effect=AssertionError('Network is forbidden during inference')):
        yield OfflineSemanticRanker(directory)


def test_actual_multilingual_ranker_and_complete_token_coverage(ranker):
    docs = ('Railway transport connected industrial cities.',
            'Залізниця з’єднувала промислові міста.',
            'Botanists studied flowers in a garden.',
            'У саду ботаніки досліджували квіти.')
    keys = tuple(('workspace', f'edition-{i}', 'revision', 'page') for i in range(4))
    ranked = ranker('How did trains link manufacturing towns?', tuple(zip(keys, docs)))
    assert set(ranked[:2]) == set(keys[:2])
    assert ranker.tokenizer.truncation is None and ranker.tokenizer.padding is None
    with pytest.raises(ValueError, match='complete token budget'):
        ranker('train', ((keys[0], 'word ' * 500),))


def test_actual_application_semantic_scope_and_original_quotes(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Research')
    foreign = app.create_workspace('Foreign')
    collection = app.create_collection(workspace, 'Selected')
    raw = 'Залізниця з’єднувала промислові міста.'
    imported = app.add_text(workspace, 'Railway', raw, collections=(collection,))
    app.add_text(workspace, 'Botany', 'Botanists studied flowers in a garden.')
    app.add_text(foreign, 'Excluded', 'Trains linked manufacturing towns in another private library.')
    hits = app.search(workspace, 'How did trains link manufacturing towns?', mode='semantic',
                      collection=collection)
    assert len(hits) == 1 and hits[0].quote == raw and hits[0].verified
    hit = hits[0]
    assert 'MiniLM' in hit.reason
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == raw
    with app.catalog_transaction(workspace) as catalog:
        catalog.remove(collection, imported['edition'])
    assert app.search(workspace, 'trains manufacturing towns', mode='semantic', collection=collection) == ()
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_long_source_tail_is_not_silently_truncated(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Long documents')
    raw = ('Botanists studied flowers in a garden. ' * 80
           + '\r\nThe distinct railway archive describes trains connecting industrial cities.')
    windows = source_windows(ranker.tokenizer, raw)
    assert len(windows) > 1
    assert windows[0][0] == 0 and windows[-1][1] == len(raw)
    assert ''.join(raw[start:end] for start, end in windows) == raw
    assert all(len(ranker.tokenizer.encode(raw[start:end]).ids) <= TOKEN_LIMIT
               for start, end in windows)
    app.add_text(workspace, 'Long source', raw)
    hit, = app.search(workspace, 'distinct railway archive', mode='semantic')
    assert 'distinct railway archive' in hit.quote
    assert hit.quote in raw and app.copy(workspace, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.context(workspace, hit.ref, hit.revision).pages[0].anchor.start > 2000


def test_overlong_query_has_explicit_lexical_fallback(ranker, tmp_path):
    app = LibraryApp(tmp_path / 'library', semantic_ranker=ranker)
    workspace = app.create_workspace('Limits')
    app.add_text(workspace, 'Source', 'x is an exact source symbol.')
    hit, = app.search(workspace, 'x ' * 150, mode='semantic')
    assert 'using lexical thematic search' in hit.reason
    assert hit.quote == 'x is an exact source symbol.'
    with pytest.raises(TimeoutError):
        app.search(workspace, 'source', mode='semantic', cancelled=lambda: True)


def test_manifest_cannot_approve_another_model(ranker):
    changed = deepcopy(APPROVED_MANIFEST)
    changed['hashes']['model_optimized.onnx'] = 'a' * 64
    with pytest.raises(ValueError, match='not the approved'):
        verify_model_bundle(ranker.directory, changed)


def test_linked_model_directory_is_rejected(ranker, tmp_path):
    link = tmp_path / 'alias'
    try:
        link.symlink_to(ranker.directory, target_is_directory=True)
    except OSError:
        pytest.skip('This host cannot create a directory symlink.')
    with pytest.raises(ValueError, match='unlinked'):
        verify_model_bundle(link)

