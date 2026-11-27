"""Bounded document structure and exact retained-source selection for translation.

This is a derivative-text adapter to the existing ReadingDesk and translation
service. It does not interpret Word/PDF layout or create quotation authority.
"""
from dataclasses import asdict
from hashlib import sha256
import html
import re
from time import monotonic

from .models import canonical, text
from ..reader.reading_state import _location
from ..reader.legacy_reading import MAX_SOURCE_ORDINAL

SCHEMA = 'carr-translation-structure/1'
MAX_DOCUMENTS = 32
MAX_SECTIONS = 10000
MAX_CHARACTERS = 2_000_000


def _origin(value, fragment):
    if value is None:
        return
    if (type(value) is not dict or set(value) != {'location', 'ordinal', 'start', 'end'}
            or type(value['ordinal']) is not int or not 1 <= value['ordinal'] <= MAX_SOURCE_ORDINAL
            or type(value['start']) is not int or type(value['end']) is not int
            or not 0 <= value['start'] <= value['end'] <= MAX_CHARACTERS
            or value['end'] - value['start'] != len(fragment)):
        raise ValueError('Invalid exact translation source range.')
    _location(value['location'])


def source_text(document):
    return '\n\n'.join(''.join(section['text'] for section in item['sections'])
                       for item in document['documents'])


def validate_structure(document, value=None):
    if (type(document) is not dict or set(document) != {'schema', 'documents'}
            or document['schema'] != SCHEMA or type(document['documents']) is not list
            or not 1 <= len(document['documents']) <= MAX_DOCUMENTS):
        raise ValueError('Choose a bounded ordered translation document selection.')
    count = characters = 0
    for item in document['documents']:
        if (type(item) is not dict or set(item) != {'title', 'kind', 'sections', 'context'}
                or item['kind'] not in ('selected-text', 'document', 'range', 'chapter')
                or type(item['sections']) is not list or not item['sections']):
            raise ValueError('Invalid translation document structure.')
        text(item['title'], 'translation document title',
             maximum=16384 if item['context'] is not None else 512)
        count += len(item['sections'])
        if count > MAX_SECTIONS:
            raise ValueError('The translation selection exceeds its section allowance.')
        for section in item['sections']:
            if (type(section) is not dict or set(section) != {'kind', 'level', 'text', 'origin'}
                    or section['kind'] not in ('heading', 'paragraph', 'literal')
                    or type(section['level']) is not int
                    or (not 1 <= section['level'] <= 6 if section['kind'] == 'heading' else section['level'] != 0)):
                raise ValueError('Invalid translation heading or paragraph boundary.')
            fragment = text(section['text'], 'translation structure text', maximum=MAX_CHARACTERS, empty=True)
            if not fragment:
                raise ValueError('Empty translation sections are not stored.')
            characters += len(fragment)
            if characters > MAX_CHARACTERS:
                raise ValueError('The complete translation selection exceeds two million characters.')
            _origin(section['origin'], fragment)
        _validate_context(item)
    result = source_text(document)
    text(result, 'selected translation document', maximum=MAX_CHARACTERS)
    if value is not None and result != value:
        raise ValueError('Translation structure does not match the immutable selected text.')
    return document


