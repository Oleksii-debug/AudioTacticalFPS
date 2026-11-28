"""Fixed, offline DOCX layout worker over the canonical ordered extractor.

This worker does not translate, fetch resources, open an external application or
publish a user file. The host supplies completed ordinary translation receipts.
Only bounded typed text replacements enter the existing DOCX body.
"""
from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_REQUEST = 16 * 1024 * 1024
MAX_REPLY = 16 * 1024 * 1024
MAX_CHARACTERS = 2_000_000
MAX_UNITS = 2048
PROFILE = 'carr-docx-body-layout/1'
NOTICE = (
    'Machine translation of body paragraphs and simple table cells. Paragraph, '
    'table and style definitions are retained. Ordinary mixed runs use the first '
    'textual run formatting; original per-word emphasis and line/page layout '
    'are not reconstructed. A single whole-paragraph hyperlink is retained; '
    'mixed hyperlink text requires review and is refused. Other package members '
    'are copied byte-for-byte, including eligible media, relationship and style '
    'parts; their text is not translated. No quotation authority is created.'
)


class LayoutError(ValueError):
    """A fixed explanation with no original document text or local path."""


def _digest(data):
    return sha256(data).hexdigest()


def _package(data):
    from ..extract.docx_ordered import extract_docx
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    if type(data) is not bytes or not 0 < len(data) <= MAX_ARCHIVE:
        raise LayoutError('Choose a DOCX within the existing 64 MiB input budget.')
    # This is the authoritative parser admission. Do not relax its rejected
    # constructs, hidden-style rule, table rules or source locator semantics.
    extracted = extract_docx(data)
    document = Document(BytesIO(data))
    with ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        if any(name.casefold().startswith(('_xmlsignatures/', 'word/activex/'))
               or 'vbaproject' in name.casefold() for name in names):
            raise LayoutError('Signed, macro or ActiveX packages are not supported for translation.')
        types = archive.read('[Content_Types].xml').lower()
        if b'macroenabled' in types or b'vbaproject' in types:
            raise LayoutError('Macro-enabled packages are not supported for translation.')
        if str(document.part.partname) != '/word/document.xml':
            raise LayoutError('Choose an ordinary DOCX main document part.')
    if extracted['provenance'].get('source_profile') == 'carr-docx-rich-source/1':
        return document, None, extracted['records']
    paragraphs = []
    for index, block in enumerate(document.iter_inner_content(), 1):
        if isinstance(block, Paragraph):
            paragraphs.append(([index], block))
        elif isinstance(block, Table):
            # extract_docx already rejected merged/nested/nonrectangular cells.
            for row_index, row in enumerate(block.rows, 1):
                for cell_index, cell in enumerate(row.cells, 1):
                    for paragraph_index, paragraph in enumerate(cell.paragraphs, 1):
                        paragraphs.append(([index, row_index, cell_index, paragraph_index], paragraph))
        else:
            raise LayoutError('The selected DOCX body structure requires separate review.')
    records = extracted['records']
    if len(paragraphs) != len(records):
        raise LayoutError('The canonical DOCX locator inventory changed.')
    total = 0
    for (locator, paragraph), record in zip(paragraphs, records):
        if (record['logical_locator'] != locator or record['raw_text'] != paragraph.text
                or record['text_sha256'] != _digest(paragraph.text.encode('utf-8'))):
            raise LayoutError('The DOCX mapping differs from canonical retained text.')
        total += len(paragraph.text)
    if total > MAX_CHARACTERS:
        raise LayoutError('Choose a DOCX body within the 2,000,000-character translation budget.')
    return document, paragraphs, records


