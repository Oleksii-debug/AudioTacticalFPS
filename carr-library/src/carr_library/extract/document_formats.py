"""One capability registry for the isolated, local document import pipeline.

Additional formats are reading previews, never independently verified source
transcriptions. Classification screens an immutable snapshot; the actual parser
still validates the content before any catalog/revision publication.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import time


@dataclass(frozen=True)
class DocumentFormat:
    name: str
    suffixes: tuple[str, ...]
    mime: str
    family: str
    limitations: str


EXTENDED_FORMATS = (
    DocumentFormat('HTML', ('.html', '.htm', '.xhtml'), 'text/html', 'text',
        'Static text only. Scripts, embedded content and remote resources are not run or loaded. '
        'Stylesheet visibility and generated content are not evaluated; text hidden only by a stylesheet may appear.'),
    DocumentFormat('Markdown', ('.md', '.markdown'), 'text/markdown', 'text',
        'Literal text including Markdown notation. Embedded HTML and links remain text; no code or markup is executed.'),
    DocumentFormat('Email', ('.eml',), 'message/rfc822', 'text',
        'Readable MIME message body only. Attachments are not imported or opened; remote images and links are not fetched.'),
    DocumentFormat('FictionBook 2', ('.fb2',), 'application/x-fictionbook+xml', 'xml',
        'Ordered book text and headings. Illustrations and visual layout are not rendered; unsupported structures require review.'),
    DocumentFormat('FictionBook 3', ('.fb3',), 'application/x-fictionbook3', 'xml',
        'Ordered text from the book body. Embedded images and visual layout are not rendered; unsupported structures require review.'),
    DocumentFormat('OpenDocument text', ('.odt',), 'application/vnd.oasis.opendocument.text', 'xml',
        'Ordered text content. No macros, external objects or tracked changes are executed; unsupported structures require review.'),
    DocumentFormat('OpenDocument presentation', ('.odp',), 'application/vnd.oasis.opendocument.presentation', 'xml',
        'Text in presentation order. Slide positions are logical locations, not printed page numbers; media is not played.'),
    DocumentFormat('OpenDocument spreadsheet', ('.ods',), 'application/vnd.oasis.opendocument.spreadsheet', 'xml',
        'Stored cell text in sheet/row order. Formulas are not calculated and unsupported or ambiguous content requires review.'),
    DocumentFormat('PowerPoint', ('.pptx',), 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'xml',
        'Ordered slide text. No presentation playback, macros, linked resources or embedded objects; unsupported content requires review.'),
    DocumentFormat('Excel', ('.xlsx',), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xml',
        'Stored cell text in workbook order. No formula evaluation or macros; ambiguous hidden/formula content requires review.'),
)

FORMAT_BY_SUFFIX = {suffix: format for format in EXTENDED_FORMATS for suffix in format.suffixes}
FORMAT_BY_MIME = {format.mime: format for format in EXTENDED_FORMATS}
EXTENDED_MIMES = frozenset(FORMAT_BY_MIME)
MIME_SUFFIXES = {
    'text/plain': '.txt',
    'application/pdf': '.pdf',
    'application/epub+zip': '.epub',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    **{format.mime: format.suffixes[0] for format in EXTENDED_FORMATS},
}
SUPPORTED_SUFFIXES = frozenset(('.txt', '.pdf', '.docx', '.epub', *FORMAT_BY_SUFFIX))
ISOLATED_SUFFIXES = SUPPORTED_SUFFIXES - {'.txt'}
# Match ReadingDesk's supported text-control payload. Preserve every character
# when a format supplies one very large logical paragraph/message/chapter.
MAX_SEGMENT_CHARACTERS = 2_000_000


def file_dialog_wildcard():
    patterns = ';'.join('*' + suffix for suffix in sorted(SUPPORTED_SUFFIXES))
    return f'Supported documents ({patterns})|{patterns}'


def supported_formats_text():
    lines = [
        'Document formats', '',
        'TXT: strict UTF-8 original text, with exact-source verification.',
        'PDF: native text or installed local OCR. OCR text requires independent review.',
        'DOCX: supported ordered body paragraphs and simple tables; unverified logical segments.',
        'EPUB: supported static XHTML book content in spine order; unverified logical segments.', '',
        'Additional local reading formats:',
    ]
    for format in EXTENDED_FORMATS:
        lines.extend((f'{format.name} ({", ".join(format.suffixes)})', format.limitations, ''))
    lines.extend((
        'All additional formats retain the exact original bytes and use unverified logical segments. '
        'Search and Reading Desk work with those segments. Import does not approve quotations or create printed page numbers.', '',
        'Use Reading Desk (Ctrl+R) for safe extracted reading. Additional document formats are not launched '
        'in external viewers that could execute active content or load remote resources.', '',
        'Encrypted or password-protected inputs are not decrypted. DRM is not bypassed.',
        'AZW/AZW3, CHM, DjVu, legacy DOC/PPT/XLS, LIT, MOBI, PDB/PRC, RTF, TCR, WPD '
        'and standalone image/TIFF import are not handled by this adapter. Convert a copy with a trusted '
        'separately installed tool to a supported format, then import it as a new source. Original pagination '
        'and extraction fidelity must be checked separately. No converter or model is silently downloaded.', '',
        'Resource checks reject oversized, malformed or unsupported documents before publication; '
        'the import report describes the result. TXT/PDF support a bounded 128 MiB streaming profile; '
        'other in-memory formats retain the 64 MiB source limit. Larger TXT retains original form-feed '
        'segments up to 4 MiB each (at most 1000), with exact text and no invented pagination. '
        'Archive, XML, MIME, checkpoint, page and extracted-text budgets remain in force.',
    ))
    return '\n'.join(lines)


def classify_extended(data, suffix):
    """Screen a declared extension without execution or external resolution."""
    format = FORMAT_BY_SUFFIX.get(suffix.casefold() if isinstance(suffix, str) else '')
    if format is None or type(data) is not bytes or not data:
        raise ValueError('Unsupported document format or empty source.')
    if format.family == 'xml' and suffix.casefold() != '.fb2':
        from ..catalog.office_archive import validate_office
        return validate_office(data, suffix.casefold())
    if data.startswith((b'MZ', b'\x7fELF', b'PK\x03\x04', b'PK\x05\x06', b'%PDF-', b'{\\rtf')):
        raise ValueError('The source signature does not match its document format.')
    # MIME envelopes may declare an explicit body encoding; the bounded email
    # parser validates it later. Other text families use a lossless Unicode decode.
    if suffix.casefold() == '.eml':
        prefix = data[:8192]
        if b'\x00' in prefix or b':' not in prefix or b'\n' not in prefix:
            raise ValueError('The source is not a supported email message.')
    elif suffix.casefold() in ('.html', '.htm', '.xhtml'):
        # An HTML meta declaration may legitimately specify a supported legacy
        # charset. Its exact decoding belongs to the parser, never a lossy guess
        # in file preflight. A BOM-marked UTF-16 document is checked there too.
        if not data.startswith((b'\xff\xfe', b'\xfe\xff', b'\x00\x00\xfe\xff')) and any(
                c < 32 and c not in (9, 10, 12, 13) for c in data):
            raise ValueError('Binary control characters are not supported in HTML.')
    else:
        try:
            if data.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
                encoding = 'utf-32'
            else:
                encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
            text = data.decode(encoding, errors='strict')
        except UnicodeError:
            raise ValueError('The document requires UTF-8 or BOM-marked UTF-16/UTF-32 text.') from None
        if any(ord(c) < 32 and c not in '\t\r\n\f' for c in text):
            raise ValueError('Binary control characters are not supported in document text.')
        if suffix.casefold() == '.fb2' and 'FictionBook' not in text[:8192]:
            raise ValueError('The source is not a supported FictionBook document.')
    return format.mime


def _valid_locator(value):
    """Locations are bounded data, never file paths to open or code to run."""
    remaining = 256

    def visit(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 12:
            return False
        if type(item) is str:
            return len(item) <= 1024 and '\x00' not in item
        if type(item) is int:
            return 0 <= item <= 1_000_000_000
        if isinstance(item, (list, tuple)):
            return all(visit(child, depth + 1) for child in item)
        if type(item) is dict:
            return all(type(key) is str and 0 < len(key) <= 64 and '\x00' not in key
                       and visit(child, depth + 1) for key, child in item.items())
        return False

    return (isinstance(value, (dict, list, tuple)) and visit(value)
            and len(json.dumps(value, ensure_ascii=False)) <= 4096)


def extract_document(data, suffix, *, max_bytes=64 * 1024 * 1024,
                     max_chars=10_000_000, cancelled=None, deadline=None):
    """Dispatch to a bounded parser and validate its unverified preview contract."""
    format = FORMAT_BY_SUFFIX.get(suffix.casefold() if isinstance(suffix, str) else '')
    if (format is None or type(data) is not bytes
            or type(max_bytes) is not int or not 0 < len(data) <= max_bytes
            or type(max_chars) is not int or max_chars < 1
            or (cancelled is not None and not callable(cancelled))
            or (deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)))):
        raise ValueError('Invalid document extraction request.')

    def check():
        if cancelled is not None and cancelled():
            raise InterruptedError('Document import cancelled.')
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('Document extraction time limit reached.')

    check()
    if format.family == 'text':
        from .document_formats_text import extract_text_document
        extractor = extract_text_document
    else:
        from .document_formats_xml import extract_xml_document
        extractor = extract_xml_document
    result = extractor(data, suffix.casefold(), max_bytes=max_bytes, max_chars=max_chars,
                       cancelled=cancelled, deadline=deadline)
    check()
    if (not isinstance(result, dict) or result.get('status') != 'review_required'
            or not isinstance(result.get('records'), list) or not 0 < len(result['records']) <= 10_000
            or not isinstance(result.get('provenance'), dict)
            or result['provenance'].get('asset_sha256') != sha256(data).hexdigest()
            or not isinstance(result.get('warnings'), list)):
        raise ValueError('The document parser returned an invalid reading preview.')
    total = 0
    segmented = []
    for record in result['records']:
        check()
        if (not isinstance(record, dict) or not isinstance(record.get('raw_text'), str)
                or record.get('physical_index') is not None or record.get('printed_label') is not None
                or record.get('printed_label_verified') is not False
                or record.get('text_sha256') != sha256(record['raw_text'].encode('utf-8')).hexdigest()
                or not _valid_locator(record.get('logical_locator'))):
            raise ValueError('The document parser returned an invalid logical segment.')
        total += len(record['raw_text'])
        if total > max_chars or len(json.dumps(record['logical_locator'], ensure_ascii=False)) > 4096:
            raise ValueError('The document reading preview exceeds its text or location budget.')
        raw = record['raw_text']
        if len(raw) <= MAX_SEGMENT_CHARACTERS:
            segmented.append(record)
        else:
            start, part = 0, 0
            while start < len(raw):
                check()
                end = min(start + MAX_SEGMENT_CHARACTERS, len(raw))
                if end < len(raw):
                    boundary = raw.rfind('\n', start + MAX_SEGMENT_CHARACTERS // 2, end)
                    if boundary >= 0:
                        end = boundary + 1
                part += 1
                value = raw[start:end]
                segmented.append(dict(record, raw_text=value,
                    text_sha256=sha256(value.encode('utf-8')).hexdigest(),
                    logical_locator={'source_locator': record['logical_locator'],
                                     'part': part, 'characters': [start, end]}))
                start = end
        if len(segmented) > 10_000:
            raise ValueError('The document reading preview exceeds its segment budget.')
    if not any(record['raw_text'].strip() for record in result['records']):
        raise ValueError('The document has no supported readable text.')
    if (len(result['warnings']) > 100 or any(not isinstance(warning, str) or len(warning) > 256
                                           for warning in result['warnings'])):
        raise ValueError('The document parser returned invalid fidelity warnings.')
    if len(segmented) != len(result['records']):
        if any(not _valid_locator(record['logical_locator']) for record in segmented):
            raise ValueError('The document reading preview exceeds its location budget.')
        if len(result['warnings']) == 100:
            raise ValueError('The document reading preview exceeds its fidelity report budget.')
        result = dict(result, records=segmented,
                      warnings=[*result['warnings'], 'long_text_split_without_changing_characters'])
    return result


def extraction_settings(extracted, mime):
    """Persist format, extraction provenance and logical structure with revision."""
    format = FORMAT_BY_MIME[mime]
    return {
        'adapter': 'ordered-document-v1', 'kind': 'logical_segment',
        'format': format.name, 'provenance': extracted['provenance'],
        'logical_locators': [record['logical_locator'] for record in extracted['records']],
        'warnings': extracted['warnings'],
    }
