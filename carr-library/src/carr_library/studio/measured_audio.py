"""Exact PCM frame partitions of a completed source-bound Book production.

This is a publication format, not another render queue. Source text is retained
at its measured whole-line scope; no character/word timing is inferred.
"""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import re
import os
import stat
import struct
from zipfile import ZipFile, ZipInfo, ZipExtFile, ZIP_STORED

from ..durable_state import MAX_JSON_BYTES, encode, local_path, regular
from .book_audio import SplitOptions, validate_provenance
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
        if isinstance(self.stream, ZipExtFile):
            # ZipExtFile.seek may decode a long forward region internally.
            # Retain bounded cancellation for any admitted intervening chunks.
            if whence != 0 or type(offset) is not int or offset < 0:
                raise ValueError('Use bounded absolute archived PCM positions.')
            if offset < self.stream.tell():
                self.stream.seek(0)
            while self.stream.tell() < offset:
                raw = self.read(min(1024 * 1024, offset - self.stream.tell()))
                if not raw:
                    raise ValueError('Archived PCM ended before the selected chunk.')
            return self.stream.tell()
        result = self.stream.seek(offset, whence)
        self.check()
        return result

    def tell(self):
        return self.stream.tell()


class _PCMReader:
    """Bounded RIFF/RF64 integer-PCM reader, adapted from the existing WAV inspector."""
    def __init__(self, stream, check, size=None):
        self.stream = _CheckedReader(stream, check)
        if size is None:
            if isinstance(stream, ZipExtFile):
                raise ValueError('An archived PCM member needs its bounded inventory size.')
            self.stream.seek(0, 2)
            total = self.stream.tell()
        else:
            if type(size) is not int:
                raise ValueError('PCM byte size must be an exact integer.')
            total = size
        if not 44 <= total < 2**63:
            raise ValueError('Invalid bounded PCM container size.')
        self.stream.seek(0)
        head = self.stream.read(12)
        if len(head) != 12 or head[:4] not in (b'RIFF', b'RF64') or head[8:] != b'WAVE':
            raise ValueError('Choose a completed RIFF/RF64 integer PCM master.')
        rf64 = head[:4] == b'RF64'
        declared = struct.unpack('<I', head[4:8])[0]
        if (rf64 and declared != 0xffffffff) or (not rf64 and declared + 8 != total):
            raise ValueError('PCM container length differs from the actual file.')
        geometry = data = extended = None
        chunks = 0
        while self.stream.tell() < total:
            chunks += 1
            if chunks > 1024:
                raise ValueError('Too many PCM container chunks.')
            header = self.stream.read(8)
            if len(header) != 8:
                raise ValueError('Truncated PCM chunk header.')
            kind, size = struct.unpack('<4sI', header)
            if size == 0xffffffff:
                if not rf64 or kind != b'data' or extended is None:
                    raise ValueError('Invalid RF64 chunk-size declaration.')
                size = extended[1]
            first = self.stream.tell()
            if first + size + size % 2 > total:
                raise ValueError('PCM chunk exceeds the captured container.')
            if kind == b'ds64':
                if not rf64 or extended is not None or not 28 <= size <= 65536 or data is not None:
                    raise ValueError('Invalid RF64 size metadata.')
                raw = self.stream.read(size)
                riff_size, data_size, frame_count, table = struct.unpack('<QQQI', raw[:28])
                if table != 0 or size != 28 or riff_size + 8 != total:
                    raise ValueError('Unsupported or inconsistent RF64 size table.')
                extended = (riff_size, data_size, frame_count)
            elif kind == b'fmt ':
                if geometry is not None or data is not None or not 16 <= size <= 65536:
                    raise ValueError('Invalid PCM format chunk.')
                raw = self.stream.read(size)
                encoding, channels, rate, byte_rate, align, bits = struct.unpack('<HHIIHH', raw[:16])
                if encoding == 0xfffe:
                    if (len(raw) < 40 or struct.unpack('<H', raw[16:18])[0] != 22
                            or struct.unpack('<H', raw[18:20])[0] not in (0, bits)
                            or raw[24:40] != bytes.fromhex('0100000000001000800000aa00389b71')):
                        raise ValueError('Only uncompressed integer PCM extensions are supported.')
                    encoding = 1
                if (encoding != 1 or channels not in (1, 2) or bits not in (8, 16, 24, 32)
                        or not 8000 <= rate <= 192000 or align != channels * (bits // 8)
                        or byte_rate != rate * align):
                    raise ValueError('Invalid uncompressed mono/stereo PCM geometry.')
                geometry = (rate, channels, bits // 8, align)
            elif kind == b'data':
                if data is not None or geometry is None or not size or size % geometry[3]:
                    raise ValueError('Invalid or ambiguous PCM data chunk.')
                data = (first, size)
                if first + size + size % 2 == total:
                    break  # Final PCM data needs no unbounded ZIP seek over its payload.
            self.stream.seek(first + size + size % 2)
        if geometry is None or data is None or (rf64 and extended is None):
            raise ValueError('PCM container lacks its exact format/data metadata.')
        self.rate, self.channels, self.width, self.align = geometry
        self.offset, self.size = data
        self.frames = self.size // self.align
        if rf64 and (extended[1] != self.size or extended[2] not in (0, self.frames)):
            raise ValueError('RF64 metadata differs from the actual PCM frame count.')
        self.remaining = self.frames
        self.stream.seek(self.offset)

    def getframerate(self):
        return self.rate

    def getnchannels(self):
        return self.channels

    def getsampwidth(self):
        return self.width

    def getnframes(self):
        return self.frames

    def readframes(self, count):
        if type(count) is not int or count < 0 or count * self.align > 1024 * 1024:
            raise ValueError('Read PCM in bounded frame blocks.')
        count = min(count, self.remaining)
        raw = self.stream.read(count * self.align)
        if len(raw) != count * self.align:
            raise ValueError('PCM data ended before its declared frame count.')
        self.remaining -= count
        return raw


@contextmanager
def pcm_reader(stream, check, *, size=None):
    check()
    yield _PCMReader(stream, check, size)
    check()


def _stamp(info):
    # Windows path/fd mode bits differ for executables; compare the file kind,
    # stable identity, bounded bytes and timestamps, not permission decoration.
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


@contextmanager
def _regular_stream(path, check):
    path = local_path(path, exists=True)
    before = regular(path)
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The selected PCM/archive changed before opening.')
        yield stream
        check()
        if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(regular(path)) != _stamp(before)):
            raise ValueError('The selected PCM/archive changed while being verified.')


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
    with _regular_stream(path, check) as source, pcm_reader(source, check) as reader:
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
    validate_provenance(project, value)
    if sha256(encode(value)).hexdigest() != timeline.get('book_source_sha256'):
        raise ValueError('The measured Book receipt differs from its exact completed timeline.')
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
    """Preserve ordinary RIFF bytes; use the existing RF64 size contract when needed."""
    size = frames * pcm['channels'] * pcm['sample_width']
    padding = size % 2
    fmt = struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, pcm['channels'], pcm['sample_rate'],
        pcm['sample_rate'] * pcm['channels'] * pcm['sample_width'],
        pcm['channels'] * pcm['sample_width'], pcm['sample_width'] * 8)
    if size + padding <= MAX_WAVE_DATA:
        return struct.pack('<4sI4s', b'RIFF', 36 + size + padding, b'WAVE') + fmt + struct.pack('<4sI', b'data', size)
    return (struct.pack('<4sI4s', b'RF64', 0xffffffff, b'WAVE')
        + struct.pack('<4sIQQQI', b'ds64', 28, 72 + size + padding, size, frames, 0)
        + fmt + struct.pack('<4sI', b'data', 0xffffffff))


