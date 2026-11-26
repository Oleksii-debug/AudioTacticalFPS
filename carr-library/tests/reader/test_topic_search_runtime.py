"""Cancellation and multilingual matching through real saved-evidence codecs."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.saved_topic_query import SavedTopicQueries
from carr_library.research.topic_search import search_topic
from carr_library.research.topic_tree import TopicTree


class TopicSearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = sqlite3.connect(Path(temporary.name) / 'library.sqlite')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        root = Path(__file__).parent
        self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' + ''.join(
            (root / filename).read_text() for filename in (
                'topic_tree_candidate.sql', 'saved_evidence_candidate.sql',
                'saved_topic_query_candidate.sql')))
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("INSERT INTO workspace VALUES('library')")
        TopicTree(self.db, 'library').create('topic', 'Research')
        self.pages = {}
        self.metadata = {}
        self.stop = False
        self.clock = 0.0
        self.on_page = lambda: None
        self.on_metadata = lambda: None
        self.page_calls = []
        self.metadata_calls = []
        self.store = SavedEvidenceStore(self.db, 'library', self.load, self.attest)

    def add(self, evidence, texts, note=''):
        identity = Identity('library', evidence, 'a' * 64, 'sealed-' + evidence)
        metadata = Metadata('Synthetic author', 'Synthetic ' + evidence)
        self.metadata[identity] = metadata
        anchors = []
        for number, text in enumerate(texts, 1):
            locator = Locator('physical_page', number)
            self.pages[identity, locator] = text
            anchors.append(make_anchor(identity, locator, text, 0, len(text), metadata,
                                       text_verified=True))
        self.store.save('topic', evidence, tuple(anchors), note)
        self.page_calls.clear()
        self.metadata_calls.clear()

    def load(self, identity, locator):
        self.page_calls.append((identity, locator))
        self.on_page()
        return identity, locator, self.pages[identity, locator], True

    def attest(self, identity, metadata):
        self.metadata_calls.append(identity)
        self.on_metadata()
        return self.metadata[identity] == metadata

    def cancel(self):
        self.stop = True

    def search(self, query='Railway', **options):
        return search_topic(self.store, 'library', 'topic', query,
                            cancelled=lambda: self.stop, **options)

    def test_precancelled_search_never_reads_sources(self):
        self.add('one', ('Railway history',))
        self.stop = True
        with self.assertRaises(TimeoutError):
            self.search()
        self.assertEqual(self.page_calls, [])
        self.assertEqual(self.metadata_calls, [])

    def test_cancel_during_multipage_verification_stops_before_next_page(self):
        self.add('one', ('Railway history', 'Second source page', 'Third source page'))
        before = self.db.execute('SELECT payload,checksum FROM research_evidence').fetchall()
        self.on_page = self.cancel
        with self.assertRaises(TimeoutError):
            self.search()
        self.assertEqual(len(self.page_calls), 1)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute('SELECT payload,checksum FROM research_evidence').fetchall(), before)
        self.stop = False
        self.on_page = lambda: None
        self.assertEqual(len(self.search()), 1)

    def test_cancel_during_metadata_attestation_never_loads_page(self):
        self.add('one', ('Railway history', 'Second source page'))
        self.on_metadata = self.cancel
        with self.assertRaises(TimeoutError):
            self.search()
        self.assertEqual(len(self.metadata_calls), 1)
        self.assertEqual(self.page_calls, [])

    def test_deadline_covers_verification_and_returns_no_partial_result(self):
        self.add('one', ('Railway history', 'Second source page'))
        self.on_page = lambda: setattr(self, 'clock', 2.0)
        with patch('carr_library.research.topic_search.monotonic', lambda: self.clock):
            with self.assertRaises(TimeoutError):
                self.search(timeout=1.0)
        self.assertEqual(len(self.page_calls), 1)

    def test_cancellation_during_sql_match_rejects_even_empty_results(self):
        self.add('one', ('Railway history',))
        index = sqlite3.connect(':memory:')
        index.set_trace_callback(lambda sql: self.cancel() if 'MATCH' in sql else None)
        with patch('carr_library.research.topic_search.sqlite3.connect', return_value=index):
            with self.assertRaises(TimeoutError):
                self.search('missing word')

    def test_saved_query_propagates_deadline_and_cancellation(self):
        self.add('one', ('Railway history', 'Second source page'))
        queries = SavedTopicQueries(self.store)
        queries.create('topic', 'query', 'Railway', 'Railway')
        self.on_page = self.cancel
        with self.assertRaises(TimeoutError):
            queries.run('topic', 'query', cancelled=lambda: self.stop)
        self.assertEqual(len(self.page_calls), 1)
        self.stop = False
        self.on_page = lambda: None
        self.assertEqual(len(queries.run('topic', 'query')), 1)

    def test_limit_does_not_skip_authentication_of_nonmatching_record(self):
        self.add('a-match', ('Railway history',))
        self.add('b-other', ('Unrelated source',))
        key = next(key for key in self.pages if key[0].edition_id == 'b-other')
        self.pages[key] = 'Different source'
        with self.assertRaisesRegex(ValueError, 'source digest or quotation mismatch'):
            self.search(limit=1)
        self.assertEqual(len(self.page_calls), 2)

    def test_list_saved_stays_tuple_and_iterator_verifies_one_record_at_a_time(self):
        self.add('a', ('First Railway source',))
        self.add('b', ('Second Railway source',))
        iterator = self.store.iter_saved('topic')
        self.assertEqual(self.page_calls, [])
        self.assertEqual(next(iterator).evidence_id, 'a')
        self.assertEqual(len(self.page_calls), 1)
        iterator.close()
        self.assertIs(type(self.store.list_saved('topic')), tuple)
        self.assertEqual(len(self.store.list_saved('topic')), 2)

    def test_combining_marks_remain_part_of_quote_and_note_tokens(self):
        self.add('first', ('किताब عَلَم v\u0329',), 'किताब عَلَم v\u0329')
        self.add('second', ('कुतुब عِلْم v',), 'कुतुब عِلْم v')
        for field in ('quote', 'note'):
            for query, expected in (('किताब', 'first'), ('عَلَم', 'first'),
                                    ('v\u0329', 'first'), ('कुतुब', 'second'),
                                    ('عِلْم', 'second'), ('v', 'second')):
                with self.subTest(field=field, query=query):
                    hits = self.search(query, field=field)
                    self.assertEqual([hit.evidence.evidence_id for hit in hits], [expected])

    def test_invalid_budgets_fail_before_source_access(self):
        self.add('one', ('Railway history',))
        for timeout in (True, 0, float('nan'), float('inf'), 61):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.search(timeout=timeout)
        with self.assertRaises(ValueError):
            search_topic(self.store, 'library', 'topic', 'Railway', cancelled=None)
        self.assertEqual(self.page_calls, [])


if __name__ == '__main__':
    unittest.main()
