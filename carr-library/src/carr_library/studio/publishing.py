"""Local EPUB 3 media overlays from verified, completed Studio productions.

Only measured line intervals are synchronized. Audio between scripted lines is
retained with explicit interval labels, never presented as an invented transcript.
The existing local audio runtime supplies MP3, an EPUB core audio media type.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import UUID, uuid4
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED, ZIP_STORED

from ..audio.models import AudioBudget, AudioCancelled, finite
from ..audio.process import publish_file
from ..durable_state import (MAX_JSON_BYTES, atomic_json, encode, local_path,
                             read_json, regular, state_lock)
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
          'EPUBCheck, third-party reader interoperability, DAISY and Braille are not qualified by this export.')
CSS = ('.spoken,.source{white-space:pre-wrap} .role{font-weight:bold} '
       '.audio-interval{font-style:italic} .active-line{background:#fff2a6;color:#111;outline:2px solid #111} '
       'body{line-height:1.5} pre{white-space:pre-wrap;overflow-wrap:anywhere}').encode()
_HASH = re.compile(r'[0-9a-f]{64}')
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
    if (type(record) is not dict or set(record) != fields
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
    path(record['output'], 'EPUB output')
    if not record['output'].lower().endswith('.epub'):
        raise ValueError('A publication receipt must declare an EPUB output.')
    artifact = record['artifact']
    if type(artifact) is not dict or set(artifact) != {'size', 'sha256'}:
        raise ValueError('Invalid completed EPUB artifact receipt.')
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



def _warning(result, message):
    result.setdefault('warnings', []).append(message)


@contextmanager
def _publication_staging(parent, completed):
    """Cleanup cannot turn an already verified publication into a failed export."""
    temporary = TemporaryDirectory(prefix='.pending-epub-', dir=parent)
    try:
        yield local_path(temporary.name, exists=True)
    finally:
        try:
            temporary.cleanup()
        except (OSError, KeyboardInterrupt) as exc:
            if completed.get('status') != 'complete':
                raise
            _warning(completed, 'The EPUB is saved and verified. Its temporary staging cleanup '
                     'did not finish: ' + str(exc)[:1000])


def _owned_publication_digest(staged, output, owner, parent_owner, expected, maximum):
    """Read only this attempt's linked output, before removing its staging name.

    The shared writer creates a second hard link. No generic source/state path
    rule is relaxed: both regular names must still identify the captured staging
    inode, and all read bytes must match the already inspected EPUB. A different
    file with identical contents cannot be mistaken for this publication.
    """
    local_path(staged.parent, exists=True)
    parent = regular(local_path(output.parent, exists=True), directory=True)
    if (parent.st_dev, parent.st_ino) != (parent_owner.st_dev, parent_owner.st_ino):
        raise ValueError('The selected output directory changed during publication.')

    def validate(info):
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or (info.st_dev, info.st_ino) != (owner.st_dev, owner.st_ino)
                or info.st_nlink != 2 or info.st_size != expected['size']):
            raise ValueError('The selected EPUB is not this attempt\'s atomically published file.')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_nlink)

    if expected['size'] > maximum:
        raise ValueError('Publication exceeds its byte budget.')
    validate(staged.lstat())
    before = validate(output.lstat())
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    digest, count = sha256(), 0
    with os.fdopen(os.open(output, flags), 'rb') as stream:
        if validate(os.fstat(stream.fileno())) != before:
            raise ValueError('The completed EPUB changed before verification.')
        while block := stream.read(1024 * 1024):
            count += len(block)
            if count > expected['size']:
                raise ValueError('The completed EPUB grew during verification.')
            digest.update(block)
        if (validate(os.fstat(stream.fileno())) != before
                or validate(output.lstat()) != before or validate(staged.lstat()) != before):
            raise ValueError('The completed EPUB changed during verification.')
    actual = {'size': count, 'sha256': digest.hexdigest()}
    if actual != expected:
        raise ValueError('The completed EPUB differs from its inspected publication bytes.')
    return actual


def _finalize_receipt(path, previous):
    """Return verified completion even when its saved status needs recovery."""
    completed = dict(previous, status='complete')
    try:
        atomic_json(path, completed, replace=True)
    except (Exception, KeyboardInterrupt) as exc:
        # A replacement may itself have succeeded before a later fsync/cleanup
        # failed. Report only what bounded readback can establish, without
        # turning the already verified output into a failed publication.
        saved = None
        try:
            saved = read_json(path)
        except (ValueError, OSError):
            pass
        known = saved == previous or saved == completed
        completed['receipt_status'] = saved['status'] if known else 'unconfirmed'
        completed['receipt_finalized'] = saved == dict(previous, status='complete')
        _warning(completed, 'The EPUB bytes are saved and verified, but receipt finalization '
                 'reported an interruption: ' + str(exc)[:1000] + '. Receipt: ' + previous['id']
                 + '. Verify this existing EPUB before considering another export.')
    return completed



@contextmanager
def _publication_resources(production, budget, parent, state, cancelled, completed):
    """Use the existing Studio coordinator for this bounded derivative export."""
    from ..operations import ResourceCancelled, ResourceRequest
    if state.get('status') != 'complete' or state.get('recovery', {}).get('requires_output_directory'):
        raise ValueError('Choose a completed Studio job with verified local output files.')
    artifacts = state.get('artifacts')
    if not isinstance(artifacts, list) or not 3 <= len(artifacts) <= 100000:
        raise ValueError('The source job has no bounded completed artifact manifest.')
    sizes = []
    for artifact in artifacts:
        if (not isinstance(artifact, dict) or type(artifact.get('size')) is not int
                or artifact['size'] < 0):
            raise ValueError('Invalid source artifact receipt.')
        sizes.append(artifact['size'])
    source_bytes = sum(sizes)
    if source_bytes > budget.max_output_bytes:
        raise ValueError('The complete source artifact graph exceeds the publication byte budget.')
    timeline = state.get('result')
    if not isinstance(timeline, dict):
        raise ValueError('The source job has no measured timeline.')
    duration = finite(timeline.get('duration'), 'Master duration', 1e-6, 365 * 86400)
    # The current MP3 converter uses 192 kbit/s. Reserve the larger of its
    # declared duration estimate and the existing source artifact, plus both
    # packaged/staged copies, bounded XML metadata and the selected free reserve.
    # These are conservative adapter estimates, not measured process peaks.
    encoded = max(max(sizes), math.ceil((duration + 1) * 192000 / 8) + 1024**2)
    request = ResourceRequest(cpu_slots=budget.threads,
        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
        disk_path=parent,
        disk_bytes=source_bytes + 2 * encoded + 4 * MAX_JSON_BYTES + budget.reserve_disk_bytes,
        additional_disks=((production.root, 2 * MAX_JSON_BYTES),))
    lease = None
    try:
        broker = production._admission_broker(cancelled)
        with broker.reserve(request, operation_id='publication:' + uuid4().hex,
                            cancelled=cancelled, timeout=min(60, budget.timeout),
                            run_timeout=budget.timeout) as lease:
            try:
                yield lease
            except (Exception, KeyboardInterrupt) as exc:
                if completed.get('status') != 'complete':
                    raise
                _warning(completed, 'The EPUB is saved and verified, but final notification '
                         'or cleanup was interrupted: ' + type(exc).__name__ + ': ' + str(exc)[:1000])
    except ResourceCancelled as exc:
        if completed.get('status') != 'complete':
            raise AudioCancelled('EPUB publication cancelled while waiting for shared resources.') from exc
        completed['resource_release_confirmed'] = False
        _warning(completed, 'The EPUB is saved and verified, but shared resource cleanup '
                 'reported an interruption: ' + str(exc)[:1000])
    except (Exception, KeyboardInterrupt) as exc:
        if completed.get('status') != 'complete':
            raise
        completed['resource_release_confirmed'] = False
        _warning(completed, 'The EPUB is saved and verified. Shared resource release was not '
                 'confirmed: ' + type(exc).__name__ + ': ' + str(exc)[:1000])
    finally:
        if lease is not None and lease.cleanup_pending and completed.get('status') == 'complete':
            completed['resource_cleanup_pending'] = True
            _warning(completed, 'The EPUB is saved and verified. Shared resource ownership '
                     'was released; stale coordination metadata awaits normal cleanup.')


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
        if abs(start - cursor) > tolerance or end <= start:
            raise ValueError('Measured chapters must cover the master in order without overlaps or gaps.')
        chapter_rows.append({'id': f'chapter-{index}', 'scene': scene.id, 'title': scene.title})
        def append(kind, begin, finish, line=None):
            if round(finish, 6) <= round(begin, 6):
                return
            segments.append({'id': f'interval-{len(segments) + 1}', 'chapter': f'chapter-{index}',
                'kind': kind, 'start': round(begin, 6), 'end': round(finish, 6),
                'text': line.text if line is not None else 'Audio interval without a scripted line.',
                'role': line.role if line is not None else None,
                'line': line.id if line is not None else None, 'scene': scene.id,
                'source_ref': line.source_ref if line is not None else ''})
        for line, begin, finish in grouped[scene.id]:
            if begin < cursor - tolerance or finish > end + tolerance:
                raise ValueError('A speech interval overlaps another line or exceeds its measured chapter.')
            if begin > cursor:
                append('audio-interval', cursor, begin)
            append('line', max(begin, cursor), finish, line)
            cursor = finish
        if end > cursor:
            append('audio-interval', cursor, end)
        cursor = end
    if abs(cursor - duration) > tolerance or not segments:
        raise ValueError('The measured intervals do not cover the complete master.')
    # Match the measured master endpoint; all serialized times have microsecond
    # precision, finer than an audio sample at supported Studio sample rates.
    segments[-1]['end'] = round(duration, 6)
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
        return _inspect_epub_archive(archive, check, max_bytes)


def _inspect_epub_archive(archive, check, max_bytes):
    infos = archive.infolist()
    if (len(infos) != len(_NAMES) or {item.filename for item in infos} != _NAMES
            or infos[0].filename != 'mimetype' or infos[0].compress_type != ZIP_STORED
            or infos[0].header_offset != 0 or infos[0].extra or any(item.flag_bits & 1 for item in infos)
            or sum(item.file_size for item in infos) > max_bytes):
        raise ValueError('Unexpected EPUB container graph or size.')
    if archive.read('mimetype') != b'application/epub+zip':
        raise ValueError('Invalid EPUB mimetype member.')
    def document(name):
        check()
        if archive.getinfo(name).file_size > MAX_JSON_BYTES * 4:
            raise ValueError('EPUB XML exceeds its metadata budget.')
        chunks = []
        with archive.open(name) as stream:
            while block := stream.read(1024 * 1024):
                check(); chunks.append(block)
        raw = b''.join(chunks)
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('EPUB metadata must not contain entity declarations.')
        return ET.fromstring(raw)
    metadata = document('META-INF/metadata.xml')
    node = metadata.find('{' + PROVENANCE + '}provenance')
    if node is None or node.get('format') != 'application/json':
        raise ValueError('Missing derivative provenance.')
    provenance = _json((node.text or '').encode('utf-8'))
    if provenance.get('schema') != 'resonance-epub/1':
        raise ValueError('Unsupported EPUB provenance.')
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




def _receipt_epub(value, expected, check, maximum):
    """Verify explicit receipt bytes without following or cleaning another name.

    An abruptly stopped create-only writer can leave its staging hard link.
    Only this read-only receipt path permits one or two regular links. The
    generic inspector, source guards and private state rules remain unchanged.
    A byte-identical copied EPUB is valid for explicit receipt verification;
    this does not attribute that copy to a new publication attempt.
    """
    if (not isinstance(expected, dict) or set(expected) != {'size', 'sha256'}
            or type(expected['size']) is not int or not 0 < expected['size'] <= maximum
            or not isinstance(expected['sha256'], str) or not _HASH.fullmatch(expected['sha256'])):
        raise ValueError('Invalid publication artifact receipt.')
    if str(value).startswith(('//', '\\\\')) or '\x00' in str(value):
        raise ValueError('Choose a local publication file.')
    requested = Path(value).absolute()
    parent = local_path(requested.parent, exists=True)
    parent_info = regular(parent, directory=True)
    selected = parent / requested.name

    def validate(info):
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_nlink not in (1, 2) or info.st_size != expected['size']):
            raise ValueError('The selected publication differs from its exact completed receipt.')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_nlink)

    before = validate(selected.lstat())

    def stable(stream):
        try:
            observed_parent = regular(local_path(parent, exists=True), directory=True)
            current = validate(selected.lstat())
            opened = validate(os.fstat(stream.fileno()))
        except (ValueError, FileNotFoundError) as exc:
            raise ValueError('The publication changed during structural verification.') from exc
        if ((observed_parent.st_dev, observed_parent.st_ino) != (parent_info.st_dev, parent_info.st_ino)
                or opened != before or current != before):
            raise ValueError('The publication changed during structural verification.')

    def digest(stream):
        stream.seek(0)
        hasher, count = sha256(), 0
        while block := stream.read(1024 * 1024):
            check()
            count += len(block)
            if count > expected['size']:
                raise ValueError('The publication changed during structural verification.')
            hasher.update(block)
        if {'size': count, 'sha256': hasher.hexdigest()} != expected:
            raise ValueError('The selected publication differs from its exact completed receipt.')
        stable(stream)
        check()

    check()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(selected, flags), 'rb') as stream:
        stable(stream)
        digest(stream)
        stream.seek(0)
        with ZipFile(stream) as archive:
            provenance = _inspect_epub_archive(archive, check, maximum)
        # Parsing may race an in-place write or a pathname replacement. Recheck
        # the descriptor, all bytes and the currently selected name together.
        stable(stream)
        digest(stream)
    return selected, provenance, before[-1]


class PublicationService:
    def __init__(self, production, *, budget=None):
        self.production = production
        self.root = local_path(production.root, exists=True)
        self.budget = budget or AudioBudget()
        if not isinstance(self.budget, AudioBudget):
            raise TypeError('Use the shared AudioBudget for publication limits.')
        self.receipts = self.root / 'publications'
        self.private_root = (self.root.parent.parent if self.root.name == 'studio'
                             and self.root.parent.name == 'state' else self.root)

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
            value = read_json(path)
            if (value.get('schema') != 'resonance-publication/1' or value.get('id') != path.stem
                    or value.get('status') not in ('prepared', 'complete')):
                raise ValueError('Invalid saved publication receipt.')
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

    def prepare_destination(self, output):
        """Capture the existing export approval before a UI queues background work."""
        from ..research.export_file import capture_export_destination
        output = local_path(output)
        if output.suffix.lower() != '.epub':
            raise ValueError('Choose a new .epub filename.')
        local_path(output.parent, exists=True)
        if output.is_relative_to(self.private_root):
            raise ValueError('Choose an EPUB destination outside private library state.')
        if os.path.lexists(output):
            raise FileExistsError('The selected EPUB already exists; it will not be overwritten.')
        selected = capture_export_destination(output, private_root=self.private_root)
        if not selected.require_absent:
            raise FileExistsError('The selected EPUB already exists; it will not be overwritten.')
        return selected

    def export_epub(self, job_id, output, *, language='und', creator='', cancelled=lambda: False,
                    progress=None, approval=None):
        language = language_tag(language)
        creator = _text(creator, 'Creator', maximum=512, empty=True)
        job_id = _id(job_id)
        from ..research.export_file import ExportDestination
        selected = self.prepare_destination(output)
        if approval is not None and (not isinstance(approval, ExportDestination) or approval != selected):
            raise ValueError('The chosen EPUB destination changed before publication. Choose it again.')
        approval = selected if approval is None else approval
        output, parent = approval.target, approval.target.parent
        parent_info = regular(parent, directory=True)
        self.production.get(job_id)  # Unknown IDs must not create a job directory.
        started, lease = monotonic(), None
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
                raise AudioCancelled('EPUB publication cancelled; previous completed files are retained.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError('EPUB publication exceeded its selected time budget.')
            if lease is not None and lease.cancelled():
                raise AudioCancelled('EPUB publication stopped by its shared resource reservation.')
        def stopping():
            return (cancelled() or monotonic() - started > self.budget.timeout
                    or (lease is not None and lease.cancelled()))
        def scope():
            check()
            observed = regular(local_path(parent, exists=True), directory=True)
            if (observed.st_dev, observed.st_ino) != (parent_info.st_dev, parent_info.st_ino):
                raise ValueError('The selected output directory changed during publication.')
            local_path(output)
            if os.path.lexists(output):
                raise FileExistsError('The selected EPUB became occupied; nothing will be overwritten.')
            if self.prepare_destination(output) != approval:
                raise ValueError('The chosen EPUB destination changed during publication. Choose it again.')

        with state_lock(self.production.jobs_root / job_id, 'render'):
            state = self.production.get(job_id)
            completed = {}
            with _publication_resources(self.production, self.budget, parent, state,
                                        cancelled, completed) as lease:
                state_digest = sha256(encode(state)).hexdigest()
                project, timeline, paths = self._source(state, check, progress)
                source_name = 'audiobook.mp3' if state.get('format') == 'mp3' and 'audiobook.mp3' in paths else 'master.wav'
                source_path, source_receipt = paths[source_name]
                if shutil.disk_usage(parent).free < source_receipt['size'] * 2 + self.budget.reserve_disk_bytes:
                    raise OSError('Not enough free disk for verified audio staging and the selected reserve.')
                scope()
                with _publication_staging(parent, completed) as staging:
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
                    audio_identity = _digest(audio, check, self.budget.max_output_bytes)
                    identity = uuid4().hex
                    provenance = {'schema': 'resonance-epub/1', 'publication_id': identity,
                        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                        'source': {'job_id': job_id, 'job_sha256': state_digest, 'project_id': project.id,
                            'project_sha256': project.fingerprint, 'source_script_sha256': timeline['source_sha256'],
                            'artifacts': [{'name': name, 'size': receipt['size'], 'sha256': receipt['sha256']}
                                          for name, (_, receipt) in sorted(paths.items())]},
                        'audio': {'identity': audio_identity, 'media_type': 'audio/mpeg', 'codec': observed.codec,
                            'duration': observed.duration, 'sample_rate': observed.sample_rate, 'channels': observed.channels,
                            'source_artifact': source_name, 'reused': source_name.endswith('.mp3')},
                        'validation': {'zip_xml_graph': 'verified on export', 'epubcheck': 'NOT_RUN',
                                       'reader_interoperability': 'NOT_RUN'}}
                    parts = _parts(project, timeline, provenance, language, creator, check)
                    raw_size = sum(len(raw) for raw in parts.values()) + audio_identity['size'] + 20
                    if raw_size + sum(path.stat().st_size for path in staging.iterdir()) > self.budget.max_output_bytes:
                        raise ValueError('EPUB metadata/audio staging exceeds the selected byte budget.')
                    staged = staging / 'publication.epub'
                    progress('Packaging EPUB text, navigation and measured media overlays')
                    with ZipFile(staged, 'x', compression=ZIP_DEFLATED, allowZip64=True) as archive:
                        mimetype = ZipInfo('mimetype'); mimetype.compress_type = ZIP_STORED
                        archive.writestr(mimetype, b'application/epub+zip')
                        for name, raw in parts.items():
                            check(); archive.writestr(name, raw)
                        member = ZipInfo('EPUB/audio/master.mp3'); member.compress_type = ZIP_STORED
                        digest, count = sha256(), 0
                        with audio.open('rb') as source, archive.open(member, 'w', force_zip64=True) as target:
                            while block := source.read(1024 * 1024):
                                check(); count += len(block); digest.update(block); target.write(block)
                        if {'size': count, 'sha256': digest.hexdigest()} != audio_identity:
                            raise ValueError('The staged MP3 changed during EPUB packaging.')
                    inspect_epub(staged, cancelled=stopping, max_bytes=self.budget.max_output_bytes)
                    archive_identity = _digest(staged, check, self.budget.max_output_bytes)
                    progress('Rechecking source receipts and selected destination before publication')
                    self._source(state, check, lambda message: None)
                    if sha256(encode(self.production.get(job_id))).hexdigest() != state_digest:
                        raise ValueError('The completed Studio job changed during EPUB publication.')
                    scope()
                    record = {'schema': 'resonance-publication/1', 'id': identity, 'status': 'prepared',
                        'studio_root': str(self.root), 'output': str(output), 'artifact': archive_identity,
                        'source': provenance['source'], 'created': provenance['created'], 'notice': NOTICE}
                    self.receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
                    receipt_path = self.receipts / (identity + '.json')
                    atomic_json(receipt_path, record)
                    progress('Prepared publication receipt: ' + identity)
                    try:
                        scope()
                        owner = regular(staged)
                        publication_error = None
                        try:
                            publish_file(staged, output, cancelled=stopping)
                        except (AudioCancelled, FileExistsError):
                            # These are prepublication outcomes from the shared
                            # create-only writer, even if a competitor left bytes.
                            raise
                        except (Exception, KeyboardInterrupt) as exc:
                            publication_error = exc
                        try:
                            _owned_publication_digest(staged, output, owner, parent_info, archive_identity,
                                                      self.budget.max_output_bytes)
                        except Exception:
                            if publication_error is not None:
                                raise publication_error
                            raise
                        # Publication already happened. A late cancellation or time
                        # limit cannot roll it back or authorize another export.
                        # The bounded verification above deliberately has no
                        # cancellation callback, and still rejects changed bytes.
                        completed.update(_finalize_receipt(receipt_path, record))
                        if publication_error is not None:
                            _warning(completed, 'The EPUB was atomically published and its exact bytes '
                                     'were verified, but the writer reported a later interruption: '
                                     + str(publication_error)[:1000])
                        try:
                            staged.unlink()
                        except (OSError, KeyboardInterrupt) as exc:
                            _warning(completed, 'The EPUB is saved and verified. Removal of its owned '
                                     'temporary link was delayed: ' + str(exc)[:1000])
                    except Exception as exc:
                        message = f'{exc} Publication receipt: {identity}. Verify any completed EPUB before retrying.'
                        if isinstance(exc, AudioCancelled):
                            raise AudioCancelled(message) from exc
                        if isinstance(exc, FileExistsError):
                            raise FileExistsError(message) from exc
                        raise RuntimeError(message) from exc
                    progress('EPUB saved and its exact bytes verified')
            return completed

    def verify(self, publication_id, *, output=None, cancelled=lambda: False):
        if not isinstance(publication_id, str) or not re.fullmatch('[0-9a-f]{32}', publication_id):
            raise ValueError('Choose a saved publication receipt ID.')
        path = self.receipts / (publication_id + '.json')
        record = read_json(path)
        if (record.get('schema') != 'resonance-publication/1' or record.get('id') != publication_id
                or record.get('status') not in ('prepared','complete')):
            raise ValueError('Invalid publication receipt.')
        if record.get('studio_root') != str(self.root) and output is None:
            raise ValueError('This receipt was restored elsewhere; explicitly select its existing EPUB file to verify.')
        selected = output if output is not None else record['output']
        started = monotonic()
        def check():
            if cancelled():
                raise AudioCancelled('Publication verification cancelled.')
            if monotonic() - started > self.budget.timeout:
                raise TimeoutError('Publication verification exceeded its time budget.')
        with state_lock(self.receipts, publication_id):
            selected, provenance, links = _receipt_epub(selected, record['artifact'], check,
                                                       self.budget.max_output_bytes)
            if provenance.get('publication_id') != publication_id or provenance.get('source') != record['source']:
                raise ValueError('The EPUB source identity differs from its publication receipt.')
            check()
            selected_record = dict(record, output=str(selected), studio_root=str(self.root))
            result = _finalize_receipt(path, selected_record)
            if links == 2:
                _warning(result, 'The verified EPUB has an additional hard link. Verification '
                         'left all file names unchanged.')
            return result
