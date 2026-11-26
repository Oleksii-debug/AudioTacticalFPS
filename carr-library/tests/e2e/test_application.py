"""Application acceptance with real local TXT bytes and shared production services.

All sources are generated synthetic fixtures. No fake source/metadata authority,
mock database, native GUI or OS clipboard is used or claimed by these tests.
"""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import Library
from carr_library.citations.anchors import Identity
from carr_library.storage.attestations import MetadataSnapshots


@pytest.fixture
def library(tmp_path):
    app = Library(tmp_path / 'private-library')
    app.create_workspace('First research', workspace='alpha')
    app.create_workspace('Other research', workspace='beta')
    app.create_topic('alpha', 'Research', topic='topic')
    app.create_topic('beta', 'Research', topic='topic')
    return app


def import_source(app, workspace='alpha', text='Історія   Café — exact source.\r\n', **options):
    return app.import_text(workspace, text, title='Same title', authors=('Author ' + workspace,),
                           language='uk', edition='first', **options)


def test_import_search_more_copy_save_reopen_export(library, tmp_path):
    raw = 'Історія   Café — exact source.\r\n'
    original = b'\xef\xbb\xbf' + (raw + '\fSecond segment with Napoleon.\r\n').encode('utf-8')
    selected = tmp_path / 'selected.txt'
    selected.write_bytes(original)
    state = library.import_file('alpha', selected, title='Same title', authors=('Author alpha',),
                                edition='first', language='uk')
    assert state['state'] == 'complete'
    assert state['citation_status'] == 'byte_verified'
    assert (library.originals / (sha256(original).hexdigest() + '.txt')).read_bytes() == original
    import_source(library, 'beta', text=raw)
    scopes = library.bind_scope('alpha', 'topic')
    hits = scopes.search('alpha', 'історія café', 'literal', 'short', 20)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.quote == 'Історія   Café'
    assert hit.author == 'Author alpha'
    assert hit.page == 'Segment 1 (unpaginated)'
    context = scopes.load_context('alpha', hit.ref, hit.revision)
    assert context.page(0).raw_text == raw
    assert scopes.verified_copy('alpha', hit.ref, hit.revision, 'quote') == hit.quote
    assert scopes.verified_copy('alpha', hit.ref, hit.revision, 'citation') == hit.citation
    saved = scopes.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    with scopes.evidence_tx() as evidence:
        evidence.edit_note('topic', saved, 'My separate note')
        rows = evidence.list_saved('topic')
        assert rows[0].anchor.quote == hit.quote
        assert rows[0].note == 'My separate note'
    reopened = Library(library.root)
    for format in ('text', 'markdown', 'ris'):
        exported = reopened.export_topic('alpha', 'topic', format=format)
        assert 'Same title' in exported
        assert 'Author alpha' in exported
        assert 'Author beta' not in exported
        if format != 'ris':
            assert hit.quote in exported
            assert 'My separate note' in exported
    assert 'Same title' not in reopened.export_topic('beta', 'topic')
    with reopened.bind_scope('beta', 'topic').evidence_tx() as evidence:
        assert evidence.list_saved('topic') == ()
    assert reopened.search('beta', 'історія café')[0].author == 'Author beta'
    checked = reopened.check()
    assert checked['schema_version'] == 3 and checked['integrity'] == 'ok'
    assert checked['originals_verified'] == 2


def test_identical_sources_remain_separate_and_wrong_scope_is_rejected(library):
    a = import_source(library)
    b = import_source(library, 'beta')
    assert a['asset'] == b['asset']
    hit = library.search('alpha', 'Café')[0]
    alpha = library.bind_scope('alpha', 'topic')
    beta = library.bind_scope('beta', 'topic')
    with pytest.raises(ValueError, match='scope mismatch'):
        alpha.load_context('beta', hit.ref, hit.revision)
    with pytest.raises(ValueError, match='source unavailable'):
        beta.load_context('beta', hit.ref, hit.revision)
    with pytest.raises(ValueError, match='scope mismatch'):
        alpha.save_hit('beta', hit.ref, hit.revision, hit.quote, hit.citation)
    with library.storage() as storage:
        assert storage.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
        assert storage.db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 2


