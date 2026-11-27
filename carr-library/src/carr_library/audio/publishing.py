"""Local EPUB 3 and DAISY 2.02 from verified, completed Studio productions.

Only measured line intervals are synchronized. Audio between scripted lines is
retained with explicit interval labels, never presented as an invented transcript.
The existing local audio runtime supplies MP3, an EPUB core audio media type.
"""
from datetime import datetime, timezone
from io import BytesIO
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4
import xml.etree.ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

from .jobs import RenderCancelled as AudioCancelled, project_lock, publish_noreplace
from .publication_io import PublicationBudget, finite, read_identity, bounded_zip, inspect_audio
from ..durable_state import (MAX_JSON_BYTES, atomic_json, encode, local_path,
                             read_json, regular, state_lock)
from .publication_source import load_source, completed_jobs, render_id
from ..research.export_file import ExportDestination, capture_export_destination


XHTML = 'http://www.w3.org/1999/xhtml'
OPS = 'http://www.idpf.org/2007/ops'
OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
SMIL = 'http://www.w3.org/ns/SMIL'
CONTAINER = 'urn:oasis:names:tc:opendocument:xmlns:container'
METADATA = 'http://www.idpf.org/2013/metadata'
PROVENANCE = 'urn:resonance:epub:provenance:1'
NOTICE = ('Synchronization uses measured Studio speech-line durations, not word alignment. '
          'Other audio may have no textual transcription. Headings, role labels, source script '
          'and provenance are not narrated. Form-feed source pages use reversible JSON text on the source page. '
          'MP3 encoding may add codec padding. '
          'Overlay times retain measured line boundaries rounded to six decimal places; '
          'this precision is not proof of sample, word or acoustic alignment. '
          'EPUBCheck, third-party reader interoperability, DAISY and Braille are not qualified by this export.')
CSS = ('.spoken,.source{white-space:pre-wrap} .role{font-weight:bold} '
       '.audio-interval{font-style:italic} .active-line{background:#fff2a6;color:#111;outline:2px solid #111} '
       'body{line-height:1.5} pre{white-space:pre-wrap;overflow-wrap:anywhere}').encode()
_HASH = re.compile(r'[0-9a-f]{64}')
MAX_XML_BYTES = 64 * 1024**2
MAX_XML_TOTAL = 128 * 1024**2
MAX_XML_NODES = 250000
MAX_XML_DEPTH = 64
_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
_NAMES = {'mimetype', 'META-INF/container.xml', 'META-INF/metadata.xml', 'EPUB/package.opf',
          'EPUB/nav.xhtml', 'EPUB/content.xhtml', 'EPUB/source.xhtml', 'EPUB/overlay.smil',
          'EPUB/styles.css', 'EPUB/audio/master.mp3'}


def language_tag(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}', value):
        raise ValueError('Choose a language tag such as en, uk or und (undetermined).')
    return value


def _text(value, label, *, maximum=16_000_000, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or _INVALID_XML.search(value)
            or (not empty and not value.strip())):
        raise ValueError(f'{label} cannot be represented faithfully in XML.')
    return value


def _source_text(value):
    # Form feeds delimit literal narration pages in the current Studio.
    # XML cannot carry them directly; retain the exact bytes in JSON provenance.
    if (not isinstance(value, str) or len(value) > 16 * 1024**2
            or _INVALID_XML.search(value.replace('\f', ''))):
        raise ValueError('The original source cannot be represented faithfully.')
    return value


def _source_display(value):
    value = _source_text(value)
    return json.dumps(value, ensure_ascii=False) if '\f' in value else value


def _xml_document(raw, check):
    count, depth = 0, 0
    try:
        iterator = SafeET.iterparse(BytesIO(raw), events=('start', 'end'),
                                   forbid_dtd=True, forbid_entities=True, forbid_external=True)
        for event, _node in iterator:
            if event == 'start':
                count += 1
                depth += 1
                if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
                    raise ValueError('Publication XML exceeds its node or nesting budget.')
                if count % 128 == 0:
                    check()
            else:
                depth -= 1
        check()
        return iterator.root
    except (DefusedXmlException, ET.ParseError) as exc:
        raise ValueError('Publication metadata must be bounded XML without DTDs or entities.') from exc


def _xhtml_profile(root):
    ns = '{' + XHTML + '}'
    ep = '{' + OPS + '}'
    allowed = {'html': {'lang', '{http://www.w3.org/XML/1998/namespace}lang'},
        'head': set(), 'title': set(), 'meta': {'charset'}, 'link': {'rel', 'href', 'type'},
        'body': set(), 'h1': set(), 'h2': set(), 'section': {'id', ep + 'type'},
        'nav': {'id', ep + 'type'}, 'p': {'class'}, 'span': {'id', 'class'},
        'pre': {'id', 'class'}, 'ol': set(), 'li': set(), 'a': {'href'}}
    if root.tag != ns + 'html':
        raise ValueError('Publication text must use the expected XHTML document root.')
    for node in root.iter():
        local = node.tag.removeprefix(ns)
        if node.tag != ns + local or local not in allowed or not set(node.attrib) <= allowed[local]:
            raise ValueError('EPUB does not permit active or unsupported XHTML content.')
        if 'href' in node.attrib and node.get('href').split('#')[0] not in ('content.xhtml', 'source.xhtml', 'styles.css'):
            raise ValueError('EPUB content points outside the declared publication file set.')


