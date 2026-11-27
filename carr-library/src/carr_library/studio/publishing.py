"""Local EPUB 3 media overlays from verified, completed Studio productions.

Only measured line intervals are synchronized. Audio between scripted lines is
retained with explicit interval labels, never presented as an invented transcript.
The existing local audio runtime supplies MP3, an EPUB core audio media type.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

from ..audio.models import AudioBudget, AudioCancelled, finite
from ..audio.process import publish_file
from ..durable_state import (MAX_JSON_BYTES, atomic_json, encode, local_path,
                             read_json, regular, state_lock)
from ..research.export_file import ExportDestination, capture_export_destination
from .project import Project, _id


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
          'and provenance are not narrated. MP3 encoding may add codec padding. '
          'Overlay times retain measured line boundaries rounded to six decimal places; '
          'this precision is not proof of sample, word or acoustic alignment. '
          'EPUBCheck, third-party reader interoperability, DAISY and Braille are not qualified by this export.')
CSS = ('.spoken,.source{white-space:pre-wrap} .role{font-weight:bold} '
       '.audio-interval{font-style:italic} .active-line{background:#fff2a6;color:#111;outline:2px solid #111} '
       'body{line-height:1.5} pre{white-space:pre-wrap;overflow-wrap:anywhere}').encode()
_HASH = re.compile(r'[0-9a-f]{64}')
MAX_XML_BYTES = 64 * 1024**2
MAX_XML_TOTAL = 128 * 1024**2
_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
_NAMES = {'mimetype', 'META-INF/container.xml', 'META-INF/metadata.xml', 'EPUB/package.opf',
          'EPUB/nav.xhtml', 'EPUB/content.xhtml', 'EPUB/source.xhtml', 'EPUB/overlay.smil',
          'EPUB/styles.css', 'EPUB/audio/master.mp3'}


def validate_publication_receipt(record, *, publication_id=None):
    """Validate a saved declaration without opening any recorded external path.

    This composes with the typed backup inventory. Source hashes describe the
    exact historical production; validation does not grant source authority,
    execute a job, or imply that the external EPUB is included in a backup.
    Relocation still requires explicit selection and byte verification.
    """
    fields = {'schema', 'id', 'status', 'studio_root', 'output', 'artifact',
              'source', 'created', 'notice'}
    if (type(record) is not dict or not fields <= set(record) or set(record) - fields - {'format'}
            or record['schema'] != 'resonance-publication/1'
            or type(record['id']) is not str or not re.fullmatch('[0-9a-f]{32}', record['id'])
            or record['status'] not in ('prepared', 'complete')
            or (publication_id is not None and record['id'] != publication_id)):
        raise ValueError('Invalid saved publication receipt identity or fields.')

    def path(value, label):
        if (type(value) is not str or not value or len(value) > 32768
                or any(ord(character) < 32 for character in value)
                or value.startswith(('//', '\\\\'))
                or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())):
            raise ValueError('Invalid absolute ' + label + ' declaration in publication receipt.')

    def digest(value):
        if type(value) is not str or not _HASH.fullmatch(value):
            raise ValueError('Invalid publication receipt checksum.')

    def size(value, *, positive=False):
        if type(value) is not int or not int(positive) <= value <= 2**63 - 1:
            raise ValueError('Invalid publication receipt byte count.')

    path(record['studio_root'], 'Studio root')
    path(record['output'], 'publication output')
    book_format = record.get('format', 'epub')
    if book_format not in ('epub', 'daisy-2.02'):
        raise ValueError('Unsupported saved publication receipt format.')
    # The verified archive may have been explicitly relocated/renamed. Its
    # receipt format and exact bytes, not the historical filename extension,
    # determine the inspector; export itself still requires the proper suffix.
    artifact = record['artifact']
    if type(artifact) is not dict or set(artifact) != {'size', 'sha256'}:
        raise ValueError('Invalid completed publication artifact receipt.')
    size(artifact['size'], positive=True)
    digest(artifact['sha256'])
    source = record['source']
    if (type(source) is not dict or set(source) != {'job_id', 'job_sha256', 'project_id',
            'project_sha256', 'source_script_sha256', 'artifacts'}):
        raise ValueError('Invalid historical publication source identity.')
    _id(source['job_id'])
    _id(source['project_id'])
    for field in ('job_sha256', 'project_sha256', 'source_script_sha256'):
        digest(source[field])
    artifacts = source['artifacts']
    if type(artifacts) is not list or not 3 <= len(artifacts) <= 100000:
        raise ValueError('Invalid bounded source artifact graph in publication receipt.')
    names = set()
    for row in artifacts:
        if type(row) is not dict or set(row) != {'name', 'size', 'sha256'}:
            raise ValueError('Invalid source artifact receipt fields.')
        name = row['name']
        if (type(name) is not str or not name or len(name) > 4096
                or any(ord(character) < 32 for character in name) or '\\' in name or ':' in name):
            raise ValueError('Invalid source artifact receipt filename.')
        relative = PurePosixPath(name)
        if (not relative.parts or relative.is_absolute() or str(relative) != name or '..' in relative.parts
                or name.casefold() in names):
            raise ValueError('Invalid or duplicate relative source artifact name.')
        names.add(name.casefold())
        size(row['size'])
        digest(row['sha256'])
    if not {'master.wav', 'project.json', 'timeline.json'} <= names:
        raise ValueError('Publication receipt is missing its original production artifacts.')
    created = record['created']
    if type(created) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', created):
        raise ValueError('Invalid publication receipt timestamp.')
    datetime.strptime(created, '%Y-%m-%dT%H:%M:%SZ')
    _text(record['notice'], 'Publication receipt notice', maximum=8192)
    encode(record)  # The same bounded JSON contract used by the producer.
    return record


def language_tag(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,7}', value):
        raise ValueError('Choose a language tag such as en, uk or und (undetermined).')
    return value


def _text(value, label, *, maximum=16_000_000, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or _INVALID_XML.search(value)
            or (not empty and not value.strip())):
        raise ValueError(f'{label} cannot be represented faithfully in XML.')
    return value


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
    path = local_path(path, exists=True)
    before = regular(path)
    if collect and expected['size'] > MAX_JSON_BYTES:
        raise ValueError('Publication metadata exceeds the bounded JSON limit.')
    digest, count, chunks = sha256(), 0, []
    destination = copy_to.open('xb') if copy_to is not None else None
    try:
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError('A source artifact changed before it was opened.')
            while block := stream.read(1024 * 1024):
                check()
                count += len(block)
                if count > expected['size']:
                    raise ValueError('A source artifact grew beyond its verified size.')
                digest.update(block)
                if destination is not None:
                    destination.write(block)
                if collect:
                    chunks.append(block)
        if destination is not None:
            destination.flush(); os.fsync(destination.fileno())
    finally:
        if destination is not None:
            destination.close()
    if count != expected['size'] or digest.hexdigest() != expected['sha256']:
        raise ValueError('A source artifact is missing or its checksum changed.')
    regular(path)
    check()
    return b''.join(chunks) if collect else {'size': count, 'sha256': digest.hexdigest()}


def _digest(path, check, maximum):
    path = local_path(path, exists=True)
    size = regular(path).st_size
    if size > maximum:
        raise ValueError('Publication exceeds its byte budget.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            check(); count += len(block)
            if count > maximum:
                raise ValueError('Publication grew beyond its byte budget.')
            digest.update(block)
    if count != size:
        raise ValueError('Publication changed while it was being verified.')
    check()
    return {'size': count, 'sha256': digest.hexdigest()}


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
    _text(project.title, 'Publication title', maximum=512)
    _text(project.original_script, 'Original script', empty=True)
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
    _element(source_body, 'pre', project.original_script, **{'class': 'source', 'id': 'original-script'})

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
    with ZipFile(path) as archive:
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
                return SafeET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            except (DefusedXmlException, ET.ParseError) as exc:
                raise ValueError('EPUB metadata must be bounded XML without DTDs or entities.') from exc
        metadata = document('META-INF/metadata.xml')
        node = metadata.find('{' + PROVENANCE + '}provenance')
        if node is None or node.get('format') != 'application/json':
            raise ValueError('Missing derivative provenance.')
        provenance = _json((node.text or '').encode('utf-8'))
        if provenance.get('schema') != 'resonance-epub/1':
            raise ValueError('Unsupported EPUB provenance.')
        script = _text(provenance.get('original_script'), 'Original script', empty=True)
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
        ids = [node.get('id') for node in content.iter() if node.get('id')]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate XHTML fragment identifier.')
        text_nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
        source = document('EPUB/source.xhtml')
        original = source.find('.//{' + XHTML + '}pre')
        if original is None or ''.join(original.itertext()) != provenance.get('original_script'):
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


class PublicationService:
    def __init__(self, production, *, budget=None, private_root=None):
        self.production = production
        self.root = local_path(production.root, exists=True)
        self.private_root = local_path(private_root if private_root is not None else self.root,
                                       exists=True)
        if not self.root.is_relative_to(self.private_root):
            raise ValueError('The publication service must belong to the selected private library.')
        self.budget = budget or AudioBudget()
        if not isinstance(self.budget, AudioBudget):
            raise TypeError('Use the shared AudioBudget for publication limits.')
        self.receipts = self.root / 'publications'

    def jobs(self):
        return tuple({'id': state['id'], 'title': state['project']['title'],
                      'duration': state.get('result', {}).get('duration'),
                      'lines': len(state.get('result', {}).get('lines', []))}
                     for state in self.production.jobs() if state.get('status') == 'complete')

    def publications(self):
        """Saved receipt metadata only; current output bytes require explicit verify."""
        if not self.receipts.exists():
            return ()
        local_path(self.receipts, exists=True)
        rows = []
        for path in sorted(self.receipts.glob('*.json')):
            value = validate_publication_receipt(read_json(path), publication_id=path.stem)
            rows.append(value)
        return tuple(rows)

    def _source(self, state, check, progress):
        if state.get('status') != 'complete' or state.get('recovery', {}).get('requires_output_directory'):
            raise ValueError('Choose a completed Studio job with verified local output files.')
        destination = local_path(state['destination'], exists=True)
        if read_json(destination / 'production-owner.json') != {'job': state['id'], 'fingerprint': state['fingerprint']}:
            raise ValueError('The Studio output directory belongs to another production.')
        artifacts = state.get('artifacts')
        if not isinstance(artifacts, list) or not 3 <= len(artifacts) <= 100000:
            raise ValueError('The source job has no bounded completed artifact manifest.')
        total, paths, names, documents = 0, {}, set(), {}
        for index, artifact in enumerate(artifacts, 1):
            check()
            if (not isinstance(artifact, dict) or set(artifact) != {'path','sha256','size'}
                    or not isinstance(artifact['sha256'], str) or not _HASH.fullmatch(artifact['sha256'])
                    or type(artifact['size']) is not int or artifact['size'] < 0):
                raise ValueError('Invalid source artifact receipt.')
            total += artifact['size']
            if total > self.budget.max_output_bytes:
                raise ValueError('The complete source artifact graph exceeds the publication byte budget.')
            path = local_path(artifact['path'], root=destination, exists=True)
            name = path.relative_to(destination).as_posix()
            if name.casefold() in names:
                raise ValueError('Duplicate source artifact path.')
            names.add(name.casefold()); paths[name] = (path, artifact)
            progress(f'Verifying source artifact {index}/{len(artifacts)}')
            raw = _read(path, artifact, check, collect=name in ('project.json','timeline.json'))
            if name in ('project.json','timeline.json'):
                documents[name] = _json(raw)
        if not {'master.wav','project.json','timeline.json'} <= paths.keys():
            raise ValueError('The completed job lacks its exact WAV master, project or measured timeline.')
        project = Project.from_dict(documents['project.json'])
        if (project.fingerprint != state['fingerprint']
                or documents['timeline.json'] != state.get('result')):
            raise ValueError('Published project/timeline bytes differ from the completed job checkpoint.')
        _text(project.title, 'Publication title', maximum=512)
        _text(project.original_script, 'Original script', empty=True)
        for role in project.roles:
            _text(role.name, 'Role name', maximum=256)
        _segments(project, documents['timeline.json'], check)
        return project, documents['timeline.json'], paths

    def export_epub(self, job_id, output, *, language='und', creator='', cancelled=lambda: False,
                    progress=None, approval=None):
        return self._export(job_id, output, book_format='epub', language=language, creator=creator,
                            cancelled=cancelled, progress=progress, approval=approval)
    def export_daisy(self, job_id, output, *, publisher, language='und', creator='',
                     cancelled=lambda: False, progress=None, approval=None):
        return self._export(job_id, output, book_format='daisy-2.02', language=language, creator=creator,
                            publisher=publisher, cancelled=cancelled, progress=progress, approval=approval)
    def _export(self, job_id, output, *, book_format, language, creator, publisher='',
                cancelled, progress, approval):
        if book_format == 'epub':
            label, suffix, notice, inspect = 'EPUB', '.epub', NOTICE, inspect_epub
            schema, member_name, archive_limit = 'resonance-epub/1', 'EPUB/audio/master.mp3', self.budget.max_output_bytes
        elif book_format == 'daisy-2.02':
            from . import daisy
            daisy.language_tag(language)
            label, suffix, notice, inspect = 'DAISY', '.zip', daisy.NOTICE, daisy.inspect_daisy
            schema, member_name = 'resonance-daisy-2.02/1', 'master.mp3'
            archive_limit = self.budget.max_output_bytes
            publisher = _text(publisher, 'DAISY publisher', maximum=512)
        else:
            raise ValueError('Choose the existing EPUB3 or DAISY 2.02 publication format.')
        language = language_tag(language)
        creator = _text(creator, 'Creator', maximum=512, empty=True)
        job_id = _id(job_id)
        output = local_path(output)
        if output.suffix.lower() != suffix:
            raise ValueError(f'Choose a new {suffix} filename.')
        parent = local_path(output.parent, exists=True)
        if output.is_relative_to(self.root):
            raise ValueError('Choose a publication destination outside private Studio state.')
        parent_info = regular(parent, directory=True)
        self.production.get(job_id)  # Unknown IDs must not create a job directory.
        started = monotonic()
        observer = progress
        def progress(message):
            if observer is not None:
                try:
                    observer(message)
                except Exception:
                    # Notifications cannot replace a verified publication result
                    # or its actual failure. Cancellation uses cancelled below.
                    pass
        def check():
            if cancelled():
                raise AudioCancelled(f'{label} publication cancelled; previous completed files are retained.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError(f'{label} publication exceeded its selected time budget.')
        def stopping():
            return cancelled() or monotonic() - started > self.budget.timeout
        def destination_snapshot():
            check()
            try:
                value = capture_export_destination(output, private_root=self.private_root,
                                                   cancelled=cancelled)
            except TimeoutError:
                # Keep the shared publication cancellation/deadline outcome
                # when the destination helper notices it first.
                check()
                raise
            check()
            return value
        def scope():
            check()
            observed = regular(local_path(parent, exists=True), directory=True)
            if (observed.st_dev, observed.st_ino) != (parent_info.st_dev, parent_info.st_ino):
                raise ValueError('The selected output directory changed during publication.')
            local_path(output)
            if os.path.lexists(output):
                raise FileExistsError('The selected publication became occupied; nothing will be overwritten.')
            if destination_snapshot() != destination:
                raise ValueError('The selected output directory changed during publication.')

        destination = destination_snapshot()
        if approval is not None and (type(approval) is not ExportDestination or approval != destination):
            raise ValueError('The export destination changed; it will not be overwritten. Choose the destination again.')
        if os.path.lexists(output):
            raise FileExistsError(f'The selected {label} already exists; it will not be overwritten.')

        with state_lock(self.production.jobs_root / job_id, 'render'):
            state = self.production.get(job_id)
            state_digest = sha256(encode(state)).hexdigest()
            project, timeline, paths = self._source(state, check, progress)
            if output.is_relative_to(local_path(state['destination'], exists=True)):
                raise ValueError('Choose a publication destination outside the completed production folder.')
            source_name = 'audiobook.mp3' if state.get('format') == 'mp3' and 'audiobook.mp3' in paths else 'master.wav'
            source_path, source_receipt = paths[source_name]
            if shutil.disk_usage(parent).free < source_receipt['size'] * 2 + self.budget.reserve_disk_bytes:
                raise OSError('Not enough free disk for verified audio staging and the selected reserve.')
            scope()
            with TemporaryDirectory(prefix='.pending-' + label.lower() + '-', dir=parent) as temporary:
                staging = local_path(temporary, exists=True)
                selected_audio = staging / ('source' + source_path.suffix)
                progress('Copying the verified local audio master')
                _read(source_path, source_receipt, check, copy_to=selected_audio)
                if self.production.runtime_factory is not None:
                    runtime = self.production.runtime_factory(staging, input_roots=(staging,))
                    runtime.budget = self.budget
                else:
                    from ..runtime_settings import audio_runtime
                    runtime = audio_runtime(staging, state_root=self.root.parent,
                                            input_roots=(staging,), budget=self.budget)
                observed = runtime.inspect(selected_audio, cancelled=stopping)
                if (abs(observed.duration - timeline['duration']) > (.15 if source_name.endswith('.mp3') else 2 / project.sample_rate)
                        or observed.sample_rate != timeline['sample_rate'] or observed.channels != timeline['channels']
                        or (source_name.endswith('.mp3') and observed.codec != 'mp3')
                        or (source_name.endswith('.wav') and not observed.codec.startswith('pcm_'))):
                    raise ValueError('The actual audio format/duration does not match the completed timeline.')
                if source_name.endswith('.mp3'):
                    audio = selected_audio
                else:
                    progress('Encoding MP3 with the configured local audio runtime')
                    audio = staging / 'master.mp3'
                    runtime.convert(selected_audio, audio, sample_rate=observed.sample_rate,
                                    channels=observed.channels, cancelled=stopping)
                    observed = runtime.inspect(audio, cancelled=stopping)
                check()
                if observed.codec != 'mp3' or observed.duration + 1e-6 < timeline['duration']:
                    raise ValueError('The packaged MP3 does not cover the measured master duration.')
                if book_format == 'daisy-2.02' and (observed.sample_rate not in (16000, 22050, 24000, 32000, 44100, 48000)
                        or observed.channels not in (1, 2)):
                    raise ValueError('DAISY requires supported MPEG-1/2 mono or stereo audio.')
                audio_identity = _digest(audio, check, self.budget.max_output_bytes)
                identity = uuid4().hex
                provenance = {'schema': schema, 'publication_id': identity,
                    'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'source': {'job_id': job_id, 'job_sha256': state_digest, 'project_id': project.id,
                        'project_sha256': project.fingerprint, 'source_script_sha256': timeline['source_sha256'],
                        'artifacts': [{'name': name, 'size': receipt['size'], 'sha256': receipt['sha256']}
                                      for name, (_, receipt) in sorted(paths.items())]},
                    'audio': {'identity': audio_identity, 'media_type': 'audio/mpeg', 'codec': observed.codec,
                        'duration': observed.duration, 'sample_rate': observed.sample_rate, 'channels': observed.channels,
                        'source_artifact': source_name, 'reused': source_name.endswith('.mp3')},
                    'validation': {'zip_xml_graph': 'verified on export',
                                   'epubcheck' if book_format == 'epub' else 'independent_daisy_validator': 'NOT_RUN',
                                   'reader_interoperability': 'NOT_RUN'}}
                parts = (_parts(project, timeline, provenance, language, creator, check) if book_format == 'epub'
                         else daisy.parts(project, timeline, provenance, language, creator, publisher, check))
                raw_size = sum(len(raw) for raw in parts.values()) + audio_identity['size'] + 20
                if (raw_size > archive_limit
                        or raw_size + sum(path.stat().st_size for path in staging.iterdir()) > self.budget.max_output_bytes):
                    raise ValueError(f'{label} metadata/audio staging exceeds the selected byte budget.')
                staged = staging / ('publication' + suffix)
                progress(f'Packaging {label} text, navigation and measured media overlays')
                with ZipFile(staged, 'x', compression=ZIP_DEFLATED, allowZip64=True) as archive:
                    if book_format == 'epub':
                        mimetype = ZipInfo('mimetype'); mimetype.compress_type = ZIP_STORED
                        archive.writestr(mimetype, b'application/epub+zip')
                    for name, raw in parts.items():
                        check(); archive.writestr(name, raw)
                    member = ZipInfo(member_name); member.compress_type = ZIP_STORED
                    digest, count = sha256(), 0
                    with audio.open('rb') as source, archive.open(member, 'w', force_zip64=True) as target:
                        while block := source.read(1024 * 1024):
                            check(); count += len(block); digest.update(block); target.write(block)
                    if {'size': count, 'sha256': digest.hexdigest()} != audio_identity:
                        raise ValueError(f'The staged MP3 changed during {label} packaging.')
                inspect(staged, cancelled=stopping, max_bytes=archive_limit)
                archive_identity = _digest(staged, check, archive_limit)
                progress('Rechecking source receipts and selected destination before publication')
                self._source(state, check, lambda message: None)
                if sha256(encode(self.production.get(job_id))).hexdigest() != state_digest:
                    raise ValueError(f'The completed Studio job changed during {label} publication.')
                scope()
                record = {'schema': 'resonance-publication/1', 'id': identity, 'status': 'prepared',
                    'studio_root': str(self.root), 'output': str(output), 'artifact': archive_identity,
                    'source': provenance['source'], 'created': provenance['created'], 'notice': notice}
                if book_format != 'epub':
                    record['format'] = book_format
                validate_publication_receipt(record, publication_id=identity)
                self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
                receipt_path = self.receipts / (identity + '.json')
                with state_lock(self.receipts, identity):
                    atomic_json(receipt_path, record)
                    progress('Prepared publication receipt: ' + identity)
                    published = False
                    try:
                        scope()
                        publish_file(staged, output, cancelled=stopping)
                        published = True
                        staged.unlink()
                        # Prepared receipts make a crash after atomic publication
                        # recoverable by an explicit verify, never by re-encoding.
                        # Atomic publication has completed. A late cancellation
                        # cannot turn it into an unpublished/cancelled result. Keep
                        # receipt finalization bounded and recoverable separately.
                        final_deadline = monotonic() + 5
                        def final_check():
                            if monotonic() > final_deadline:
                                raise TimeoutError(f'The published {label} needs explicit receipt verification.')
                        if _digest(output, final_check, self.budget.max_output_bytes) != archive_identity:
                            raise ValueError(f'The completed {label} changed before its receipt was finalized.')
                        record['status'] = 'complete'
                        atomic_json(receipt_path, record, replace=True)
                    except Exception as exc:
                        outcome = f'{label} was published; receipt finalization needs verification.' if published else 'Publication did not confirm completion.'
                        message = f'{outcome} {exc} Publication receipt: {identity}. Verify any completed {label} before retrying.'
                        if isinstance(exc, AudioCancelled) and not published:
                            raise AudioCancelled(message) from exc
                        raise RuntimeError(message) from exc
                    progress(f'{label} saved and its exact bytes verified')
                    return record

    def verify(self, publication_id, *, output=None, cancelled=lambda: False):
        if not isinstance(publication_id, str) or not re.fullmatch('[0-9a-f]{32}', publication_id):
            raise ValueError('Choose a saved publication receipt ID.')
        path = self.receipts / (publication_id + '.json')
        started = monotonic()
        def check():
            if cancelled():
                raise AudioCancelled('Publication verification cancelled.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError('Publication verification exceeded its time budget.')
        with state_lock(self.receipts, publication_id):
            check()
            record = validate_publication_receipt(read_json(path), publication_id=publication_id)
            if record.get('studio_root') != str(self.root) and output is None:
                raise ValueError('This receipt was restored elsewhere; explicitly select its existing publication file to verify.')
            selected = local_path(output if output is not None else record['output'], exists=True)
            if _digest(selected, check, self.budget.max_output_bytes) != record['artifact']:
                raise ValueError('The selected publication differs from its exact completed receipt.')
            inspector = inspect_epub
            if record.get('format', 'epub') == 'daisy-2.02':
                from .daisy import inspect_daisy
                inspector = inspect_daisy
            provenance = inspector(selected,
                cancelled=lambda: cancelled() or monotonic() - started > self.budget.timeout,
                max_bytes=self.budget.max_output_bytes)
            if provenance.get('publication_id') != publication_id or provenance.get('source') != record['source']:
                raise ValueError('The publication source identity differs from its publication receipt.')
            if _digest(selected, check, self.budget.max_output_bytes) != record['artifact']:
                raise ValueError('The publication changed during structural verification.')
            check()
            record.update(status='complete', output=str(selected), studio_root=str(self.root))
            atomic_json(path, record, replace=True)
            return record
