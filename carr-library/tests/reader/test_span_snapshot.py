import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.research.span_snapshot import MAX_PAGES, seal_span, restore_span


class SpanSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity('library-a', 'edition-1', 'a'*64, 'revision-old')
        self.metadata = Metadata('Synthetic Author', 'Synthetic Title', 'II', '1')
        self.raw = {1: 'Before: Київ і\n', 2: 'železnica — after.'}
        self.loc = {1: Locator('physical_page', 1, 'iv', True),
                    2: Locator('physical_page', 2, 'v', False)}
        self.anchors = tuple(make_anchor(self.identity, self.loc[n], self.raw[n],
                             8 if n == 1 else 0, len(self.raw[n]) if n == 1 else 9,
                             self.metadata, text_verified=True) for n in (1, 2))

    def load(self, identity, locator):
        if identity != self.identity or self.loc[locator.index] != locator:
            raise ValueError('unknown historical source')
        return identity, locator, self.raw[locator.index], True

    def attest(self, identity, metadata):
        return identity == self.identity and metadata == self.metadata

    def seal(self, anchors=None):
        return seal_span('library-a', self.anchors if anchors is None else anchors,
                         self.load, self.attest)

    def restore(self, payload, checksum, workspace='library-a', load=None, attest=None):
        return restore_span(workspace, payload, checksum, load or self.load, attest or self.attest)

    def test_exact_pages_and_labels(self):
        context = self.restore(*self.seal())
        self.assertEqual([p.quote for p in context.pages], ['Київ і\n', 'železnica'])
        self.assertIn('p. iv (physical page 1)', context.pages[0].citation)
        self.assertIn('printed page unverified', context.pages[1].citation)
        self.assertEqual(context.clipboard('library-a', 0, 'quote', self.load), 'Київ і\n')

    def test_sqlite_reopen_note_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'synthetic.db'
            payload, checksum = self.seal()
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE fixture(payload TEXT, checksum TEXT, note TEXT)')
                db.execute('INSERT INTO fixture VALUES(?,?,?)', (payload, checksum, 'initial'))
                db.execute('UPDATE fixture SET note=?', ('edited',))
            db.close()
            with sqlite3.connect(path) as db:
                saved, check, note = db.execute('SELECT * FROM fixture').fetchone()
                self.assertEqual(note, 'edited')
                self.assertEqual((saved, check), (payload, checksum))
                self.assertEqual(self.restore(saved, check).pages[1].quote, 'železnica')
            db.close()

    def test_scope_rejected_before_load(self):
        def forbidden(*args):
            self.fail('foreign workspace reached source loader')
        with self.assertRaises(ValueError):
            self.restore(*self.seal(), workspace='library-b', load=forbidden)

    def test_missing_historical_revision(self):
        def missing(*args):
            raise ValueError('missing revision')
        with self.assertRaises(ValueError):
            self.restore(*self.seal(), load=missing)

    def test_drift_on_second_page(self):
        payload, check = self.seal()
        self.raw[2] += '!'
        with self.assertRaises(ValueError):
            self.restore(payload, check)

    def test_unattested_metadata(self):
        with self.assertRaises(ValueError):
            self.restore(*self.seal(), attest=lambda *_: False)

    def test_invalid_spans(self):
        for anchors in ((), self.anchors[::-1], (self.anchors[0],)*2,
                        (replace(self.anchors[0], end=9), self.anchors[1])):
            with self.subTest(anchors=anchors), self.assertRaises(ValueError):
                self.seal(anchors)

    def test_bounded_iterator(self):
        from itertools import repeat
        with self.assertRaises(ValueError):
            self.seal(repeat(self.anchors[0], MAX_PAGES + 100))

    def test_payload_corruption_and_unknown_version(self):
        payload, check = self.seal()
        with self.assertRaises(ValueError):
            self.restore(payload + ' ', check)
        data = json.loads(payload)
        data['version'] = 2
        changed = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.assertRaises(ValueError):
            self.restore(changed, digest(changed))
        with self.assertRaises(ValueError):
            self.restore(payload + ' ', digest(payload + ' '))

    def test_single_page_compatible_context(self):
        context = self.restore(*self.seal(self.anchors[:1]))
        self.assertEqual(len(context.pages), 1)
        self.assertEqual(context.pages[0].anchor, self.anchors[0])


if __name__ == '__main__':
    unittest.main()
