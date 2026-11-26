import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from carr_library.research.topic_tree import TopicTree
from carr_library.research.topic_search import search_topic
from carr_library.research.saved_topic_query import SavedTopicQueries

ROOT = Path(__file__).parent


class SyntheticStore:
    """Only the historical verifier boundary is synthetic; search/SQLite are real."""
    def __init__(self, db, workspace):
        self.db, self.workspace = db, workspace
        self.tree = TopicTree(db, workspace)
        self.valid = True

    def list_saved(self, topic):
        if not self.valid:
            raise ValueError('synthetic attestation failure')
        return tuple(NS(topic_id=topic, evidence_id=eid,
                        anchor=NS(identity=NS(workspace=self.workspace), quote=raw), note=note)
                     for eid, raw, note in self.db.execute(
                         'SELECT id,payload,note FROM research_evidence WHERE workspace=? AND topic=? ORDER BY id',
                         (self.workspace, topic)))


class SavedQueryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'library.db'
        self.db = self.connect()
        self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' +
                              (ROOT/'topic_tree_candidate.sql').read_text() +
                              (ROOT/'saved_evidence_candidate.sql').read_text() +
                              (ROOT/'saved_topic_query_candidate.sql').read_text())
        self.db.execute('BEGIN IMMEDIATE')
        for ws in ('a','b'):
            self.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
            tree = TopicTree(self.db, ws)
            for topic in ('t','other'):
                tree.create(topic, topic)
                raw = 'Railway Україна' if ws == 'a' and topic == 't' else 'Foreign secret'
                self.db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                                (ws,topic,'same',raw,'synthetic','Note only'))
        self.store = SyntheticStore(self.db,'a')
        self.q = SavedTopicQueries(self.store)

    def connect(self):
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def test_reopen_and_run_existing_search(self):
        self.q.create('t','q','Залізниця','Україна')
        expected = search_topic(self.store,'a','t','Україна')
        self.assertEqual(self.q.run('t','q'), expected)
        self.db.commit()
        db2 = self.connect()
        db2.execute('BEGIN')
        q2 = SavedTopicQueries(SyntheticStore(db2,'a'))
        self.assertEqual(q2.list_queries('t')[0].name,'Залізниця')
        self.assertEqual(q2.run('t','q'),expected)

    def test_scope_and_same_ids(self):
        self.q.create('t','q','A','Railway')
        b = SavedTopicQueries(SyntheticStore(self.db,'b'))
        b.create('t','q','B','secret')
        self.q.create('other','q','Other','secret')
        self.assertEqual(self.q.list_queries('t')[0].name,'A')
        self.assertEqual(b.list_queries('t')[0].name,'B')
        self.assertEqual(self.q.run('t','q')[0].evidence.anchor.quote,'Railway Україна')
        with self.assertRaises(ValueError):
            self.q.run('missing','q')

    def test_update_archive_and_no_retarget(self):
        self.q.create('t','q','A','Railway')
        self.q.update('t','q','B','Note only',field='note',limit=1)
        self.assertEqual(self.q.run('t','q')[0].matched_field,'note')
        self.q.archive('t','q')
        self.assertEqual(self.q.list_queries('t'),())
        self.assertTrue(self.q.list_queries('t',include_archived=True)[0].archived)
        with self.assertRaises(ValueError):
            self.q.run('t','q')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE research_saved_query SET topic='other' WHERE id='q'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM research_saved_query WHERE id='q'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO research_saved_query VALUES('a','t','q','x','x','quote',1,0)")

    def test_literal_operators_and_validation(self):
        self.q.create('t','q','A','"Railway" OR secret')
        self.assertEqual(self.q.run('t','q'),())
        for query,field,limit in [('','quote',1),('x\x00','quote',1),('x','sql',1),('x','quote',True),('x','quote',101)]:
            with self.assertRaises(ValueError):
                self.q.create('t','bad','Bad',query,field=field,limit=limit)

    def test_archived_topic_read_only(self):
        self.q.create('t','q','A','Railway')
        self.store.tree.archive('t')
        self.assertEqual(len(self.q.run('t','q')),1)
        with self.assertRaises(ValueError):
            self.q.update('t','q','B','Railway')
        with self.assertRaises(ValueError):
            self.q.archive('t','q')

    def test_rollback_and_attestation_failure(self):
        self.q.create('t','q','A','Railway')
        self.store.valid = False
        with self.assertRaises(ValueError):
            self.q.run('t','q')
        self.db.rollback()
        with self.assertRaises(ValueError):
            self.q.list_queries('t')
        self.db.execute('BEGIN')
        self.assertEqual(self.db.execute('SELECT count(*) FROM research_saved_query').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
