"""Cross-lane contract regression: real SQLite, no fake historical loader.

Requires merged #21, #26, #29, #34, #38, #40 modules. Synthetic fixture
attestations and authenticated rows stand in for the not-yet-wired repository.
"""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.reader.context import open_context
from carr_library.research.anchored_export import SavedEvidence, export_saved
from carr_library.storage.revisions import Page, Storage


class HistoricalReaderIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'library.db'
        self.store = Storage(self.path)
        self.addCleanup(lambda: self.store.close())
        self.identity = Identity('library-a', 'edition-a', 'a' * 64, 'old')
        self.loc1 = Locator('physical_page', 5, 'iv', True)
        self.loc2 = Locator('physical_page', 6, 'v', False)
        self.raw1 = 'Preface: Київ e\u0301 🚂\n'
        self.raw2 = 'Продовження. End.'
        self.meta = Metadata('Author A', 'Shared title', '1', 'first')
        self.a = make_anchor(self.identity, self.loc1, self.raw1, 9, len(self.raw1), self.meta, text_verified=True)
        self.b = make_anchor(self.identity, self.loc2, self.raw2, 0, 12, self.meta, text_verified=True)
        self.row = SavedEvidence('topic', 'e1', self.a, 'Note, not source text.')
        self.attested = {(self.identity, self.loc1, digest(self.raw1)),
                         (self.identity, self.loc2, digest(self.raw2))}
        self.store.publish_revision('library-a', 'edition-a', 'old', 'a' * 64, 'config',
                                    [Page(self.raw1, 5, 'iv', True), Page(self.raw2, 6, 'v', False)])
        self.store.publish_revision('library-b', 'edition-b', 'other', 'b' * 64, 'config',
                                    [Page(self.raw1, 5, 'iv', True)])
        self.store.publish_revision('library-a', 'edition-a', 'new', 'a' * 64, 'config2',
                                    [Page('New OCR, not the saved quotation.', 5, '5', True)])

    def loader(self, store=None, workspace='library-a'):
        store = self.store if store is None else store
        return historical_loader(store.db, workspace, lambda i, l, h: (i, l, h) in self.attested)

    def check_text(self, out):
        fields = dict(line.split(': ', 1) for line in out.splitlines() if ': ' in line)
        self.assertEqual(json.loads(fields['Exact quotation']), self.a.quote)
        self.assertEqual(json.loads(fields['Research note']), self.row.note)
        self.assertEqual(json.loads(fields['Revision ID']), 'old')
        self.assertIn('printed page iv', json.loads(fields['Citation']))
        self.assertNotIn('New OCR', out)

    def test_old_revision_context_and_export(self):
        self.assertEqual(self.store.db.execute('SELECT revision FROM active WHERE workspace=?', ('library-a',)).fetchone()[0], 'new')
        with self.store.transaction():
            load = self.loader()
            ctx = open_context('library-a', (self.a, self.b), load)
            self.assertEqual(ctx.page(0).raw_text, self.raw1)
            self.assertEqual(ctx.page(1).raw_text, self.raw2)
            self.assertEqual(ctx.clipboard('library-a', 0, 'quote', load), self.a.quote)
            self.assertIn('printed page unverified', ctx.clipboard('library-a', 1, 'citation', load))
            self.check_text(export_saved('library-a', 'topic', [self.row], load))

    def test_reopen_historical_export(self):
        self.store.close()
        self.store = Storage(self.path)
        with self.store.transaction():
            self.check_text(export_saved('library-a', 'topic', [self.row], self.loader()))

    def test_checked_backup_reopens(self):
        path = Path(self.tmp.name) / 'backup.db'
        self.store.backup(path)
        backup = Storage(path)
        try:
            with backup.transaction():
                self.check_text(export_saved('library-a', 'topic', [self.row], self.loader(backup)))
        finally:
            backup.close()

    def test_scope_before_source_read(self):
        with self.store.transaction():
            load = self.loader(workspace='library-b')
            with self.assertRaises(ValueError):
                open_context('library-b', (self.a,), load)
            with self.assertRaises(ValueError):
                export_saved('library-b', 'topic', [self.row], load)
            with self.assertRaises(ValueError):
                load(self.identity, self.loc1)

    def test_attestation_revoked_before_copy(self):
        with self.store.transaction():
            load = self.loader()
            ctx = open_context('library-a', (self.a, self.b), load)
            self.attested.remove((self.identity, self.loc2, digest(self.raw2)))
            with self.assertRaises(ValueError):
                ctx.clipboard('library-a', 0, 'quote', load)
            with self.assertRaises(ValueError):
                export_saved('library-a', 'topic', [SavedEvidence('topic', 'e2', self.b)], load)

    def test_wrong_locator_and_edition(self):
        with self.store.transaction():
            load = self.loader()
            for bad in (replace(self.a, locator=Locator('physical_page', 1)),
                        replace(self.a, identity=replace(self.identity, edition_id='edition-b')),
                        replace(self.a, locator=Locator('physical_page', 5, '4', True))):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    open_context('library-a', (bad,), load)

    def test_no_transaction_or_dirty_read(self):
        load = self.loader()
        with self.assertRaises(ValueError):
            open_context('library-a', (self.a,), load)
        self.store.db.execute('PRAGMA read_uncommitted=1')
        with self.store.transaction(), self.assertRaises(ValueError):
            export_saved('library-a', 'topic', [self.row], load)

    def test_sqlite_prevents_source_mutation(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction():
                self.store.db.execute('UPDATE page SET raw=? WHERE revision=?', ('tampered', 'old'))
        with self.store.transaction():
            self.check_text(export_saved('library-a', 'topic', [self.row], self.loader()))

    def test_unknown_label_never_exported_as_verified(self):
        with self.store.transaction():
            out = export_saved('library-a', 'topic', [SavedEvidence('topic', 'e2', self.b)], self.loader())
            self.assertIn('PDF page 6; printed page unknown', out)
            self.assertNotIn('printed page v', out)


if __name__ == '__main__':
    unittest.main()
