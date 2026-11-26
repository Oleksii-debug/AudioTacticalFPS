import sqlite3
import tempfile
import unittest
from pathlib import Path

from carr_library.research.topic_tree import TopicTree


class TopicTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'Бібліотека space.db'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(lambda: self.db.close())
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
        self.db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
        self.db.executescript(Path(__file__).with_name('topic_tree_candidate.sql').read_text())
        self.db.execute('BEGIN IMMEDIATE')
        self.a = TopicTree(self.db, 'a')
        self.b = TopicTree(self.db, 'b')

    def test_persistent_order_and_rename(self):
        self.a.create('r', 'Історія')
        self.a.create('x', 'A', parent='r', position=9)
        self.a.create('y', 'B', parent='r', position=2)
        self.a.rename('x', 'Železnica')
        self.a.move('x', 'r', position=1)
        self.db.commit()
        self.db.close()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('BEGIN')
        rows = TopicTree(self.db, 'a').list_topics()
        self.assertEqual([(r.id, r.name) for r in rows], [('r','Історія'),('x','Železnica'),('y','B')])

    def test_workspace_same_id_and_cross_scope(self):
        self.a.create('same', 'Alpha')
        self.b.create('same', 'Beta')
        self.b.create('private', 'Other')
        with self.assertRaises(ValueError):
            self.a.create('child', 'Wrong', parent='private')
        with self.assertRaises(ValueError):
            self.a.move('same', 'private')
        with self.assertRaises(ValueError):
            self.a.rename('private', 'Leak')
        self.assertEqual([r.name for r in self.a.list_topics()], ['Alpha'])
        self.assertEqual({r.name for r in self.b.list_topics()}, {'Beta','Other'})

    def test_cycles_and_reparent(self):
        self.a.create('r', 'Root')
        self.a.create('c', 'Child', parent='r')
        self.a.create('g', 'Grandchild', parent='c')
        for parent in ('r','c','g'):
            with self.assertRaises(ValueError):
                self.a.move('r', parent)
        self.a.move('g', None)
        self.a.move('r', 'g')
        self.assertEqual({r.id:r.parent for r in self.a.list_topics()}, {'g':None,'r':'g','c':'r'})

    def test_archive_hides_subtree_without_deleting(self):
        self.a.create('r', 'Root')
        self.a.create('c', 'Child', parent='r')
        self.a.create('g', 'Grandchild', parent='c')
        self.a.archive('c')
        self.assertEqual([r.id for r in self.a.list_topics()], ['r'])
        self.assertEqual(len(self.a.list_topics(include_archived=True)), 3)
        for operation in (lambda: self.a.rename('g','New'), lambda: self.a.move('g',None),
                          lambda: self.a.create('x','New',parent='g')):
            with self.assertRaises(ValueError):
                operation()

    def test_rollback_and_no_implicit_commit(self):
        self.a.create('r','Root')
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        with self.assertRaises(ValueError):
            self.a.list_topics()
        self.db.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.a.list_topics(), ())

    def test_reject_invalid_data_and_unknown_workspace(self):
        for invalid in ('', ' ', '\x00'):
            with self.assertRaises(ValueError):
                self.a.create('id', invalid)
        for invalid in (-1, True, 2**31):
            with self.assertRaises(ValueError):
                self.a.create('id', 'Valid', position=invalid)
        with self.assertRaises(sqlite3.IntegrityError):
            TopicTree(self.db,'missing').create('id','Unknown')
        self.assertEqual(self.a.list_topics(), ())

    def test_fk_and_read_uncommitted_guard(self):
        self.db.rollback()
        self.db.execute('PRAGMA foreign_keys=OFF')
        self.db.execute('BEGIN IMMEDIATE')
        with self.assertRaises(ValueError):
            self.a.create('x','No FK')
        self.db.rollback()
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA read_uncommitted=ON')
        self.db.execute('BEGIN')
        with self.assertRaises(ValueError):
            self.a.list_topics()

    def test_sql_metacharacters_are_data(self):
        value = "' ; DROP TABLE research_topic; --"
        self.a.create('x',value)
        self.assertEqual(self.a.list_topics()[0].name,value)
        self.assertEqual(self.b.list_topics(), ())


if __name__ == '__main__':
    unittest.main()
