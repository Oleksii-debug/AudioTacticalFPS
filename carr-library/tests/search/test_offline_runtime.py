"""Pinned bundle boundaries plus actual opt-in CPU/catalog integration."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import socket

import pytest

from carr_library.catalog.membership import Catalog
from carr_library.citations.text_attestation import CONFIG
from carr_library.search.offline_runtime import HASHES, REPO, REVISION, OfflineRuntime, verify_model_bundle
from carr_library.storage.revisions import Page, Storage


def manifest(path):
    path.write_text(json.dumps(dict(repo=REPO, revision=REVISION, license='Apache-2.0', hashes=HASHES)))
    return path


def test_unapproved_small_file_rehash_is_rejected(tmp_path):
    bundle = tmp_path / 'model'
    bundle.mkdir()
    approved = manifest(tmp_path / 'manifest.json')
    data = json.loads(approved.read_text())
    data['hashes']['config.json'] = 'a'*64
    approved.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='unapproved'):
        verify_model_bundle(bundle, approved)


def test_symlinked_ancestor_rejected(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'model').mkdir()
    linked = tmp_path / 'linked'
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match='linked'):
        verify_model_bundle(linked / 'model', manifest(tmp_path / 'manifest.json'))


def test_hardlinked_manifest_rejected(tmp_path):
    bundle = tmp_path / 'model'
    bundle.mkdir()
    approved = manifest(tmp_path / 'manifest.json')
    os.link(approved, tmp_path / 'other.json')
    with pytest.raises(ValueError, match='one link'):
        verify_model_bundle(bundle, approved)


@pytest.fixture(scope='module')
def runtime():
    directory, manifest_path = os.environ.get('CARR07_MODEL_DIR'), os.environ.get('CARR07_MODEL_MANIFEST')
    if not directory or not manifest_path:
        pytest.skip('NOT_RUN: reviewed local model bundle not supplied')
    # No network permitted by Python during model initialization or inference.
    def blocked(*args, **kwargs):
        pytest.fail('offline runtime attempted a network connection')
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', blocked)
        patch.setattr(socket.socket, 'connect_ex', blocked)
        patch.setattr(socket, 'create_connection', blocked)
        patch.setenv('HF_HUB_OFFLINE', '1')
        patch.setenv('TRANSFORMERS_OFFLINE', '1')
        yield OfflineRuntime(directory, manifest_path)


def test_actual_model_uses_untruncated_window_tokenizer(runtime):
    assert runtime.tokenizer.truncation is None and runtime.tokenizer.padding is None
    key = ('a', 'edition', 'revision', 'passage')
    assert runtime.rank('обмеження ціни хліба', [(key, 'The council set a ceiling on the price of bread.')]) == [key]
    with pytest.raises(ValueError, match='token budget'):
        runtime.rank('word ' * 300, [(key, 'short text')])


def test_actual_rank_rejects_mixed_workspace(runtime):
    with pytest.raises(ValueError, match='mixed-scope'):
        runtime.rank('bread', [(('a','e','r','p'),'bread'), (('b','e','r','p'),'bread')])


def test_actual_catalog_semantics_preserve_scope_and_exact_source(tmp_path, runtime):
    raw = 'Preface. The council set a ceiling on the price of bread. End.'
    asset = sha256(raw.encode()).hexdigest()
    with closing(Storage(tmp_path / 'catalog.sqlite', schema_version=2)) as store:
        with store.transaction():
            for workspace in ('a', 'b'):
                store.db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
                catalog = Catalog(store.db, workspace)
                catalog.add_work('w', 'Same title', [workspace])
                catalog.add_edition('e', 'w', dict(sha256=asset,mime='text/plain',size=len(raw.encode())))
                catalog.create_collection('c', 'Collection')
                catalog.assign('c', 'e')
                store.stage_revision(workspace,'e',workspace+'1',asset,CONFIG,[Page(raw)])
        with store.transaction():
            hits = runtime.search_catalog(store.db,'a','обмеження ціни хліба',collection='c')
            assert len(hits) == 1
            hit = hits[0]
            assert hit['workspace_id'] == 'a' and hit['revision_id'] == 'a1'
            assert hit['page_id'] == int(hit['parent_passage_id'])
            assert hit['raw_quote'] == raw[hit['start']:hit['end']]
            assert hit['asset_sha256'] == asset and hit['locator_kind'] == 'logical_segment'
            Catalog(store.db,'a').remove('c','e')
            assert runtime.search_catalog(store.db,'a','bread',collection='c') == []
            assert runtime.search_catalog(store.db,'b','bread',collection='c')[0]['revision_id'] == 'b1'
        with pytest.raises(ValueError, match='transaction'):
            runtime.search_catalog(store.db,'a','bread')


def test_real_bundle_has_exact_approved_small_config_hashes(runtime):
    # Test changed state detection without altering any shared model files.
    old = runtime._signatures
    runtime._signatures = dict(old, **{'config.json': (0,)})
    try:
        with pytest.raises(ValueError, match='changed'):
            runtime.rank('bread', [(('a','e','r','p'),'bread')])
    finally:
        runtime._signatures = old
