"""Synthetic regression for integrity before candidate pruning, not full storage QA."""
import hashlib
import sqlite3
import unittest
from carr_library.search.literal import search


class ScopedIntegrity(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY, workspace TEXT, edition TEXT, revision TEXT, asset TEXT, ordinal INTEGER, physical INTEGER, printed TEXT, verified INTEGER, raw TEXT, digest TEXT)')

    def page(self, id, raw, *, expected=None, workspace='w', edition='a'):
        digest = hashlib.sha256((raw if expected is None else expected).encode('utf-8')).hexdigest()
        self.db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)', (id, workspace, edition, f'r{id}', 'a'*64, id, id, str(id), 1, raw, digest))

    def test_removed_term(self):
        self.page(1, 'replaced text', expected='railway history')
        with self.assertRaisesRegex(ValueError, 'raw page digest mismatch'):
            search(self.db, 'w', 'railway')

    def test_corrupt_later_page_even_at_limit(self):
        self.page(1, 'railway history')
        self.page(2, 'railway replacement', expected='railway original')
        with self.assertRaisesRegex(ValueError, 'raw page digest mismatch'):
            search(self.db, 'w', 'railway', limit=1)

    def test_corrupt_foreign_workspace_ignored(self):
        self.page(1, 'railway history')
        self.page(2, 'railway replacement', expected='railway original', workspace='other')
        self.assertEqual([h['page_id'] for h in search(self.db, 'w', 'railway')], [1])

    def test_corrupt_excluded_edition_ignored(self):
        self.page(1, 'railway history')
        self.page(2, 'railway replacement', expected='railway original', edition='b')
        self.assertEqual([h['page_id'] for h in search(self.db, 'w', 'railway', edition_ids=['a'])], [1])

    def test_exact_raw_unicode_and_page(self):
        raw = 'Історія: Straße\nісторії'
        self.page(1, raw)
        hits = search(self.db, 'w', 'STRASSE історії')
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h['raw_quote'], 'Straße\nісторії')
        self.assertEqual(raw[h['start']:h['end']], h['raw_quote'])
        self.assertEqual((h['physical_index'], h['printed_label']), (1, '1'))

    def test_no_result_valid_scope(self):
        self.page(1, 'railway history')
        self.assertEqual(search(self.db, 'w', 'botany'), [])


if __name__ == '__main__':
    unittest.main()
