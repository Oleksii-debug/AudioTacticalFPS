"""Real SQLite coverage for the assembled research library and safe upgrade."""
from hashlib import sha256
import sqlite3
import tempfile
import unittest
from pathlib import Path

from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor, digest
from carr_library.citations.historical import historical_loader
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.research.saved_evidence import SavedEvidenceStore, _encode
from carr_library.research.saved_topic_query import SavedTopicQueries
from carr_library.research.topic_tree import TopicTree
from carr_library.storage.revisions import Page, Storage, migrate_v2_snapshot


class ResearchSchema(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'library.db'
        self.storage = Storage(self.path, schema_version=3)
        self.addCleanup(lambda: self.storage.close())
        self.raw = 'Точна цитата. Railway archive.'
        self.data = self.raw.encode('utf-8')
        self.asset = sha256(self.data).hexdigest()
        self.assets = {self.asset: self.data}
        with self.storage.transaction():
            for workspace in ('a', 'b'):
                self.seed(workspace)

    def seed(self, workspace):
        db = self.storage.db
        db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
        db.execute('INSERT INTO workspace_meta(workspace,name,description) VALUES(?,?,?)',
                   (workspace, 'Library ' + workspace, 'Independent research'))
        catalog = Catalog(db, workspace)
        catalog.create_collection('collection', 'Books')
        register_batch(catalog, (Registration('work', 'edition',
            {'sha256': self.asset, 'mime': 'text/plain', 'size': len(self.data)},
            'Same title', ('Author ' + workspace,), 'uk', 'first', None, ('collection',)),))
        self.storage.stage_revision(workspace, 'edition', workspace + '-r1', self.asset,
                                    CONFIG, (Page(self.raw),))
        db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                   (workspace, 'edition', workspace + '-r1', 'Author ' + workspace,
                    'Same title', None, 'first'))
        TopicTree(db, workspace).create('topic', 'Sources')

    def store(self, workspace='a', storage=None):
        db = (storage or self.storage).db
        attest = strict_text_attestor(db, workspace, lambda identity: self.assets[identity.asset_sha256])
        load = historical_loader(db, workspace, attest)

        def meta(identity, metadata):
            row = db.execute('''SELECT author,title,volume,edition_label FROM source_metadata
                WHERE workspace=? AND edition=? AND revision=?''',
                (workspace, identity.edition_id, identity.revision)).fetchone()
            return row is not None and Metadata(*row) == metadata and identity.workspace == workspace

        return SavedEvidenceStore(db, workspace, load, meta)

    def anchor(self, workspace='a'):
        return make_anchor(Identity(workspace, 'edition', self.asset, workspace + '-r1'),
            Locator('logical_segment', 1), self.raw, 0, 12,
            Metadata('Author ' + workspace, 'Same title', None, 'first'), text_verified=True)

    def test_reopen_backup_preserves_quotes_queries_notes_and_tags(self):
        with self.storage.transaction():
            store = self.store()
            store.save('topic', 'quote', self.anchor(), 'My note')
            store.edit_note('topic', 'quote', 'Updated note')
            SavedTopicQueries(store).create('topic', 'query', 'Exact Ukrainian', 'Точна')
            self.storage.db.execute('INSERT INTO research_tag VALUES(?,?,?)', ('a','tag','Railways'))
            self.storage.db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                                    ('a','topic','quote','tag'))
        self.storage.close()
        self.storage = Storage(self.path, schema_version=3)
        snapshot = self.path.with_name('snapshot.db')
        self.storage.backup(snapshot)
        copy = Storage(snapshot, schema_version=3)
        try:
            copy.check()
            with copy.transaction():
                store = self.store(storage=copy)
                evidence, = store.list_saved('topic')
                self.assertEqual(evidence.anchor.quote, self.raw[:12])
                self.assertEqual(evidence.note, 'Updated note')
                results = SavedTopicQueries(store).run('topic', 'query')
                self.assertEqual(len(results), 1)
                self.assertEqual(copy.db.execute('SELECT count(*) FROM research_evidence_tag').fetchone()[0],1)
                self.assertEqual(self.store('b', copy).list_saved('topic'), ())
            self.assertEqual(copy.db.execute('SELECT count(*) FROM source_page_review').fetchone()[0],0)
        finally:
            copy.close()

    def test_old_evidence_survives_reextraction_and_membership_removal(self):
        with self.storage.transaction():
            self.store().save('topic', 'quote', self.anchor())
            self.storage.stage_revision('a','edition','a-r2',self.asset,'different extractor',
                                        [Page('Different candidate extraction')])
            self.assertTrue(Catalog(self.storage.db,'a').remove('collection','edition'))
        self.storage.check()
        with self.storage.transaction():
            old, = self.store().list_saved('topic')
            self.assertEqual(old.anchor.identity.revision,'a-r1')
            self.assertEqual(old.anchor.quote,self.raw[:12])
            self.assertEqual(Catalog(self.storage.db,'a').editions('collection'),())

    def test_multipage_snapshot_backup_keeps_separate_source_slices(self):
        first, second = 'First segment\r\n', 'Другий сегмент.'
        data = (first + '\f' + second).encode('utf-8')
        asset = sha256(data).hexdigest()
        self.assets[asset] = data
        identity = Identity('a','long',asset,'a-long')
        metadata = Metadata('Author a','Long source',None,None)
        anchors = (make_anchor(identity,Locator('logical_segment',1),first,6,len(first),metadata,text_verified=True),
                   make_anchor(identity,Locator('logical_segment',2),second,0,6,metadata,text_verified=True))
        with self.storage.transaction():
            register_batch(Catalog(self.storage.db,'a'), (Registration('long','long',
                {'sha256':asset,'mime':'text/plain','size':len(data)}, 'Long source',
                ('Author a',),'uk',None,None,('collection',)),))
            self.storage.stage_revision('a','long','a-long',asset,CONFIG,[Page(first),Page(second)])
            self.storage.db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                                    ('a','long','a-long','Author a','Long source',None,None))
            self.store().save('topic','span',anchors,'Separate commentary')
        target = self.path.with_name('span.db')
        self.storage.backup(target)
        copy = Storage(target,schema_version=3)
        try:
            copy.check()
            with copy.transaction():
                saved, = self.store(storage=copy).list_saved('topic')
                self.assertEqual(tuple(a.quote for a in saved.anchors),(first[6:],second[:6]))
                self.assertEqual(saved.note,'Separate commentary')
        finally:
            copy.close()

    def test_authoritative_fk_and_immutable_metadata(self):
        with self.storage.transaction():
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                    ('a','edition','b-r1','Wrong','Wrong',None,None))
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.db.execute("UPDATE source_metadata SET author='Wrong' WHERE workspace='a'")
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.db.execute('INSERT OR REPLACE INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                    ('a','edition','a-r1','Wrong','Wrong',None,None))
            with self.assertRaisesRegex(ValueError,'cross-workspace'):
                self.store().save('topic','cross',self.anchor('b'))
            self.store().save('topic','quote',self.anchor())
            self.storage.db.execute('INSERT INTO research_tag VALUES(?,?,?)',('b','private','Private'))
            with self.assertRaises(sqlite3.IntegrityError):
                self.storage.db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                                        ('a','topic','quote','private'))
        self.storage.check()

    def test_backup_rejects_reachable_cycle_corruption(self):
        with self.storage.transaction():
            tree = TopicTree(self.storage.db,'a')
            tree.create('child','Child',parent='topic')
            with self.assertRaisesRegex(ValueError,'cycle'):
                tree.move('topic','child')
            # Simulate damage by bypassing the service; the backup must not publish it.
            self.storage.db.execute("UPDATE research_topic SET parent='child' WHERE workspace='a' AND id='topic'")
        target = self.path.with_name('bad.db')
        with self.assertRaisesRegex(ValueError,'cycle'):
            self.storage.backup(target)
        self.assertFalse(target.exists())

    def test_backup_rejects_cross_workspace_json_snapshot(self):
        payload = _encode(self.anchor('b'))
        with self.storage.transaction():
            self.storage.db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                                    ('a','topic','forged',payload,digest(payload),''))
        target = self.path.with_name('bad.db')
        with self.assertRaisesRegex(ValueError,'identity'):
            self.storage.backup(target)
        self.assertFalse(target.exists())

    def test_research_and_publication_rollback_together(self):
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            with self.storage.transaction():
                self.storage.stage_revision('a','edition','a-r2',self.asset,CONFIG,[Page(self.raw)])
                TopicTree(self.storage.db,'a').create('new','New research')
                self.store().save('new','quote',self.anchor())
                raise RuntimeError('cancel')
        self.assertIsNone(self.storage.get_page('a','a-r2',1))
        self.assertIsNone(self.storage.db.execute("SELECT 1 FROM research_topic WHERE id='new'").fetchone())
        self.assertIsNone(self.storage.db.execute('SELECT 1 FROM research_evidence').fetchone())
        self.storage.check()

    def test_safe_v2_upgrade_keeps_original_and_unknown_review_state(self):
        source = self.path.with_name('v2.db')
        old = Storage(source, schema_version=2)
        try:
            with old.transaction():
                old.db.execute("INSERT INTO workspace VALUES('old')")
                old.db.execute('INSERT INTO catalog_asset VALUES(?,?,?)',(self.asset,'text/plain',len(self.data)))
                old.db.execute("INSERT INTO catalog_work VALUES('old','work','Old title')")
                old.db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)',
                               ('old','edition','work',self.asset,'uk',None,None))
                old.stage_revision('old','edition','old-r1',self.asset,CONFIG,[Page(self.raw)])
        finally:
            old.close()
        before = source.read_bytes()
        target = self.path.with_name('v3.db')
        self.assertEqual(migrate_v2_snapshot(source,target),target)
        self.assertEqual(source.read_bytes(),before)
        migrated = Storage(target,schema_version=3)
        try:
            migrated.check()
            self.assertEqual(migrated.get_page('old','old-r1',1)['raw'],self.raw)
            self.assertEqual(migrated.db.execute('SELECT name FROM workspace_meta').fetchone()[0],'old')
            self.assertEqual(migrated.db.execute('SELECT count(*) FROM source_metadata').fetchone()[0],0)
            self.assertEqual(migrated.db.execute('SELECT count(*) FROM source_page_review').fetchone()[0],0)
        finally:
            migrated.close()
        with self.assertRaises(FileExistsError):
            migrate_v2_snapshot(source,target)
        with self.assertRaisesRegex(ValueError,'schema version'):
            Storage(source,schema_version=3)
        self.assertEqual(source.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