def _json(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('Duplicate publication metadata key.')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite publication metadata.')))


def _read(path, expected, check, *, copy_to=None, collect=False):
    result = read_identity(path, check, min(MAX_JSON_BYTES, expected['size']) if collect else expected['size'],
                           expected=expected, collect=collect, copy_to=copy_to)
    return result[0] if collect else result


def _digest(path, check, maximum):
    return read_identity(path, check, maximum)


def _clock(value):
    return f'{0.0 if value == 0 else value:.6f}s'


def _xml(element):
    # Literal carriage returns would be normalized by an XML reader. Character
    # references retain the exact source text, including CRLF scripts.
    return ET.tostring(element, encoding='utf-8', xml_declaration=True).replace(b'\r', b'&#13;')


def _element(parent, tag, value=None, **attributes):
    child = ET.SubElement(parent, tag, attributes)
    if value is not None:
        child.text = str(value)
    return child


def _html(title, language):
    root = ET.Element('html', {'xmlns': XHTML, 'xmlns:epub': OPS, 'lang': language, 'xml:lang': language})
    head = _element(root, 'head')
    _element(head, 'title', title)
    _element(head, 'meta', charset='utf-8')
    _element(head, 'link', rel='stylesheet', href='styles.css', type='text/css')
    return root, _element(root, 'body')


def _segments(project, timeline, check=lambda: None):
    if (not isinstance(timeline, dict) or timeline.get('schema') != 1
            or timeline.get('project') != project.id or timeline.get('revision') != project.revision
            or timeline.get('source_sha256') != sha256(project.original_script.encode('utf-8')).hexdigest()
            or timeline.get('sample_rate') != project.sample_rate):
        raise ValueError('The measured timeline does not belong to this exact project and source script.')
    duration = finite(timeline.get('duration'), 'Master duration', 1e-6, 365 * 86400)
    chapters, lines = timeline.get('chapters'), timeline.get('lines')
    if not isinstance(chapters, list) or len(chapters) != len(project.scenes) or not isinstance(lines, list):
        raise ValueError('The measured chapter/line graph is incomplete.')
    expected = [(scene, line) for scene in project.scenes for line in scene.lines if line.text]
    if len(lines) != len(expected) or len(lines) > 100000:
        raise ValueError('Every scripted speech line requires exactly one measured interval (up to 100000).')
    grouped = {scene.id: [] for scene in project.scenes}
    seen = set()
    for row, (scene, line) in zip(lines, expected):
        check()
        identity = (scene.id, line.id)
        if (not isinstance(row, dict) or identity in seen or row.get('scene') != scene.id
                or row.get('line') != line.id or row.get('role') != line.role or row.get('text') != line.text
                or row.get('source_ref', '') != line.source_ref):
            raise ValueError('The measured line identity, order or text differs from the source project.')
        seen.add(identity)
        _text(line.text, 'Spoken line', maximum=100000)
        start = finite(row.get('start'), 'Line start', 0, duration + .01)
        end = finite(row.get('end'), 'Line end', 0, duration + .01)
        if end <= start:
            raise ValueError('A measured line must have positive duration.')
        grouped[scene.id].append((line, start, end))
    segments, chapter_rows, cursor = [], [], 0.0
    tolerance = max(2 / project.sample_rate, len(chapters) * 1e-6)
    for index, (scene, row) in enumerate(zip(project.scenes, chapters), 1):
        check()
        if not isinstance(row, dict) or row.get('id') != scene.id or row.get('title') != scene.title:
            raise ValueError('A measured chapter differs from the source scene.')
        _text(scene.title, 'Chapter title', maximum=512)
        start = finite(row.get('start'), 'Chapter start', 0, duration + tolerance)
        end = finite(row.get('end'), 'Chapter end', 0, duration + tolerance)
        if round(start, 6) != round(cursor, 6) or end <= start:
            raise ValueError('Measured chapters must cover the master in order without overlaps or gaps.')
        chapter_rows.append({'id': f'chapter-{index}', 'scene': scene.id, 'title': scene.title})
        # Only the generated non-source tail may use the measured master end.
        # Never trim a speech interval to hide accumulated chapter rounding.
        presentation_end = duration if index == len(chapters) else end
        if index == len(chapters) and abs(end - duration) > tolerance:
            raise ValueError('The measured chapter endpoint differs from the master.')
        def append(kind, begin, finish, line=None):
            if round(finish, 6) <= round(begin, 6):
                if line is not None:
                    raise ValueError('A measured speech interval cannot disappear at overlay precision.')
                return
            segments.append({'id': f'interval-{len(segments) + 1}', 'chapter': f'chapter-{index}',
                'kind': kind, 'start': round(begin, 6), 'end': round(finish, 6),
                'text': line.text if line is not None else 'Audio interval without a scripted line.',
                'role': line.role if line is not None else None,
                'line': line.id if line is not None else None, 'scene': scene.id,
                'source_ref': line.source_ref if line is not None else '',
                'measured_start': begin if line is not None else None,
                'measured_end': finish if line is not None else None})
        for line, begin, finish in grouped[scene.id]:
            if (round(begin, 6) < round(cursor, 6)
                    or round(finish, 6) > round(min(end, presentation_end), 6)):
                raise ValueError('A speech interval overlaps another line or exceeds its measured chapter.')
            if begin > cursor:
                append('audio-interval', cursor, begin)
            append('line', begin, finish, line)
            cursor = finish
        if presentation_end > cursor:
            append('audio-interval', cursor, presentation_end)
        cursor = presentation_end
    if round(cursor, 6) != round(duration, 6) or not segments:
        raise ValueError('The measured intervals do not cover the complete master.')
    # Spoken boundaries were never clamped or reassigned. Generated gap labels
    # describe only the audio remaining between those measured source intervals.
    return chapter_rows, segments, duration


def _parts(project, timeline, provenance, language, creator, check):
    chapters, segments, duration = _segments(project, timeline, check)
    if 10 * (len(chapters) + len(segments)) + 128 > MAX_XML_NODES:
        raise ValueError('The measured publication exceeds its XML node budget.')
    _text(project.title, 'Publication title', maximum=512)
    _source_text(project.original_script)
    roles = {role.id: _text(role.name, 'Role name', maximum=256) for role in project.roles}
    content, body = _html(project.title, language)
    _element(body, 'h1', project.title)
    _element(body, 'p', NOTICE)
    overlay = ET.Element('smil', {'xmlns': SMIL, 'xmlns:epub': OPS, 'version': '3.0'})
    overlay_body = _element(overlay, 'body', **{'epub:textref': 'content.xhtml'})
    nav, nav_body = _html('Contents — ' + project.title, language)
    navigation = _element(nav_body, 'nav', **{'epub:type': 'toc', 'id': 'toc'})
    _element(navigation, 'h1', 'Contents')
    listing = _element(navigation, 'ol')
    grouped = {chapter['id']: [] for chapter in chapters}
    for segment in segments:
        grouped[segment['chapter']].append(segment)
    for chapter in chapters:
        check()
        section = _element(body, 'section', id=chapter['id'], **{'epub:type': 'chapter'})
        _element(section, 'h2', chapter['title'])
        _element(_element(listing, 'li'), 'a', chapter['title'], href='content.xhtml#' + chapter['id'])
        sequence = _element(overlay_body, 'seq', id='seq-' + chapter['id'],
                            **{'epub:textref': 'content.xhtml#' + chapter['id'], 'epub:type': 'chapter'})
        for segment in grouped[chapter['id']]:
            check()
            paragraph = _element(section, 'p', **{'class': 'spoken' if segment['kind'] == 'line' else 'audio-interval'})
            if segment['role'] is not None:
                role = _element(paragraph, 'span', roles[segment['role']] + ':', **{'class': 'role'})
                role.tail = ' '
            _element(paragraph, 'span', segment['text'], id=segment['id'])
            par = _element(sequence, 'par', id='par-' + segment['id'])
            _element(par, 'text', src='content.xhtml#' + segment['id'])
            _element(par, 'audio', src='audio/master.mp3', clipBegin=_clock(segment['start']), clipEnd=_clock(segment['end']))
    _element(_element(listing, 'li'), 'a', 'Original script and provenance', href='source.xhtml')
    source, source_body = _html('Original script and provenance', language)
    _element(source_body, 'h1', 'Original script and provenance')
    _element(source_body, 'p', NOTICE)
    _element(source_body, 'p', 'Source job: ' + provenance['source']['job_id'] +
             '; project fingerprint: ' + provenance['source']['project_sha256'])
    _element(source_body, 'pre', _source_display(project.original_script), **{'class': 'source', 'id': 'original-script'})

    package = ET.Element('package', {'xmlns': OPF, 'version': '3.0', 'unique-identifier': 'publication-id',
        'prefix': 'schema: http://schema.org/'})
    metadata = _element(package, 'metadata', **{'xmlns:dc': DC})
    _element(metadata, 'dc:identifier', 'urn:uuid:' + str(UUID(hex=provenance['publication_id'])), id='publication-id')
    _element(metadata, 'dc:title', project.title)
    _element(metadata, 'dc:language', language)
    if creator:
        _element(metadata, 'dc:creator', creator)
    _element(metadata, 'dc:source', 'urn:sha256:' + provenance['source']['project_sha256'])
    _element(metadata, 'meta', provenance['created'], property='dcterms:modified')
    _element(metadata, 'meta', _clock(duration), property='media:duration')
    _element(metadata, 'meta', _clock(duration), property='media:duration', refines='#overlay')
    _element(metadata, 'meta', 'active-line', property='media:active-class')
    for mode in ('textual', 'auditory'):
        _element(metadata, 'meta', mode, property='schema:accessMode')
    features = ['tableOfContents', 'structuralNavigation']
    if any(segment['kind'] == 'line' for segment in segments):
        features.append('synchronizedAudioText')
    for feature in features:
        _element(metadata, 'meta', feature, property='schema:accessibilityFeature')
    _element(metadata, 'meta', 'unknown', property='schema:accessibilityHazard')
    _element(metadata, 'meta', NOTICE, property='schema:accessibilitySummary')
    manifest = _element(package, 'manifest')
    for identity, href, media, extra in (
        ('nav', 'nav.xhtml', 'application/xhtml+xml', {'properties': 'nav'}),
        ('content', 'content.xhtml', 'application/xhtml+xml', {'media-overlay': 'overlay'}),
        ('source', 'source.xhtml', 'application/xhtml+xml', {}),
        ('overlay', 'overlay.smil', 'application/smil+xml', {}),
        ('style', 'styles.css', 'text/css', {}),
        ('audio', 'audio/master.mp3', 'audio/mpeg', {})):
        _element(manifest, 'item', id=identity, href=href, **{'media-type': media, **extra})
    spine = _element(package, 'spine')
    _element(spine, 'itemref', idref='content')
    _element(spine, 'itemref', idref='source', linear='no')
    container = ET.Element('container', {'xmlns': CONTAINER, 'version': '1.0'})
    _element(_element(container, 'rootfiles'), 'rootfile',
             **{'full-path': 'EPUB/package.opf', 'media-type': 'application/oebps-package+xml'})
    provenance.update(original_script=project.original_script,
        source_display_encoding='json-string' if '\f' in project.original_script else 'literal-xml',
        roles=[{'id': role.id, 'name': role.name, 'language': role.language} for role in project.roles],
        intervals=[{key: value for key, value in segment.items() if key != 'text'} |
                   {'text_sha256': sha256(segment['text'].encode()).hexdigest()} for segment in segments],
        duration=duration, timing_notice=NOTICE)
    metadata_document = ET.Element('metadata', {'xmlns': METADATA, 'xmlns:r': PROVENANCE})
    _element(metadata_document, 'r:provenance', encode(provenance).decode('utf-8'), format='application/json')
    return {'META-INF/container.xml': _xml(container), 'META-INF/metadata.xml': _xml(metadata_document),
            'EPUB/package.opf': _xml(package), 'EPUB/nav.xhtml': _xml(nav),
            'EPUB/content.xhtml': _xml(content), 'EPUB/source.xhtml': _xml(source),
            'EPUB/overlay.smil': _xml(overlay), 'EPUB/styles.css': CSS}


def inspect_epub(path, *, cancelled=lambda: False, max_bytes=32 * 1024**3):
    """Verify our bounded package graph; this is not a substitute for EPUBCheck."""
    def check():
        if cancelled():
            raise AudioCancelled('EPUB verification cancelled.')
    path = local_path(path, exists=True)
    if regular(path).st_size > max_bytes:
        raise ValueError('EPUB exceeds the verification budget.')
    with bounded_zip(path, len(_NAMES), max_bytes, check) as archive:
        infos = archive.infolist()
        if (len(infos) != len(_NAMES) or {item.filename for item in infos} != _NAMES
                or infos[0].filename != 'mimetype' or infos[0].compress_type != ZIP_STORED
                or infos[0].header_offset != 0 or infos[0].extra or any(item.flag_bits & 1 for item in infos)
                or sum(item.file_size for item in infos) > max_bytes):
            raise ValueError('Unexpected EPUB container graph or size.')
        if archive.getinfo('mimetype').file_size != 20 or archive.read('mimetype') != b'application/epub+zip':
            raise ValueError('Invalid EPUB mimetype member.')
        xml_total = 0
        def document(name):
            nonlocal xml_total
            check()
            if archive.getinfo(name).file_size > MAX_XML_BYTES:
                raise ValueError('EPUB XML exceeds its metadata budget.')
            chunks = []
            with archive.open(name) as stream:
                while block := stream.read(1024 * 1024):
                    check(); xml_total += len(block)
                    if xml_total > MAX_XML_TOTAL:
                        raise ValueError('EPUB XML exceeds its total metadata budget.')
                    chunks.append(block)
            raw = b''.join(chunks)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('EPUB metadata must not contain entity declarations.')
            try:
                return _xml_document(raw, check)
            except (DefusedXmlException, ET.ParseError) as exc:
                raise ValueError('EPUB metadata must be bounded XML without DTDs or entities.') from exc
        metadata = document('META-INF/metadata.xml')
        node = metadata.find('{' + PROVENANCE + '}provenance')
        if node is None or node.get('format') != 'application/json':
            raise ValueError('Missing derivative provenance.')
        provenance = _json((node.text or '').encode('utf-8'))
        if provenance.get('schema') != 'resonance-epub/1':
            raise ValueError('Unsupported EPUB provenance.')
        script = _source_text(provenance.get('original_script'))
        if provenance.get('source_display_encoding', 'literal-xml') != ('json-string' if '\f' in script else 'literal-xml'):
            raise ValueError('The source-page encoding does not match its exact source text.')
        if sha256(script.encode('utf-8')).hexdigest() != provenance.get('source', {}).get('source_script_sha256'):
            raise ValueError('The original script differs from its recorded source hash.')
        if archive.getinfo('EPUB/styles.css').file_size != len(CSS) or archive.read('EPUB/styles.css') != CSS:
            raise ValueError('Unexpected EPUB stylesheet; external resources are not accepted.')
        container = document('META-INF/container.xml')
        roots = container.findall('.//{' + CONTAINER + '}rootfile')
        if len(roots) != 1 or roots[0].get('full-path') != 'EPUB/package.opf':
            raise ValueError('Invalid EPUB root document.')
        package = document('EPUB/package.opf')
        items = package.findall('{' + OPF + '}manifest/{' + OPF + '}item')
        manifest = {item.get('id'): item for item in items}
        if (set(manifest) != {'nav','content','source','overlay','style','audio'} or len(items) != 6
                or manifest['content'].get('media-overlay') != 'overlay'
                or manifest['overlay'].get('media-type') != 'application/smil+xml'
                or manifest['audio'].get('media-type') != 'audio/mpeg'
                or manifest['nav'].get('properties') != 'nav'
                or any('EPUB/' + item.get('href', '') not in _NAMES for item in items)):
            raise ValueError('Invalid EPUB manifest bindings.')
        expected_members = {'nav': ('nav.xhtml', 'application/xhtml+xml'),
            'content': ('content.xhtml', 'application/xhtml+xml'),
            'source': ('source.xhtml', 'application/xhtml+xml'),
            'overlay': ('overlay.smil', 'application/smil+xml'),
            'style': ('styles.css', 'text/css'), 'audio': ('audio/master.mp3', 'audio/mpeg')}
        if (package.get('version') != '3.0' or any(
                (manifest[key].get('href'), manifest[key].get('media-type')) != expected
                for key, expected in expected_members.items())
                or [node.get('idref') for node in package.findall('{' + OPF + '}spine/{' + OPF + '}itemref')]
                    != ['content', 'source']):
            raise ValueError('The EPUB manifest or reading order differs from its local package graph.')
        content = document('EPUB/content.xhtml')
        _xhtml_profile(content)
        ids = [node.get('id') for node in content.iter() if node.get('id')]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate XHTML fragment identifier.')
        text_nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
        source = document('EPUB/source.xhtml')
        _xhtml_profile(source)
        original = source.find('.//{' + XHTML + '}pre')
        if original is None or ''.join(original.itertext()) != _source_display(provenance.get('original_script')):
            raise ValueError('The readable source script differs from exact derivative provenance.')
        overlay = document('EPUB/overlay.smil')
        pars = overlay.findall('.//{' + SMIL + '}par')
        intervals = provenance.get('intervals')
        if not isinstance(intervals, list) or len(pars) != len(intervals) or not pars:
            raise ValueError('The EPUB media overlay graph is incomplete.')
        cursor = 0.0
        for par, expected in zip(pars, intervals):
            check()
            text_node, audio = par.find('{' + SMIL + '}text'), par.find('{' + SMIL + '}audio')
            if (text_node is None or audio is None or len(par) != 2
                    or text_node.get('src') != 'content.xhtml#' + expected['id']
                    or audio.get('src') != 'audio/master.mp3'
                    or audio.get('clipBegin') != _clock(expected['start'])
                    or audio.get('clipEnd') != _clock(expected['end'])
                    or expected['end'] <= expected['start'] or abs(expected['start'] - cursor) > 1e-6
                    or sha256(text_nodes.get(expected['id'], '').encode()).hexdigest() != expected['text_sha256']):
                raise ValueError('An EPUB overlay does not match its exact text and bounded audio interval.')
            cursor = expected['end']
        durations = package.findall('{' + OPF + '}metadata/{' + OPF + '}meta[@property="media:duration"]')
        if (len(durations) != 2 or any(node.text != _clock(provenance['duration']) for node in durations)
                or {node.get('refines') for node in durations} != {None, '#overlay'}
                or abs(cursor - provenance['duration']) > 1e-6):
            raise ValueError('EPUB overlay duration does not cover the measured presentation.')
        nav = document('EPUB/nav.xhtml')
        _xhtml_profile(nav)
        for link in nav.findall('.//{' + XHTML + '}a'):
            href = link.get('href', '')
            if href != 'source.xhtml' and not (href.startswith('content.xhtml#') and href.split('#')[1] in text_nodes):
                raise ValueError('EPUB navigation points outside the packaged text.')
        digest, size = sha256(), 0
        with archive.open('EPUB/audio/master.mp3') as stream:
            while block := stream.read(1024 * 1024):
                check(); size += len(block); digest.update(block)
        if {'sha256': digest.hexdigest(), 'size': size} != provenance.get('audio', {}).get('identity'):
            raise ValueError('Packaged audio differs from the verified derivative identity.')
        check()
        return provenance



def validate_publication_receipt(record, *, publication_id=None):
    """Validate passive receipt data without opening any recorded external path."""
    required = {'schema', 'id', 'status', 'project_root', 'output', 'artifact',
                'source', 'created', 'format', 'notice'}
    if (not isinstance(record, dict) or set(record) != required
            or record['schema'] != 'carr-audio-publication/1'
            or not isinstance(record['id'], str) or not re.fullmatch('[0-9a-f]{32}', record['id'])
            or publication_id is not None and record['id'] != publication_id
            or record['status'] not in ('prepared', 'complete')
            or record['format'] not in ('epub', 'daisy-2.02')):
        raise ValueError('Invalid Audio Studio publication receipt.')
    for key in ('project_root', 'output'):
        if (not isinstance(record[key], str) or not 1 <= len(record[key]) <= 32768
                or '\0' in record[key]):
            raise ValueError('Invalid passive publication location.')
    if (not isinstance(record['created'], str)
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', record['created'])
            or not isinstance(record['notice'], str) or len(record['notice']) > 8192):
        raise ValueError('Invalid publication receipt metadata.')
    artifact = record['artifact']
    if (not isinstance(artifact, dict) or set(artifact) != {'size', 'sha256'}
            or type(artifact['size']) is not int or not 0 < artifact['size'] <= 32 * 1024**3
            or not isinstance(artifact['sha256'], str) or not _HASH.fullmatch(artifact['sha256'])):
        raise ValueError('Invalid completed publication byte identity.')
    source = record['source']
    required_source = {'job_id', 'job_sha256', 'project_id', 'project_sha256',
                       'source_script_sha256', 'artifacts', 'origin', 'input_kind',
                       'approved_library_evidence', 'frame_rate', 'frame_intervals'}
    if (not isinstance(source, dict) or set(source) != required_source
            or source['origin'] != 'carr-audio-production/1'
            or source['input_kind'] not in ('narration', 'script')
            or source['approved_library_evidence'] is not False
            or type(source['frame_rate']) is not int or not 8000 <= source['frame_rate'] <= 192000
            or not isinstance(source['artifacts'], list) or not 6 <= len(source['artifacts']) <= 100002
            or not isinstance(source['frame_intervals'], list) or len(source['frame_intervals']) > 100000):
        raise ValueError('Invalid publication source provenance.')
    for key in ('job_id', 'job_sha256', 'project_id', 'project_sha256', 'source_script_sha256'):
        if not isinstance(source[key], str) or not _HASH.fullmatch(source[key]):
            raise ValueError('Invalid publication source identity.')
    if source['job_id'] != source['project_id']:
        raise ValueError('The publication source identities disagree.')
    from ..research.export_file import _filename
    names = set()
    for item in source['artifacts']:
        if (not isinstance(item, dict) or set(item) != {'name', 'size', 'sha256'}
                or not isinstance(item['name'], str) or type(item['size']) is not int
                or item['size'] < 0 or not isinstance(item['sha256'], str)
                or not _HASH.fullmatch(item['sha256'])):
            raise ValueError('Invalid publication source artifact.')
        _filename(item['name'])
        if item['name'].casefold() in names:
            raise ValueError('Duplicate publication source artifact.')
        names.add(item['name'].casefold())
    seen = set()
    for item in source['frame_intervals']:
        if (not isinstance(item, dict)
                or set(item) != {'event_id', 'scene_id', 'start_frame', 'end_frame',
                                 'literal_narration', 'source_start', 'source_end'}
                or not isinstance(item['event_id'], str) or not isinstance(item['scene_id'], str)
                or not 1 <= len(item['event_id']) <= 100 or not 1 <= len(item['scene_id']) <= 100
                or type(item['literal_narration']) is not bool
                or type(item['start_frame']) is not int or type(item['end_frame']) is not int
                or not 0 <= item['start_frame'] < item['end_frame'] <= source['frame_rate'] * 365 * 86400):
            raise ValueError('Invalid original integer-frame provenance.')
        identity = item['scene_id'], item['event_id']
        if identity in seen:
            raise ValueError('Duplicate publication frame provenance.')
        seen.add(identity)
        if item['literal_narration']:
            if (type(item['source_start']) is not int or type(item['source_end']) is not int
                    or not 0 <= item['source_start'] <= item['source_end'] <= 16 * 1024**2):
                raise ValueError('Invalid literal narration source range.')
        elif item['source_start'] is not None or item['source_end'] is not None:
            raise ValueError('Only literal narration can claim exact source ranges.')
    return record


def publication_receipts(project_root, *, cancelled=lambda: False):
    """Read-only typed inventory for the existing project snapshot transaction."""
    root = local_path(project_root, exists=True)
    folder = root / 'publications'
    if not os.path.lexists(folder):
        return {}
    local_path(folder, root=root, exists=True)
    result = {}
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries, 1):
            if cancelled():
                raise AudioCancelled('Publication receipt inventory cancelled.')
            if index > 10000:
                raise ValueError('Publication receipts exceed their inventory budget.')
            path = folder / entry.name
            regular(path)
            if re.fullmatch(r'\.[0-9a-f]{32}\.lock', entry.name):
                continue
            if not re.fullmatch(r'[0-9a-f]{32}\.json', entry.name):
                raise ValueError('Unexpected entry in the publication receipt directory.')
            value = read_json(path)
            validate_publication_receipt(value, publication_id=entry.name[:-5])
            result['audio/publications/' + entry.name] = path
    return result


