"""Released V3 query definitions survive the real host's V4 migration."""
from contextlib import closing
from hashlib import sha256

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.ingest.retained_assets import RetainedAssets
from carr_library.research.query_manager import QueryManager
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.saved_topic_query import SavedTopicQueries
from carr_library.research.topic_tree import TopicTree
from carr_library.storage.revisions import Page, Storage, _expected_signature, _schema_signature


def test_populated_v3_queries_preserve_all_criteria_and_run_after_v4_upgrade(tmp_path):
    root = tmp_path / 'previous-release-library'
    root.mkdir()
    (root / 'originals').mkdir()
    assets = RetainedAssets(root / 'originals')
    quote = 'Railway history is preserved verbatim.'
    raw = 'Before.\r\n' + quote + '\r\nAfter.'
    data = raw.encode('utf-8')
    asset = sha256(data).hexdigest()
    original = assets.retain(data, 'text/plain', asset)
    workspace, topic = 'existing-library', 'existing-topic'
    identity = Identity(workspace, 'edition', asset, 'historical-revision')
    metadata = Metadata('Original author', 'Original title')
    query_rows_sql = (
        'SELECT workspace,topic,id,name,query,field,result_limit,archived '
        'FROM research_saved_query ORDER BY workspace,topic,id')
    evidence_rows_sql = (
        'SELECT workspace,topic,id,payload,checksum,note '
        'FROM research_evidence ORDER BY workspace,topic,id')

    # Build actual V3 storage without opening the V4 LibraryApp constructor.
    with closing(Storage(root / 'library.sqlite', schema_version=3)) as storage:
        db = storage.db
        with storage.transaction():
            db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
            db.execute('INSERT INTO workspace_meta VALUES(?,?,?,0)',
                       (workspace, 'Бібліотека попередньої версії', 'Saved criteria'))
            register_batch(Catalog(db, workspace), (Registration('work', 'edition',
                {'sha256': asset, 'mime': 'text/plain', 'size': len(data)},
                metadata.title, (metadata.author,), 'en', None, None, ()),))
            storage.stage_revision(workspace, 'edition', identity.revision, asset, CONFIG, [Page(raw)])
            db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                (workspace, 'edition', identity.revision, metadata.author, metadata.title, None, None))
            TopicTree(db, workspace).create(topic, 'Historical evidence')

            def attest_metadata(candidate, supplied):
                row = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
                    'WHERE workspace=? AND edition=? AND revision=?',
                    (workspace, candidate.edition_id, candidate.revision)).fetchone()
                return candidate == identity and row is not None and Metadata(*row) == supplied

            attest = strict_text_attestor(db, workspace,
                lambda candidate: assets.resolve(candidate.asset_sha256).read_bytes())
            store = SavedEvidenceStore(db, workspace,
                historical_loader(db, workspace, attest), attest_metadata)
            start = raw.index(quote)
            anchor = make_anchor(identity, Locator('logical_segment', 1), raw,
                                 start, start + len(quote), metadata, text_verified=True)
            store.save(topic, 'existing-evidence', anchor, 'Моя власна гіпотеза про транспорт.')
            queries = SavedTopicQueries(store)
            queries.create(topic, 'quote-query', 'Цитати з V3', '  Railway history  ',
                           field='quote', limit=7)
            queries.create(topic, 'note-query', 'Примітки з V3', 'власна гіпотеза',
                           field='note', limit=3)
            queries.create(topic, 'archived-query', 'Архівний пошук', 'транспорт',
                           field='note', limit=11)
            queries.archive(topic, 'archived-query')

            # The saved quotation must remain historical when execution resumes.
            storage.stage_revision(workspace, 'edition', 'current-revision', asset, CONFIG, [Page(raw)])
            db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                (workspace, 'edition', 'current-revision', metadata.author, metadata.title, None, None))
            definitions = {row.id: row for row in queries.list_queries(topic, include_archived=True)}
            saved, = store.list_saved(topic)
        storage.check()
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert _schema_signature(db) == _expected_signature(3)
        before_queries = tuple(tuple(row) for row in db.execute(query_rows_sql))
        before_evidence = tuple(tuple(row) for row in db.execute(evidence_rows_sql))

    # The production constructor performs the checked, transactional migration.
    app = LibraryApp(root)
    with app.read(workspace) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert _schema_signature(db) == _expected_signature(4)
        assert tuple(tuple(row) for row in db.execute(query_rows_sql)) == before_queries
        assert tuple(tuple(row) for row in db.execute(evidence_rows_sql)) == before_evidence
        assert db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                          (workspace, 'edition')).fetchone()[0] == 'current-revision'

    manager = QueryManager(app, workspace, topic)
    assert set(manager.list().rows) == {definitions['quote-query'], definitions['note-query']}
    assert set(manager.list(include_archived=True).rows) == set(definitions.values())
    for query_id in ('quote-query', 'note-query'):
        current = manager.get(query_id)
        assert current == definitions[query_id]
        hit, = manager.run(current)
        assert hit.matched_field == current.field
        assert hit.evidence == saved
        assert hit.evidence.anchor.identity.revision == 'historical-revision'
        assert hit.evidence.anchor.quote == quote

    archived = manager.get('archived-query')
    assert archived == definitions['archived-query'] and archived.archived
    with pytest.raises(ValueError, match='Restore'):
        manager.run(archived)
    with app.read(workspace) as db:
        assert tuple(tuple(row) for row in db.execute(query_rows_sql)) == before_queries
        assert tuple(tuple(row) for row in db.execute(evidence_rows_sql)) == before_evidence
    assert original.read_bytes() == data
