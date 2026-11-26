"""Trusted-local DOCX body preview using python-docx; never a verified citation."""
from hashlib import sha256
from io import BytesIO
from importlib.metadata import version

from carr_library.catalog.office_archive import validate_office


def extract_docx(data: bytes):
    """Return separate logical paragraphs, never invented page numbers.

    Body-only preview; unsupported structures fail closed. Use an isolated worker
    before processing untrusted documents. Publication requires separate review.
    """
    if type(data) is not bytes or not 0 < len(data) <= 64 * 1024 * 1024:
        raise ValueError('invalid source snapshot')
    validate_office(data, '.docx')
    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    if version('python-docx') != '1.2.0':
        raise RuntimeError('unreviewed python-docx version')
    doc = Document(BytesIO(data))
    # The upstream paragraph/run iterators intentionally skip these constructs.
    # They must not turn a sentence containing an equation, symbol or wrapped
    # text into a shorter accepted preview. Match namespace URIs, since the
    # source document is free to choose its own XML prefixes.
    unsupported = {qn(name) for name in (
        'w:ins', 'w:del', 'w:sdt', 'w:moveFrom', 'w:moveTo', 'w:altChunk',
        'w:drawing', 'w:pict', 'w:object', 'w:fldSimple', 'w:fldChar',
        'w:footnoteReference', 'w:endnoteReference', 'w:vanish',
        'w:commentRangeStart', 'w:customXml', 'w:smartTag', 'w:ruby', 'w:sym',
        'w:softHyphen', 'w:dir', 'w:bdo',
        'm:oMath', 'm:oMathPara',
    )}
    # python-docx does not select an mc:Choice or mc:Fallback for us.
    unsupported.add('{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent')
    if any(element.tag in unsupported for element in doc.element.iter()):
        raise ValueError('unsupported body structure requires review')
    records = []
    total = 0

    def add(paragraph, locator):
        nonlocal total
        text = paragraph.text
        total += len(text)
        if len(records) >= 10000 or total > 10_000_000:
            raise ValueError('document text budget exceeded')
        records.append(dict(logical_locator=locator, raw_text=text,
                            text_sha256=sha256(text.encode('utf-8')).hexdigest(),
                            physical_index=None, printed_label=None,
                            printed_label_verified=False))

    for i, block in enumerate(doc.iter_inner_content(), 1):
        if isinstance(block, Paragraph):
            add(block, [i])
        elif isinstance(block, Table):
            if block._tbl.xpath('.//w:gridSpan | .//w:vMerge | .//w:tbl'):
                raise ValueError('complex table requires review')
            for j, row in enumerate(block.rows, 1):
                if row.grid_cols_before or row.grid_cols_after:
                    raise ValueError('nonrectangular table requires review')
                for k, cell in enumerate(row.cells, 1):
                    for n, paragraph in enumerate(cell.paragraphs, 1):
                        add(paragraph, [i, j, k, n])
        else:
            raise ValueError('unknown block type')
    if not any(r['raw_text'].strip() for r in records):
        raise ValueError('empty body preview')
    return dict(status='review_required', records=records,
                warnings=['body_only', 'logical_locator_not_page', 'layout_not_verified'],
                provenance=dict(asset_sha256=sha256(data).hexdigest(),
                                engine='python-docx', version='1.2.0'))