def package(staged, master, content, provenance, metadata, *, check, max_bytes):
    """Stream exact frame slices into one existing publication transaction."""
    if set(metadata) != {'source/project.json', 'source/timeline.json', 'source/book-source.json'}:
        raise ValueError('Measured publication requires its exact retained project and timeline.')
    pcm = content['pcm']
    manifest = dict(provenance, content=content, parts=[])
    total_pcm, written = sha256(), 0
    with _regular_stream(master, check) as source, pcm_reader(source, check) as reader:
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


def _bounded_directory(stream, total):
    """Bound the ZIP central directory before ZipFile materializes any names."""
    if total < 22:
        raise ValueError('Truncated measured archive.')
    stream.seek(total - 22)
    end = stream.read(22)
    if len(end) != 22 or end[:4] != b'PK\x05\x06':
        raise ValueError('Measured archives require an exact unadorned ZIP ending.')
    _, disk, start_disk, count_disk, count, size, offset, comment = struct.unpack('<4s4H2IH', end)
    if disk or start_disk or comment or count_disk != count:
        raise ValueError('Multivolume or annotated measured archives are unsupported.')
    directory_end = total - 22
    locator = b''
    if total >= 42:
        stream.seek(total - 42)
        locator = stream.read(20)
    # Python emits ZIP64 at its conservative 2 GiB limit, before the ordinary
    # end-record's 32-bit offset overflows. Detect a real locator independently
    # of sentinel fields so these valid multi-hour archives remain readable.
    has_zip64 = len(locator) == 20 and locator[:4] == b'PK\x06\x07'
    if has_zip64 or count == 0xffff or size == 0xffffffff or offset == 0xffffffff:
        if total < 98:
            raise ValueError('Truncated ZIP64 measured archive.')
        if not has_zip64:
            raise ValueError('Missing ZIP64 measured archive locator.')
        _, disk, position, disks = struct.unpack('<4sIQI', locator)
        if disk or disks != 1 or not 0 <= position <= total - 98:
            raise ValueError('Invalid ZIP64 measured archive locator.')
        stream.seek(position)
        header = stream.read(56)
        if len(header) != 56 or header[:4] != b'PK\x06\x06':
            raise ValueError('Missing ZIP64 measured archive directory.')
        _, record_size, _, _, disk, start_disk, count_disk, count, size, offset = struct.unpack('<4sQ2H2I4Q', header)
        if (record_size != 44 or disk or start_disk or count_disk != count
                or position + 56 != total - 42):
            raise ValueError('Invalid bounded ZIP64 directory.')
        directory_end = position
    if not 5 <= count <= MAX_PARTS + 4 or not 0 < size <= 256 * 1024 or offset + size != directory_end:
        raise ValueError('Measured archive central directory exceeds its bounded graph.')
    stream.seek(0)



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
    with _regular_stream(path, check) as outer:
        _bounded_directory(_CheckedReader(outer, check), regular(path).st_size)
        with ZipFile(outer) as archive:
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
                    or manifest['schema'] != SCHEMA or type(manifest['parts']) is not list
                    or not 1 <= len(manifest['parts']) <= MAX_PARTS):
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
            from .project import validate_project_effects
            validate_project_effects(project, timeline)
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
                with archive.open(part['name']) as stream, pcm_reader(
                        stream, check, size=by_name[part['name']].file_size) as reader:
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
