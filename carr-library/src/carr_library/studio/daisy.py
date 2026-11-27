"""Bounded DAISY 2.02 interchange from the existing measured Studio timeline.

The ZIP transports a conventional NCC/XHTML/SMIL/MP3 file set. It is not a
claim of hardware-reader, human listening or tactile/Braille qualification.
"""
from hashlib import sha256
import re
import stat
from uuid import UUID
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from ..audio.models import AudioCancelled, finite
from ..durable_state import MAX_JSON_BYTES, encode, local_path, regular
from .publishing import (CSS, XHTML, _clock, _element, _json, _segments,
                         _text, _xml)


NOTICE = ('DAISY 2.02 interchange uses measured Studio line intervals, not word alignment. '
          'Audio without a scripted line remains explicitly untranscribed. Headings, role labels '
          'and the retained original script are not narrated. Extract the complete ZIP file set '
          'for a compatible reader. Independent DAISY validation, reader interoperability, '
          'human listening and Braille are not established by this export.')
NAMES = {'ncc.html', 'content.html', 'source.html', 'book.smil',
         'styles.css', 'provenance.json', 'master.mp3'}
HTML_DTD = (b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
            b'"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">')
SMIL_DTD = (b'<!DOCTYPE smil PUBLIC "-//W3C//DTD SMIL 1.0//EN" '
            b'"http://www.w3.org/TR/REC-SMIL/SMIL10.dtd">')


def language_tag(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2})?', value):
        raise ValueError('DAISY 2.02 requires a two/three-letter language, optionally followed by a country code.')
    return value


def _document(title, language):
    root = ET.Element('html', {'xmlns': XHTML, 'lang': language, 'xml:lang': language})
    head = _element(root, 'head')
    _element(head, 'title', title)
    _element(head, 'meta', **{'http-equiv': 'Content-Type', 'content': 'text/html; charset=utf-8'})
    _element(head, 'link', rel='stylesheet', href='styles.css', type='text/css')
    return root, head, _element(root, 'body')


def _serialize(element, doctype):
    declaration, body = _xml(element).split(b'\n', 1)
    return declaration + b'\n' + doctype + b'\n' + body


def _metadata(head, values):
    for name, value in values.items():
        _element(head, 'meta', name=name, content=str(value))


def _ncc_clock(value):
    # NCC uses a full hh:mm:ss clock, optionally with fractional seconds.
    # Preserve measured microseconds and carry rounding across minute/hour boundaries.
    micros = int(round(finite(value, 'NCC duration', minimum=0) * 1_000_000))
    hours, remainder = divmod(micros, 3_600_000_000)
    minutes, seconds = divmod(remainder, 60_000_000)
    final = f'{seconds / 1_000_000:09.6f}'.rstrip('0').rstrip('.')
    return f'{hours:02}:{minutes:02}:{final}'


