import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research.topic_tree import TopicTree
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.anchored_export import export_saved

ROOT = Path(__file__).parent
RAW = 'Перший рядок.\nExact quote — citácia.'


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.db'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('CREATE TABLE workspace(id TEXT PRIMARY KEY);' +
                              (ROOT/'topic_tree_candidate.sql').read_text() +
                              (ROOT/'saved_evidence_candidate.sql').read_text())
        self.db.execute('BEGIN IMMEDIATE')
        for scope in ('a', 'b'):
            self.db.execute('INSERT INTO workspace VALUES(?)', (scope,))
            TopicTree(self.db, scope).create('t', 'Root')
        self.identity = Identity('a', 'edition', 'a'*64, 'old')
        self.loc = Locator('physical_page', 2)
        self.meta = Metadata('Author A', 'Same title')
        self.anchor = make_anchor(self.identity, self.loc, RAW, 0, len(RAW), self.meta, text_verified=True)
        self.store = self.new_store()

    def new_store(self):
        return SavedEvidenceStore(self.db, 'a', self.load, self.attest)

    def load(self, identity, locator):
        if identity != self.identity or locator != self.loc:
            raise ValueError('missing historical source')
        return identity, locator, RAW, True

    def attest(self, identity, meta):
        return identity == self.identity and meta == self.meta

    def test_reopen_export(self):
        self.store.save('t', 'e', self.anchor, 'old')
        self.store.edit_note('t', 'e', 'нова примітка')
        self.db.commit()
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('BEGIN IMMEDIATE')
        rows = self.new_store().list_saved('t')
        self.assertEqual(rows[0].anchor, self.anchor)
        self.assertEqual(rows[0].note, 'нова примітка')
        result = export_saved('a', 't', rows, self.load)
        self.assertIn('Exact quote — citácia.', result)
        self.assertIn('printed page unknown', result)
        self.assertIn('нова примітка', result)

    def test_cross_scope(self):
        self.store.save('t', 'e', self.anchor)
        other = SavedEvidenceStore(self.db, 'b', self.load, self.attest)
        self.assertEqual(other.list_saved('t'), ())
        with self.assertRaises(ValueError):
            other.save('t', 'e', self.anchor)
        with self.assertRaises(ValueError):
            other.edit_note('t', 'e', 'wrong')

    def test_wrong_metadata_and_quote(self):
        for bad in (replace(self.anchor, metadata=Metadata('B', 'Same title')),
                    replace(self.anchor, quote='fabricated')):
            with self.assertRaises(ValueError):
                self.store.save('t', 'e', bad)
        self.assertEqual(self.store.list_saved('t'), ())

    def test_archive_read_only(self):
        self.store.save('t', 'e', self.anchor)
        TopicTree(self.db, 'a').archive('t')
        self.assertEqual(len(self.store.list_saved('t')), 1)
        with self.assertRaises(ValueError):
            self.store.edit_note('t', 'e', 'new')
        with self.assertRaises(ValueError):
            self.store.save('t', 'f', self.anchor)

    def test_immutable(self):
        self.store.save('t', 'e', self.anchor)
        for statement in ('UPDATE research_evidence SET payload=\'bad\'', 'DELETE FROM research_evidence'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)

    def test_duplicate_and_replace(self):
        self.store.save('t', 'e', self.anchor, 'original')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save('t', 'e', self.anchor, 'changed')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT OR REPLACE INTO research_evidence SELECT workspace,topic,id,payload,checksum,'changed' FROM research_evidence")
        self.assertEqual(self.store.list_saved('t')[0].note, 'original')

    def test_corruption(self):
        self.store.save('t', 'e', self.anchor)
        self.db.execute('DROP TRIGGER research_evidence_immutable')
        self.db.execute("UPDATE research_evidence SET payload='{}'")
        with self.assertRaises(ValueError):
            self.store.list_saved('t')

    def test_rollback(self):
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')
        self.store.save('t', 'e', self.anchor)
        self.db.rollback()
        self.db.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.store.list_saved('t'), ())

    def test_revoked_attestation(self):
        self.store.save('t', 'e', self.anchor)
        self.store.attest_metadata = lambda *_: False
        with self.assertRaises(ValueError):
            self.store.list_saved('t')

if __name__ == '__main__':
    unittest.main()