def test_collection_removal_rechecks_stale_hit_before_copy_or_save(library):
    scope = library.bind_scope('alpha', 'topic')
    with scope.catalog_tx() as catalog:
        catalog.create_collection('selected', 'Selected books')
    state = import_source(library, collections=('selected',))
    scope.select_collection('alpha', 'selected')
    hit = scope.search('alpha', 'Café')[0]
    with scope.catalog_tx() as catalog:
        assert catalog.remove('selected', state['edition_id'])
    assert scope.search('alpha', 'Café') == ()
    for action in ('context', 'copy', 'save'):
        with pytest.raises(ValueError, match='no longer in selected collection'):
            if action == 'context':
                scope.load_context('alpha', hit.ref, hit.revision)
            elif action == 'copy':
                scope.verified_copy('alpha', hit.ref, hit.revision, 'quote')
            else:
                scope.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    assert len(library.search('alpha', 'Café')) == 1


def test_archived_topic_and_workspace_revoke_bound_services(library):
    import_source(library)
    topic = library.bind_scope('alpha', 'topic')
    root = library.bind_scope('alpha')
    hit = topic.search('alpha', 'Café')[0]
    with topic.topic_tx() as topics:
        topics.archive('topic')
    with pytest.raises(ValueError, match='archived topic'):
        topic.verified_copy('alpha', hit.ref, hit.revision, 'quote')
    assert len(root.search('alpha', 'Café')) == 1
    library.archive_workspace('alpha')
    with pytest.raises(ValueError, match='archived workspace'):
        root.search('alpha', 'Café')
    assert [w.id for w in library.workspaces()] == ['beta']
    assert len(library.workspaces(include_archived=True)) == 2
    assert Library(library.root).check()['integrity'] == 'ok'


