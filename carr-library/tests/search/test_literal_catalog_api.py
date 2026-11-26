"""Production catalog API integration; requires assembled PR21/53/28 tree."""
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from carr_library.catalog.membership import Catalog
from carr_library.search.literal import search_catalog_file, search_file
from carr_library.storage.revisions import Page, Storage


class CatalogSearchAPI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'library.db'
        self.storage = Storage(self.path, schema_version=2)
        self.addCleanup(self.storage.close)
        self.db = self.storage.db
        self.db.execute('PRAGMA journal_mode=WAL')
        raw = 'Історія: Straße\nісторії'
        self.asset = sha256(raw.encode()).hexdigest()
        self.manifest = dict(sha256=self.asset, mime='text/plain', size=len(raw.encode()))
        with self.storage.transaction():
            for ws in ('alpha', 'beta'):
                self.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                c = Catalog(self.db, ws)
                c.create_collection('c', 'Research')
                c.create_collection('empty', 'Empty')
                c.add_work('w', 'Same title', [ws])
                for edition in ('same', 'other'):
                    c.add_edition(edition, 'w', self.manifest, language='uk')
                    self.storage.stage_revision(ws, edition, ws+edition, self.asset,
                                                'synthetic', [Page(raw, 3, 'iv', True)])
                c.assign('c', 'same')

    def test_scope_exact_and_whole_workspace(self):
        for ws in ('alpha', 'beta'):
            hits = search_catalog_file(self.path, ws, 'STRASSE історії', collection='c')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['raw_quote'], 'Straße\nісторії')
            self.assertEqual(hits[0]['revision_id'], ws+'same')
            self.assertEqual(hits[0]['printed_label'], 'iv')
            self.assertEqual(len(search_catalog_file(self.path, ws, 'Straße')), 2)
        self.assertEqual(search_catalog_file(self.path, 'alpha', 'Straße', collection='empty'), [])

    def test_invalid_scope_and_cached_ids(self):
        with self.assertRaises(ValueError):
            search_catalog_file(self.path, 'missing', 'Straße', collection='c')
        with self.assertRaises(ValueError):
            search_catalog_file(self.path, 'alpha', 'Straße', collection='missing')
        with self.assertRaises(ValueError):
            search_catalog_file(self.path, 'alpha', 'Straße', collection='c', edition_ids=['other'])
        with self.storage.transaction():
            Catalog(self.db, 'alpha').archive('c')
        with self.assertRaises(ValueError):
            search_catalog_file(self.path, 'alpha', '', collection='c')

    def test_real_wal_commit_between_catalog_and_search(self):
        original = Catalog.editions
        def race(catalog, collection):
            rows = original(catalog, collection)
            self.assertTrue(catalog.db.in_transaction)
            with self.storage.transaction():
                self.assertTrue(Catalog(self.db, 'alpha').remove('c', 'same'))
                self.storage.stage_revision('alpha', 'same', 'new', self.asset,
                                            'new extraction', [Page('new token', 3)])
            return rows
        with patch.object(Catalog, 'editions', race):
            hits = search_catalog_file(self.path, 'alpha', 'Straße', collection='c')
        self.assertEqual([h['revision_id'] for h in hits], ['alphasame'])
        self.assertEqual(search_catalog_file(self.path, 'alpha', 'new token', collection='c'), [])
        self.assertEqual(search_catalog_file(self.path, 'alpha', 'new token')[0]['revision_id'], 'new')
        self.assertEqual(search_catalog_file(self.path, 'beta', 'Straße', collection='c')[0]['revision_id'], 'betasame')
        self.storage.check()

    def test_cancel_during_actual_catalog_sql_is_sticky(self):
        with self.storage.transaction():
            c = Catalog(self.db, 'alpha')
            for i in range(250):
                c.add_edition('extra'+str(i), 'w', self.manifest)
                c.assign('c', 'extra'+str(i))
        real_connect = sqlite3.connect
        state = dict(started=False, pulses=0)
        def trace(sql):
            if sql.lstrip().startswith('SELECT e.id,e.work,e.asset'):
                state['started'] = True
        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(trace)
            return db
        def cancel():
            if state['started'] and state['pulses'] == 0:
                state['pulses'] += 1
                return True
            return False
        with patch('carr_library.search.literal.sqlite3.connect', connect):
            with self.assertRaises(TimeoutError) as exc:
                search_catalog_file(self.path, 'alpha', 'Straße', collection='c', cancelled=cancel)
        self.assertEqual(state['pulses'], 1)
        self.assertIsInstance(exc.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(exc.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_INTERRUPT)
        self.assertEqual(len(search_catalog_file(self.path, 'alpha', 'Straße', collection='c')), 1)
        self.storage.check()

    def test_legacy_file_search_and_missing_file(self):
        self.assertEqual(search_file(self.path, 'alpha', 'Straße', edition_ids=['other'])[0]['edition_id'], 'other')
        missing = self.path.with_name('missing.db')
        with self.assertRaises(sqlite3.OperationalError):
            search_catalog_file(missing, 'alpha', 'Straße', collection='c')
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
