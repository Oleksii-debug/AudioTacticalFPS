"""EPUB 3 and DAISY 2.02 from the existing verified local audio renderer.

The package graph and validators are adapted from canonical PR152 (934b632),
without its alternate Studio/production/runtime stores. Only measured speech
line boundaries are synchronized; audio gaps remain explicitly untranscribed.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4
import xml.etree.ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

from . import ffmpeg as audio
from .jobs import (atomic_json, checked_directory, file_identity, project_lock,
                   publish_noreplace, read_json, regular_file, sha256_file,
                   verified_reader, _sync_directory)
from .models import ProductionPlan, Line, canonical, digest, number
from .paths import filename_component
from ..research.export_file import ExportDestination, capture_export_destination

MAX_JSON_BYTES = 64 * 1024**2


class PublicationCancelled(InterruptedError):
    """The publication stopped before confirming a complete derivative."""


# Names retained inside the adapted canonical format validators.
AudioCancelled = PublicationCancelled
regular = regular_file


def finite(value, label, minimum=-(2**53), maximum=2**53):
    return number(value, minimum, maximum, label)


def encode(value):
    raw = canonical(value).encode('utf-8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Publication metadata exceeds its bounded JSON budget.')
    return raw


def local_path(path, *, exists=True):
    selected = Path(path).absolute()
    checked_directory(selected.parent)
    filename_component(selected.name)
    if exists:
        regular_file(selected)
    return selected


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
          'Source page boundaries appear as labels in the readable source; exact form feeds remain in JSON provenance. '
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


def _source_text(value):
    # Literal narration retains page separators in JSON provenance. XML 1.0
    # cannot contain form feed, so only its readable display uses a label.
    if not isinstance(value, str) or len(value) > 16 * 1024 * 1024:
        raise ValueError('Original script exceeds the publication text budget.')
    _text(value.replace('\f', ''), 'Original script', empty=True,
          maximum=16 * 1024 * 1024)
    return value


def _source_display(value):
    return _source_text(value).replace('\f', '\n[Source page boundary]\n')


def _integer(value, label, low=0, high=2**53):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(label + ' is outside its bounded integer range.')
    return value


def _segments(project, timeline, check=lambda: None):
    """Bind every measured sample interval to the current typed plan.

    This adapter replaces the donor's alternate Studio project schema. It uses
    our renderer's integer sample positions without re-parsing spoken text.
    """
    if (not isinstance(project, ProductionPlan) or type(timeline) is not dict
            or set(timeline) != {'schema_version', 'sample_rate', 'source_sha256',
                                 'chapters', 'lines', 'scenes'}
            or type(timeline['schema_version']) is not int or timeline['schema_version'] != project.schema_version
            or type(timeline['sample_rate']) is not int or timeline['sample_rate'] != project.sample_rate
            or timeline['source_sha256'] != project.source_sha256):
        raise ValueError('The measured timeline does not belong to this typed plan and source script.')
    chapters, lines, scenes = timeline['chapters'], timeline['lines'], timeline['scenes']
    if (type(chapters) is not list or len(chapters) != len(project.scenes)
            or type(scenes) is not list or len(scenes) != len(project.scenes)
            or type(lines) is not list or len(lines) > 100000):
        raise ValueError('The measured chapter and line graph is incomplete.')
    expected_lines = sum(isinstance(event, Line) for scene in project.scenes for event in scene.events)
    if len(lines) != expected_lines:
        raise ValueError('Every scripted line requires one measured interval.')
    segments, chapter_rows, seen_events = [], [], set()
    cursor, line_index = 0, 0
    rate = project.sample_rate
    for index, (scene, chapter, measured) in enumerate(zip(project.scenes, chapters, scenes), 1):
        check()
        if (type(chapter) is not dict or set(chapter) != {'id', 'title', 'start_frame', 'end_frame', 'sample_rate'}
                or chapter['id'] != scene.id or chapter['title'] != scene.title
                or type(chapter['sample_rate']) is not int or chapter['sample_rate'] != rate
                or _integer(chapter['start_frame'], 'Chapter start') != cursor
                or _integer(chapter['end_frame'], 'Chapter end', 1, rate * 365 * 86400) <= cursor):
            raise ValueError('Measured chapters must cover the master in order with exact source titles.')
        scene_fields = {'scene_id', 'events'} | ({'clips', 'minimum_duration'} if project.schema_version >= 2 else set())
        if (type(measured) is not dict or set(measured) != scene_fields
                or measured['scene_id'] != scene.id or type(measured['events']) is not list
                or len(measured['events']) != len(scene.events)):
            raise ValueError('The scene event receipt is incomplete.')
        _text(scene.title, 'Chapter title', maximum=512)
        chapter_id = f'chapter-{index}'
        chapter_rows.append({'id': chapter_id, 'scene': scene.id, 'title': scene.title})
        local, spoken = 0, []
        for event, receipt in zip(scene.events, measured['events']):
            check()
            if event.id in seen_events:
                raise ValueError('Publication requires unambiguous event identities.')
            seen_events.add(event.id)
            if (type(receipt) is not dict
                    or set(receipt) != {'event_id', 'kind', 'start_frame', 'frames', 'sha256'}
                    or receipt['event_id'] != event.id
                    or receipt['kind'] != type(event).__name__.lower()
                    or _integer(receipt['start_frame'], 'Event start') != local
                    or not isinstance(receipt['sha256'], str) or not _HASH.fullmatch(receipt['sha256'])):
                raise ValueError('A measured event differs from the source plan.')
            frames = _integer(receipt['frames'], 'Event frames', 1, rate * 365 * 86400)
            if cursor + local + frames > chapter['end_frame']:
                raise ValueError('An event exceeds its measured chapter.')
            if isinstance(event, Line):
                row = lines[line_index]
                line_index += 1
                if (type(row) is not dict
                        or set(row) != {'role', 'text', 'start_frame', 'end_frame', 'event_id',
                                       'literal_narration', 'source_start', 'source_end', 'timing'}
                        or row['event_id'] != event.id or row['role'] != event.role or row['text'] != event.text
                        or type(row['literal_narration']) is not bool or row['literal_narration'] != event.literal
                        or row['source_start'] != event.source_start or row['source_end'] != event.source_end
                        or _integer(row['start_frame'], 'Line start') != cursor + local
                        or _integer(row['end_frame'], 'Line end') != cursor + local + frames):
                    raise ValueError('A measured speech line differs from its exact source text and sample bounds.')
                _text(event.text, 'Spoken line', maximum=100000)
                spoken.append((event, cursor + local, cursor + local + frames))
            local += frames
        extent = local
        if project.schema_version >= 2:
            from .clip_audio import clip_window
            if (type(measured['clips']) is not list or len(measured['clips']) != len(scene.clips)
                    or type(measured['minimum_duration']) not in (int, float)
                    or measured['minimum_duration'] != scene.minimum_duration):
                raise ValueError('The measured clip graph differs from the source chapter.')
            extent = max(extent, round(scene.minimum_duration * rate))
            for clip, row in zip(scene.clips, measured['clips']):
                check()
                if (type(row) is not dict or set(row) != {'clip_id', 'asset', 'track', 'start_frame',
                        'frames', 'sha256', 'source_sha256', 'source_rate', 'source_frames',
                        'source_start', 'source_end', 'fade_in', 'fade_out'}
                        or row['clip_id'] != clip.id or row['asset'] != clip.asset or row['track'] != clip.track
                        or any(not isinstance(row[key], str) or not _HASH.fullmatch(row[key])
                               for key in ('sha256', 'source_sha256'))):
                    raise ValueError('A measured clip is not bound to the typed retained asset.')
                source_rate = _integer(row['source_rate'], 'Clip source rate', 8000, 192000)
                source_frames = _integer(row['source_frames'], 'Clip source frames', 1, source_rate * 365 * 86400)
                window = clip_window(clip, audio.WaveInfo(source_rate, 2, source_frames, 0, source_frames * 4), rate)
                expected = {'start_frame': window.start_frame, 'frames': window.frames,
                    'source_start': window.source_start, 'source_end': window.source_end,
                    'fade_in': window.fade_in, 'fade_out': window.fade_out}
                if any(type(row[key]) is not int or row[key] != expected[key] for key in expected):
                    raise ValueError('A measured clip changed its trim, timing or fades.')
                extent = max(extent, window.start_frame + window.frames)
        if cursor + max(1, extent) != chapter['end_frame']:
            raise ValueError('The measured events and clips do not cover the chapter.')
        segment_cursor = cursor

        def append(kind, begin, finish, event=None):
            if finish <= begin:
                return
            segment = {'id': f'interval-{len(segments) + 1}', 'chapter': chapter_id,
                'kind': kind, 'start': round(begin / rate, 6), 'end': round(finish / rate, 6),
                'start_frame': begin, 'end_frame': finish, 'sample_rate': rate,
                'text': event.text if event is not None else 'Audio interval without a scripted line.',
                'role': event.role if event is not None else None,
                'line': event.id if event is not None else None, 'scene': scene.id,
                'source_ref': '',
                'source_start': event.source_start if event is not None else None,
                'source_end': event.source_end if event is not None else None,
                'measured_start': begin / rate if event is not None else None,
                'measured_end': finish / rate if event is not None else None}
            if segment['end'] <= segment['start']:
                raise ValueError('A sample interval disappears at publication clock precision.')
            segments.append(segment)

        for event, begin, end in spoken:
            append('audio-interval', segment_cursor, begin)
            append('line', begin, end, event)
            segment_cursor = end
        append('audio-interval', segment_cursor, chapter['end_frame'])
        cursor = chapter['end_frame']
    if not segments or line_index != len(lines):
        raise ValueError('Measured publication intervals are incomplete.')
    return chapter_rows, segments, cursor / rate



def _safe_xhtml(root, *, names):
    allowed = {'html': {'lang', '{http://www.w3.org/XML/1998/namespace}lang'},
        'head': set(), 'title': set(), 'meta': {'charset'},
        'link': {'rel', 'href', 'type'}, 'body': set(), 'h1': set(), 'h2': set(),
        'section': {'id', '{' + OPS + '}type'}, 'p': {'class'}, 'span': {'class', 'id'},
        'pre': {'id', 'class'}, 'nav': {'id', '{' + OPS + '}type'},
        'ol': set(), 'li': set(), 'a': {'href'}}
    ns = '{' + XHTML + '}'
    if root.tag != ns + 'html':
        raise ValueError('Publication requires bounded XHTML documents.')
    for node in root.iter():
        local = node.tag.removeprefix(ns)
        if node.tag != ns + local or local not in allowed or not set(node.attrib) <= allowed[local]:
            raise ValueError('Publication contains active or unsupported XHTML content.')
        if 'href' in node.attrib and node.get('href').split('#', 1)[0] not in names:
            raise ValueError('Publication points outside its packaged documents.')


def _sample_provenance(provenance):
    if not str(provenance.get('schema', '')).endswith('/2'):
        return  # Canonical v1 packages keep their documented clock-only proof.
    intervals = provenance.get('intervals')
    if type(intervals) is not list or not 1 <= len(intervals) <= 201000:
        raise ValueError('Publication has an invalid sample-interval inventory.')
    cursor, sample_rate = 0, None
    for row in intervals:
        if type(row) is not dict:
            raise ValueError('Publication interval is invalid.')
        rate = _integer(row.get('sample_rate'), 'Publication sample rate', 8000, 192000)
        start = _integer(row.get('start_frame'), 'Publication interval start')
        end = _integer(row.get('end_frame'), 'Publication interval end', 1, rate * 365 * 86400)
        if (start != cursor or end <= start or sample_rate not in (None, rate)
                or finite(row.get('start'), 'Publication start', 0, 365 * 86400) != round(start / rate, 6)
                or finite(row.get('end'), 'Publication end', 0, 365 * 86400) != round(end / rate, 6)):
            raise ValueError('Publication clocks differ from their exact measured sample bounds.')
        cursor, sample_rate = end, rate
    if finite(provenance.get('duration'), 'Publication duration', 0, 365 * 86400) != cursor / sample_rate:
        raise ValueError('Publication duration differs from its measured samples.')


def _parts(project, timeline, provenance, language, creator, check):
    chapters, segments, duration = _segments(project, timeline, check)
    _text(project.title, 'Publication title', maximum=512)
    _source_text(project.original_script)
    roles = {role.name: _text(role.name, 'Role name', maximum=256) for role in project.roles}
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
        roles=[{'id': role.name, 'name': role.name, 'language': role.language} for role in project.roles],
        intervals=[{key: value for key, value in segment.items() if key != 'text'} |
                   {'text_sha256': sha256(segment['text'].encode()).hexdigest()} for segment in segments],
        duration=duration, timing_notice=NOTICE)
    metadata_document = ET.Element('metadata', {'xmlns': METADATA, 'xmlns:r': PROVENANCE})
    _element(metadata_document, 'r:provenance', encode(provenance).decode('utf-8'), format='application/json')
    return {'META-INF/container.xml': _xml(container), 'META-INF/metadata.xml': _xml(metadata_document),
            'EPUB/package.opf': _xml(package), 'EPUB/nav.xhtml': _xml(nav),
            'EPUB/content.xhtml': _xml(content), 'EPUB/source.xhtml': _xml(source),
            'EPUB/overlay.smil': _xml(overlay), 'EPUB/styles.css': CSS}


def _inspect_epub(path, *, cancelled=lambda: False, max_bytes=32 * 1024**3):
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
                or sum(item.file_size for item in infos) > max_bytes
                or any(item.is_dir() or stat.S_ISLNK(item.external_attr >> 16)
                       or item.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for item in infos)):
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
        if provenance.get('schema') not in ('resonance-epub/1', 'resonance-epub/2'):
            raise ValueError('Unsupported EPUB provenance.')
        _sample_provenance(provenance)
        script = _source_text(provenance.get('original_script'))
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
        _safe_xhtml(content, names={'styles.css'})
        ids = [node.get('id') for node in content.iter() if node.get('id')]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate XHTML fragment identifier.')
        text_nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
        source = document('EPUB/source.xhtml')
        _safe_xhtml(source, names={'styles.css'})
        original = source.find('.//{' + XHTML + '}pre')
        if original is None or ''.join(original.itertext()) != _source_display(provenance.get('original_script')):
            raise ValueError('The readable source script differs from exact derivative provenance.')
        overlay = document('EPUB/overlay.smil')
        smil_allowed = {'smil': {'version'}, 'body': {'{' + OPS + '}textref'},
            'seq': {'id', '{' + OPS + '}textref', '{' + OPS + '}type'}, 'par': {'id'},
            'text': {'src'}, 'audio': {'src', 'clipBegin', 'clipEnd'}}
        if overlay.tag != '{' + SMIL + '}smil' or overlay.get('version') != '3.0':
            raise ValueError('Invalid EPUB media-overlay root.')
        for node in overlay.iter():
            tag = node.tag.removeprefix('{' + SMIL + '}')
            if (node.tag != '{' + SMIL + '}' + tag or tag not in smil_allowed
                    or not set(node.attrib) <= smil_allowed[tag]):
                raise ValueError('EPUB overlay contains unsupported or active elements.')

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
        _safe_xhtml(nav, names={'styles.css', 'source.xhtml', 'content.xhtml'})
        for link in nav.findall('.//{' + XHTML + '}a'):
            href = link.get('href', '')
            if href != 'source.xhtml' and not (href.startswith('content.xhtml#') and href.split('#')[1] in text_nodes):
                raise ValueError('EPUB navigation points outside the packaged text.')
        digest, size = sha256(), 0
        with archive.open('EPUB/audio/master.mp3') as stream:
            while block := stream.read(1024 * 1024):
                check(); size += len(block); digest.update(block)
                if size > max_bytes:
                    raise ValueError('EPUB audio exceeds its verification budget.')
        if {'sha256': digest.hexdigest(), 'size': size} != provenance.get('audio', {}).get('identity'):
            raise ValueError('Packaged audio differs from the verified derivative identity.')
        check()
        return provenance



def inspect_epub(path, *, cancelled=lambda: False, max_bytes=32 * 1024**3):
    """Read-only verification of this bounded profile; independent EPUBCheck is separate."""
    _integer(max_bytes, 'EPUB byte budget', 1, 2 * 1024**4)
    try:
        return _inspect_epub(path, cancelled=cancelled, max_bytes=max_bytes)
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError, OverflowError) as exc:
        raise ValueError('The EPUB publication metadata is invalid or incomplete.') from exc


def _read(path, expected, check, *, copy_to=None, collect=False):
    if (type(expected) is not dict or set(expected) - {'name', 'size', 'sha256'}
            or type(expected.get('size')) is not int or expected['size'] < 0
            or not isinstance(expected.get('sha256'), str) or not _HASH.fullmatch(expected['sha256'])):
        raise ValueError('A source artifact has an invalid content receipt.')
    if collect and expected['size'] > MAX_JSON_BYTES:
        raise ValueError('Publication metadata exceeds its JSON budget.')
    total, hashed, chunks = 0, sha256(), []
    with ExitStack() as stack:
        source = stack.enter_context(verified_reader(path))
        if os.fstat(source.fileno()).st_size != expected['size']:
            raise ValueError('A source artifact changed size.')
        target = stack.enter_context(Path(copy_to).open('xb')) if copy_to is not None else None
        while block := source.read(1024 * 1024):
            check()
            total += len(block)
            if total > expected['size']:
                raise ValueError('A source artifact grew beyond its verified size.')
            hashed.update(block)
            if target is not None:
                target.write(block)
            if collect:
                chunks.append(block)
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
    check()
    if total != expected['size'] or hashed.hexdigest() != expected['sha256']:
        raise ValueError('A source artifact differs from its recorded checksum.')
    return b''.join(chunks) if collect else {'size': total, 'sha256': hashed.hexdigest()}


def _digest(path, check, maximum):
    info = regular_file(path)
    if info.st_size > maximum:
        raise ValueError('Publication exceeds its selected byte budget.')
    hashed, total = sha256(), 0
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        while block := stream.read(1024 * 1024):
            check()
            total += len(block)
            if total > maximum:
                raise ValueError('Publication grew beyond its selected byte budget.')
            hashed.update(block)
    check()
    if total != info.st_size:
        raise ValueError('Publication changed while being verified.')
    return {'size': total, 'sha256': hashed.hexdigest()}


def _json_snapshot(path, check):
    info = regular_file(path)
    if info.st_nlink != 1 or info.st_size > MAX_JSON_BYTES:
        raise ValueError('Publication metadata requires bounded independent files.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    check()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Publication metadata exceeds its JSON budget.')
    value = _json(raw)
    if type(value) is not dict:
        raise ValueError('Publication metadata requires a JSON object.')
    return value, {'size': len(raw), 'sha256': sha256(raw).hexdigest()}


def _publication_receipt(value, identity):
    if (not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity)
            or type(value) is not dict
            or set(value) != {'schema', 'id', 'status', 'format', 'project_root', 'output',
                             'artifact', 'source', 'created', 'notice'}
            or value['schema'] != 'resonance-publication/2' or value['id'] != identity
            or value['status'] not in ('prepared', 'complete')
            or value['format'] not in ('epub', 'daisy-2.02')
            or not isinstance(value['project_root'], str) or not Path(value['project_root']).is_absolute()
            or not isinstance(value['output'], str) or not Path(value['output']).is_absolute()):
        raise ValueError('Invalid saved publication receipt.')
    artifact = value['artifact']
    if (type(artifact) is not dict or set(artifact) != {'size', 'sha256'}
            or type(artifact['size']) is not int or artifact['size'] <= 0
            or not isinstance(artifact['sha256'], str) or not _HASH.fullmatch(artifact['sha256'])
            or type(value['source']) is not dict
            or not isinstance(value['source'].get('job_id'), str)
            or not _HASH.fullmatch(value['source']['job_id'])
            or not isinstance(value['source'].get('project_sha256'), str)
            or not _HASH.fullmatch(value['source']['project_sha256'])):
        raise ValueError('Invalid publication content or source receipt.')
    _text(value['notice'], 'Publication notice', maximum=8192)
    if not isinstance(value['created'], str) or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value['created']):
        raise ValueError('Invalid publication creation time.')
    return value


class PublicationService:
    """One adapter over this application's existing typed completed renders.

    Resource requests are optional for headless embedding. Desktop callers
    supply the existing per-library broker; no other renderer is constructed.
    """
    def __init__(self, project_dir, *, private_root, ffmpeg=None,
                 max_bytes=32 * 1024**3, timeout=3600, reserve_bytes=64 * 1024**2,
                 broker=None, operation_id=None, expected_plan_sha256=None):
        self.root = checked_directory(project_dir)
        self.private_root = checked_directory(private_root)
        self.ffmpeg = ffmpeg
        self.max_bytes = _integer(max_bytes, 'Publication byte budget', 1024, 2 * 1024**4)
        self.timeout = finite(timeout, 'Publication time budget', 1, 86400)
        self.reserve_bytes = _integer(reserve_bytes, 'Publication free-space reserve', 0, 2 * 1024**4)
        self.broker, self.operation_id = broker, operation_id
        if expected_plan_sha256 is not None and (not isinstance(expected_plan_sha256, str)
                or not _HASH.fullmatch(expected_plan_sha256)):
            raise ValueError('Expected publication plan identity is invalid.')
        self.expected_plan_sha256 = expected_plan_sha256

    def publications(self):
        """Bounded saved metadata; current output bytes need explicit verify."""
        rows, count = [], 0
        with os.scandir(self.root) as entries:
            for item in entries:
                count += 1
                if count > 100000:
                    raise ValueError('The audio project exceeds its publication discovery budget.')
                match = re.fullmatch(r'publication-([0-9a-f]{32})\.json', item.name)
                if match is not None:
                    value = read_json(self.root / item.name, limit=MAX_JSON_BYTES)
                    rows.append(_publication_receipt(value, match[1]))
                    if len(rows) > 1000:
                        raise ValueError('The project exceeds its publication receipt budget.')
        return tuple(sorted(rows, key=lambda row: (row['created'], row['id'])))

    def _source(self, run_key, check, progress):
        if not isinstance(run_key, str) or not _HASH.fullmatch(run_key):
            raise ValueError('Choose an exact completed production identity.')
        directory = checked_directory(checked_directory(self.root / 'renders') / run_key)
        result, result_proof = _json_snapshot(directory / 'complete.json', check)
        raw_plan, plan_file_proof = _json_snapshot(self.root / ('plan-' + run_key + '.json'), check)
        plan = ProductionPlan.from_dict(raw_plan)
        if self.expected_plan_sha256 is not None and digest(plan.to_dict()) != self.expected_plan_sha256:
            raise ValueError('The current Studio plan differs from this completed audio. Render the reviewed plan first.')
        if (result.get('schema_version') != 1 or result.get('status') != 'complete'
                or result.get('run_key') != run_key or result.get('source_sha256') != plan.source_sha256
                or result.get('plan_sha256') != digest(plan.to_dict())
                or type(result.get('sample_rate')) is not int or result['sample_rate'] != plan.sample_rate
                or type(result.get('frames')) is not int or result['frames'] < 1):
            raise ValueError('The completed audio lacks its exact typed-plan binding. '
                             'Render / resume this plan to verify and bind a legacy receipt first.')
        artifacts = result.get('files')
        if type(artifacts) is not list or not 3 <= len(artifacts) <= 100000:
            raise ValueError('The completed production has no bounded artifact manifest.')
        total, names, paths, documents = 0, set(), {}, {}
        for index, artifact in enumerate(artifacts, 1):
            check()
            if (type(artifact) is not dict or set(artifact) != {'name', 'size', 'sha256'}
                    or not isinstance(artifact['name'], str)):
                raise ValueError('Invalid completed artifact receipt.')
            name = filename_component(artifact['name'])
            if name.casefold() in names or '/' in name or '\\' in name:
                raise ValueError('Duplicate or non-local completed artifact name.')
            names.add(name.casefold())
            _integer(artifact['size'], 'Completed artifact size', 0, self.max_bytes)
            total += artifact['size']
            if total > self.max_bytes:
                raise ValueError('The completed artifact graph exceeds the publication byte budget.')
            path = directory / name
            progress(f'Verifying completed audio artifact {index}/{len(artifacts)}')
            raw = _read(path, artifact, check, collect=name == 'timeline.json')
            paths[name] = path, artifact
            if name == 'timeline.json':
                documents[name] = _json(raw)
        if not {'master.wav', 'timeline.json', 'subtitles.srt'} <= paths.keys():
            raise ValueError('The completed production lacks its WAV master or measured timeline.')
        timeline = documents['timeline.json']
        _, _, duration = _segments(plan, timeline, check)
        info = audio.wav_info(paths['master.wav'][0])
        if (info.frames != result['frames'] or info.rate != plan.sample_rate or info.channels != 2
                or timeline['chapters'] != result.get('chapters')
                or result.get('input_kind', 'script') != plan.input_kind
                or duration != info.seconds
                or finite(result.get('duration_seconds'), 'Completed duration', 0, 365 * 86400) != info.seconds):
            raise ValueError('The actual WAV master differs from its measured plan and timeline.')
        proof = {'job_id': run_key, 'job_sha256': result_proof['sha256'],
            'project_sha256': digest(plan.to_dict()), 'plan_file_sha256': plan_file_proof['sha256'],
            'source_script_sha256': plan.source_sha256,
            'artifacts': [{'name': name, 'size': receipt['size'], 'sha256': receipt['sha256']}
                          for name, (_, receipt) in sorted(paths.items())]}
        return plan, timeline, paths, proof, info

    def export_epub(self, run_key, output, *, language='und', creator='', cancelled=lambda: False,
                    progress=None, approval=None):
        return self._export(run_key, output, book_format='epub', language=language, creator=creator,
                            publisher='', cancelled=cancelled, progress=progress, approval=approval)

    def export_daisy(self, run_key, output, *, publisher, language='und', creator='',
                     cancelled=lambda: False, progress=None, approval=None):
        return self._export(run_key, output, book_format='daisy-2.02', language=language,
                            creator=creator, publisher=publisher, cancelled=cancelled,
                            progress=progress, approval=approval)

    def _export(self, run_key, output, *, book_format, language, creator, publisher,
                cancelled, progress, approval):
        if book_format == 'epub':
            label, suffix, notice, inspect = 'EPUB', '.epub', NOTICE, inspect_epub
            schema, audio_member = 'resonance-epub/2', 'EPUB/audio/master.mp3'
        else:
            from . import daisy
            daisy.language_tag(language)
            publisher = _text(publisher, 'DAISY publisher', maximum=512)
            label, suffix, notice, inspect = 'DAISY', '.zip', daisy.NOTICE, daisy.inspect_daisy
            schema, audio_member = 'resonance-daisy-2.02/2', 'master.mp3'
        language = language_tag(language)
        creator = _text(creator, 'Creator', maximum=512, empty=True)
        captured = capture_export_destination(output, private_root=self.private_root)
        if approval is not None and (type(approval) is not ExportDestination or approval != captured):
            raise ValueError('The selected export destination changed. Choose it again.')
        approval = captured if approval is None else approval
        output, parent = approval.target, checked_directory(approval.target.parent)
        if not approval.require_absent:
            raise FileExistsError('Choose a new publication filename; completed books are not overwritten.')
        if output.suffix.casefold() != suffix:
            raise ValueError('Choose a new ' + suffix + ' publication filename.')
        if output.is_relative_to(self.root):
            raise ValueError('Choose a publication outside its source audio project.')
        started, identity = monotonic(), uuid4().hex
        live_stage, lease, last_budget_check = None, None, 0.0
        receipt_path = self.root / ('publication-' + identity + '.json')
        observer = progress

        def progress(message):
            if observer is not None:
                try:
                    observer(message)
                except Exception:
                    pass

        def check():
            nonlocal last_budget_check
            if cancelled() or (lease is not None and lease.cancelled()):
                raise PublicationCancelled('Publication cancelled; existing completed books are retained.')
            now = monotonic()
            if now - started > self.timeout:
                raise TimeoutError('Publication exceeded its selected time budget.')
            if live_stage is not None and now - last_budget_check >= .25:
                last_budget_check = now
                total = 0
                with os.scandir(live_stage) as entries:
                    for count, item in enumerate(entries, 1):
                        if count > 16:
                            raise ValueError('Publication staging has unexpected files.')
                        total += regular_file(Path(live_stage) / item.name).st_size
                if total > self.max_bytes:
                    raise ValueError('Publication exceeded its working-disk byte budget.')
                if shutil.disk_usage(parent).free < self.reserve_bytes:
                    raise OSError('Publication reached its selected free-space reserve.')

        def external_stop():
            return cancelled() or monotonic() - started > self.timeout

        def stopping():
            check()
            return False

        def scope():
            check()
            current = capture_export_destination(approval.requested, private_root=self.private_root)
            if current.state is not None:
                raise FileExistsError('The selected publication filename became occupied.')
            if current != approval:
                raise ValueError('The selected publication directory changed.')

        with project_lock(self.root), ExitStack() as resources:
            plan, timeline, paths, source_proof, info = self._source(run_key, check, progress)
            # An independent WAV copy and decoded verification copy never
            # coexist. Budget also covers MP3+ZIP, XML and a fixed reserve.
            rate = info.rate if info.rate in (16000, 22050, 24000, 32000, 44100, 48000) else 48000
            estimated_mp3 = math.ceil(info.seconds * 24000) + 1024**2
            decoded_bytes = math.ceil(info.frames * rate / info.rate) * 4 + 16384
            working = max(info.data_size + 16384, decoded_bytes) + estimated_mp3 * 2 + MAX_XML_TOTAL + 1024**2
            if working > self.max_bytes:
                raise ValueError('Verified publication staging exceeds the selected byte budget.')
            if shutil.disk_usage(parent).free < working + self.reserve_bytes:
                raise OSError('Not enough free disk for verified publication staging and its reserve.')
            if self.broker is not None:
                from ..operations import ResourceBroker, ResourceRequest
                if not isinstance(self.broker, ResourceBroker) or self.broker.limits.memory_bytes == 0:
                    raise ValueError('Configure positive shared local resources for publication.')
                request = ResourceRequest(cpu_slots=self.broker.limits.cpu_slots,
                    memory_bytes=self.broker.limits.memory_bytes, disk_bytes=working, disk_path=parent)
                progress('Waiting for shared local publication resources.')
                lease = resources.enter_context(self.broker.reserve(request,
                    operation_id=self.operation_id or 'publication:' + identity,
                    cancelled=external_stop, timeout=min(60, self.timeout), run_timeout=self.timeout))
            scope()
            with TemporaryDirectory(prefix='.publication-', dir=parent) as temporary:
                stage = live_stage = checked_directory(temporary)
                pcm, encoded, decoded = stage / 'source.wav', stage / 'master.mp3', stage / 'decoded.wav'
                master, master_proof = paths['master.wav']
                _read(master, master_proof, check, copy_to=pcm)
                from .runtime import resolve_ffmpeg
                ffmpeg = resolve_ffmpeg(self.ffmpeg)
                if ffmpeg is None:
                    raise ValueError('Select an installed reviewed FFmpeg executable for publication.')
                processor = audio.ffmpeg_identity(ffmpeg, cancelled=stopping)
                progress('Encoding verified local audio as MP3')
                # One final output-rate sample covers fractional resampling
                # endpoints; it is padding, never an invented spoken interval.
                audio.run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'wav', '-i', str(pcm),
                    '-map', '0:a:0', '-af', f'aresample={rate},apad=pad_len=1', '-ac', '2',
                    '-c:a', 'libmp3lame', '-b:a', '192k', '-f', 'mp3', '-n', str(encoded)], cancelled=stopping)
                check()
                pcm.unlink()
                progress('Decoding the packaged MP3 to verify actual duration and format')
                audio.run_ffmpeg(ffmpeg, ['-protocol_whitelist', 'file', '-f', 'mp3', '-i', str(encoded),
                    '-map', '0:a:0', '-c:a', 'pcm_s16le', '-f', 'wav', '-rf64', 'always',
                    '-n', str(decoded)], cancelled=stopping)
                observed = audio.wav_info(decoded)
                if (observed.rate != rate or observed.channels != 2
                        or observed.frames < math.ceil(info.frames * rate / info.rate)
                        or observed.frames > math.ceil(info.frames * rate / info.rate) + 2304):
                    raise ValueError('The encoded MP3 does not cover the exact measured master within codec padding.')
                decoded.unlink()
                audio_identity = _digest(encoded, check, self.max_bytes)
                provenance = {'schema': schema, 'publication_id': identity,
                    'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'source': source_proof,
                    'audio': {'identity': audio_identity, 'media_type': 'audio/mpeg', 'codec': 'mp3',
                        'duration': observed.seconds, 'sample_rate': observed.rate, 'channels': observed.channels,
                        'decoded_frames': observed.frames, 'source_artifact': 'master.wav', 'reused': False,
                        'processor': processor, 'resampling_tail_samples': 1},
                    'validation': {'zip_xml_graph': 'verified on export', 'decoded_mp3': 'verified on export',
                        'epubcheck' if book_format == 'epub' else 'independent_daisy_validator': 'NOT_RUN',
                        'reader_interoperability': 'NOT_RUN'}}
                parts = (_parts(plan, timeline, provenance, language, creator, check) if book_format == 'epub'
                         else daisy.parts(plan, timeline, provenance, language, creator, publisher, check))
                raw_size = sum(len(raw) for raw in parts.values()) + audio_identity['size'] + 20
                if raw_size + audio_identity['size'] > self.max_bytes or sum(len(raw) for raw in parts.values()) > MAX_XML_TOTAL:
                    raise ValueError('Publication text and audio exceed their bounded staging budget.')
                staged = stage / ('publication' + suffix)
                progress('Packaging text, navigation and measured media overlays')
                with ZipFile(staged, 'x', compression=ZIP_DEFLATED, allowZip64=True) as archive:
                    if book_format == 'epub':
                        mimetype = ZipInfo('mimetype')
                        mimetype.compress_type = ZIP_STORED
                        archive.writestr(mimetype, b'application/epub+zip')
                    for name, raw in parts.items():
                        check()
                        archive.writestr(name, raw)
                    member = ZipInfo(audio_member)
                    member.compress_type = ZIP_STORED
                    hashed, count = sha256(), 0
                    with verified_reader(encoded) as source, archive.open(member, 'w', force_zip64=True) as target:
                        while block := source.read(1024 * 1024):
                            check()
                            count += len(block)
                            if count > audio_identity['size']:
                                raise ValueError('Staged MP3 grew during publication.')
                            hashed.update(block)
                            target.write(block)
                    if {'size': count, 'sha256': hashed.hexdigest()} != audio_identity:
                        raise ValueError('The staged MP3 changed during packaging.')
                inspect(staged, cancelled=stopping, max_bytes=self.max_bytes)
                artifact = _digest(staged, check, self.max_bytes)
                progress('Rechecking exact source receipts before publication')
                if self._source(run_key, check, lambda _: None)[3] != source_proof:
                    raise ValueError('The completed source changed during publication.')
                scope()
                record = {'schema': 'resonance-publication/2', 'id': identity, 'status': 'prepared',
                    'format': book_format, 'project_root': str(self.root), 'output': str(output),
                    'artifact': artifact, 'source': source_proof, 'created': provenance['created'], 'notice': notice}
                if os.path.lexists(receipt_path):
                    raise FileExistsError('The new publication receipt identity is already occupied.')
                atomic_json(receipt_path, record)
                progress('Publication prepared; recovery receipt ' + identity)
                scope()
                try:
                    publish_noreplace(staged, output, cancelled=stopping)
                    # The move is the publication boundary. Late cancellation
                    # cannot erase a completed book or turn it into a retry.
                    final_deadline = monotonic() + min(60, self.timeout)

                    def final_check():
                        if monotonic() > final_deadline:
                            raise TimeoutError('The published book needs explicit receipt verification.')

                    if _digest(output, final_check, self.max_bytes) != artifact:
                        raise ValueError('The published book changed before final receipt verification.')
                    record['status'] = 'complete'
                    atomic_json(receipt_path, record)
                except PublicationCancelled:
                    # The final publication callback runs only before the move.
                    # A prepared receipt is retained, but no new book exists.
                    raise
                except Exception as exc:
                    raise RuntimeError('Publication needs explicit verification using saved receipt '
                                       + identity + '. Do not repeat the export over an existing book.') from exc
                progress(label + ' saved and its exact bytes verified')
                return record

    def verify(self, publication_id, *, output=None, cancelled=lambda: False):
        """Verify bytes offline; restored external paths require explicit selection."""
        if not isinstance(publication_id, str) or not re.fullmatch('[0-9a-f]{32}', publication_id):
            raise ValueError('Choose a saved publication receipt identity.')
        started = monotonic()

        def check():
            if cancelled():
                raise PublicationCancelled('Publication verification cancelled.')
            if monotonic() - started > self.timeout:
                raise TimeoutError('Publication verification exceeded its time budget.')

        with project_lock(self.root):
            path = self.root / ('publication-' + publication_id + '.json')
            record = _publication_receipt(read_json(path, limit=MAX_JSON_BYTES), publication_id)
            if record['project_root'] != str(self.root) and output is None:
                raise ValueError('This receipt was restored elsewhere. Explicitly select the existing book to verify.')
            selected = local_path(output if output is not None else record['output'])
            if _digest(selected, check, self.max_bytes) != record['artifact']:
                raise ValueError('The selected publication differs from its saved content receipt.')
            inspect = inspect_epub
            if record['format'] == 'daisy-2.02':
                from .daisy import inspect_daisy
                inspect = inspect_daisy
            provenance = inspect(selected,
                cancelled=lambda: cancelled() or monotonic() - started > self.timeout, max_bytes=self.max_bytes)
            if provenance.get('publication_id') != publication_id or provenance.get('source') != record['source']:
                raise ValueError('Publication provenance differs from the saved source receipt.')
            if _digest(selected, check, self.max_bytes) != record['artifact']:
                raise ValueError('The publication changed during structural verification.')
            check()
            # Recover a move whose directory fsync failed before its receipt
            # could be completed. Verification never rewrites the book itself.
            _sync_directory(selected.parent)
            record.update(status='complete', project_root=str(self.root), output=str(selected))
            atomic_json(path, record)
            return record