def _validate_context(item):
    context = item['context']
    if context is None:
        if (item['kind'] != 'selected-text'
                or any(section['origin'] is not None for section in item['sections'])):
            raise ValueError('Source fragments require their exact retained document context.')
        return
    if item['kind'] == 'selected-text':
        raise ValueError('A retained document cannot claim the independent selected-text profile.')
    if type(context) is not dict or set(context) != {'source_anchor', 'source_metadata', 'source_topic'}:
        raise ValueError('Invalid retained document context.')
    from ..reader.book_source import source_pages, source_context
    value = ''.join(section['text'] for section in item['sections'])
    checked = source_context(value, context['source_anchor'], context['source_metadata'], context['source_topic'])
    if item['title'] != (checked['source_metadata']['title'] or 'Unknown title'):
        raise ValueError('The retained translation document title differs from its exact source metadata.')
    pages = source_pages(dict(text=value, title=item['title'], warnings=[], **checked))
    by_ordinal = {row['ordinal']: row for row in pages}
    separators = {row['source_start'] - 2 for row in pages[1:]}
    offset = 0
    for section in item['sections']:
        origin = section['origin']
        if origin is None:
            if not (section['kind'] == 'literal' and section['text'] == '\n\n' and offset in separators):
                raise ValueError('A retained document contains an unbound source fragment.')
        else:
            row = by_ordinal.get(origin['ordinal'])
            if (row is None or origin['location'] != row['location']
                    or row['source_start'] + origin['start'] != offset
                    or origin['end'] > len(row['raw_text'])
                    or row['raw_text'][origin['start']:origin['end']] != section['text']):
                raise ValueError('The translation fragment does not match its retained page context.')
        offset += len(section['text'])


def structure_hash(document):
    validate_structure(document)
    return sha256(canonical(document).encode('utf-8')).hexdigest()


def blocks(value, *, location=None, ordinal=None, markdown=False, cancelled=lambda: False):
    """Preserve paragraphs, ATX heading levels/markers and fenced code literally."""
    text(value, 'selected translation text', maximum=MAX_CHARACTERS, empty=True)
    if not value:
        raise ValueError('Choose a nonempty source text section.')
    if type(markdown) is not bool:
        raise TypeError('Choose the explicit plain-text or Markdown structure profile.')
    if location is not None:
        _location(asdict(location))
        if type(ordinal) is not int or not 1 <= ordinal <= MAX_SOURCE_ORDINAL:
            raise ValueError('A retained source ordinal is required.')
    rows, offset, paragraph_start, paragraph = [], 0, 0, []
    fence = None

    def emit(kind, fragment, start, level=0):
        if not fragment:
            return
        origin = None if location is None else {'location': asdict(location),
            'ordinal': ordinal, 'start': start, 'end': start + len(fragment)}
        rows.append({'kind': kind, 'level': level, 'text': fragment, 'origin': origin})
        if len(rows) > MAX_SECTIONS:
            raise ValueError('The selected text exceeds its structure allowance.')

    def flush():
        if paragraph:
            emit('paragraph', ''.join(paragraph), paragraph_start)
            paragraph.clear()

    for line in value.splitlines(keepends=True):
        if cancelled():
            raise InterruptedError('Translation document capture cancelled.')
        if markdown and (match := re.match(r'^[ ]{0,3}(`{3,}|~{3,})', line)):
            marker = match[1]
            flush()
            emit('literal', line, offset)
            if fence is None:
                fence = marker
            elif (marker[0] == fence[0] and len(marker) >= len(fence)
                  and not line[match.end():].strip()):
                fence = None
        elif fence is not None:
            emit('literal', line, offset)
        elif not line.strip():
            flush()
            emit('literal', line, offset)
        elif markdown and (match := re.match(r'^(#{1,6})([ \t]+)([^\r\n]*)(\r\n|\r|\n)?$', line)):
            flush()
            prefix = match[1] + match[2]
            emit('literal', prefix, offset)
            heading, closing = match[3], ''
            closing_match = re.match(r'^(.*?)([ \t]+#+[ \t]*)$', heading)
            if closing_match:
                heading, closing = closing_match.groups()
            emit('heading', heading, offset + len(prefix), len(match[1]))
            emit('literal', closing, offset + len(prefix) + len(heading))
            emit('literal', match[4] or '', offset + len(line) - len(match[4] or ''))
        else:
            if not paragraph:
                paragraph_start = offset
            paragraph.append(line)
        offset += len(line)
    flush()
    if ''.join(row['text'] for row in rows) != value:
        raise ValueError('The structure profile did not retain the exact original text.')
    return rows


