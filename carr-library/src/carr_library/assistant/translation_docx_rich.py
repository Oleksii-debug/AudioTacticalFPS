"""Rich DOCX layout fragments over canonical records and fixed inline objects.

No provider, store or process is introduced. The existing DOCX worker calls
these functions only after its canonical rich-source admission.
"""
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile

PROFILE = 'carr-docx-rich-layout/1'
NOTICE = (
    'Machine translation of canonical body and independent footnote/endnote text. '
    'Inline note markers, ordinary embedded PNG/JPEG pictures, relationships, '
    'paragraph/table structure and style definitions are retained. Text is split '
    'at fixed inline markers and hyperlink boundaries; each fragment uses its '
    'first textual run formatting. Per-word emphasis and Word page/visual layout '
    'are not reconstructed. Note numbers are not invented or inserted into quote '
    'text. Picture bytes and unselected package members remain unchanged; image '
    'alternatives and text in headers or other parts are not translated. Floating '
    'pictures, linked images, fields, tracked/hidden/custom text and complex '
    'tables remain refused. No quotation authority is created.'
)
MAX_UNITS = 2048
MAX_CHARACTERS = 2_000_000
PARTS = frozenset(('word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml'))


def _sha(value):
    return sha256(value).hexdigest()


def _error():
    raise ValueError('The rich DOCX mapping is invalid or no longer matches its canonical source.')


