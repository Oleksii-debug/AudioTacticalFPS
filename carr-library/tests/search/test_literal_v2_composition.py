"""Composition probe: exact PR21 V2 + PR53 Catalog + PR28 literal, no mocks.

Run only in assembled scratch tree, with all three pinned modules present.
No production API or schema changes. Synthetic texts only.
"""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest

from carr_library.catalog.membership import Catalog
from carr_library.search.literal import search, search_file
from carr_library.storage.revisions import Page, Storage


class LiteralV2Composition(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'library.db'
        self.storage = Storage(self.path, schema_version=2)
        self.addCleanup(self.storage.close)
        self.db = self.storage.db
        self.raw = 'Історія: Straße\nісторії'
        self.asset = sha256(self.raw.encode()).hexdigest()
        manifest = dict(sha256=self.asset, mime='text/plain', size=len(self.raw.encode()))
        with self.storage.transaction():
            for ws in ('alpha', 'beta'):
                self.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                c = Catalog(self.db, ws)
                c.create_collection('c', 'Research')
                c.create_collection('empty', 'Empty')
                c.add_work('w', 'Same title', [ws])
                c.add_edition('same', 'w', manifest, language='uk')
                c.add_edition('other', 'w', manifest, language='uk')
                c.assign('c', 'same')
                self.storage.stage_revision(ws, 'same', ws+'1', self.asset,
                                            'synthetic', [Page(self.raw, 3, 'iv', True)])
                self.storage.stage_revision(ws, 'other', ws+'2', self.asset,
                                            'synthetic', [Page(self.raw)])

    def collection_search(self, path, workspace, collection, query):
        # Reference composition only: same read transaction for collection IDs
        # and active source pages. Never cache catalog IDs across snapshots.
        with closing(sqlite3.connect(path.absolute().as_uri() + '?mode=ro',
                                     uri=True, timeout=0.0)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            ids = tuple(row[0] for row in Catalog(db, workspace).editions(collection))
            return search(db, workspace, query, edition_ids=ids)

    def test_scope_and_exact_span(self):
        for ws in ('alpha', 'beta'):
            hits = self.collection_search(self.path, ws, 'c', 'STRASSE історії')
            self.assertEqual(len(hits), 1)
            hit = hits[0]
            self.assertEqual((hit['workspace_id'], hit['edition_id'], hit['revision_id']),
                             (ws, 'same', ws+'1'))
            self.assertEqual(hit['raw_quote'], 'Straße\nісторії')
            self.assertEqual(self.raw[hit['start']:hit['end']], hit['raw_quote'])
            self.assertEqual((hit['physical_index'], hit['printed_label']), (3, 'iv'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0], 1)

    def test_empty_unknown_and_archived_collection(self):
        self.assertEqual(self.collection_search(self.path, 'alpha', 'empty', 'Straße'), [])
        with self.assertRaises(ValueError):
            self.collection_search(self.path, 'alpha', 'missing', 'Straße')
        with self.storage.transaction():
            Catalog(self.db, 'alpha').archive('c')
        with self.assertRaises(ValueError):
            self.collection_search(self.path, 'alpha', 'c', 'Straße')
        self.assertEqual(len(self.collection_search(self.path, 'beta', 'c', 'Straße')), 1)

    def test_revision_switch_and_historical_quote(self):
        old = self.collection_search(self.path, 'alpha', 'c', 'Straße')[0]
        self.storage.publish_revision('alpha', 'same', 'alpha3', self.asset,
                                      'new extraction', [Page('Новий текст', 3, 'iv', True)])
        self.assertEqual(self.collection_search(self.path, 'alpha', 'c', 'Straße'), [])
        new = self.collection_search(self.path, 'alpha', 'c', 'Новий')[0]
        self.assertEqual(new['revision_id'], 'alpha3')
        original = self.storage.get_page('alpha', old['revision_id'], 1)
        self.assertEqual(original['raw'][old['start']:old['end']], old['raw_quote'])
        self.assertIsNone(self.storage.get_page('beta', old['revision_id'], 1))
        self.storage.check()

    def test_uncommitted_publication_invisible(self):
        with self.storage.transaction():
            self.storage.stage_revision('alpha', 'same', 'alpha3', self.asset,
                                        'new extraction', [Page('new token', 3)])
            old = self.collection_search(self.path, 'alpha', 'c', 'Straße')
            self.assertEqual(old[0]['revision_id'], 'alpha1')
            self.assertEqual(self.collection_search(self.path, 'alpha', 'c', 'new token'), [])
        self.assertEqual(self.collection_search(self.path, 'alpha', 'c', 'new token')[0]['revision_id'], 'alpha3')

    def test_backup_and_logical_locator(self):
        out = self.path.with_name('backup.db')
        self.storage.backup(out)
        self.assertEqual(self.collection_search(out, 'alpha', 'c', 'Straße'),
                         self.collection_search(self.path, 'alpha', 'c', 'Straße'))
        logical = search_file(out, 'alpha', 'Straße', edition_ids=['other'])[0]
        self.assertEqual((logical['locator_kind'], logical['segment_index']), ('logical_segment', 1))
        self.assertIsNone(logical['physical_index'])
        self.assertIsNone(logical['printed_label'])

    def test_split_snapshot_hazard_reproduction(self):
        # This is an explicit reproducer of a composition hazard, NOT a passing
        # release acceptance of the split-snapshot pattern.
        stale_ids = tuple(row[0] for row in Catalog(self.db, 'alpha').editions('c'))
        with self.storage.transaction():
            self.assertTrue(Catalog(self.db, 'alpha').remove('c', 'same'))
            self.storage.stage_revision('alpha', 'same', 'alpha3', self.asset,
                                        'new extraction', [Page('new token', 3)])
        self.assertEqual(search_file(self.path, 'alpha', 'new token', edition_ids=stale_ids)[0]['revision_id'], 'alpha3')
        self.assertEqual(self.collection_search(self.path, 'alpha', 'c', 'new token'), [])
        self.assertEqual(search_file(self.path, 'alpha', 'new token')[0]['revision_id'], 'alpha3')


if __name__ == '__main__':
    unittest.main()