def capture_text(value, *, title='Selected text', markdown=False, cancelled=lambda: False):
    document = {'schema': SCHEMA, 'documents': [{'title': title, 'kind': 'selected-text',
        'sections': blocks(value, markdown=markdown, cancelled=cancelled), 'context': None}]}
    return validate_structure(document, value)


def capture_library(library, selections, *, markdown=False, cancelled=lambda: False, timeout=60):
    """Use the same current source capture as retained audiobook production."""
    from ..reader.book_source import capture_sources, source_pages
    if type(selections) not in (tuple, list) or not 1 <= len(selections) <= MAX_DOCUMENTS:
        raise ValueError('Choose one to 32 explicit ordered sources.')
    for selected in selections:
        if type(selected) is not dict:
            raise ValueError('Choose an explicit retained document, chapter or source range.')
        if selected.get('kind', 'document') == 'document' and (
                selected.get('first') not in (None, 1) or selected.get('last') is not None):
            raise ValueError('Choose a source range for a partial document.')
    sources = capture_sources(library, selections, max_documents=MAX_DOCUMENTS,
        max_total_characters=MAX_CHARACTERS, timeout=timeout, cancelled=cancelled)
    documents, count = [], 0
    for selected, source in zip(selections, sources):
        sections = []
        for index, page in enumerate(source_pages(source)):
            if cancelled():
                raise InterruptedError('Translation source capture cancelled.')
            if index:
                sections.append({'kind': 'literal', 'level': 0, 'text': '\n\n', 'origin': None})
            if page['raw_text']:
                sections.extend(blocks(page['raw_text'], location=_location(page['location']),
                    ordinal=page['ordinal'], markdown=markdown, cancelled=cancelled))
            if count + len(sections) > MAX_SECTIONS:
                raise ValueError('Choose a smaller explicit batch within the section allowance.')
        count += len(sections)
        documents.append({'title': source['title'], 'kind': selected.get('kind', 'document'),
            'sections': sections, 'context': {key: source[key] for key in
                ('source_anchor', 'source_metadata', 'source_topic')}})
    return validate_structure({'schema': SCHEMA, 'documents': documents})


def validate_current_sources(library, document, *, cancelled=lambda: False, timeout=60):
    """Reuse shared source, metadata, topic and full-original authorization."""
    from ..reader.book_source import validate_source
    from ..audio.models import number
    number(timeout, .001, 3600, 'Translation source-validation time allowance')
    validate_structure(document)
    deadline = monotonic() + timeout
    def stopped():
        if cancelled():
            raise InterruptedError('Translation source validation cancelled.')
        if monotonic() >= deadline:
            raise TimeoutError('Translation source validation reached its selected allowance.')
        return False
    for item in document['documents']:
        stopped()
        context = item['context']
        if context is None:
            continue
        remaining = deadline - monotonic()
        if remaining < .001:
            raise TimeoutError('Translation source validation reached its selected allowance.')
        validate_source(library, ''.join(section['text'] for section in item['sections']),
            context['source_anchor'], context['source_metadata'], context['source_topic'],
            timeout=min(60, remaining), cancelled=stopped)
    stopped()
    return document


def section_parts(section):
    from .translation_state import split_text
    fragment = section['text']
    return (split_text(fragment) if fragment.strip()
            else tuple(fragment[index:index + 4500] for index in range(0, len(fragment), 4500)))


def parts_and_mask(value, document=None):
    from .translation_state import split_text
    if document is None:
        parts = split_text(value)
        return parts, tuple(bool(part.strip()) for part in parts)
    validate_structure(document, value)
    parts, mask = [], []
    for index, item in enumerate(document['documents']):
        if index:
            parts.append('\n\n')
            mask.append(False)
        for section in item['sections']:
            for part in section_parts(section):
                parts.append(part)
                mask.append(section['kind'] != 'literal' and bool(part.strip()))
    if not any(mask):
        raise ValueError('Choose at least one nonempty paragraph or heading to translate.')
    if ''.join(parts) != value:
        raise ValueError('The structured translation lost source text.')
    return tuple(parts), tuple(mask)


