"""Real disk composition of shared V3 services, original TXT bytes and backup."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.saved_topic_query import SavedTopicQueries
from carr_library.research.topic_tree import TopicTree
from carr_library.storage.attestations import MetadataSnapshots
from carr_library.storage.revisions import Page, Storage
from carr_library.storage.workspace import WorkspaceStore


class SharedV3(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'library.sqlite'
        self.raw = 'Дослівний текст — Café.\n'
        self.data = self.raw.encode('utf-8')
        self.digest = sha256(self.data).hexdigest()
        self.original = self.root / 'original.txt'
        self.original.write_bytes(self.data)
        self.s = Storage(self.path, schema_version=3)
        self.addCleanup(lambda: self.s.close())
        with self.s.transaction():
            for workspace in ('alpha', 'beta'):
                WorkspaceStore(self.s.db).create(workspace, 'Same library', 'Independent research')

    def seed(self, workspace='alpha', revision='rev-alpha', *, topic='topic'):
        db = self.s.db
        catalog = Catalog(db, workspace)
        catalog.create_collection('collection', 'Sources')
        metadata = Metadata('Author ' + workspace, 'Same title', 'I', 'first')
        registration = Registration('work', 'edition',
            {'sha256': self.digest, 'mime': 'text/plain', 'size': len(self.data)},
            metadata.title, (metadata.author,), 'uk', metadata.edition, metadata.volume,
            ('collection',))
        register_batch(catalog, (registration,))
        self.s.stage_revision(workspace, 'edition', revision, self.digest, CONFIG, [Page(self.raw)])
        identity = Identity(workspace, 'edition', self.digest, revision)
        MetadataSnapshots(db, workspace).stage(identity, metadata)
        tree = TopicTree(db, workspace)
        tree.create(topic, 'Research')
        tree.create('child', 'Subsection', parent=topic)
        anchor = make_anchor(identity, Locator('logical_segment', 1), self.raw, 0,
                             len(self.raw), metadata, text_verified=True)
        store = SavedEvidenceStore(db, workspace, *self.readers(db, workspace))
        store.save(topic, 'evidence', anchor, 'Research note')
        SavedTopicQueries(store).create(topic, 'query', 'Exact text', 'Дослівний')
        return identity, metadata, anchor

    def readers(self, db, workspace):
        def resolve_asset(identity):
            # The fixture reads actual synthetic original bytes only after the
            # same DB snapshot authorizes the exact catalog identity.
            if identity.workspace != workspace or not db.execute(
                    'SELECT 1 FROM catalog_edition WHERE workspace=? AND id=? AND asset=?',
                    (workspace, identity.edition_id, identity.asset_sha256)).fetchone():
                raise ValueError('unauthorized asset')
            return self.original.read_bytes()
        text = strict_text_attestor(db, workspace, resolve_asset)
        return historical_loader(db, workspace, text), MetadataSnapshots(db, workspace).attest

    def test_full_services_reopen_and_checked_backup(self):
        with self.s.transaction():
            identity, metadata, _ = self.seed()
            self.seed('beta', 'rev-beta')
            WorkspaceStore(self.s.db).update('alpha', name='Історія', description='Primary sources')
            store = SavedEvidenceStore(self.s.db, 'alpha', *self.readers(self.s.db, 'alpha'))
            store.edit_note('topic', 'evidence', 'Edited personal note')
        self.s.check()
        self.s.close()
        self.s = Storage(self.path)
        self.assertEqual(self.s.schema_version, 3)
        with self.s.transaction():
            rows = SavedEvidenceStore(self.s.db, 'alpha', *self.readers(self.s.db, 'alpha')).list_saved('topic')
            self.assertEqual(rows[0].anchor.identity, identity)
            self.assertEqual(rows[0].anchor.metadata, metadata)
            self.assertEqual(rows[0].note, 'Edited personal note')
            queries = SavedTopicQueries(SavedEvidenceStore(self.s.db, 'alpha', *self.readers(self.s.db, 'alpha')))
            self.assertEqual(queries.list_queries('topic')[0].query, 'Дослівний')
            self.assertEqual(len(queries.run('topic', 'query')), 1)
            beta = SavedEvidenceStore(self.s.db, 'beta', *self.readers(self.s.db, 'beta')).list_saved('topic')
            self.assertEqual(beta[0].anchor.metadata.author, 'Author beta')
        backup = self.root / 'backup.sqlite'
        self.s.backup(backup)
        copy = Storage(backup, schema_version=3)
        try:
            copy.check()
            with copy.transaction():
                self.assertEqual(WorkspaceStore(copy.db).require_active('alpha').name, 'Історія')
                self.assertEqual(MetadataSnapshots(copy.db, 'alpha').load(identity), metadata)
                self.assertEqual(copy.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)
        finally:
            copy.close()

    def test_publication_rollback_includes_metadata_topics_and_evidence(self):
        with self.assertRaisesRegex(RuntimeError, 'interrupt'):
            with self.s.transaction():
                self.seed()
                raise RuntimeError('interrupt')
        for table in ('revision', 'catalog_edition', 'metadata_snapshot', 'research_topic',
                      'research_evidence', 'research_saved_query', 'indexed'):
            self.assertEqual(self.s.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.s.check()

    def test_immutable_attribution_independent_of_catalog_and_revocable(self):
        with self.s.transaction():
            identity, metadata, _ = self.seed()
            self.s.db.execute("UPDATE catalog_work SET title='Edited catalog title' WHERE workspace='alpha'")
            snapshots = MetadataSnapshots(self.s.db, 'alpha')
            self.assertTrue(snapshots.attest(identity, metadata))
            self.assertFalse(snapshots.attest(identity, replace(metadata, title='Edited catalog title')))
            snapshots.revoke(identity)
            self.assertFalse(snapshots.attest(identity, metadata))
            with self.assertRaisesRegex(ValueError, 'unattested metadata'):
                SavedEvidenceStore(self.s.db, 'alpha', *self.readers(self.s.db, 'alpha')).list_saved('topic')
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'revoked metadata'):
                self.s.db.execute('UPDATE metadata_snapshot SET active=1')
        # Revocation prevents citation use but does not destroy historical data
        # or make a lossless integrity-checked backup impossible.
        self.s.check()
        self.s.backup(self.root / 'revoked-backup.sqlite')

    def test_metadata_identity_binding_and_replace_guards(self):
        with self.s.transaction():
            identity, metadata, _ = self.seed()
            with self.assertRaisesRegex(ValueError, 'cross-workspace'):
                MetadataSnapshots(self.s.db, 'beta').stage(identity, metadata)
            with self.assertRaises(sqlite3.IntegrityError):
                MetadataSnapshots(self.s.db, 'alpha').stage(replace(identity, asset_sha256='f'*64), metadata)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'already exists'):
                MetadataSnapshots(self.s.db, 'alpha').stage(identity, metadata)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable metadata'):
                self.s.db.execute("UPDATE metadata_snapshot SET payload='{}'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'metadata snapshot already exists'):
                self.s.db.execute('INSERT OR REPLACE INTO metadata_snapshot SELECT * FROM metadata_snapshot')
        self.s.check()

    def test_workspace_archive_persists_and_preserves_sources(self):
        with self.s.transaction():
            self.seed()
            workspaces = WorkspaceStore(self.s.db)
            workspaces.archive('alpha')
            with self.assertRaisesRegex(ValueError, 'archived workspace'):
                workspaces.require_active('alpha')
            self.assertEqual([w.id for w in workspaces.list_workspaces()], ['beta'])
            self.assertEqual(len(workspaces.list_workspaces(include_archived=True)), 2)
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable workspace'):
                self.s.db.execute("UPDATE workspace SET id='renamed' WHERE id='alpha'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'archive workspace'):
                self.s.db.execute("DELETE FROM workspace WHERE id='alpha'")
        self.s.check()
        self.assertEqual(self.s.get_page('alpha', 'rev-alpha', 1)['raw'], self.raw)

    def test_shared_schema_rejects_topic_cycles_and_evidence_retargeting(self):
        with self.s.transaction():
            self.seed()
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'topic cycle'):
                self.s.db.execute("UPDATE research_topic SET parent='child' WHERE id='topic'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable evidence'):
                self.s.db.execute("UPDATE research_evidence SET topic='child'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'topic identity already exists'):
                self.s.db.execute('INSERT OR REPLACE INTO research_topic SELECT * FROM research_topic')
        self.s.check()

    def test_corrupt_evidence_never_publishes_backup(self):
        with self.s.transaction():
            self.seed()
            self.s.db.execute("INSERT INTO research_evidence SELECT workspace,topic,'corrupt',payload,?,note "
                              "FROM research_evidence WHERE id='evidence'", ('0'*64,))
        with self.assertRaisesRegex(ValueError, 'snapshot drift'):
            self.s.check()
        output = self.root / 'invalid.sqlite'
        with self.assertRaisesRegex(ValueError, 'snapshot drift'):
            self.s.backup(output)
        self.assertFalse(output.exists())

    def test_missing_profile_rejected_by_integrity_check(self):
        with self.s.transaction():
            self.s.db.execute("INSERT INTO workspace VALUES('unprofiled')")
        with self.assertRaisesRegex(ValueError, 'profile missing'):
            self.s.check()

    def test_legacy_versions_not_implicitly_upgraded(self):
        for version in (1, 2):
            path = self.root / f'legacy-{version}.sqlite'
            old = Storage(path, schema_version=version)
            old.close()
            with self.assertRaisesRegex(ValueError, 'schema version'):
                Storage(path, schema_version=3)
            reopened = Storage(path, schema_version=version)
            try:
                reopened.check()
                self.assertEqual(reopened.schema_version, version)
                self.assertIsNone(reopened.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='research_topic'").fetchone())
            finally:
                reopened.close()

    def test_readers_reject_original_byte_drift_after_reopen(self):
        with self.s.transaction():
            self.seed()
        self.original.write_text('Changed original', encoding='utf-8')
        with self.s.transaction():
            with self.assertRaisesRegex(ValueError, 'unverified extraction'):
                SavedEvidenceStore(self.s.db, 'alpha', *self.readers(self.s.db, 'alpha')).list_saved('topic')


if __name__ == '__main__':
    unittest.main()
