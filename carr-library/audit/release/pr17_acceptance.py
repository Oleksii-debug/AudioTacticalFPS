"""Independent synthetic acceptance probe. Run with PR17 checkout on PYTHONPATH.
Do not use private books. Failures are release defects, not expected successes.
"""
import json
import tempfile
import unittest
from pathlib import Path
from bootstrap.universal_library import ResearchLibrary
from bootstrap.external_reuse import import_external


class CandidateAcceptance(unittest.TestCase):
    def test_workspace_result_isolation_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'catalog.db'
            lib = ResearchLibrary(db)
            a, b = lib.workspace('A'), lib.workspace('B')
            lib.add_book(a, 'Same title', 'Author A', '1', [(None, 'railway')])
            lib.add_book(b, 'Same title', 'Author B', '1', [(None, 'railway')])
            topic = lib.topic(a, 'Research')
            hit = lib.search(a, 'railway')[0]
            self.assertEqual(hit['author'], 'Author A')
            self.assertEqual(len(lib.search(a, 'railway')), 1)
            lib.save(a, topic, hit, 'note')
            lib.close()
            lib = ResearchLibrary(db)
            try:
                self.assertEqual(json.loads(lib.export_topic(a, topic))[0]['author'], 'Author A')
                self.assertEqual(json.loads(lib.export_topic(b, topic)), [])
            finally:
                lib.close()

    def test_unpaginated_source_has_no_physical_page(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'notes.md'
            source.write_text('railway bridge', encoding='utf-8')
            lib = ResearchLibrary(':memory:')
            try:
                w = lib.workspace('A')
                topic = lib.topic(w, 'Research')
                import_external(lib, w, source, 'Title', 'Author', '1')
                hit = lib.search(w, 'railway')[0]
                lib.save(w, topic, hit)
                exported = json.loads(lib.export_topic(w, topic))[0]
                self.assertEqual(hit['quote'], 'railway bridge')
                self.assertIsNone(hit['printed'])
                self.assertEqual((hit['physical'], exported['number']), (None, None),
                                 'logical segment is not a physical page')
            finally:
                lib.close()

    def test_workspace_rank_isolation(self):
        lib = ResearchLibrary(':memory:')
        try:
            a, b = lib.workspace('A'), lib.workspace('B')
            lib.add_book(a, 'Rail', 'One', '1', [(None, 'railway')])
            lib.add_book(a, 'River', 'Two', '1', [(None, 'river')])
            def titles():
                return [h['title'] for h in lib.search(a, 'railway river', thematic=True)]
            before = titles()
            self.assertEqual(set(before), {'Rail', 'River'})
            lib.add_book(b, 'Other', 'Three', '1', [(None, 'railway')] * 12)
            self.assertEqual(titles(), before, 'other workspace changes BM25 ordering')
        finally:
            lib.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