def test_saved_attribution_revocation_blocks_fresh_copy_and_export(library):
    state = import_source(library)
    services = library.bind_scope('alpha', 'topic')
    hit = services.search('alpha', 'Café')[0]
    services.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    with library.authorized_transaction('alpha') as db:
        identity = Identity('alpha', state['edition_id'], state['asset'], state['id'])
        MetadataSnapshots(db, 'alpha').revoke(identity)
    with pytest.raises(ValueError, match='revoked metadata'):
        services.verified_copy('alpha', hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unattested metadata'):
        library.export_topic('alpha', 'topic')
    assert library.check()['integrity'] == 'ok'


def test_import_checkpoint_resume_uses_retained_original_after_input_removed(library, tmp_path):
    raw = 'First segment.\fSecond segment.\fThird segment.'
    selected = tmp_path / 'chapters.txt'
    selected.write_text(raw, encoding='utf-8')
    state = library.import_file('alpha', selected, title='Chapters', max_steps=1)
    assert state['state'] == 'pending' and state['completed'] == 1
    assert library.search('alpha', 'First') == ()
    selected.unlink()
    reopened = Library(library.root)
    completed = reopened.resume_import('alpha', state['id'])
    assert completed['state'] == 'complete' and completed['completed'] == 3
    assert reopened.search('alpha', 'Third')[0].page == 'Segment 3 (unpaginated)'
    with pytest.raises(ValueError, match='unknown job in workspace'):
        reopened.resume_import('beta', state['id'])


def test_cancelled_import_requires_explicit_retry_and_no_partial_visibility(library):
    state = import_source(library, cancelled=lambda: True)
    assert state['state'] == 'cancelled' and state['completed'] == 0
    assert library.search('alpha', 'Café') == ()
    assert library.resume_import('alpha', state['id'])['state'] == 'cancelled'
    assert library.resume_import('alpha', state['id'], retry=True)['state'] == 'complete'
    assert len(library.search('alpha', 'Café')) == 1


def test_catalog_failure_rolls_back_whole_publication_and_is_retryable(library):
    import_source(library, text='Existing exact source.')
    with pytest.raises(ValueError, match='unknown or archived collection'):
        import_source(library, text='New recoverable source.', collections=('later',))
    jobs = library.import_jobs('alpha')
    failed = next(job for job in jobs if job['state'] == 'failed')
    assert library.search('alpha', 'New') == ()
    assert library.search('alpha', 'Existing')[0].quote == 'Existing'
    with library.storage() as storage:
        for table in ('catalog_work', 'catalog_edition', 'revision', 'metadata_snapshot'):
            assert storage.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    with library.bind_scope('alpha').catalog_tx() as catalog:
        catalog.create_collection('later', 'Ready now')
    assert library.resume_import('alpha', failed['id'], retry=True)['state'] == 'complete'
    assert len(library.search('alpha', 'New', collection='later')) == 1
    assert library.check()['integrity'] == 'ok'


def test_process_exit_between_library_commit_and_journal_commit_recovers(library, tmp_path):
    source = tmp_path / 'crash.txt'
    source.write_bytes('Crash-resilient exact source.'.encode('utf-8'))
    program = '''
import os, sys
from carr_library.application import Library
from carr_library.ingest.catalog_pipeline import CatalogPublisher
publish = CatalogPublisher.publish_revision
def crash_after_commit(self, *args, **kwargs):
    publish(self, *args, **kwargs)
    os._exit(73)
CatalogPublisher.publish_revision = crash_after_commit
Library(sys.argv[1]).import_file('alpha', sys.argv[2], title='Crash source')
raise AssertionError('publication checkpoint not reached')
'''
    process = subprocess.run([sys.executable, '-c', program, str(library.root), str(source)],
                              capture_output=True, text=True, timeout=30)
    assert process.returncode == 73, (process.stdout, process.stderr)
    reopened = Library(library.root)
    job, = reopened.import_jobs('alpha')
    assert job['state'] == 'pending'
    completed = reopened.resume_import('alpha', job['id'])
    assert completed['state'] == 'complete'
    assert len(reopened.search('alpha', 'Crash-resilient')) == 1
    with reopened.storage() as storage:
        for table in ('catalog_edition', 'revision', 'metadata_snapshot'):
            assert storage.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1


def test_original_drift_fails_closed_for_fresh_copy_and_evidence(library):
    state = import_source(library)
    scope = library.bind_scope('alpha', 'topic')
    hit = scope.search('alpha', 'Café')[0]
    scope.save_hit('alpha', hit.ref, hit.revision, hit.quote, hit.citation)
    (library.originals / (state['asset'] + '.txt')).write_text('Tampered bytes', encoding='utf-8')
    with pytest.raises(ValueError, match='unverified extraction'):
        scope.verified_copy('alpha', hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unverified extraction'):
        library.export_topic('alpha', 'topic')


def test_changed_displayed_selection_not_silently_saved(library):
    import_source(library)
    services = library.bind_scope('alpha', 'topic')
    hit = services.search('alpha', 'Café')[0]
    with pytest.raises(ValueError, match='displayed source changed'):
        services.save_hit('alpha', hit.ref, hit.revision, 'Invented quote', hit.citation)
    reference = json.loads(hit.ref)
    reference[1] = True  # bool is not a page row ID
    with pytest.raises(ValueError, match='reference changed'):
        services.load_context('alpha', json.dumps(reference, ensure_ascii=False, separators=(',', ':')),
                              hit.revision)
    with services.evidence_tx() as evidence:
        assert evidence.list_saved('topic') == ()


def test_thematic_hits_keep_exact_source_and_attribution(library):
    raw = 'Napoleon built railway bridges. Exact historical source.'
    import_source(library, text=raw)
    import_source(library, 'beta', text=raw)
    hits = library.search('alpha', 'railway', retrieval_mode='thematic', result_mode='thematic')
    assert hits
    assert all(hit.workspace == 'alpha' and hit.author == 'Author alpha' for hit in hits)
    services = library.bind_scope('alpha', 'topic')
    for hit in hits:
        assert hit.quote in raw
        assert services.verified_copy('alpha', hit.ref, hit.revision, 'quote') == hit.quote
        assert 'Segment 1 (unpaginated)' in hit.citation


def test_explicit_import_identity_retry_never_duplicates_or_rewrites_metadata(library):
    first = import_source(library, work_id='work', edition_id='edition')
    again = import_source(library, work_id='work', edition_id='edition')
    assert again['id'] == first['id']
    assert again['state'] == 'complete'
    with library.storage() as storage:
        for table in ('catalog_edition', 'revision', 'metadata_snapshot'):
            assert storage.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
    with pytest.raises(ValueError, match='conflicts with original intent'):
        library.import_text('alpha', 'Історія   Café — exact source.\r\n',
                             title='Different attribution', work_id='work', edition_id='edition')
    assert library.search('alpha', 'Café')[0].title == 'Same title'
