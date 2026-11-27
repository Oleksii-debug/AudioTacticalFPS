"""Exact PCM frame partitions of a completed source-bound Book production.

This is a publication format, not another render queue. Source text is retained
at its measured whole-line scope; no character/word timing is inferred.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import re
import struct
import wave
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from ..durable_state import MAX_JSON_BYTES, encode, local_path, regular
from .book_audio import SplitOptions
from .project import Project
from .render import _output_name

FORMAT = 'pcm-parts'
SCHEMA = 'resonance-measured-pcm/1'
NOTICE = ('Audio boundaries are exact frames in the verified completed PCM master. '
          'Text mappings retain whole measured Studio lines only. A partial line has '
          'approximate whole-line source scope; no character or word alignment is inferred. '
          'The original source, accepted production and its output are unchanged. '
          'This archive creates no quotation or extraction approval.')
MAX_PARTS = 256
MAXIMUM_MS = 86_400_000
MAX_WAVE_DATA = 0xffffffff - 36
_HASH = re.compile('[0-9a-f]{64}')


def options(maximum_ms, name_template):
    if type(maximum_ms) is not int or not 1 <= maximum_ms <= MAXIMUM_MS:
        raise ValueError('Choose a maximum measured part length between 1 and 86400000 milliseconds.')
    # Reuse the exact bounded and portable Book naming grammar.
    selected = SplitOptions(name_template=name_template)
    return maximum_ms, selected


class _CheckedReader:
    def __init__(self, stream, check):
        self.stream, self.check = stream, check

    def read(self, size=-1):
        self.check()
        if not 0 <= size <= 1024 * 1024:
            raise ValueError('A PCM header or frame read exceeded its bounded block size.')
        value = self.stream.read(size)
        self.check()
        return value

    def seek(self, offset, whence=0):
        self.check()
        result = self.stream.seek(offset, whence)
        self.check()
        return result

    def tell(self):
        return self.stream.tell()


@contextmanager
def pcm_reader(stream, check):
    check()
    try:
        with wave.open(_CheckedReader(stream, check), 'rb') as reader:
            if (reader.getcomptype() != 'NONE' or reader.getnchannels() not in (1, 2)
                    or reader.getsampwidth() not in (1, 2, 3, 4)
                    or not 8000 <= reader.getframerate() <= 192000
                    or reader.getnframes() <= 0):
                raise ValueError('Choose a completed uncompressed mono/stereo integer PCM master.')
            yield reader
    except (wave.Error, EOFError) as exc:
        raise ValueError('The measured split needs a bounded RIFF PCM WAV master; compressed WAV/RF64 is unavailable.') from exc
    check()


def _info(reader):
    return {'sample_rate': reader.getframerate(), 'channels': reader.getnchannels(),
            'sample_width': reader.getsampwidth(), 'frames': reader.getnframes()}


def _frames(reader, count, check):
    frame_size = reader.getnchannels() * reader.getsampwidth()
    remaining = count
    while remaining:
        check()
        number = min(remaining, max(1, 1024 * 1024 // frame_size))
        raw = reader.readframes(number)
        if len(raw) != number * frame_size:
            raise ValueError('The PCM master ended before its declared measured frame count.')
        remaining -= number
        yield raw
    check()


def pcm_info(path, timeline, check):
    """Read actual retained PCM frames; duration is never estimated from text."""
    path = local_path(path, exists=True)
    regular(path)
    with path.open('rb') as source, pcm_reader(source, check) as reader:
        info = _info(reader)
        if (info['sample_rate'] != timeline.get('sample_rate')
                or info['channels'] != timeline.get('channels')
                or abs(info['frames'] / info['sample_rate'] - timeline['duration']) > 2 / info['sample_rate']):
            raise ValueError('The actual PCM frames differ from the completed measured timeline.')
        identity = sha256()
        for block in _frames(reader, info['frames'], check):
            identity.update(block)
        if reader.readframes(1):
            raise ValueError('The PCM stream exceeds its captured frame count.')
        info['pcm_sha256'] = identity.hexdigest()
    return info


def validate_book_source(project, timeline, value):
    """Check the retained Book receipt against its exact literal part."""
    required = {'schema', 'plan', 'plan_sha256', 'source_anchor', 'source_metadata',
                'source_topic', 'warnings', 'source_sha256', 'part', 'notice'}
    if (type(value) is not dict or not required <= set(value) <= required | {'landmarks'}
            or value['schema'] != 'carr-book-audio-source/1'
            or type(value['plan']) is not str or not re.fullmatch('[0-9a-f]{32}', value['plan'])
            or type(value['plan_sha256']) is not str or not _HASH.fullmatch(value['plan_sha256'])
            or type(value['part']) is not dict or set(value['part']) != {'index', 'start', 'end', 'text_sha256'}
            or type(value['source_anchor']) is not dict
            or value['source_anchor'].get('quotation_authority') is not False
            or value['notice'] != 'A literal reading derivative; this anchor never authorizes a quotation.'):
        raise ValueError('Measured publication needs the exact non-authorizing Book source receipt.')
    part = value['part']
    if (type(part['index']) is not int or not 1 <= part['index'] <= 256
            or type(part['start']) is not int or type(part['end']) is not int
            or not 0 <= part['start'] < part['end'] <= 2_000_000
            or part['end'] - part['start'] != len(project.original_script)
            or part['text_sha256'] != sha256(project.original_script.encode('utf-8')).hexdigest()
            or sha256(encode(value)).hexdigest() != timeline.get('book_source_sha256')):
        raise ValueError('The measured Book receipt differs from its exact original part.')
    from .book_source import validate_project_provenance
    validate_project_provenance(value, project)
    prefix = 'carr-book-audio:%s:%s:%d:' % (value['plan'], value['plan_sha256'], part['index'])
    if any(not line.source_ref.startswith(prefix) for scene in project.scenes for line in scene.lines):
        raise ValueError('The measured source lines belong to another Book plan.')
    return value


def content_review(project, timeline, source, pcm, maximum_ms, name_template, check=lambda: None):
    """Deterministic, destination-independent content review for the archive."""
    from .publishing import _segments
    maximum_ms, selected = options(maximum_ms, name_template)
    _, segments, _ = _segments(project, timeline, check)
    frames_per_part = maximum_ms * pcm['sample_rate'] // 1000
    if frames_per_part <= 0:
        raise ValueError('The selected interval is shorter than one PCM frame.')
    count = (pcm['frames'] + frames_per_part - 1) // frames_per_part
    if not 1 <= count <= MAX_PARTS:
        raise ValueError('The measured split exceeds 256 parts; choose a larger measured interval.')
    windows = []
    for row in segments:
        check()
        if row['kind'] == 'line':
            first = round(row['measured_start'] * pcm['sample_rate'])
            last = round(row['measured_end'] * pcm['sample_rate'])
            if not 0 <= first < last <= pcm['frames'] + 2:
                raise ValueError('A measured source line cannot be represented on the captured PCM frame grid.')
            windows.append((row, first, last))
    parts, names, cursor = [], set(), 0
    for index, start in enumerate(range(0, pcm['frames'], frames_per_part), 1):
        check()
        end = min(start + frames_per_part, pcm['frames'])
        frame_bytes = (end - start) * pcm['channels'] * pcm['sample_width']
        if frame_bytes + (frame_bytes % 2) > MAX_WAVE_DATA:
            raise ValueError('A selected PCM part exceeds RIFF WAV size; choose a shorter interval.')
        label = 'Frames %d to %d' % (start, end)
        name = 'audio/' + _output_name(selected.title(index, project.title, label)) + '.wav'
        from ..state_backup import relative_name
        relative_name(name)  # Includes Windows device aliases even on POSIX.
        if name.casefold() in names:
            raise ValueError('The measured part names collide on a case-insensitive filesystem.')
        names.add(name.casefold())
        mapped = []
        while cursor < len(windows) and windows[cursor][2] <= start:
            cursor += 1
        selected_line = cursor
        while selected_line < len(windows) and windows[selected_line][1] < end:
            check()
            row, first, last = windows[selected_line]
            # Times retain producer precision. The text always refers to the
            # whole original line, even when only its intersecting audio is kept.
            mapped.append({'scene': row['scene'], 'line': row['line'],
                'source_ref': row['source_ref'], 'scope': 'whole-line',
                'partial_line': first < start or last > end,
                'alignment': 'approximate whole-line source scope; no character or word alignment'})
            selected_line += 1
        parts.append({'index': index, 'name': name, 'start_frame': start, 'end_frame': end,
                      'frames': end - start, 'source_lines': mapped})
    value = {'schema': 'carr-measured-audio-content/1', 'source': source,
        'pcm': pcm, 'maximum_ms': maximum_ms, 'name_template': name_template,
        'parts': parts, 'notice': NOTICE}
    if len(encode(value)) > MAX_JSON_BYTES:
        raise ValueError('Measured part/source metadata exceeds the existing JSON budget.')
    return value


def review_document(content, destination):
    if not destination.require_absent or destination.target.suffix.casefold() != '.zip':
        raise ValueError('Choose a new .zip file for the reviewed measured audio parts.')
    value = {'schema': 'carr-measured-audio-review/1', 'content': content,
             'output': str(destination.target), 'requested': str(destination.requested),
             'parent_identity': list(destination.parent_identity)}
    return dict(value, approval_sha256=sha256(encode(value)).hexdigest())


def _header(pcm, frames):
    size = frames * pcm['channels'] * pcm['sample_width']
    padding = size % 2
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 36 + size + padding, b'WAVE',
        b'fmt ', 16, 1, pcm['channels'], pcm['sample_rate'],
        pcm['sample_rate'] * pcm['channels'] * pcm['sample_width'],
        pcm['channels'] * pcm['sample_width'], pcm['sample_width'] * 8, b'data', size)


def package(staged, master, content, provenance, metadata, *, check, max_bytes):
    """Stream exact frame slices into one existing publication transaction."""
    if set(metadata) != {'source/project.json', 'source/timeline.json', 'source/book-source.json'}:
        raise ValueError('Measured publication requires its exact retained project and timeline.')
    pcm = content['pcm']
    manifest = dict(provenance, content=content, parts=[])
    total_pcm, written = sha256(), 0
    with Path(master).open('rb') as source, pcm_reader(source, check) as reader:
        if _info(reader) != {key: pcm[key] for key in ('sample_rate', 'channels', 'sample_width', 'frames')}:
            raise ValueError('The staged PCM header changed after review.')
        with ZipFile(staged, 'x', compression=ZIP_STORED, allowZip64=True) as archive:
            for part in content['parts']:
                check()
                identity, raw_identity, count = sha256(), sha256(), 0
                info = ZipInfo(part['name'])
                info.compress_type = ZIP_STORED
                with archive.open(info, 'w', force_zip64=True) as target:
                    def write(raw):
                        nonlocal written, count
                        check()
                        written += len(raw)
                        count += len(raw)
                        if written > max_bytes:
                            raise ValueError('Measured PCM packaging exceeds its byte budget.')
                        identity.update(raw)
                        target.write(raw)
                    write(_header(pcm, part['frames']))
                    for block in _frames(reader, part['frames'], check):
                        raw_identity.update(block)
                        total_pcm.update(block)
                        write(block)
                    if part['frames'] * pcm['channels'] * pcm['sample_width'] % 2:
                        write(b'\0')
                manifest['parts'].append({'name': part['name'],
                    'identity': {'size': count, 'sha256': identity.hexdigest()},
                    'pcm_sha256': raw_identity.hexdigest()})
            if reader.readframes(1) or total_pcm.hexdigest() != pcm['pcm_sha256']:
                raise ValueError('The staged PCM differs from the exact reviewed master frames.')
            for name, raw in metadata.items():
                check()
                written += len(raw)
                if len(raw) > MAX_JSON_BYTES or written > max_bytes:
                    raise ValueError('Measured source metadata exceeds its byte budget.')
                archive.writestr(name, raw)
            encoded = encode(manifest)
            if len(encoded) > MAX_JSON_BYTES or written + len(encoded) + 65536 > max_bytes:
                raise ValueError('Measured publication manifest exceeds its byte budget.')
            archive.writestr('manifest.json', encoded)
    check()
    if regular(staged).st_size > max_bytes:
        raise ValueError('Measured publication exceeds its final archive byte budget.')
    return manifest


def inspect(path, *, cancelled=lambda: False, max_bytes=32 * 1024**3):
    """Verify the bounded archive graph, original metadata and every PCM frame."""
    from .publishing import _json, validate_publication_source
    from datetime import datetime
    from ..audio.models import AudioCancelled
    def check():
        if cancelled():
            raise AudioCancelled('Measured audio verification cancelled.')
    path = local_path(path, exists=True)
    if not 0 < regular(path).st_size <= max_bytes:
        raise ValueError('Measured publication exceeds its archive byte budget.')
    with ZipFile(path) as archive:
        members = archive.infolist()
        if (not 5 <= len(members) <= MAX_PARTS + 4
                or len({item.filename.casefold() for item in members}) != len(members)
                or any(item.is_dir() or item.flag_bits & 1 or item.compress_type != ZIP_STORED
                       or item.file_size < 0 or item.file_size > max_bytes
                       or ((item.external_attr >> 16) & 0o170000) == 0o120000 for item in members)
                or sum(item.file_size for item in members) > max_bytes):
            raise ValueError('Invalid bounded measured publication archive graph.')
        by_name = {item.filename: item for item in members}
        def raw(name):
            check()
            item = by_name.get(name)
            if item is None or not 0 < item.file_size <= MAX_JSON_BYTES:
                raise ValueError('Missing or oversized measured publication metadata.')
            result = archive.read(item)
            check()
            if len(result) != item.file_size:
                raise ValueError('Measured publication metadata changed while reading.')
            return result
        manifest = _json(raw('manifest.json'))
        if (type(manifest) is not dict or set(manifest) != {
                'schema', 'publication_id', 'created', 'source', 'content', 'parts'}
                or manifest['schema'] != SCHEMA or type(manifest['parts']) is not list):
            raise ValueError('Invalid measured publication manifest.')
        if (type(manifest['publication_id']) is not str
                or not re.fullmatch('[0-9a-f]{32}', manifest['publication_id'])
                or type(manifest['created']) is not str
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', manifest['created'])):
            raise ValueError('Invalid measured publication identity or creation time.')
        datetime.strptime(manifest['created'], '%Y-%m-%dT%H:%M:%SZ')
        validate_publication_source(manifest['source'])
        content = manifest['content']
        if (type(content) is not dict or set(content) != {
                'schema', 'source', 'pcm', 'maximum_ms', 'name_template', 'parts', 'notice'}
                or content['schema'] != 'carr-measured-audio-content/1'
                or content['source'] != manifest['source'] or content['notice'] != NOTICE):
            raise ValueError('Measured publication differs from its reviewed source content.')
        pcm = content['pcm']
        if (type(pcm) is not dict or set(pcm) != {
                'sample_rate', 'channels', 'sample_width', 'frames', 'pcm_sha256'}
                or type(pcm['sample_rate']) is not int or not 8000 <= pcm['sample_rate'] <= 192000
                or type(pcm['channels']) is not int or pcm['channels'] not in (1, 2)
                or type(pcm['sample_width']) is not int or pcm['sample_width'] not in (1, 2, 3, 4)
                or type(pcm['frames']) is not int or not 0 < pcm['frames'] < 2**63
                or type(pcm['pcm_sha256']) is not str or not _HASH.fullmatch(pcm['pcm_sha256'])):
            raise ValueError('Invalid measured PCM identity.')
        receipts = {item['name']: item for item in manifest['source']['artifacts']}
        documents = {}
        for name in ('project.json', 'timeline.json', 'book-source.json'):
            selected = 'source/' + name
            data = raw(selected)
            receipt = receipts.get(name)
            if receipt is None or {'size': len(data), 'sha256': sha256(data).hexdigest()} != {
                    'size': receipt['size'], 'sha256': receipt['sha256']}:
                raise ValueError('Measured publication source metadata differs from its original artifact receipt.')
            documents[name] = _json(data)
        project = Project.from_dict(documents['project.json'])
        timeline = documents['timeline.json']
        if (project.id != manifest['source']['project_id']
                or project.fingerprint != manifest['source']['project_sha256']
                or timeline.get('source_sha256') != manifest['source']['source_script_sha256']
                or abs(pcm['frames'] / pcm['sample_rate'] - timeline['duration']) > 2 / pcm['sample_rate']
                or pcm['sample_rate'] != timeline.get('sample_rate') or pcm['channels'] != timeline.get('channels')
                or sha256(encode(documents['book-source.json'])).hexdigest() != timeline.get('book_source_sha256')):
            raise ValueError('Measured publication project/timing/provenance identities disagree.')
        validate_book_source(project, timeline, documents['book-source.json'])
        expected = content_review(project, timeline, manifest['source'], pcm,
                                  content['maximum_ms'], content['name_template'], check)
        if encode(content) != encode(expected) or len(manifest['parts']) != len(content['parts']):
            raise ValueError('Measured audio ranges or whole-line source mappings differ from their reviewed source.')
        names = {'manifest.json', 'source/project.json', 'source/timeline.json', 'source/book-source.json'}
        names.update(part['name'] for part in content['parts'])
        if set(by_name) != names:
            raise ValueError('Measured publication contains missing or unreviewed files.')
        whole_pcm = sha256()
        for part, record in zip(content['parts'], manifest['parts']):
            check()
            if type(record) is not dict or set(record) != {'name', 'identity', 'pcm_sha256'} or record['name'] != part['name']:
                raise ValueError('Invalid measured part receipt.')
            identity = sha256()
            size = 0
            with archive.open(part['name']) as stream:
                while block := stream.read(1024 * 1024):
                    check()
                    size += len(block)
                    if size > by_name[part['name']].file_size:
                        raise ValueError('Measured part exceeds its declared size.')
                    identity.update(block)
            if record['identity'] != {'size': size, 'sha256': identity.hexdigest()}:
                raise ValueError('A measured part checksum changed.')
            with archive.open(part['name']) as stream, pcm_reader(stream, check) as reader:
                if _info(reader) != dict({key: pcm[key] for key in ('sample_rate', 'channels', 'sample_width')},
                                         frames=part['frames']):
                    raise ValueError('A measured part has different actual PCM parameters or frames.')
                part_pcm = sha256()
                for block in _frames(reader, part['frames'], check):
                    part_pcm.update(block)
                    whole_pcm.update(block)
                if reader.readframes(1) or part_pcm.hexdigest() != record['pcm_sha256']:
                    raise ValueError('A measured part differs from its exact PCM receipt.')
        if whole_pcm.hexdigest() != pcm['pcm_sha256']:
            raise ValueError('The concatenated measured parts differ from their captured source PCM.')
    check()
    return manifest