def section_results(document, receipts):
    """Pair every complete section with its accepted derivative, in source order."""
    from .translation_state import split_text
    validate_structure(document)
    cursor, result = 0, []
    for index, item in enumerate(document['documents']):
        if index:
            cursor += 1
        sections = []
        for section in item['sections']:
            count = len(section_parts(section))
            rows = receipts[cursor:cursor + count]
            if len(rows) != count:
                raise ValueError('Complete all structured translation sections before exporting.')
            translated = ''.join(row['text'] for row in rows)
            sections.append({**section, 'translated': translated})
            cursor += count
        result.append({**item, 'sections': sections})
    if cursor != len(receipts):
        raise ValueError('The structured translation receipt inventory changed.')
    return result


def export_structure(document, receipts):
    result = section_results(document, receipts)
    payload = {'schema': 'carr-structured-translation-export/1', 'documents': result,
               'source_is_immutable': True, 'quotation_authority': False}
    lines = ['<!doctype html><html><head><meta charset="utf-8"><title>Translation derivative</title>'
             '</head><body><p>Machine translation derivative; original source retained.</p>']
    for item in result:
        lines.append('<article><h1>' + html.escape(item['title']) + '</h1>')
        for section in item['sections']:
            if section['kind'] == 'literal':
                # Literal Markdown markers and blank lines are retained exactly
                # in JSON/text; do not turn their contents into executable HTML.
                tag = 'pre'
            else:
                tag = 'h' + str(section['level']) if section['kind'] == 'heading' else 'p'
            lines.append('<section><' + tag + '>' + html.escape(section['translated'])
                         + '</' + tag + '><details><summary>Original</summary><pre>'
                         + html.escape(section['text']) + '</pre></details></section>')
        lines.append('</article>')
    lines.append('</body></html>')
    return {'structured-translation.json': canonical(payload) + '\n',
            'structured-bilingual.html': '\n'.join(lines)}

def validate_historical_sources(db, document, *, cancelled=lambda: False):
    """Bind retained lineage to sealed historical pages during complete backup.

    This accepts archived workspaces and old sealed revisions as history. It
    does not open a runtime, grant source access or rebind remote approval.
    """
    from ..reader.legacy_reading import validate_anchor, validate_anchor_database
    validate_structure(document)
    for item in document['documents']:
        if cancelled():
            raise InterruptedError('Translation source snapshot cancelled.')
        context = item['context']
        if context is None:
            continue
        value = ''.join(section['text'] for section in item['sections'])
        anchor = validate_anchor(context['source_anchor'], value)
        validate_anchor_database(db, anchor, cancelled=cancelled)
        row = db.execute(
            'SELECT author,title,volume,edition_label FROM source_metadata '
            'WHERE workspace=? AND edition=? AND revision=?',
            (anchor.workspace, anchor.edition, anchor.revision)).fetchone()
        metadata = None if row is None else dict(zip(('author', 'title', 'volume', 'edition'), row))
        if (metadata != context['source_metadata']
                or item['title'] != (metadata['title'] or 'Unknown title')):
            raise ValueError('Retained translation metadata differs from its sealed source.')
        for position in anchor.positions:
            if cancelled():
                raise InterruptedError('Translation source snapshot cancelled.')
            expected = value[position.start:position.end]
            size = db.execute(
                'SELECT length(raw),typeof(raw) FROM page WHERE revision=? AND ordinal=?',
                (anchor.revision, position.ordinal)).fetchone()
            if size is None or size[1] != 'text' or size[0] != len(expected):
                raise ValueError('Retained translation page bytes differ from their sealed source.')
            row = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=?',
                             (anchor.revision, position.ordinal)).fetchone()
            if row is None or row[0] != expected:
                raise ValueError('Retained translation page bytes differ from their sealed source.')
