"""Independent application-boundary checks with actual retained source bytes."""
import json

import pytest

from carr_library.application import Library
from carr_library.catalog.membership import Catalog
from carr_library.storage.attestations import MetadataSnapshots


RAW = 'Before: Українська цитата — Straße.\nAfter.'


@pytest.fixture
def library(tmp_path):
    app = Library(tmp_path / 'private')
    for workspace in ('a', 'b'):
        app.create_workspace(workspace.upper(), workspace=workspace)
        app.create_topic(workspace, 'Research', topic='topic')
        with app.authorized_transaction(workspace) as db:
            Catalog(db, workspace).create_collection('c', 'Collection')
        imported = app.import_text(workspace, RAW, title='Same title', authors=(workspace,),
                                   work_id='w', edition_id='e', collections=('c',))
        assert imported['state'] == 'complete'
    return app


def test_foreign_source_reference_and_collection_removal_fail_closed(library):
    a, = library.search('a', 'Straße', collection='c')
    b, = library.search('b', 'Straße', collection='c')
    assert a.quote == b.quote == 'Straße'
    assert a.author == 'a' and b.author == 'b'
    binding = library.bind_scope('a', 'topic')
    assert binding.verified_copy('a', a.ref, a.revision, 'quote') == a.quote
    with pytest.raises(ValueError):
        binding.verified_copy('a', b.ref, b.revision, 'quote')
    with library.authorized_transaction('a') as db:
        Catalog(db, 'a').remove('c', 'e')
    assert library.search('a', 'Straße', collection='c') == ()
    with pytest.raises(ValueError, match='collection'):
        binding.verified_copy('a', a.ref, a.revision, 'quote')
    assert library.search('b', 'Straße', collection='c') == (b,)


def test_original_bytes_changed_after_search_block_copy(library):
    hit, = library.search('a', 'Straße')
    binding = library.bind_scope('a', 'topic')
    # Fault injection represents changed source bytes, never an application edit.
    path, = library.originals.iterdir()
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        binding.verified_copy('a', hit.ref, hit.revision, 'quote')


def test_metadata_revoked_after_search_blocks_copy_and_evidence_save(library):
    hit, = library.search('a', 'Straße')
    binding = library.bind_scope('a', 'topic')
    with library.authorized_transaction('a') as db:
        context = library.load_context(db, 'a', hit.ref, hit.revision)
        MetadataSnapshots(db, 'a').revoke(context.page(0).anchor.identity)
    with pytest.raises(ValueError, match='metadata'):
        binding.verified_copy('a', hit.ref, hit.revision, 'citation')
    with pytest.raises(ValueError, match='metadata'):
        binding.save_hit('a', hit.ref, hit.revision, hit.quote, hit.citation)


def test_archived_scope_blocks_preexisting_binding(library):
    hit, = library.search('a', 'Straße')
    binding = library.bind_scope('a', 'topic')
    library.archive_workspace('a')
    with pytest.raises(ValueError, match='archived workspace'):
        binding.verified_copy('a', hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='archived workspace'):
        binding.search('a', 'Straße', 'literal', 'short', 50)


@pytest.mark.parametrize('changed', [
    lambda values: [*values[:2], -1, values[3], values[4]],
    lambda values: [*values[:3], values[3] + 10000, values[4]],
    lambda values: [*values[:2], True, values[3], values[4]],
])
def test_malformed_offsets_never_reach_copy(library, changed):
    hit, = library.search('a', 'Straße')
    reference = json.dumps(changed(json.loads(hit.ref)), ensure_ascii=False,
                           sort_keys=True, separators=(',', ':'))
    with pytest.raises(ValueError):
        library.bind_scope('a', 'topic').verified_copy('a', reference, hit.revision, 'quote')
