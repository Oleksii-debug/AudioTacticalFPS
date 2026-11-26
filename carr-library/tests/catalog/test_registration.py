import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from hashlib import sha256
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'library.db'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA recursive_triggers=ON')
        self.db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
        self.db.executescript(Path(__file__).with_name('catalog_membership_candidate.sql').read_text())
        self.db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
        self.cat = Catalog(self.db, 'a')
        self.db.execute('BEGIN IMMEDIATE')
        self.cat.create_collection('c', 'First')
        self.cat.create_collection('d', 'Second')
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        self.r = Registration('w', 'e', {'sha256': sha256(b'abc').hexdigest(), 'mime': 'text/plain', 'size': 3}, 'Назва', ('Автор',), 'uk', collections=('c',))

    def test_retry_membership_and_reopen(self):
        register_batch(self.cat, (self.r,))
        register_batch(self.cat, (replace(self.r, collections=('c', 'd')),))
        self.db.commit()
        with sqlite3.connect(self.path) as other:
            self.assertEqual(other.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)
            self.assertEqual(other.execute('SELECT count(*) FROM catalog_edition').fetchone()[0], 1)
            self.assertEqual(other.execute('SELECT count(*) FROM catalog_membership').fetchone()[0], 2)

    def test_conflict_rolls_back_whole_batch(self):
        register_batch(self.cat, (self.r,))
        new = replace(self.r, work='w2', edition_id='e2')
        with self.assertRaises(ValueError):
            register_batch(self.cat, (new, replace(self.r, title='Different')))
        self.db.commit()
        self.assertEqual(len(self.cat.editions()), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_work').fetchone()[0], 1)

    def test_bad_collection_rolls_back(self):
        with self.assertRaises(ValueError):
            register_batch(self.cat, (replace(self.r, collections=('c', 'missing')),))
        self.db.commit()
        self.assertEqual(self.cat.editions(), ())
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 0)

    def test_scope(self):
        register_batch(self.cat, (self.r,))
        b = Catalog(self.db, 'b')
        with self.assertRaises(ValueError):
            register_batch(b, (self.r,))
        register_batch(b, (replace(self.r, collections=(), title='Other author', authors=('Other',)),))
        self.assertEqual(b.editions()[0][3], 'Other author')
        self.assertEqual(self.cat.editions()[0][3], 'Назва')

    def test_outer_rollback(self):
        register_batch(self.cat, (self.r,))
        self.db.rollback()
        self.assertEqual(self.cat.editions(), ())
        with self.assertRaises(ValueError):
            register_batch(self.cat, (self.r,))

    def test_manifest_and_edition_conflict(self):
        register_batch(self.cat, (self.r,))
        for record in (replace(self.r, volume='2'), replace(self.r, manifest=dict(self.r.manifest, size=True)), replace(self.r, manifest=dict(self.r.manifest, size=4))):
            with self.assertRaises(ValueError):
                register_batch(self.cat, (record,))
        self.assertEqual(len(self.cat.editions()), 1)

    def test_distinct_editions_same_bytes(self):
        register_batch(self.cat, (self.r, replace(self.r, edition_id='translation', language='en')))
        self.assertEqual(len(self.cat.editions()), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)

    def test_archived_collection_rejected(self):
        self.cat.archive('d')
        with self.assertRaises(ValueError):
            register_batch(self.cat, (replace(self.r, collections=('d',)),))
        self.assertEqual(self.cat.editions(), ())


    def test_engine_rollback_preserves_error(self):
        self.db.execute("CREATE TRIGGER fail_author BEFORE INSERT ON catalog_author "
                        "WHEN NEW.name='Reject' BEGIN SELECT RAISE(ROLLBACK,'original rejection'); END")
        register_batch(self.cat, (self.r,))
        bad = replace(self.r, work='bad', edition_id='bad', authors=('Reject',))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'original rejection'):
            register_batch(self.cat, (bad,))
        self.assertFalse(self.db.in_transaction)
        with sqlite3.connect(self.path) as other:
            for table in ('catalog_work', 'catalog_author', 'catalog_edition', 'catalog_asset', 'catalog_membership'):
                self.assertEqual(other.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.db.execute('BEGIN IMMEDIATE')
        register_batch(self.cat, (self.r,))
        self.db.commit()
        self.assertEqual(len(self.cat.editions()), 1)

    def test_recursive_triggers_required(self):
        self.db.rollback()
        self.db.execute('PRAGMA recursive_triggers=OFF')
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(ValueError, 'recursive triggers required'):
            register_batch(self.cat, (self.r,))
        self.assertEqual(self.cat.editions(), ())

if __name__ == '__main__':
    unittest.main()