def _text_runs(paragraph):
    from docx.text.hyperlink import Hyperlink
    from docx.text.run import Run
    contents = tuple(paragraph.iter_inner_content())
    hyperlinks = tuple(item for item in contents if isinstance(item, Hyperlink))
    ordinary = tuple(item for item in contents if isinstance(item, Run))
    if hyperlinks:
        if len(hyperlinks) != 1 or any(run.text for run in ordinary):
            raise LayoutError('Mixed hyperlink paragraphs are refused before translation approval.')
        runs = tuple(hyperlinks[0].runs)
        mode = 'one-whole-paragraph-hyperlink'
    else:
        runs, mode = ordinary, 'first-textual-run-style'
    if ''.join(run.text for run in runs) != paragraph.text:
        raise LayoutError('The paragraph has unsupported inline text structure.')
    if paragraph.text.strip() and not any(run.text for run in runs):
        raise LayoutError('The paragraph has no replaceable ordinary text run.')
    return runs, mode


def describe(data):
    _, paragraphs, records = _package(data)
    if paragraphs is None:
        from .translation_docx_rich import describe as describe_rich
        return describe_rich(data)
    rows, selected = [], 0
    for ordinal, ((locator, paragraph), record) in enumerate(zip(paragraphs, records), 1):
        runs, mode = _text_runs(paragraph)
        active = bool(paragraph.text.strip())
        selected += int(active)
        if selected > MAX_UNITS:
            raise LayoutError('Choose a DOCX with at most 2,048 nonempty body or table paragraphs.')
        rows.append({'ordinal': ordinal, 'locator': locator, 'text': paragraph.text,
                     'sha256': record['text_sha256'], 'selected': active, 'inline_mode': mode})
    if not selected:
        raise LayoutError('The DOCX has no nonempty body paragraphs.')
    return {'schema': PROFILE, 'source_sha256': _digest(data), 'source_size': len(data),
            'records': rows, 'characters': sum(len(row['text']) for row in rows),
            'selected': selected, 'notice': NOTICE, 'quotation_authority': False}


def render(data, replacements):
    from docx.opc.oxml import serialize_part_xml
    from .models import canonical, text
    mapping = describe(data)
    if mapping['schema'] == 'carr-docx-rich-layout/1':
        from .translation_docx_rich import render as render_rich
        return render_rich(data, replacements)
    if type(replacements) is not list or len(replacements) != mapping['selected']:
        raise LayoutError('A completed translation is required for every selected body paragraph.')
    expected = [row for row in mapping['records'] if row['selected']]
    translated, total = {}, 0
    for row, replacement in zip(expected, replacements):
        if (type(replacement) is not dict
                or set(replacement) != {'ordinal', 'source_sha256', 'text'}
                or type(replacement['ordinal']) is not int
                or replacement['ordinal'] != row['ordinal']
                or replacement['source_sha256'] != row['sha256']):
            raise LayoutError('Completed translations do not match the exact DOCX paragraph inventory.')
        value = text(replacement['text'], 'completed paragraph', maximum=MAX_CHARACTERS)
        total += len(value)
        if total > MAX_CHARACTERS:
            raise LayoutError('The translated body exceeds its character budget.')
        translated[row['ordinal']] = value
    document, paragraphs, _ = _package(data)
    for ordinal, (_, paragraph) in enumerate(paragraphs, 1):
        if ordinal not in translated:
            continue
        runs, _ = _text_runs(paragraph)
        first = next(run for run in runs if run.text)
        # Run.text preserves rPr. Paragraph.text would discard every run style.
        # A reviewed mixed-run limitation is explicit in the admission notice.
        for run in runs:
            run.text = translated[ordinal] if run is first else ''
    body = serialize_part_xml(document.part.element)
    if len(body) > 32 * 1024 * 1024:
        raise LayoutError('The translated DOCX XML exceeds the package member budget.')
    target = BytesIO()
    preserved = {}
    with ZipFile(BytesIO(data)) as original, ZipFile(target, 'w') as output:
        output.comment = original.comment
        for info in original.infolist():
            raw = body if info.filename == 'word/document.xml' else original.read(info)
            if info.filename != 'word/document.xml':
                preserved[info.filename] = _digest(raw)
            output.writestr(info, raw)
            if target.tell() > MAX_ARCHIVE:
                raise LayoutError('The translated DOCX exceeds its output byte budget.')
    result = target.getvalue()
    if len(result) > MAX_ARCHIVE:
        raise LayoutError('The translated DOCX exceeds its output byte budget.')
    # Replay the same authoritative parser over the derivative. Empty original
    # paragraphs are unchanged, and no translation may manufacture new units.
    after = describe(result)
    if len(after['records']) != len(mapping['records']):
        raise LayoutError('The exported DOCX paragraph inventory changed.')
    for before, current in zip(mapping['records'], after['records']):
        if (current['locator'] != before['locator']
                or current['text'] != translated.get(before['ordinal'], before['text'])):
            raise LayoutError('The exported DOCX differs from the accepted translated text.')
    with ZipFile(BytesIO(result)) as archive:
        if any(_digest(archive.read(name)) != digest for name, digest in preserved.items()):
            raise LayoutError('An unselected DOCX package member changed.')
    return result, {'schema': 'carr-docx-layout-output/1', 'source_sha256': mapping['source_sha256'],
        'mapping_sha256': _digest(canonical(mapping).encode('utf-8')),
        'output_sha256': _digest(result), 'output_bytes': len(result),
        'changed_members': ['word/document.xml'], 'preserved_members': preserved,
        'paragraphs': len(translated), 'quotation_authority': False, 'notice': NOTICE}


