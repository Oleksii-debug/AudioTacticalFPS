import sqlite3
import tempfile
import unittest
from pathlib import Path
from carr_library.catalog.membership import Catalog
from carr_library.catalog.read_views import active_collections, browse_editions

SCHEMA = Path(__file__).with_name('catalog_membership_candidate.sql').read_text()
MANIFEST = dict(sha256='a'*64, mime='text/plain', size=8)


class CatalogReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = sqlite3.connect(Path(self.tmp.name)/'catalog.db')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA recursive_triggers=ON')
        self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);'+SCHEMA)
        self.db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
        self.db.commit()
        self.db.execute('BEGIN')
        self.a, self.b = Catalog(self.db, 'a'), Catalog(self.db, 'b')
        for catalog, authors, name in [(self.a, ('Олена', 'Žan'), 'Local'),
                                       (self.b, ('Private',), 'Other')]:
            catalog.add_work('w', 'Історія 100%_test', authors)
            catalog.add_edition('e1', 'w', MANIFEST, language='uk', edition='1')
            catalog.create_collection('c', name)
            catalog.assign('c', 'e1')
        self.a.add_edition('e2', 'w', MANIFEST, language='en', edition='2')
        self.a.add_work('unknown')
        self.a.add_edition('e3', 'unknown', MANIFEST)
        self.a.create_collection('old', 'Archived')
        self.a.assign('old', 'e2')
        self.a.archive('old')
        self.b.create_collection('b-only', 'Private')

    def test_callback_scope_and_archive(self):
        self.assertEqual(active_collections(self.a), (('c', 'Local'),))
        self.assertEqual(active_collections(self.b), (('b-only', 'Private'), ('c', 'Other')))
        self.assertEqual([r.edition_id for r in browse_editions(self.a, collection='c')], ['e1'])
        for collection in ('old', 'b-only'):
            with self.assertRaises(ValueError):
                browse_editions(self.a, collection=collection)
        self.assertEqual(len(browse_editions(self.a)), 3)

    def test_author_scope_and_order(self):
        self.assertEqual(browse_editions(self.a, author='Private'), ())
        self.assertEqual(browse_editions(self.b)[0].authors, ('Private',))
        self.assertEqual(browse_editions(self.a, author='Žan')[0].authors, ('Олена', 'Žan'))
        self.assertEqual(browse_editions(self.a)[2].authors, ())

    def test_literal_and_combined_filters(self):
        self.assertEqual([r.edition_id for r in browse_editions(self.a, title='%_')], ['e1', 'e2'])
        self.assertEqual(browse_editions(self.a, title="' OR 1=1 --"), ())
        self.assertEqual(browse_editions(self.a, author='олена'), ())
        self.assertEqual([r.edition_id for r in browse_editions(self.a, title='Історія',
                         author='Олена', language='en', edition='2')], ['e2'])
        self.assertEqual(browse_editions(self.a, language='uk', edition='2'), ())

    def test_bounded_pages_no_split_author_list(self):
        first = browse_editions(self.a, limit=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].authors, ('Олена', 'Žan'))
        second = browse_editions(self.a, after=first[0].edition_id, limit=2)
        self.assertEqual([r.edition_id for r in second], ['e2', 'e3'])
        self.assertEqual(browse_editions(self.a, after='e3'), ())

    def test_invalid_input(self):
        for kwargs in ({'limit':True}, {'limit':501}, {'limit':0}, {'after':''},
                       {'title':''}, {'author':'\x00'}, {'edition':'\ud800'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                browse_editions(self.a, **kwargs)

    def test_read_only_and_transaction_required(self):
        changes = self.db.total_changes
        active_collections(self.a)
        browse_editions(self.a)
        self.assertEqual(self.db.total_changes, changes)
        self.db.commit()
        with self.assertRaises(ValueError):
            active_collections(self.a)
        with self.assertRaises(ValueError):
            browse_editions(self.a)

    def test_real_reopen(self):
        self.db.commit()
        with sqlite3.connect(Path(self.tmp.name)/'catalog.db') as reader:
            reader.execute('PRAGMA foreign_keys=ON')
            reader.execute('BEGIN')
            cat = Catalog(reader, 'a')
            self.assertEqual(len(browse_editions(cat)), 3)
            self.assertEqual(active_collections(cat), (('c', 'Local'),))
        reader.close()
