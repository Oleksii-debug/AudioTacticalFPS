"""Actual optional model through the Library service and verified copy boundary."""
import json
import os
from pathlib import Path
import socket

import pytest

from carr_library.application import Library
from carr_library.search.offline_runtime import OfflineSemantic


@pytest.fixture(scope='module')
def semantic():
    directory = os.environ.get('CARR07_MODEL_DIR')
    manifest = os.environ.get('CARR07_MODEL_MANIFEST')
    if not directory or not manifest:
        pytest.skip('NOT_RUN: reviewed local model bundle not supplied')
    def forbidden(*args, **kwargs):
        pytest.fail('model-backed application attempted a network connection')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', forbidden)
        patch.setattr(socket.socket, 'connect_ex', forbidden)
        patch.setattr(socket, 'create_connection', forbidden)
        patch.setenv('HF_HUB_OFFLINE', '1')
        patch.setenv('TRANSFORMERS_OFFLINE', '1')
        yield OfflineSemantic(directory, manifest)


def test_multilingual_query_returns_exact_scoped_original_and_fresh_copy(tmp_path, semantic):
    library = Library(tmp_path / 'library', semantic=semantic)
    raw = 'The council set a ceiling on the price of bread.'
    for workspace in ('a', 'b'):
        library.create_workspace(workspace.upper(), workspace=workspace)
        library.create_topic(workspace, 'Research', topic='topic')
        imported = library.import_text(workspace, raw, title='Same title', authors=(workspace,),
                                       work_id='w', edition_id='e')
        assert imported['state'] == 'complete'
    assert library.search('a', 'обмеження ціни хліба', retrieval_mode='literal') == ()
    hit, = library.search('a', 'обмеження ціни хліба', retrieval_mode='thematic')
    assert hit.workspace == 'a' and hit.author == 'a'
    assert hit.quote == raw and hit.page == 'Segment 1 (unpaginated)'
    services = library.bind_scope('a', 'topic')
    assert services.verified_copy('a', hit.ref, hit.revision, 'quote') == raw
    saved = services.save_hit('a', hit.ref, hit.revision, hit.quote, hit.citation)
    with services.evidence_tx() as store:
        row, = store.list_saved('topic')
        assert row.evidence_id == saved and row.anchor.quote == raw
    assert 'The council set a ceiling' in library.export_topic('a', 'topic')
    assert library.search('b', 'обмеження ціни хліба', retrieval_mode='thematic')[0].author == 'b'
    # A changed retained original must never silently become a model result.
    source, = library.originals.iterdir()
    source.write_bytes(source.read_bytes() + b' changed')
    with pytest.raises(ValueError):
        library.search('a', 'обмеження ціни хліба', retrieval_mode='thematic')
    with pytest.raises(ValueError):
        services.verified_copy('a', hit.ref, hit.revision, 'quote')


def test_explicit_unapproved_model_manifest_fails_without_lexical_fallback(tmp_path, semantic):
    source = Path(os.environ['CARR07_MODEL_MANIFEST'])
    changed = json.loads(source.read_text())
    changed['hashes']['config.json'] = '0'*64
    manifest = tmp_path / 'unapproved.json'
    manifest.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='unapproved'):
        OfflineSemantic(semantic.directory, manifest)


def test_unrelated_semantic_candidate_is_filtered(semantic):
    assert semantic.rank('volcanic geology on Jupiter', [
        (('a','e','r','p'), 'The council set a ceiling on the price of bread.')]) == []


@pytest.mark.parametrize('limit', [0, 101, True])
def test_empty_semantic_library_rejects_invalid_result_budget(tmp_path, semantic, limit):
    library = Library(tmp_path / 'library', semantic=semantic)
    library.create_workspace('Empty', workspace='empty')
    with pytest.raises(ValueError, match='limit'):
        library.search('empty', 'railway', retrieval_mode='thematic', limit=limit)
