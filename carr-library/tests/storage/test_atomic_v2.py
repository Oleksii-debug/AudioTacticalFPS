"""Fresh catalog/revision schema candidate; synthetic disk-backed fixtures only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from carr_library.storage.revisions import Storage, Page


class AtomicV2(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'library.db'
        self.s = Storage(self.path, schema_version=2)
        self.addCleanup(self.s.close)
        with self.s.transaction():
            for ws in ('a', 'b'):
                self.seed(ws, 'edition', ws * 64)

    def seed(self, ws, edition, asset):
        db = self.s.db
        db.execute('INSERT INTO workspace VALUES(?) ON CONFLICT DO NOTHING', (ws,))
        db.execute('INSERT INTO catalog_asset VALUES(?,?,?) ON CONFLICT DO NOTHING', (asset, 'text/plain', 3))
        db.execute('INSERT INTO catalog_work VALUES(?,?,?)', (ws, edition, 'Same title'))
        db.execute('INSERT INTO catalog_edition VALUES(?,?,?,?,?,?,?)', (ws, edition, edition, asset, 'uk', None, None))

    def test_wrong_binding(self):
        for ws, edition, asset in [('a','edition','b'*64), ('a','missing','a'*64), ('c','edition','a'*64)]:
            with self.subTest(ws=ws, edition=edition), self.assertRaises(sqlite3.IntegrityError):
                self.s.publish_revision(ws, edition, 'wrong', asset, 'fixture', [Page('wrong')])
        self.s.check()

    def test_outer_rollback_and_visibility(self):
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            with self.s.transaction():
                self.seed('a', 'new', 'a'*64)
                self.s.stage_revision('a','new','r','a'*64,'fixture',[Page('залізниця')])
                other = sqlite3.connect(self.path)
                try:
                    self.assertEqual(other.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0], 0)
                    self.assertEqual(other.execute("SELECT count(*) FROM revision WHERE id='r'").fetchone()[0], 0)
                finally:
                    other.close()
                raise RuntimeError('cancel')
        self.assertIsNone(self.s.get_page('a','r',1))
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM catalog_edition WHERE id='new'").fetchone()[0], 0)
        self.s.check()

    def test_stage_failure_and_commit(self):
        with self.s.transaction():
            self.seed('a', 'new', 'a'*64)
            with self.assertRaisesRegex(ValueError, 'invalid raw page'):
                self.s.stage_revision('a','new','bad','a'*64,'fixture',[Page('ok'),Page('bad\x00')])
            self.assertTrue(self.s.db.in_transaction)
            self.assertEqual(self.s.db.execute("SELECT count(*) FROM revision WHERE id='bad'").fetchone()[0], 0)
            self.s.stage_revision('a','new','good','a'*64,'fixture',[Page('ok')])
        self.assertEqual(self.s.get_page('a','good',1)['raw'], 'ok')
        self.s.check()

    def test_historical_snapshot(self):
        self.s.publish_revision('a','edition','a1','a'*64,'fixture',[Page('oldtoken',3,'iv',True)])
        self.s.publish_revision('b','edition','b1','b'*64,'fixture',[Page('othertoken',3,'iv',True)])
        self.s.publish_revision('a','edition','a2','a'*64,'fixture',[Page('newtoken',3,'iv',True)])
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'oldtoken'").fetchone()[0],0)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'newtoken'").fetchone()[0],1)
        out = self.path.with_name('snapshot.db')
        self.s.backup(out)
        copy = Storage(out, schema_version=2)
        try:
            copy.check()
            self.assertEqual(copy.get_page('a','a1',1)['printed'],'iv')
            self.assertIsNone(copy.get_page('b','a1',1))
        finally:
            copy.close()

    def test_stage_requires_transaction(self):
        with self.assertRaisesRegex(ValueError, 'transaction'):
            self.s.stage_revision('a','edition','r','a'*64,'fixture',[Page('ok')])

    def test_legacy_explicit_no_live_upgrade(self):
        path = self.path.with_name('legacy.db')
        old = Storage(path)
        old.publish_revision('a','edition','old','a'*64,'fixture',[Page('old')])
        old.close()
        with self.assertRaisesRegex(ValueError, 'schema version'):
            Storage(path, schema_version=2)
        old = Storage(path)
        try:
            self.assertEqual(old.get_page('a','old',1)['raw'],'old')
        finally:
            old.close()

    def test_pragma_guard_and_backup_in_transaction(self):
        self.s.db.execute('PRAGMA foreign_keys=OFF')
        with self.assertRaisesRegex(ValueError, 'foreign keys'):
            self.s.publish_revision('a','edition','r','a'*64,'fixture',[Page('ok')])
        self.s.db.execute('PRAGMA foreign_keys=ON')
        with self.s.transaction():
            with self.assertRaisesRegex(ValueError, 'transaction'):
                self.s.backup(self.path.with_name('bad.db'))
        self.assertFalse(self.path.with_name('bad.db').exists())


if __name__ == '__main__':
    unittest.main()