def _digest(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _blocks(value):
    return (type(value) is list and len(value) in (1, 4)
            and all(type(n) is int and 1 <= n <= 10000 for n in value))


def _locator(value):
    if _blocks(value):
        return True
    return (type(value) is dict and set(value) == {'part', 'kind', 'note_id', 'block'}
            and value['kind'] in ('footnote', 'endnote')
            and value['part'] == 'word/' + value['kind'] + 's.xml'
            and type(value['note_id']) is int and 0 <= value['note_id'] <= 2147483647
            and _blocks(value['block']))


def _media(value):
    from pathlib import PurePosixPath
    if (type(value) is not dict or set(value) != {'part', 'sha256', 'size', 'media_type', 'width', 'height'}
            or type(value['part']) is not str or len(value['part']) > 4096
            or not value['part'].startswith('word/media/') or value['part'].count('/') != 2
            or '\\' in value['part'] or ':' in value['part']
            or str(PurePosixPath(value['part'])) != value['part']
            or any(piece in ('', '.', '..') for piece in value['part'].split('/'))
            or not _digest(value['sha256'])
            or type(value['size']) is not int or not 0 < value['size'] <= 32 * 1024 * 1024
            or value['media_type'] not in ('image/png', 'image/jpeg')
            or any(type(value[key]) is not int or not 0 < value[key] <= 100000 for key in ('width', 'height'))
            or value['width'] * value['height'] > 100_000_000):
        _error()


def validate_mapping(value):
    from .translation_layout_worker import MAX_ARCHIVE
    fields = {'schema', 'source_sha256', 'source_size', 'records', 'characters', 'selected',
              'notice', 'quotation_authority', 'parts', 'notes', 'media'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != PROFILE
            or value['notice'] != NOTICE or value['quotation_authority'] is not False
            or not _digest(value['source_sha256'])
            or type(value['source_size']) is not int or not 0 < value['source_size'] <= MAX_ARCHIVE
            or type(value['records']) is not list or not 1 <= len(value['records']) <= 10000
            or type(value['parts']) is not list or not value['parts']
            or any(type(part) is not str or part not in PARTS for part in value['parts'])
            or value['parts'] != sorted(set(value['parts'])) or 'word/document.xml' not in value['parts']
            or type(value['notes']) is not list or len(value['notes']) > 2048
            or type(value['media']) is not list or len(value['media']) > 2048):
        _error()
    media = {}
    for item in value['media']:
        _media(item)
        if item['part'] in media:
            _error()
        media[item['part']] = item
    total = units = marker_count = 0
    references, note_records = [], {}
    seen_locators = set()
    for ordinal, row in enumerate(value['records'], 1):
        if (type(row) is not dict or set(row) != {'ordinal', 'locator', 'text', 'sha256',
                    'selected', 'inline_mode', 'units', 'markers'}
                or type(row['ordinal']) is not int or row['ordinal'] != ordinal
                or not _locator(row['locator']) or type(row['text']) is not str or '\0' in row['text']
                or row['sha256'] != _sha(row['text'].encode('utf-8', 'strict'))
                or type(row['selected']) is not bool or row['selected'] != bool(row['text'].strip())
                or row['inline_mode'] != 'fixed-object-fragments'
                or type(row['units']) is not list or type(row['markers']) is not list):
            _error()
        locator_key = json.dumps(row['locator'], sort_keys=True, separators=(',', ':'))
        if locator_key in seen_locators:
            _error()
        seen_locators.add(locator_key)
        if type(row['locator']) is dict:
            key = row['locator']['kind'], row['locator']['note_id']
            note_records.setdefault(key, []).append(row)
            if row['locator']['part'] not in value['parts']:
                _error()
        previous = 0
        for unit in row['units']:
            units += 1
            if (type(unit) is not dict or set(unit) != {'ordinal', 'start', 'end', 'sha256', 'inline_mode'}
                    or type(unit['ordinal']) is not int or unit['ordinal'] != units
                    or type(unit['start']) is not int or type(unit['end']) is not int
                    or not previous <= unit['start'] < unit['end'] <= len(row['text'])
                    or row['text'][previous:unit['start']].strip()
                    or not row['text'][unit['start']:unit['end']].strip()
                    or unit['sha256'] != _sha(row['text'][unit['start']:unit['end']].encode('utf-8'))
                    or unit['inline_mode'] not in ('first-textual-run-style', 'one-whole-fragment-hyperlink')):
                _error()
            previous = unit['end']
        if row['text'][previous:].strip():
            _error()
        previous_marker = 0
        for marker in row['markers']:
            marker_count += 1
            if (type(marker) is not dict or type(marker.get('offset')) is not int
                    or not previous_marker <= marker['offset'] <= len(row['text'])
                    or any(unit['start'] < marker['offset'] < unit['end'] for unit in row['units'])):
                _error()
            previous_marker = marker['offset']
            if marker.get('kind') in ('footnote', 'endnote'):
                if (set(marker) != {'offset', 'kind', 'note_id'} or type(row['locator']) is not list
                        or type(marker['note_id']) is not int or not 0 <= marker['note_id'] <= 2147483647):
                    _error()
                references.append({'kind': marker['kind'], 'note_id': marker['note_id'],
                                   'source_record': ordinal, 'source_offset': marker['offset']})
            elif marker.get('kind') == 'note-label':
                if (set(marker) != {'offset', 'kind', 'note_kind', 'note_id'}
                        or type(row['locator']) is not dict
                        or type(marker['note_id']) is not int
                        or marker['note_kind'] != row['locator']['kind']
                        or marker['note_id'] != row['locator']['note_id']):
                    _error()
            elif marker.get('kind') == 'boundary':
                if (set(marker) != {'offset', 'kind', 'tag', 'xml_sha256'}
                        or marker['tag'] not in ('bookmarkStart', 'bookmarkEnd', 'proofErr')
                        or not _digest(marker['xml_sha256'])):
                    _error()
            elif marker.get('kind') == 'image':
                extra = {'offset', 'kind', 'relationship_id', 'xml_sha256'}
                if (set(marker) != extra | {'part', 'sha256', 'size', 'media_type', 'width', 'height'}
                        or not _digest(marker['xml_sha256']) or type(marker['relationship_id']) is not str
                        or not 0 < len(marker['relationship_id']) <= 512
                        or {key: val for key, val in marker.items() if key not in extra} != media.get(marker['part'])):
                    _error()
            else:
                _error()
        total += len(row['text'])
    if references != value['notes'] or len({(n['kind'], n['note_id']) for n in references}) != len(references):
        _error()
    if set(note_records) != {(n['kind'], n['note_id']) for n in references}:
        _error()
    for rows in note_records.values():
        labels = [(i, marker) for i, row in enumerate(rows)
                  for marker in row['markers'] if marker['kind'] == 'note-label']
        if len(labels) != 1 or labels[0][0] != 0 or rows[0]['text'][:labels[0][1]['offset']].strip():
            _error()
    if (marker_count > 4096 or type(value['characters']) is not int or value['characters'] != total
            or total > MAX_CHARACTERS or type(value['selected']) is not int
            or value['selected'] != units or not 1 <= units <= MAX_UNITS):
        _error()
    return value


def _load(data):
    from docx import Document
    from ..extract.docx_ordered import extract_docx
    from ..extract.docx_rich import SOURCE_PROFILE, inspect_document
    extracted = extract_docx(data)
    if extracted['provenance'].get('source_profile') != SOURCE_PROFILE:
        _error()
    document = Document(BytesIO(data))
    result = inspect_document(document, data)
    if result['records'] != extracted['records'] or result['structure'] != extracted['rich_structure']:
        _error()
    return document, result


def _mapping(data, result):
    rows, selected = [], 0
    for ordinal, (record, entry) in enumerate(zip(result['records'], result['entries']), 1):
        units = []
        for segment in entry.segments:
            value = record['raw_text'][segment['start']:segment['end']]
            if not value.strip():
                continue
            selected += 1
            units.append({'ordinal': selected, 'start': segment['start'], 'end': segment['end'],
                          'sha256': _sha(value.encode('utf-8')), 'inline_mode': segment['inline_mode']})
        rows.append({'ordinal': ordinal, 'locator': record['logical_locator'], 'text': record['raw_text'],
            'sha256': record['text_sha256'], 'selected': bool(record['raw_text'].strip()),
            'inline_mode': 'fixed-object-fragments', 'units': units, 'markers': entry.markers})
    return validate_mapping({'schema': PROFILE, 'source_sha256': _sha(data), 'source_size': len(data),
        'records': rows, 'characters': sum(len(row['text']) for row in rows), 'selected': selected,
        'notice': NOTICE, 'quotation_authority': False,
        'parts': sorted(['word/document.xml'] + [part['path'] for part in result['parts'].values()]),
        'notes': result['structure']['notes'], 'media': result['structure']['media']})


def describe(data):
    _, result = _load(data)
    return _mapping(data, result)


def _replace(nodes, value):
    from docx.oxml import OxmlElement
    from docx.text.run import Run
    # Generate ordinary text/tab/line-break children with the pinned text API,
    # then replace only reviewed textual children. Marker/drawing and run-style
    # nodes are not deleted, moved or reconstructed.
    temporary = OxmlElement('w:r')
    Run(temporary, None).text = value
    first = nodes[0]
    parent, index = first.getparent(), first.getparent().index(first)
    for child in tuple(temporary):
        parent.insert(index, child)
        index += 1
    for node in nodes:
        node.getparent().remove(node)


def render(data, replacements):
    from docx.opc.oxml import serialize_part_xml
    from .models import canonical, text
    from .translation_layout_worker import MAX_ARCHIVE
    document, result = _load(data)
    mapping = _mapping(data, result)
    expected = [unit for row in mapping['records'] for unit in row['units']]
    if type(replacements) is not list or len(replacements) != len(expected):
        _error()
    values, total = {}, 0
    for unit, replacement in zip(expected, replacements):
        if (type(replacement) is not dict or set(replacement) != {'ordinal', 'source_sha256', 'text'}
                or type(replacement['ordinal']) is not int or replacement['ordinal'] != unit['ordinal']
                or replacement['source_sha256'] != unit['sha256']):
            _error()
        value = text(replacement['text'], 'completed rich DOCX fragment', maximum=MAX_CHARACTERS)
        total += len(value)
        if total > MAX_CHARACTERS:
            _error()
        values[unit['ordinal']] = value
    for row, entry in zip(mapping['records'], result['entries']):
        for unit in row['units']:
            segment = next(segment for segment in entry.segments
                           if (segment['start'], segment['end']) == (unit['start'], unit['end']))
            _replace(segment['nodes'], values[unit['ordinal']])
    changed = {'word/document.xml': serialize_part_xml(document.part.element)}
    changed.update({part['path']: serialize_part_xml(part['root']) for part in result['parts'].values()})
    if any(len(raw) > 32 * 1024 * 1024 for raw in changed.values()):
        _error()
    output, preserved = BytesIO(), {}
    with ZipFile(BytesIO(data)) as original, ZipFile(output, 'w') as archive:
        archive.comment = original.comment
        for item in original.infolist():
            raw = changed.get(item.filename)
            if raw is None:
                raw = original.read(item)
                preserved[item.filename] = _sha(raw)
            archive.writestr(item, raw)
            if output.tell() > MAX_ARCHIVE:
                _error()
    rendered = output.getvalue()
    if len(rendered) > MAX_ARCHIVE:
        _error()
    after = describe(rendered)
    if len(after['records']) != len(mapping['records']) or after['media'] != mapping['media']:
        _error()
    for before, current in zip(mapping['records'], after['records']):
        pieces, position = [], 0
        for unit in before['units']:
            pieces.extend((before['text'][position:unit['start']], values[unit['ordinal']]))
            position = unit['end']
        pieces.append(before['text'][position:])
        if current['locator'] != before['locator'] or current['text'] != ''.join(pieces):
            _error()
        expected_markers = []
        for marker in before['markers']:
            delta = sum(len(values[unit['ordinal']]) - (unit['end'] - unit['start'])
                        for unit in before['units'] if unit['end'] <= marker['offset'])
            expected_markers.append({**marker, 'offset': marker['offset'] + delta})
        if current['markers'] != expected_markers:
            _error()
    with ZipFile(BytesIO(rendered)) as archive:
        if any(_sha(archive.read(name)) != digest for name, digest in preserved.items()):
            _error()
    return rendered, {'schema': 'carr-docx-layout-output/1', 'source_sha256': mapping['source_sha256'],
        'mapping_sha256': _sha(canonical(mapping).encode('utf-8')),
        'output_sha256': _sha(rendered), 'output_bytes': len(rendered),
        'changed_members': sorted(changed), 'preserved_members': preserved,
        'paragraphs': len(values), 'quotation_authority': False, 'notice': NOTICE}