class PublicationService:
    """Publish selected existing renders through the current Studio contracts."""
    def __init__(self, project_root, *, private_root, ffmpeg=None, budget=None):
        self.root = local_path(project_root, exists=True)
        regular(self.root, directory=True)
        self.private_root = local_path(private_root, exists=True)
        regular(self.private_root, directory=True)
        self.ffmpeg = ffmpeg
        self.budget = budget or PublicationBudget()
        if type(self.budget) is not PublicationBudget:
            raise TypeError('Choose explicit bounded publication settings.')
        self.receipts = self.root / 'publications'

    def jobs(self, *, cancelled=lambda: False):
        def check():
            if cancelled():
                raise AudioCancelled('Completed production listing cancelled.')
        return completed_jobs(self.root, check)

    def publications(self, *, cancelled=lambda: False):
        return tuple(validate_publication_receipt(read_json(path), publication_id=path.stem)
                     for path in publication_receipts(self.root, cancelled=cancelled).values())

    def _source(self, job_id, check, progress):
        progress('Verifying the completed source plan and audio artifact graph')
        snapshot = load_source(self.root, job_id, check, self.budget)
        _source_text(snapshot.project.original_script)
        _text(snapshot.project.title, 'Publication title', maximum=512)
        _segments(snapshot.project, snapshot.timeline, check)
        return snapshot

    def _destination(self, output, approval=None):
        captured = capture_export_destination(output, private_root=self.private_root)
        if approval is not None and (type(approval) is not ExportDestination or approval != captured):
            raise ValueError('The export destination changed. Choose the filename again.')
        captured = captured if approval is None else approval
        if not captured.require_absent:
            raise FileExistsError('Existing publication files are never overwritten.')
        if captured.target.is_relative_to(self.root):
            raise ValueError('Choose a publication destination outside the private audio project directory.')
        return captured

    def export_epub(self, job_id, output, *, language='und', creator='',
                    cancelled=lambda: False, progress=None, approval=None):
        return self._export(job_id, output, book_format='epub', language=language,
            creator=creator, cancelled=cancelled, progress=progress, approval=approval)

    def export_daisy(self, job_id, output, *, publisher, language='und', creator='',
                     cancelled=lambda: False, progress=None, approval=None):
        return self._export(job_id, output, book_format='daisy-2.02', language=language,
            creator=creator, publisher=publisher, cancelled=cancelled,
            progress=progress, approval=approval)

    def _export(self, job_id, output, *, book_format, language, creator, publisher='',
                cancelled, progress, approval):
        if book_format == 'epub':
            label, suffix, notice, inspect = 'EPUB', '.epub', NOTICE, inspect_epub
            schema, member_name = 'resonance-epub/1', 'EPUB/audio/master.mp3'
        elif book_format == 'daisy-2.02':
            from . import daisy
            daisy.language_tag(language)
            label, suffix, notice, inspect = 'DAISY', '.zip', daisy.NOTICE, daisy.inspect_daisy
            schema, member_name = 'resonance-daisy-2.02/1', 'master.mp3'
            publisher = _text(publisher, 'DAISY publisher', maximum=512)
        else:
            raise ValueError('Choose EPUB 3 or DAISY 2.02 publication.')
        language = language_tag(language)
        creator = _text(creator, 'Creator', maximum=512, empty=True)
        job_id = render_id(job_id)
        approval = self._destination(output, approval)
        output, parent = approval.target, approval.target.parent
        if output.suffix.lower() != suffix:
            raise ValueError(f'Choose a new {suffix} filename.')
        # Unknown IDs do not create a render or publication directory.
        from .jobs import checked_directory
        checked_directory(self.root / 'renders' / job_id)
        from .runtime import find_ffmpeg
        binary = find_ffmpeg(self.ffmpeg)
        if binary is None:
            raise ValueError('Choose a verified local FFmpeg and sibling ffprobe installation.')
        started = monotonic()
        observer = progress
        identity = uuid4().hex
        lease = None
        def progress(message):
            if observer is not None:
                try:
                    observer(message)
                except Exception:
                    pass
        def check():
            if cancelled() or lease is not None and lease.cancelled():
                raise AudioCancelled(f'{label} publication cancelled; completed files are retained.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError(f'{label} publication exceeded its selected time budget.')
        def stopping():
            check()
            return False
        def scope():
            check()
            if self._destination(approval.requested) != approval:
                raise ValueError('The selected publication directory changed during queued work.')
        from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled
        request = ResourceRequest(cpu_slots=1, devices=('heavy-media',))
        broker = ResourceBroker.for_app(self.private_root)
        try:
            with broker.reserve(request, operation_id='publication:' + identity,
                                cancelled=cancelled, timeout=min(60, self.budget.timeout)) as admitted:
                lease = admitted
                with project_lock(self.root):
                    snapshot = self._source(job_id, check, progress)
                    state_digest = sha256(encode(snapshot.identity)).hexdigest()
                    project, timeline, paths = snapshot.project, snapshot.timeline, snapshot.paths
                    source_name = 'master.mp3' if snapshot.marker['output_name'] == 'master.mp3' else 'master.wav'
                    source_path, source_receipt = paths[source_name]
                    if shutil.disk_usage(parent).free < source_receipt['size'] * 4 + MAX_XML_TOTAL + self.budget.reserve_disk_bytes:
                        raise OSError('Not enough free space for publication staging and the selected reserve.')
                    scope()
                    with TemporaryDirectory(prefix='.pending-' + label.lower() + '-', dir=parent) as temporary:
                        staging = local_path(temporary, exists=True)
                        selected_audio = staging / ('source' + source_path.suffix)
                        progress('Copying the verified audio master')
                        read_identity(source_path, check, self.budget.max_output_bytes,
                                      expected=source_receipt, copy_to=selected_audio, allow_hardlinks=True)
                        observed = inspect_audio(selected_audio, binary, check=check,
                                                 timeout=min(30, self.budget.timeout))
                        if (abs(observed.duration - timeline['duration']) > (.15 if source_name.endswith('.mp3') else 2 / project.sample_rate)
                                or observed.sample_rate != timeline['sample_rate'] or observed.channels != timeline['channels']
                                or (source_name.endswith('.mp3') and observed.codec != 'mp3')
                                or (source_name.endswith('.wav') and not observed.codec.startswith('pcm_'))):
                            raise ValueError('The actual recording does not match the completed frame timeline.')
                        if source_name.endswith('.mp3'):
                            audio = selected_audio
                        else:
                            from .ffmpeg import transcode
                            progress('Encoding MP3 through the existing selected local FFmpeg')
                            audio = staging / 'master.mp3'
                            transcode(selected_audio, audio, ffmpeg=binary, format='mp3', cancelled=stopping)
                            observed = inspect_audio(audio, binary, check=check, timeout=min(30, self.budget.timeout))
                        check()
                        if observed.codec != 'mp3' or observed.duration + 1e-6 < timeline['duration']:
                            raise ValueError('The packaged MP3 does not cover the measured master duration.')
                        if book_format == 'daisy-2.02' and (observed.sample_rate not in (16000, 22050, 24000, 32000, 44100, 48000)
                                or observed.channels not in (1, 2)):
                            raise ValueError('DAISY needs supported MPEG-1/2 mono or stereo audio.')
                        audio_identity = _digest(audio, check, self.budget.max_output_bytes)
                        source = {'job_id': job_id, 'job_sha256': state_digest, 'project_id': project.id,
                            'project_sha256': project.fingerprint, 'source_script_sha256': timeline['source_sha256'],
                            'artifacts': [{'name': name, **receipt} for name, (_, receipt) in sorted(paths.items())]
                                + [{'name': 'plan.json', **snapshot.identity['plan']},
                                   {'name': 'complete.json', **snapshot.identity['marker']}],
                            'origin': 'carr-audio-production/1', 'input_kind': snapshot.plan.input_kind,
                            'approved_library_evidence': False, 'frame_rate': project.sample_rate,
                            'frame_intervals': snapshot.frames}
                        provenance = {'schema': schema, 'publication_id': identity,
                            'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                            'source': source,
                            'audio': {'identity': audio_identity, 'media_type': 'audio/mpeg', 'codec': observed.codec,
                                'duration': observed.duration, 'sample_rate': observed.sample_rate, 'channels': observed.channels,
                                'source_artifact': source_name, 'reused': source_name.endswith('.mp3')},
                            'validation': {'zip_xml_graph': 'verified on export',
                                'external_reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN',
                                'full_daisy_rule_conformance': 'NOT_CLAIMED'}}
                        parts = (_parts(project, timeline, provenance, language, creator, check) if book_format == 'epub'
                                 else daisy.parts(project, timeline, provenance, language, creator, publisher, check))
                        metadata_bytes = sum(len(raw) for raw in parts.values())
                        if metadata_bytes > MAX_XML_TOTAL or metadata_bytes + audio_identity['size'] > self.budget.max_output_bytes:
                            raise ValueError('The publication exceeds its total metadata or byte budget.')
                        staged = staging / ('publication' + suffix)
                        progress('Packaging exact text and measured audio intervals')
                        with ZipFile(staged, 'x', compression=ZIP_DEFLATED, allowZip64=True) as archive:
                            if book_format == 'epub':
                                mimetype = ZipInfo('mimetype')
                                mimetype.compress_type = ZIP_STORED
                                archive.writestr(mimetype, b'application/epub+zip')
                            for name, raw in parts.items():
                                check()
                                archive.writestr(name, raw)
                            member = ZipInfo(member_name)
                            member.compress_type = ZIP_STORED
                            digest, count = sha256(), 0
                            from .publication_io import stable_reader
                            with stable_reader(audio) as stream, archive.open(member, 'w', force_zip64=True) as target:
                                while block := stream.read(1024 * 1024):
                                    check()
                                    count += len(block)
                                    if count > audio_identity['size']:
                                        raise ValueError('The staged recording grew during packaging.')
                                    digest.update(block)
                                    target.write(block)
                            if {'size': count, 'sha256': digest.hexdigest()} != audio_identity:
                                raise ValueError('The staged MP3 changed during publication packaging.')
                        inspect(staged, cancelled=stopping, max_bytes=self.budget.max_output_bytes)
                        archive_identity = _digest(staged, check, self.budget.max_output_bytes)
                        progress('Rechecking source receipts and selected destination before publication')
                        current = self._source(job_id, check, lambda _message: None)
                        if current.identity != snapshot.identity:
                            raise ValueError('The completed audio source changed during publication.')
                        scope()
                        record = {'schema': 'carr-audio-publication/1', 'id': identity, 'status': 'prepared',
                            'project_root': str(self.root), 'output': str(output), 'artifact': archive_identity,
                            'source': source, 'created': provenance['created'], 'notice': notice, 'format': book_format}
                        validate_publication_receipt(record, publication_id=identity)
                        self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
                        local_path(self.receipts, root=self.root, exists=True)
                        receipt_path = self.receipts / (identity + '.json')
                        atomic_json(receipt_path, record)
                        progress('Prepared publication receipt: ' + identity)
                        published = False
                        try:
                            scope()
                            publish_noreplace(staged, output)
                            published = True
                            # Cancellation after atomic publication cannot discard
                            # a complete artifact. Keep finalization independently bounded.
                            final_deadline = monotonic() + 5
                            def final_check():
                                if monotonic() > final_deadline:
                                    raise TimeoutError('The published file needs explicit receipt verification.')
                            if _digest(output, final_check, self.budget.max_output_bytes) != archive_identity:
                                raise ValueError('The publication changed before receipt finalization.')
                            record['status'] = 'complete'
                            atomic_json(receipt_path, record, replace=True)
                        except Exception as exc:
                            outcome = ('Publication was saved; receipt finalization needs verification.'
                                       if published or not staged.exists() else 'Publication did not confirm completion.')
                            if isinstance(exc, AudioCancelled) and not published and staged.exists():
                                raise
                            raise RuntimeError(outcome + ' Receipt: ' + identity
                                               + '. Verify any retained output before retrying.') from exc
                        progress(label + ' saved and its exact bytes verified')
                        return record
        except ResourceCancelled as exc:
            raise AudioCancelled('Publication resource admission cancelled.') from exc

    def verify(self, publication_id, *, output=None, cancelled=lambda: False):
        if not isinstance(publication_id, str) or not re.fullmatch('[0-9a-f]{32}', publication_id):
            raise ValueError('Choose a saved publication receipt ID.')
        path = self.receipts / (publication_id + '.json')
        record = validate_publication_receipt(read_json(path), publication_id=publication_id)
        if record['project_root'] != str(self.root) and output is None:
            raise ValueError('This receipt was restored elsewhere. Explicitly select the existing publication file.')
        selected = local_path(output if output is not None else record['output'], exists=True)
        started = monotonic()
        def check():
            if cancelled():
                raise AudioCancelled('Publication verification cancelled.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError('Publication verification exceeded its time budget.')
        inspect = inspect_epub
        if record['format'] == 'daisy-2.02':
            from .daisy import inspect_daisy
            inspect = inspect_daisy
        with project_lock(self.root):
            if validate_publication_receipt(read_json(path), publication_id=publication_id) != record:
                raise ValueError('The publication receipt changed before verification.')
            if _digest(selected, check, self.budget.max_output_bytes) != record['artifact']:
                raise ValueError('The publication differs from its exact receipt.')
            provenance = inspect(selected, cancelled=lambda: cancelled() or monotonic() - started > self.budget.timeout,
                                 max_bytes=self.budget.max_output_bytes)
            if provenance.get('publication_id') != publication_id or provenance.get('source') != record['source']:
                raise ValueError('The publication source identity differs from its receipt.')
            if _digest(selected, check, self.budget.max_output_bytes) != record['artifact']:
                raise ValueError('The publication changed during structural verification.')
            check()
            record.update(status='complete', output=str(selected), project_root=str(self.root))
            atomic_json(path, record, replace=True)
            return record
