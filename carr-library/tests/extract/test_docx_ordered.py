from io import BytesIO
from hashlib import sha256
import unittest
from docx import Document
from docx.oxml import OxmlElement
from carr_library.extract.docx_ordered import extract_docx


def bytes_of(doc):
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


class OrderedDocxTests(unittest.TestCase):
    def test_order_and_exact_text(self):
        doc = Document()
        doc.add_paragraph('  Перший\tабзац ')
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = 'Český text'
        table.cell(0, 1).text = 'Ελληνικά'
        doc.add_paragraph('Last paragraph')
        data = bytes_of(doc)
        result = extract_docx(data)
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(result['provenance']['asset_sha256'], sha256(data).hexdigest())
        self.assertEqual([r['raw_text'] for r in result['records']],
                         ['  Перший\tабзац ', 'Český text', 'Ελληνικά', 'Last paragraph'])
        self.assertEqual([r['logical_locator'] for r in result['records']],
                         [[1], [2, 1, 1, 1], [2, 1, 2, 1], [3]])
        for r in result['records']:
            self.assertIsNone(r['physical_index'])
            self.assertIsNone(r['printed_label'])
            self.assertFalse(r['printed_label_verified'])
            self.assertEqual(r['text_sha256'], sha256(r['raw_text'].encode()).hexdigest())

    def test_merged_table(self):
        doc = Document()
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).merge(table.cell(0, 1)).text = 'Merged'
        with self.assertRaisesRegex(ValueError, 'complex table'):
            extract_docx(bytes_of(doc))

    def test_nested_table(self):
        doc = Document()
        doc.add_table(rows=1, cols=1).cell(0, 0).add_table(rows=1, cols=1)
        with self.assertRaisesRegex(ValueError, 'complex table'):
            extract_docx(bytes_of(doc))

    def test_revision(self):
        doc = Document()
        doc.add_paragraph('Before')._p.append(OxmlElement('w:ins'))
        with self.assertRaisesRegex(ValueError, 'unsupported body'):
            extract_docx(bytes_of(doc))

    def test_corrupt(self):
        with self.assertRaises(ValueError):
            extract_docx(b'PK broken')

    def test_empty(self):
        with self.assertRaisesRegex(ValueError, 'empty body'):
            extract_docx(bytes_of(Document()))


if __name__ == '__main__':
    unittest.main()