def parts(project, timeline, provenance, language, creator, publisher, check):
    language_tag(language)
    _text(publisher, 'DAISY publisher', maximum=512)
    chapters, segments, duration = _segments(project, timeline, check)
    roles = {role.id: _text(role.name, 'Role name', maximum=256) for role in project.roles}
    grouped = {chapter['id']: [] for chapter in chapters}
    for segment in segments:
        grouped[segment['chapter']].append(segment)
    # A zero-duration scene has no audio target. Preserve its text in the
    # content document without inventing a navigable audio interval.
    audible_chapters = [chapter for chapter in chapters if grouped[chapter['id']]]
    ncc, head, navigation = _document(project.title, language)
    identifier = 'urn:uuid:' + str(UUID(hex=provenance['publication_id']))
    metadata = {'dc:title': project.title, 'dc:identifier': identifier,
        'dc:date': provenance['created'][:10], 'dc:format': 'Daisy 2.02',
        'dc:language': language, 'dc:publisher': publisher, 'ncc:charset': 'utf-8',
        'ncc:tocItems': len(audible_chapters) + 1, 'ncc:totalTime': _ncc_clock(duration),
        'ncc:pageFront': 0, 'ncc:pageNormal': 0, 'ncc:pageSpecial': 0,
        'ncc:depth': 2, 'ncc:multimediaType': 'audioPartText', 'ncc:generator': 'Resonance',
        'ncc:files': len(NAMES)}
    if creator:
        metadata['dc:creator'] = creator
    _metadata(head, metadata)
    _element(head, 'link', rel='related', href='provenance.json', type='application/json')
    _element(head, 'link', rel='related', href='source.html', type='application/xhtml+xml')
    title = _element(navigation, 'h1', id='title', **{'class': 'title'})
    _element(title, 'a', project.title, href='book.smil#par-' + segments[0]['id'])
    content, content_head, body = _document(project.title, language)
    _metadata(content_head, {'dc:identifier': identifier})
    _element(body, 'h1', project.title)
    _element(body, 'p', NOTICE)
    overlay = ET.Element('smil')
    overlay_head = _element(overlay, 'head')
    _metadata(overlay_head, {'dc:format': 'Daisy 2.02', 'dc:identifier': identifier,
        'dc:title': project.title, 'ncc:totalElapsedTime': '0.000000s',
        'ncc:timeInThisSmil': _clock(duration), 'ncc:generator': 'Resonance'})
    _element(_element(overlay_head, 'layout'), 'region', id='text-view')
    sequence = _element(_element(overlay, 'body'), 'seq', dur=_clock(duration))
    for chapter in chapters:
        check()
        if grouped[chapter['id']]:
            heading = _element(navigation, 'h2', id=chapter['id'], **{'class': 'chapter'})
            _element(heading, 'a', chapter['title'], href='book.smil#par-' + grouped[chapter['id']][0]['id'])
        section = _element(body, 'div', id=chapter['id'])
        _element(section, 'h2', chapter['title'])
        for segment in grouped[chapter['id']]:
            check()
            paragraph = _element(section, 'p', **{'class': 'spoken' if segment['kind'] == 'line' else 'audio-interval'})
            if segment['role'] is not None:
                _element(paragraph, 'span', roles[segment['role']] + ': ', **{'class': 'role'})
            _element(paragraph, 'span', segment['text'], id=segment['id'])
            par = _element(sequence, 'par', id='par-' + segment['id'], endsync='last')
            _element(par, 'text', id='text-' + segment['id'], src='content.html#' + segment['id'], region='text-view')
            _element(par, 'audio', id='audio-' + segment['id'], src='master.mp3',
                **{'clip-begin': 'npt=' + _clock(segment['start']), 'clip-end': 'npt=' + _clock(segment['end'])})
    source, source_head, source_body = _document('Original script and provenance', language)
    _metadata(source_head, {'dc:identifier': identifier})
    _element(source_body, 'h1', 'Original script and provenance')
    _element(source_body, 'p', NOTICE)
    _element(source_body, 'pre', project.original_script, id='original-script', **{'class': 'source'})
    provenance.update(original_script=project.original_script, title=project.title,
        publisher=publisher, creator=creator, language=language, chapters=audible_chapters,
        roles=[{'id': role.id, 'name': role.name, 'language': role.language} for role in project.roles],
        intervals=[{key: value for key, value in segment.items() if key != 'text'} |
                   {'text_sha256': sha256(segment['text'].encode()).hexdigest()} for segment in segments],
        duration=duration, timing_notice=NOTICE)
    return {'ncc.html': _serialize(ncc, HTML_DTD), 'content.html': _serialize(content, HTML_DTD),
        'source.html': _serialize(source, HTML_DTD), 'book.smil': _serialize(overlay, SMIL_DTD),
        'styles.css': CSS, 'provenance.json': encode(provenance)}


def inspect_daisy(path, *, cancelled=lambda: False, max_bytes=32 * 1024**3):
    """Validate this producer's exact bounded profile; never fetch DTD/resources."""
    path = local_path(path, exists=True)
    if regular(path).st_size > max_bytes:
        raise ValueError('DAISY publication exceeds its byte budget.')
    def check():
        if cancelled():
            raise AudioCancelled('DAISY publication verification cancelled.')
    with ZipFile(path) as archive:
        return _inspect_daisy_archive(archive, check, max_bytes)


