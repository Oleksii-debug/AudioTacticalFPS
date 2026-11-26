"""Cross-lane tests: PR21 V2 + PR53 Catalog + PR57 registration, no mocks.

For the assembled tree set CARR_REQUIRE_CATALOG_COMPOSITION=1 so missing
catalog modules are a collection error, not a standalone candidate skip.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from carr_library.storage.revisions import Page, Storage

try:
    from carr_library.catalog.membership import Catalog
    from carr_library.catalog.registration import Registration, register_batch
except ModuleNotFoundError as exc:
    if os.getenv('CARR_REQUIRE_CATALOG_COMPOSITION') == '1' or exc.name not in {
        'carr_library.catalog', 'carr_library.catalog.membership',
        'carr_library.catalog.registration',
    }:
        raise
    HAS_CATALOG = False
else:
    HAS_CATALOG = True


@unittest.skipUnless(HAS_CATALOG, 'requires PR53 and PR57 catalog modules')
class CatalogComposition(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'library.db'
        self.storage = Storage(self.path, schema_version=2)
        self.addCleanup(self.storage.close)
        self.db = self.storage.db
        with self.storage.transaction():
            for scope in ('alpha', 'beta'):
                self.db.execute('INSERT INTO workspace VALUES(?)', (scope,))
                catalog = Catalog(self.db, scope)
                catalog.create_collection('one', 'First')
                catalog.create_collection('two', 'Second')

    def record(self, raw, *, author='Author A', ident='same'):
        data = raw.encode('utf-8')
        return Registration(ident, ident, {'sha256': sha256(data).hexdigest(),
                            'mime': 'text/plain', 'size': len(data)},
                            'Same title', (author,), 'uk', 'first', None, ('one',))

    def stage(self, scope, record, revision, raw):
        register_batch(Catalog(self.db, scope), (record,))
        self.storage.stage_revision(scope, record.edition_id, revision,
                                    record.manifest['sha256'], 'synthetic', [Page(raw)])

    def test_scope_and_dedup_retry(self):
        raw = 'Спільний текст'
        a = self.record(raw)
        b = self.record(raw, author='Author B')
        with self.storage.transaction():
            self.stage('alpha', a, 'a1', raw)
            self.stage('beta', b, 'b1', raw)
            register_batch(Catalog(self.db, 'alpha'), (replace(a, collections=('one','two')),))
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0], 2)
        self.assertEqual(len(Catalog(self.db, 'alpha').editions('two')), 1)
        self.assertEqual(Catalog(self.db, 'beta').editions('two'), ())
        self.assertIsNone(self.storage.get_page('beta','a1',1))
        self.assertEqual(self.storage.get_page('alpha','a1',1)['raw'], raw)
        self.storage.check()

    def test_outer_rollback_on_invalid_second_revision(self):
        raw = 'first token'
        first = self.record(raw)
        second = self.record('second', ident='second')
        with self.assertRaisesRegex(ValueError, 'invalid raw page'):
            with self.storage.transaction():
                self.stage('alpha', first, 'a1', raw)
                self.stage('alpha', second, 'a2', 'bad\x00')
        for table in ('catalog_work','catalog_edition','catalog_asset','catalog_membership',
                      'revision','page','sealed','active','indexed'):
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.storage.check()

    def test_second_connection_cannot_see_staged_import(self):
        raw = 'token'
        with self.storage.transaction():
            self.stage('alpha', self.record(raw), 'a1', raw)
            with closing(sqlite3.connect(self.path)) as other:
                for table in ('catalog_edition','revision','indexed'):
                    self.assertEqual(other.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.storage.get_page('alpha','a1',1)['raw'], raw)

    def test_catalog_conflict_preserves_previous_revision(self):
        raw = 'first token'
        with self.storage.transaction():
            self.stage('alpha', self.record(raw), 'a1', raw)
        with self.assertRaisesRegex(ValueError, 'work identity conflict'):
            with self.storage.transaction():
                self.stage('alpha', self.record(raw, author='Other'), 'a2', raw)
        self.assertEqual(self.storage.get_page('alpha','a1',1)['raw'], raw)
        self.assertIsNone(self.storage.get_page('alpha','a2',1))
        self.storage.check()

    def test_snapshot_and_membership_removal_preserve_history(self):
        raw = 'oldtoken'
        record = self.record(raw)
        with self.storage.transaction():
            self.stage('alpha', record, 'a1', raw)
        with self.storage.transaction():
            self.storage.stage_revision('alpha', 'same', 'a2', record.manifest['sha256'],
                                        're-extraction', [Page('newtoken')])
            self.assertTrue(Catalog(self.db, 'alpha').remove('one', 'same'))
        out = self.path.with_name('backup.db')
        self.storage.backup(out)
        copy = Storage(out, schema_version=2)
        try:
            copy.check()
            self.assertEqual(copy.get_page('alpha','a1',1)['raw'], raw)
            self.assertIsNone(copy.get_page('beta','a1',1))
            self.assertEqual(Catalog(copy.db, 'alpha').editions('one'), ())
            self.assertEqual(len(Catalog(copy.db, 'alpha').editions()), 1)
            self.assertEqual(copy.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'oldtoken'").fetchone()[0], 0)
            self.assertEqual(copy.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'newtoken'").fetchone()[0], 1)
        finally:
            copy.close()


if __name__ == '__main__':
    unittest.main()
