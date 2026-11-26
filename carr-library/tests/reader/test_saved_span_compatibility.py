"""Real SQLite regression for legacy/single/multi-page evidence interoperability."""
from contextlib import contextmanager, closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.citations.authorized_action import AuthorizedSelection, copy_authorized_span
from carr_library.citations.historical import historical_loader
from carr_library.research.anchored_export import export_saved
from carr_library.research.saved_evidence import SavedEvidenceStore, _encode
from carr_library.research.span_snapshot import restore_anchors, restore_span
from carr_library.research.topic_search import search_topic
from carr_library.research.topic_tree import TopicTree


class SavedSpanCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'library.sqlite'
        self.raw = ('Дослівний уривок end', 'start Druhý úryvok\n')
        self.identity = Identity('a', 'edition', 'a' * 64, 'sealed-old')
        self.metadata = Metadata('Synthetic author', 'Synthetic title', 'II', 'first')
        self.anchors = tuple(make_anchor(
            self.identity, Locator('physical_page', i), raw, 0, len(raw),
            self.metadata, text_verified=True) for i, raw in enumerate(self.raw, 1))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE workspace(id TEXT PRIMARY KEY);
                INSERT INTO workspace VALUES('a'),('b');
                CREATE TABLE revision(workspace,edition,asset,id);
                CREATE TABLE sealed(revision);
                CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);
                CREATE TABLE approval(physical PRIMARY KEY,hash,active);
                CREATE TABLE metadata_approval(payload,active);
            ''')
            for filename in ('topic_tree_candidate.sql', 'saved_evidence_candidate.sql'):
                db.executescript((Path(__file__).parent / filename).read_text())
            db.execute('INSERT INTO revision VALUES(?,?,?,?)',
                       ('a', 'edition', 'a' * 64, 'sealed-old'))
            db.execute('INSERT INTO sealed VALUES(?)', ('sealed-old',))
            db.execute('INSERT INTO metadata_approval VALUES(?,1)',
                       (json.dumps(asdict(self.metadata), sort_keys=True),))
            for i, raw in enumerate(self.raw, 1):
                db.execute('INSERT INTO page VALUES(?,?,?,?,?,?,?)',
                           ('sealed-old', i, i, None, 0, raw, digest(raw)))
                db.execute('INSERT INTO approval VALUES(?,?,1)', (i, digest(raw)))
            for scope in ('a', 'b'):
                TopicTree(db, scope).create('topic', 'Research')
            # Existing PR54 byte encoding must remain readable without rewrite.
            payload = _encode(self.anchors[0])
            self.legacy = payload, digest(payload)
            db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                       ('a', 'topic', 'legacy', *self.legacy, 'Separate legacy note'))

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            yield db

    def readers(self, db, workspace):
        def approve(identity, locator, raw_hash):
            return identity == self.identity and db.execute(
                'SELECT 1 FROM approval WHERE physical=? AND hash=? AND active=1',
                (locator.index, raw_hash)).fetchone() == (1,)

        def attest(identity, metadata):
            return identity == self.identity and db.execute(
                'SELECT 1 FROM metadata_approval WHERE payload=? AND active=1',
                (json.dumps(asdict(metadata), sort_keys=True),)).fetchone() == (1,)

        return historical_loader(db, workspace, approve), attest

    def store(self, db):
        return SavedEvidenceStore(db, 'a', *self.readers(db, 'a'))

    def add_span(self):
        with self.transaction() as db:
            self.store(db).save('topic', 'span', self.anchors, 'Separate span note')
        return self.checksum('span')

    def checksum(self, evidence):
        with self.transaction() as db:
            return db.execute('SELECT checksum FROM research_evidence WHERE id=?',
                              (evidence,)).fetchone()[0]

    @contextmanager
    def open_read(self, workspace, topic, evidence):
        with self.transaction() as db:
            TopicTree(db, workspace)._visible(topic)
            row = db.execute('SELECT workspace,topic,id,payload,checksum FROM research_evidence '
                             'WHERE workspace=? AND topic=? AND id=?',
                             (workspace, topic, evidence)).fetchone()
            yield AuthorizedSelection(*row, *self.readers(db, workspace)) if row else None

    def copy(self, evidence, position=0, workspace='a', checksum=None):
        return copy_authorized_span(workspace, 'topic', evidence,
                                    checksum or self.checksum(evidence), position,
                                    'quote', self.open_read)

    def test_legacy_copy_binds_original_checksum_without_rewrite(self):
        self.assertEqual(self.copy('legacy'), self.raw[0])
        with self.transaction() as db:
            self.assertEqual(self.store(db).list_saved('topic')[0].checksum, self.legacy[1])
            saved = db.execute('SELECT payload,checksum FROM research_evidence').fetchone()
            self.assertEqual(saved, self.legacy)

    def test_mixed_rows_reopen_and_preserve_all_source_slices(self):
        self.add_span()
        with self.transaction() as db:
            rows = self.store(db).list_saved('topic')
            self.assertEqual([row.evidence_id for row in rows], ['legacy', 'span'])
            self.assertEqual(rows[0].anchors, (self.anchors[0],))
            self.assertEqual(rows[1].anchors, self.anchors)
            payload, checksum = db.execute(
                "SELECT payload,checksum FROM research_evidence WHERE id='span'").fetchone()
            self.assertEqual(json.loads(payload)['version'], 1)
            self.assertEqual(restore_anchors('a', payload, checksum, *self.readers(db, 'a')),
                             self.anchors)
        self.assertEqual(self.copy('span', 1), self.raw[1])

    def test_export_preserves_id_and_separate_page_quotes(self):
        self.add_span()
        with self.transaction() as db:
            rows = self.store(db).list_saved('topic')
            output = export_saved('a', 'topic', rows, self.readers(db, 'a')[0])
            quotes = [json.loads(line.split(': ', 1)[1]) for line in output.splitlines()
                      if line.startswith('Exact quotation: ')]
            self.assertEqual(quotes, [self.raw[0], *self.raw])
            self.assertEqual(output.count('Evidence ID: "span"'), 2)
            self.assertIn('source slice 2 of 2', output)
            self.assertIn('PDF page 2', output)
            ris = export_saved('a', 'topic', rows, self.readers(db, 'a')[0], format='ris')
            self.assertEqual(ris.count('TY  - '), 2)
            self.assertNotIn('Separate span note', ris)

    def test_search_matches_second_page_without_joining_source_boundary(self):
        self.add_span()
        with self.transaction() as db:
            store = self.store(db)
            hits = search_topic(store, 'a', 'topic', 'Druhý úryvok')
            self.assertEqual([hit.evidence.evidence_id for hit in hits], ['span'])
            self.assertEqual(search_topic(store, 'a', 'topic', 'end start'), ())
            self.assertEqual(search_topic(store, 'a', 'topic', 'Separate span note'), ())
            self.assertEqual(len(search_topic(store, 'a', 'topic', 'Separate span note',
                                             field='note')), 1)
            self.assertEqual(len(search_topic(store, 'a', 'topic', 'Дослівний', limit=1)), 1)

    def test_last_page_revocation_blocks_first_page_copy(self):
        self.add_span()
        with self.transaction() as db:
            db.execute('UPDATE approval SET active=0 WHERE physical=2')
        with self.assertRaisesRegex(ValueError, 'unverified extraction'):
            self.copy('span', 0)
        self.assertEqual(self.copy('legacy'), self.raw[0])

    def test_metadata_revocation_blocks_both_encodings(self):
        self.add_span()
        with self.transaction() as db:
            db.execute('UPDATE metadata_approval SET active=0')
        for evidence in ('legacy', 'span'):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, 'unattested metadata'):
                self.copy(evidence)

    def test_scope_and_archived_topic_deny_copy(self):
        self.add_span()
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            self.copy('span', workspace='b')
        with self.transaction() as db:
            TopicTree(db, 'a').archive('topic')
        for evidence in ('legacy', 'span'):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, 'archived topic'):
                self.copy(evidence)

    def test_displayed_checksum_and_immutable_storage(self):
        self.add_span()
        with self.assertRaisesRegex(ValueError, 'selection changed'):
            self.copy('legacy', checksum='b' * 64)
        with self.transaction() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE research_evidence SET payload='{}' WHERE id='legacy'")
            self.store(db).edit_note('topic', 'span', 'Changed note')
            self.assertEqual(self.store(db).list_saved('topic')[1].anchors, self.anchors)

    def test_bad_payloads_are_rejected_before_source_access(self):
        payloads = [self.legacy[0] + ' ', '[]', '{}', '{"version":true,"anchors":[]}',
                    '{"version":2,"anchors":[]}', '{"version":1,"anchors":[]}']
        def forbidden(*args):
            self.fail('invalid encoding reached source authority')
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                restore_span('a', payload, digest(payload), forbidden, forbidden)


if __name__ == '__main__':
    unittest.main()