def _main():
    from ..catalog.intake import source_snapshot
    from ..durable_state import atomic_json, read_json_snapshot
    from ..ingest.worker import _resource_limits
    if len(sys.argv) != 5:
        raise LayoutError('Use the fixed formatted-document worker protocol.')
    request_path, request_sha, expected_origin, timeout_text = sys.argv[1:]
    if Path(__file__).resolve() != Path(expected_origin).resolve():
        raise LayoutError('The formatted-document worker origin differs from the host.')
    timeout = float(timeout_text)
    if not 1 <= timeout <= 120:
        raise LayoutError('The formatted-document worker deadline is invalid.')
    _resource_limits(timeout)
    def offline(event, _arguments):
        if event.startswith('socket.'):
            raise PermissionError('The formatted-document worker is offline.')
    sys.addaudithook(offline)
    request, observed = read_json_snapshot(request_path, max_bytes=MAX_REQUEST)
    if (observed != request_sha or type(request) is not dict
            or set(request) != {'schema', 'action', 'source_sha256', 'replacements'}
            or request['schema'] != 'carr-docx-layout-request/1'
            or request['action'] not in ('describe', 'render')):
        raise LayoutError('The formatted-document request changed or is invalid.')
    source = source_snapshot(Path.cwd(), 'source.docx')
    if source.digest != request['source_sha256']:
        raise LayoutError('The private DOCX snapshot changed.')
    if request['action'] == 'describe':
        if request['replacements'] is not None:
            raise LayoutError('Description does not accept translated text.')
        response = describe(source.data)
    else:
        data, response = render(source.data, request['replacements'])
        with Path('output.docx').open('xb') as output:
            output.write(data)
            output.flush()
            import os
            os.fsync(output.fileno())
    raw = (json.dumps(response, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > MAX_REPLY:
        raise LayoutError('The formatted-document response exceeds its byte budget.')
    atomic_json(Path('response.json'), response)
    sys.stdout.buffer.write(b'{"schema":"carr-docx-layout-worker/1","status":"complete"}\n')


if __name__ == '__main__':
    try:
        _main()
    except Exception as error:
        # Parser exceptions may contain source text; only this module's fixed
        # explanations may cross the diagnostic boundary.
        message = str(error) if type(error) is LayoutError else (
            'The DOCX is unsupported, damaged or exceeds a formatting budget.')
        sys.stdout.buffer.write((json.dumps({'schema': 'carr-docx-layout-worker/1',
            'status': 'failed', 'message': message}, ensure_ascii=False) + '\n').encode('utf-8'))
        raise SystemExit(1)
