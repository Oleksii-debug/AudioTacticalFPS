import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.topic_tree import TopicTree
from carr_library.research.topic_search import search_topic

ROOT = Path(__file__).parent
RAW = 'Railway history — Україна, cita\u0301cia.'


class TopicSearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = sqlite3.connect(Path(tmp.name) / 'library.db')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' +
                              (ROOT/'topic_tree_candidate.sql').read_text() +
                              (ROOT/'saved_evidence_candidate.sql').read_text())
        self.db.execute('BEGIN IMMEDIATE')
        self.pages = {}
        for ws in ('a', 'b'):
            self.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
            tree = TopicTree(self.db, ws)
            for topic in ('t', 'other'):
                tree.create(topic, topic)
            store = self.store(ws)
            self.add(store, 't', 'same', RAW if ws == 'a' else 'Foreign secret Railway', 'Note only word')
            self.add(store, 'other', 'same', 'Railway in another topic')
        self.a = self.store('a')

    def store(self, ws):
        return SavedEvidenceStore(self.db, ws, self.load, self.attest)

    def add(self, store, topic, eid, text, note=''):
        identity = Identity(store.workspace, topic + eid, 'a'*64, 'sealed-old')
        locator = Locator('physical_page', 2)
        meta = Metadata('Author ' + store.workspace, 'Same title')
        self.pages[identity] = (locator, text, meta)
        anchor = make_anchor(identity, locator, text, 0, len(text), meta, text_verified=True)
        store.save(topic, eid, anchor, note)
        return anchor

    def load(self, identity, locator):
        loc, raw, _ = self.pages[identity]
        return identity, loc, raw, True

    def attest(self, identity, meta):
        return self.pages[identity][2] == meta

    def search(self, query, **kwargs):
        return search_topic(self.a, 'a', 't', query, **kwargs)

    def test_scope_and_original_anchor(self):
        hits = self.search('railway')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].evidence, self.a.list_saved('t')[0])
        self.assertEqual(hits[0].evidence.anchor.quote, RAW)
        self.assertEqual(self.search('secret'), ())
        self.assertEqual(self.search('another'), ())
        with self.assertRaises(ValueError):
            search_topic(self.a, 'b', 't', 'railway')

    def test_unicode(self):
        for q in ('Україна', 'citácia', 'cita\u0301cia'):
            self.assertEqual(self.search(q)[0].evidence.anchor.quote, RAW)

    def test_notes_separate(self):
        self.assertEqual(self.search('Note only word'), ())
        hit, = self.search('Note only word', field='note')
        self.assertEqual(hit.matched_field, 'note')
        self.assertEqual(hit.evidence.anchor.quote, RAW)
        self.a.edit_note('t', 'same', 'updated research')
        self.assertEqual(self.search('Note only word', field='note'), ())
        self.assertEqual(len(self.search('updated', field='note')), 1)

    def test_query_operators_are_text(self):
        for q in ('"railway" OR secret', 'rail*', 'note:word', '"', '---', ''):
            self.assertEqual(self.search(q), ())
        self.assertEqual(len(self.search('railway history')), 1)
        with self.assertRaises(ValueError):
            self.search('x\x00y')

    def test_archive_readable(self):
        self.a.tree.archive('t')
        self.assertEqual(len(self.search('railway')), 1)

    def test_attestation_fails_before_pruning(self):
        self.a.attest_metadata = lambda *_: False
        with self.assertRaises(ValueError):
            self.search('not present')

    def test_raw_drift(self):
        anchor = self.a.list_saved('t')[0].anchor
        loc, _, meta = self.pages[anchor.identity]
        self.pages[anchor.identity] = loc, RAW + '!', meta
        with self.assertRaises(ValueError):
            self.search('railway')

    def test_budget_and_limit(self):
        self.add(self.a, 't', 'second', RAW)
        self.assertEqual(len(self.search('railway', limit=1)), 1)
        with patch('carr_library.research.topic_search.MAX_ROWS', 1):
            with self.assertRaises(ValueError):
                self.search('railway')
        with patch('carr_library.research.topic_search.MAX_BYTES', 1):
            with self.assertRaises(ValueError):
                self.search('railway')
        with self.assertRaises(ValueError):
            self.search('railway', limit=0)

    def test_untrusted_row_and_transaction(self):
        rows = self.a.list_saved('t')
        with patch.object(self.a, 'list_saved', return_value=(replace(rows[0], topic_id='other'),)):
            with self.assertRaises(ValueError):
                self.search('railway')
        self.db.commit()
        with self.assertRaises(ValueError):
            self.search('railway')


if __name__ == '__main__':
    unittest.main()