def _inspect_daisy_archive(archive, check, max_bytes):
    entries = archive.infolist()
    if len(entries) != len(NAMES) or {entry.filename for entry in entries} != NAMES:
        raise ValueError('DAISY publication has an unexpected or duplicate file set.')
    if (sum(entry.file_size for entry in entries) > max_bytes
            or any(entry.flag_bits & 1 or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16)
                   or entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for entry in entries)):
        raise ValueError('DAISY publication contains unsupported or oversized archive members.')
    def read(name):
        check()
        if archive.getinfo(name).file_size > MAX_JSON_BYTES:
            raise ValueError('DAISY document exceeds its bounded metadata budget.')
        with archive.open(name) as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError('DAISY document grew beyond its metadata budget.')
        check()
        return raw
    def document(name):
        raw = read(name)
        expected = SMIL_DTD if name.endswith('.smil') else HTML_DTD
        # The exact fixed external identifier is declarative. Remove it
        # before parsing; entities or any additional declaration fail.
        if raw.count(expected) != 1:
            raise ValueError('DAISY document has an unsupported type declaration.')
        raw = raw.replace(expected, b'', 1)
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('DAISY document cannot declare external entities.')
        result = ET.fromstring(raw)
        ids = [node.get('id') for node in result.iter() if node.get('id') is not None]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', value) for value in ids):
            raise ValueError('DAISY document has duplicate or invalid fragment identifiers.')
        return result
    provenance = _json(read('provenance.json'))
    if (not isinstance(provenance, dict) or provenance.get('schema') != 'resonance-daisy-2.02/1'
            or not re.fullmatch('[0-9a-f]{32}', provenance.get('publication_id', ''))):
        raise ValueError('Invalid DAISY publication provenance.')
    duration = finite(provenance.get('duration'), 'DAISY duration', minimum=0)
    if duration <= 0 or not isinstance(provenance.get('chapters'), list) or not provenance['chapters']:
        raise ValueError('DAISY provenance requires positive duration and measured chapters.')
    language_tag(provenance.get('language'))
    _text(provenance.get('publisher'), 'DAISY publisher', maximum=512)
    content, source, ncc, smil = [document(name) for name in ('content.html', 'source.html', 'ncc.html', 'book.smil')]
    ns = '{' + XHTML + '}'
    allowed = {'html': {'lang', '{http://www.w3.org/XML/1998/namespace}lang'},
        'head': set(), 'title': set(), 'meta': {'http-equiv', 'content', 'name'},
        'link': {'rel', 'href', 'type'}, 'body': set(), 'h1': {'id', 'class'},
        'h2': {'id', 'class'}, 'div': {'id'}, 'p': {'class'}, 'span': {'class', 'id'},
        'pre': {'id', 'class'}, 'a': {'href'}}
    for root in (content, source, ncc):
        if root.tag != ns + 'html' or root.get('lang') != provenance['language']:
            raise ValueError('DAISY XHTML language or document root differs from provenance.')
        for node in root.iter():
            local = node.tag.removeprefix(ns)
            if node.tag != ns + local or local not in allowed or not set(node.attrib) <= allowed[local]:
                raise ValueError('DAISY profile does not permit active or unsupported XHTML content.')
            for key in ('src', 'href'):
                if key in node.attrib and node.get(key).split('#')[0] not in NAMES:
                    raise ValueError('DAISY content points outside the retained file set.')
    original = source.find('.//' + ns + 'pre')
    if original is None or ''.join(original.itertext()) != provenance.get('original_script'):
        raise ValueError('DAISY original script differs from provenance.')
    text_nodes = {node.get('id'): ''.join(node.itertext()) for node in content.iter() if node.get('id')}
    sequence = smil.find('body/seq')
    intervals = provenance.get('intervals')
    smil_body = smil.find('body')
    smil_head = smil.find('head')
    layout = smil.find('head/layout')
    if (smil.tag != 'smil' or smil.attrib or [node.tag for node in smil] != ['head', 'body']
            or smil_body is None or smil_body.attrib or smil_head.attrib
            or len(smil_body) != 1 or sequence is None
            or set(sequence.attrib) != {'dur'}
            or layout is None or smil_head[-1] is not layout or layout.attrib
            or len(layout) != 1 or layout[0].tag != 'region' or layout[0].attrib != {'id': 'text-view'}
            or len(layout[0])
            or any(node.tag != 'meta' or set(node.attrib) != {'name', 'content'} or len(node)
                   for node in list(smil_head)[:-1])
            or not isinstance(intervals, list) or not intervals or len(sequence) != len(intervals)):
        raise ValueError('DAISY synchronization graph is incomplete.')
    cursor = 0.0
    for par, expected in zip(sequence, intervals):
        check()
        if (not isinstance(expected, dict) or not isinstance(expected.get('id'), str)
                or not isinstance(expected.get('text_sha256'), str)):
            raise ValueError('Invalid DAISY interval provenance.')
        begin = finite(expected.get('start'), 'DAISY interval start', minimum=0)
        end = finite(expected.get('end'), 'DAISY interval end', minimum=0)
        text, audio = par.find('text'), par.find('audio')
        if (par.tag != 'par' or par.get('id') != 'par-' + expected['id'] or par.get('endsync') != 'last'
                or set(par.attrib) != {'id', 'endsync'}
                or text is None or audio is None or [node.tag for node in par] != ['text', 'audio']
                or len(text) or len(audio) or set(text.attrib) != {'id', 'src', 'region'}
                or text.get('region') != 'text-view'
                or set(audio.attrib) != {'id', 'src', 'clip-begin', 'clip-end'}
                or text.get('id') != 'text-' + expected['id'] or text.get('src') != 'content.html#' + expected['id']
                or audio.get('id') != 'audio-' + expected['id'] or audio.get('src') != 'master.mp3'
                or audio.get('clip-begin') != 'npt=' + _clock(expected['start'])
                or audio.get('clip-end') != 'npt=' + _clock(expected['end'])
                or end <= begin or abs(begin - cursor) > 1e-6 or end > duration + 1e-6
                or sha256(text_nodes.get(expected['id'], '').encode()).hexdigest() != expected['text_sha256']):
            raise ValueError('DAISY interval differs from its exact text/audio provenance.')
        cursor = expected['end']
    if sequence.get('dur') != _clock(provenance['duration']) or abs(cursor - provenance['duration']) > 1e-6:
        raise ValueError('DAISY synchronization duration is incomplete.')
    metadata_nodes = ncc.findall(ns + 'head/' + ns + 'meta[@name]')
    metadata = {node.get('name'): node.get('content') for node in metadata_nodes}
    required = {'dc:format': 'Daisy 2.02', 'dc:identifier': 'urn:uuid:' + str(UUID(hex=provenance['publication_id'])),
        'dc:title': provenance['title'], 'dc:date': provenance['created'][:10],
        'dc:language': provenance['language'], 'dc:publisher': provenance['publisher'],
        'ncc:charset': 'utf-8', 'ncc:pageFront': '0', 'ncc:pageNormal': '0', 'ncc:pageSpecial': '0',
        'ncc:totalTime': _ncc_clock(provenance['duration']), 'ncc:files': str(len(NAMES)),
        'ncc:tocItems': str(len(provenance['chapters']) + 1)}
    if len(metadata) != len(metadata_nodes) or any(metadata.get(key) != value for key, value in required.items()):
        raise ValueError('DAISY navigation metadata differs from its publication provenance.')
    navigation = ncc.find(ns + 'body')
    targets = [('title', provenance['title'], intervals[0]['id'])]
    for chapter in provenance['chapters']:
        first = next((row for row in intervals if row['chapter'] == chapter['id']), None)
        if first is None:
            raise ValueError('DAISY chapter has no measured audio target.')
        targets.append((chapter['id'], chapter['title'], first['id']))
    if navigation is None or len(navigation) != len(targets):
        raise ValueError('DAISY chapter navigation is incomplete.')
    for index, (node, (identity, label, interval)) in enumerate(zip(navigation, targets)):
        link = node.find(ns + 'a')
        if (node.tag != ns + ('h1' if index == 0 else 'h2') or node.get('id') != identity
                or link is None or len(node) != 1 or ''.join(link.itertext()) != label
                or link.get('href') != 'book.smil#par-' + interval):
            raise ValueError('DAISY navigation does not target the declared measured chapter.')
    smil_meta = {node.get('name'): node.get('content') for node in smil.findall('head/meta')}
    if (len(smil_meta) != len(smil_head) - 1 or smil_meta.get('dc:format') != 'Daisy 2.02'
            or smil_meta.get('dc:identifier') != required['dc:identifier']
            or smil_meta.get('ncc:timeInThisSmil') != _clock(provenance['duration'])):
        raise ValueError('DAISY SMIL metadata differs from its publication identity.')
    digest, size = sha256(), 0
    with archive.open('master.mp3') as stream:
        while block := stream.read(1024 * 1024):
            check(); size += len(block); digest.update(block)
            if size > max_bytes:
                raise ValueError('DAISY audio exceeds its byte budget.')
    if {'size': size, 'sha256': digest.hexdigest()} != provenance.get('audio', {}).get('identity'):
        raise ValueError('DAISY audio differs from its verified source derivative.')
    if read('styles.css') != CSS:
        raise ValueError('DAISY stylesheet differs from the supported local profile.')
    check()
    return provenance
