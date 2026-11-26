"""Synthetic native SQLite integration; never execute against a live library."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.storage import revisions
from carr_library.storage.revisions import Storage, Page

class Assembly(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)/'library.db'
        self.s = Storage(self.path); self.addCleanup(self.s.close)
        self.db = self.s.db
        # Baseline candidates have no combined schema; patched candidate initializes both.
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace'").fetchone():
            self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);'+Path(__file__).with_name('catalog_membership_candidate.sql').read_text())
        with self.s.transaction():
            for ws in ('a','b'):
                self.db.execute('INSERT INTO workspace VALUES(?)',(ws,))
                c=Catalog(self.db,ws); c.create_collection('c','Research')
                register_batch(c,(self.record(ws),))
    def record(self, ws):
        return Registration('work','edition',dict(sha256=ws*64,mime='text/plain',size=3),'Same title',(ws,), 'uk',collections=('c',))
    def stage(self, ws, edition, rev, asset, pages):
        # Baseline calls existing API; patched candidate makes staging explicit.
        return getattr(self.s,'stage_revision',self.s.publish_revision)(ws,edition,rev,asset,'synthetic',pages)
    def test_composite_binding(self):
        for ws,edition,asset in [('a','edition','b'*64),('a','missing','a'*64),('unknown','edition','a'*64)]:
            with self.subTest(ws=ws,edition=edition,asset=asset), self.assertRaises(sqlite3.IntegrityError):
                self.s.publish_revision(ws,edition,'bad-'+ws+edition+asset[0],asset,'synthetic',[Page('wrong')])
    def test_atomic_import_visibility_and_rollback(self):
        c=Catalog(self.db,'a'); new=replace(self.record('a'),work='new',edition_id='new')
        with self.assertRaisesRegex(RuntimeError,'rollback request'):
            with self.s.transaction():
                register_batch(c,(new,)); self.stage('a','new','r','a'*64,[Page('unique phrase')])
                with sqlite3.connect(self.path) as other:
                    self.assertEqual(other.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0],0)
                    self.assertEqual(other.execute("SELECT count(*) FROM revision WHERE id='r'").fetchone()[0],0)
                raise RuntimeError('rollback request')
        self.assertEqual(self.db.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0],0)
        self.assertIsNone(self.s.get_page('a','r',1)); self.s.check()
    def test_stage_failure_savepoint_preserves_outer(self):
        with self.s.transaction():
            Catalog(self.db,'a').create_collection('prior','Prior')
            with self.assertRaisesRegex(ValueError,'invalid raw page'):
                self.stage('a','edition','bad','a'*64,[Page('good'),Page('bad\x00')])
            self.assertTrue(self.db.in_transaction)
            self.assertEqual(self.db.execute("SELECT count(*) FROM revision WHERE id='bad'").fetchone()[0],0)
        self.assertEqual(Catalog(self.db,'a').editions('prior'),()); self.s.check()
    def test_history_fts_snapshot(self):
        with self.s.transaction():
            self.stage('a','edition','a1','a'*64,[Page('oldtoken',3,'iv',True)])
            self.stage('b','edition','b1','b'*64,[Page('othertoken',3,'iv',True)])
        self.s.publish_revision('a','edition','a2','a'*64,'synthetic',[Page('newtoken',3,'iv',True)])
        self.s.check()
        self.assertEqual(self.s.get_page('a','a1',1)['raw'],'oldtoken'); self.assertIsNone(self.s.get_page('b','a1',1))
        self.assertEqual(self.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'oldtoken'").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'newtoken'").fetchone()[0],1)
        out=self.path.with_name('snapshot.db'); self.s.backup(out)
        copy=Storage(out)
        try:
            copy.check(); self.assertEqual(copy.get_page('a','a1',1)['printed'],'iv')
        finally: copy.close()
    def test_corruption_rejected_at_backup(self):
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.s.publish_revision('a','edition','wrong','b'*64,'synthetic',[Page('wrong')])
        self.db.execute('PRAGMA foreign_keys=ON')
        out=self.path.with_name('bad.db')
        with self.assertRaisesRegex(ValueError,'database integrity failure'): self.s.backup(out)
        self.assertFalse(out.exists())

if __name__=='__main__': unittest.main()
