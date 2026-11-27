"""Exact PCM frame partitions of the current verified Book production.

Adapted from PR152 measured_audio (624e1dae). The current ProductionPlan,
sample-frame timeline, BookPlan and publication receipt remain authoritative.
Reads and ZIP writes are bounded blocks; no decoder, synthesizer or guessed
character timing is used. RIFF and RF64 masters use the existing PCM parser.
"""
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import re
import struct
from zipfile import ZipFile, ZipInfo, ZIP_STORED

from . import ffmpeg as audio
from .book_plan import BookPlan, SplitOptions, encode as book_encode
from .jobs import file_identity, regular_file, verified_reader
from .models import ProductionPlan, digest
from .paths import filename_component
from .publishing import (MAX_JSON_BYTES, PublicationCancelled, _json, _segments,
                         encode, local_path)

FORMAT = 'pcm-parts'
SCHEMA = 'resonance-measured-pcm/2'
NOTICE = (
    'Audio boundaries are exact frames in the verified completed PCM master. '
    'Text mappings retain whole measured speech lines only. A partial line has '
    'approximate whole-line source scope; no character or word alignment is inferred. '
    'The original source, accepted production and its output are unchanged. '
    'Retained completion is reused; this is not new synthesis or engine qualification. '
    'The archive retains the entire selected Book review text and metadata, including other parts of that review. '
    'This archive creates no quotation or extraction approval.'
)
MAX_PARTS = 256
MAXIMUM_MS = 86_400_000
MAX_MAPPING_ROWS = 100000 + MAX_PARTS
_METADATA = ('source/plan.json', 'source/timeline.json', 'source/book-plan.json')
_HASH = re.compile('[0-9a-f]{64}')


def options(maximum_ms, name_template):
    if type(maximum_ms) is not int or not 1 <= maximum_ms <= MAXIMUM_MS:
        raise ValueError('Choose 1 to 86400000 milliseconds per measured audio part.')
    return maximum_ms, SplitOptions(name_template=name_template)


def bind_book(project, book, index):
    if not isinstance(project, ProductionPlan) or not isinstance(book, BookPlan):
        raise ValueError('Choose an exact current typed Book plan and completed production.')
    expected = book.project(index)
    if project.to_dict() != expected.to_dict() or project.input_kind != 'narration':
        raise ValueError('The completed production differs from this exact literal Book part.')
    return {'plan': book.id, 'plan_sha256': book.fingerprint,
            'part': book.provenance(index)['part']}


def source_proof(value):
    required = {'job_id', 'job_sha256', 'project_sha256', 'plan_file_sha256',
                'source_script_sha256', 'artifacts', 'book'}
    if type(value) is not dict or set(value) != required:
        raise ValueError('Invalid measured-publication source receipt.')
    if any(type(value[key]) is not str or not _HASH.fullmatch(value[key])
           for key in required - {'artifacts', 'book'}):
        raise ValueError('Invalid completed-production source digest.')
    artifacts = value['artifacts']
    if type(artifacts) is not list or not 3 <= len(artifacts) <= 100000:
        raise ValueError('Invalid bounded completed-production inventory.')
    names, total = set(), 0
    for row in artifacts:
        if (type(row) is not dict or set(row) != {'name', 'size', 'sha256'}
                or type(row['name']) is not str or type(row['size']) is not int
                or not 0 <= row['size'] <= 2 * 1024**4
                or type(row['sha256']) is not str or not _HASH.fullmatch(row['sha256'])):
            raise ValueError('Invalid measured-publication artifact.')
        name = filename_component(row['name'])
        if name.casefold() in names:
            raise ValueError('Duplicate measured-publication artifact name.')
        names.add(name.casefold())
        total += row['size']
        if total > 2 * 1024**4:
            raise ValueError('The source graph exceeds the publication byte limit.')
    if not {'master.wav', 'timeline.json', 'subtitles.srt'} <= {row['name'] for row in artifacts}:
        raise ValueError('Measured publication needs the original master and timeline.')
    book = value['book']
    if (type(book) is not dict or set(book) != {'plan', 'plan_sha256', 'part'}
            or type(book['plan']) is not str or re.fullmatch('[0-9a-f]{32}', book['plan']) is None
            or type(book['plan_sha256']) is not str or not _HASH.fullmatch(book['plan_sha256'])):
        raise ValueError('Invalid original Book identity.')
    part = book['part']
    if (type(part) is not dict or set(part) != {'index', 'start', 'end', 'text_sha256'}
            or type(part['index']) is not int or not 1 <= part['index'] <= 256
            or type(part['start']) is not int or type(part['end']) is not int
            or not 0 <= part['start'] < part['end'] <= 2_000_000
            or type(part['text_sha256']) is not str or not _HASH.fullmatch(part['text_sha256'])):
        raise ValueError('Invalid exact Book part source range.')
    return value


def pcm_info(path, timeline, project, check):
    """Hash only the actual integer PCM frames from a verified RIFF/RF64 master."""
    check()
    stamp = file_identity(regular_file(path))
    info = audio.wav_info(path)
    _, _, duration = _segments(project, timeline, check)
    if info.rate != project.sample_rate or info.channels != 2 or info.seconds != duration:
        raise ValueError('The PCM master differs from the exact completed sample timeline.')
    hashed = sha256()
    with verified_reader(path, expected_identity=stamp) as stream:
        stream.seek(info.data_offset)
        remaining = info.data_size
        while remaining:
            check()
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('The PCM master ended before its declared frame count.')
            remaining -= len(block)
            hashed.update(block)
    check()
    return {'sample_rate': info.rate, 'channels': info.channels, 'sample_width': 2,
            'frames': info.frames, 'pcm_sha256': hashed.hexdigest()}


def _pcm(value):
    if (type(value) is not dict or set(value) != {
            'sample_rate', 'channels', 'sample_width', 'frames', 'pcm_sha256'}
            or type(value['sample_rate']) is not int or not 8000 <= value['sample_rate'] <= 192000
            or type(value['channels']) is not int or value['channels'] != 2
            or type(value['sample_width']) is not int or value['sample_width'] != 2
            or type(value['frames']) is not int or not 1 <= value['frames'] <= value['sample_rate'] * 365 * 86400
            or type(value['pcm_sha256']) is not str or not _HASH.fullmatch(value['pcm_sha256'])):
        raise ValueError('Invalid measured PCM identity.')
    return value


def content_review(project, timeline, source, pcm, maximum_ms, name_template, check=lambda: None):
    maximum_ms, selected = options(maximum_ms, name_template)
    source_proof(source)
    _pcm(pcm)
    _, segments, duration = _segments(project, timeline, check)
    if (pcm['sample_rate'] != project.sample_rate
            or pcm['frames'] / pcm['sample_rate'] != duration
            or source['project_sha256'] != digest(project.to_dict())
            or source['source_script_sha256'] != project.source_sha256):
        raise ValueError('Measured frames, plan and source receipts disagree.')
    frames_per_part = maximum_ms * pcm['sample_rate'] // 1000
    if frames_per_part < 1:
        raise ValueError('The selected interval is shorter than one PCM frame.')
    count = (pcm['frames'] + frames_per_part - 1) // frames_per_part
    if not 1 <= count <= MAX_PARTS:
        raise ValueError('The measured split exceeds 256 parts; choose a longer interval.')
    windows = [row for row in segments if row['kind'] == 'line']
    parts, names, cursor, mapping_count = [], set(), 0, 0
    for index, start in enumerate(range(0, pcm['frames'], frames_per_part), 1):
        check()
        end = min(start + frames_per_part, pcm['frames'])
        label = 'Frames %d to %d' % (start, end)
        # The existing explicit naming grammar and local filename validator are
        # shared with current book output; no archived path becomes authority.
        stem = selected.title(index, project.title, label)
        stem = ''.join('_' if char in '/\\:*?"<>|' else char for char in stem).strip(' .')
        stem = stem[:180].rstrip(' .') or ('Part %04d' % index)
        name = 'audio/' + filename_component(stem + '.wav')
        if name.casefold() in names:
            raise ValueError('Measured part names collide on a case-insensitive filesystem.')
        names.add(name.casefold())
        while cursor < len(windows) and windows[cursor]['end_frame'] <= start:
            cursor += 1
        mapped, position = [], cursor
        while position < len(windows) and windows[position]['start_frame'] < end:
            check()
            row = windows[position]
            if type(row['source_start']) is not int or type(row['source_end']) is not int:
                raise ValueError('A Book line has no exact literal source span.')
            mapping_count += 1
            if mapping_count > MAX_MAPPING_ROWS:
                raise ValueError('Measured source mappings exceed their bounded work budget.')
            offset = source['book']['part']['start']
            mapped.append({'scene': row['scene'], 'line': row['line'],
                'source_start': offset + row['source_start'],
                'source_end': offset + row['source_end'], 'scope': 'whole-line',
                'partial_line': row['start_frame'] < start or row['end_frame'] > end,
                'alignment': 'approximate whole-line source scope; no character or word alignment'})
            position += 1
        parts.append({'index': index, 'name': name, 'start_frame': start, 'end_frame': end,
                      'frames': end - start, 'source_lines': mapped})
    value = {'schema': 'carr-measured-audio-content/2', 'source': source, 'pcm': pcm,
             'maximum_ms': maximum_ms, 'name_template': name_template, 'parts': parts, 'notice': NOTICE}
    encode(value)
    return value


def review_document(content, destination):
    if not destination.require_absent or destination.target.suffix.casefold() != '.zip':
        raise ValueError('Choose a new .zip file for reviewed measured audio parts.')
    value = {'schema': 'carr-measured-audio-review/2', 'content': content,
             'output': str(destination.target), 'requested': str(destination.requested),
             'private_root': str(destination.private_root),
             'parent_identity': list(destination.parent_identity)}
    return dict(value, approval_sha256=sha256(encode(value)).hexdigest())


def _header(pcm, frames):
    stream = BytesIO()
    audio._write_header(stream, frames, pcm['sample_rate'], pcm['channels'])
    return stream.getvalue()


def metadata(project_raw, timeline_raw, book):
    result = {'source/plan.json': project_raw, 'source/timeline.json': timeline_raw,
              'source/book-plan.json': book_encode(book.to_dict())}
    if any(type(raw) is not bytes or not 0 < len(raw) <= MAX_JSON_BYTES for raw in result.values()):
        raise ValueError('Measured publication needs bounded exact source metadata.')
    return result


def validate_metadata(documents, source, check=lambda: None):
    source_proof(source)
    if type(documents) is not dict or set(documents) != set(_METADATA):
        raise ValueError('Measured publication requires its exact three source documents.')
    for raw in documents.values():
        check()
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_JSON_BYTES:
            raise ValueError('Measured publication metadata exceeds its JSON budget.')
    if sha256(documents['source/plan.json']).hexdigest() != source['plan_file_sha256']:
        raise ValueError('The original typed production plan bytes changed.')
    recorded = {row['name']: row for row in source['artifacts']}
    raw = documents['source/timeline.json']
    if {'size': len(raw), 'sha256': sha256(raw).hexdigest()} != {
            key: recorded['timeline.json'][key] for key in ('size', 'sha256')}:
        raise ValueError('The original measured timeline bytes changed.')
    project = ProductionPlan.from_dict(_json(documents['source/plan.json']))
    book = BookPlan.from_dict(_json(documents['source/book-plan.json']))
    index = source['book']['part']['index']
    if (book_encode(book.to_dict()) != documents['source/book-plan.json']
            or bind_book(project, book, index) != source['book']
            or digest(project.to_dict()) != source['project_sha256']
            or project.source_sha256 != source['source_script_sha256']):
        raise ValueError('The retained Book, production plan and original source disagree.')
    timeline = _json(raw)
    _segments(project, timeline, check)
    return project, timeline, book


def package(staged, master, content, provenance, documents, *, check, max_bytes):
    """Stream each frame exactly once into one ordinary publication ZIP stage."""
    validate_metadata(documents, content['source'], check)
    pcm = _pcm(content['pcm'])
    stamp = file_identity(regular_file(master))
    info = audio.wav_info(master)
    if (info.rate, info.channels, info.frames) != (pcm['sample_rate'], pcm['channels'], pcm['frames']):
        raise ValueError('The PCM header changed after review.')
    manifest = dict(provenance, content=content, parts=[])
    total_pcm, written = sha256(), 0
    with verified_reader(master, expected_identity=stamp) as source:
        source.seek(info.data_offset)
        with ZipFile(staged, 'x', compression=ZIP_STORED, allowZip64=True) as archive:
            for part in content['parts']:
                check()
                identity, raw_identity, count = sha256(), sha256(), 0
                member = ZipInfo(part['name'])
                member.compress_type = ZIP_STORED
                with archive.open(member, 'w', force_zip64=True) as target:
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
                    remaining = part['frames'] * pcm['channels'] * 2
                    while remaining:
                        check()
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError('The PCM master ended before its reviewed frame count.')
                        remaining -= len(block)
                        raw_identity.update(block)
                        total_pcm.update(block)
                        write(block)
                manifest['parts'].append({'name': part['name'],
                    'identity': {'size': count, 'sha256': identity.hexdigest()},
                    'pcm_sha256': raw_identity.hexdigest()})
            if source.tell() != info.data_offset + info.data_size or total_pcm.hexdigest() != pcm['pcm_sha256']:
                raise ValueError('The captured PCM frames differ from the exact reviewed master.')
            for name, raw in documents.items():
                check()
                written += len(raw)
                if written > max_bytes:
                    raise ValueError('Measured source metadata exceeds its byte budget.')
                archive.writestr(name, raw)
            encoded = encode(manifest)
            if written + len(encoded) + 256 * 1024 > max_bytes:
                raise ValueError('Measured publication exceeds its metadata and archive budget.')
            archive.writestr('manifest.json', encoded)
    check()
    if regular_file(staged).st_size > max_bytes:
        raise ValueError('Measured publication exceeds its final byte budget.')
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
    """Offline verification; archived sources grant no current execution authority."""
    def check():
        if cancelled():
            raise PublicationCancelled('Measured audio verification cancelled.')
    check()
    path = local_path(path)
    info = regular_file(path)
    if not 0 < info.st_size <= max_bytes:
        raise ValueError('Measured publication exceeds its archive byte budget.')
    with verified_reader(path, expected_identity=file_identity(info)) as outer:
        _bounded_directory(outer, info.st_size)
        with ZipFile(outer) as archive:
            members = archive.infolist()
            if (not 5 <= len(members) <= MAX_PARTS + 4
                    or len({row.filename.casefold() for row in members}) != len(members)
                    or any(row.is_dir() or row.flag_bits & 1 or row.compress_type != ZIP_STORED
                           or not 0 < row.file_size <= max_bytes
                           or ((row.external_attr >> 16) & 0o170000) == 0o120000 for row in members)
                    or sum(row.file_size for row in members) > max_bytes):
                raise ValueError('Invalid bounded measured archive graph.')
            by_name = {row.filename: row for row in members}
            def raw(name):
                check()
                row = by_name.get(name)
                if row is None or not 0 < row.file_size <= MAX_JSON_BYTES:
                    raise ValueError('Missing or oversized measured publication metadata.')
                with archive.open(row) as stream:
                    result = stream.read(MAX_JSON_BYTES + 1)
                check()
                if len(result) != row.file_size:
                    raise ValueError('Measured metadata size differs from its receipt.')
                return result
            manifest = _json(raw('manifest.json'))
            if (type(manifest) is not dict or set(manifest) != {
                    'schema', 'publication_id', 'created', 'source', 'content', 'parts'}
                    or manifest['schema'] != SCHEMA or type(manifest['parts']) is not list
                    or not 1 <= len(manifest['parts']) <= MAX_PARTS
                    or type(manifest['publication_id']) is not str
                    or re.fullmatch('[0-9a-f]{32}', manifest['publication_id']) is None
                    or type(manifest['created']) is not str
                    or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', manifest['created']) is None):
                raise ValueError('Invalid measured publication manifest.')
            datetime.strptime(manifest['created'], '%Y-%m-%dT%H:%M:%SZ')
            documents = {name: raw(name) for name in _METADATA}
            project, timeline, _ = validate_metadata(documents, manifest['source'], check)
            content = manifest['content']
            if (type(content) is not dict or set(content) != {
                    'schema', 'source', 'pcm', 'maximum_ms', 'name_template', 'parts', 'notice'}
                    or content['schema'] != 'carr-measured-audio-content/2'
                    or content['source'] != manifest['source'] or content['notice'] != NOTICE
                    or type(content['parts']) is not list or not 1 <= len(content['parts']) <= MAX_PARTS):
                raise ValueError('Invalid reviewed measured content.')
            pcm = _pcm(content['pcm'])
            expected = content_review(project, timeline, manifest['source'], pcm,
                                      content['maximum_ms'], content['name_template'], check)
            if content != expected or len(manifest['parts']) != len(content['parts']):
                raise ValueError('Measured ranges or whole-line source mappings changed.')
            names = {'manifest.json', *_METADATA, *(part['name'] for part in content['parts'])}
            if set(by_name) != names:
                raise ValueError('Measured publication contains missing or unreviewed files.')
            whole_pcm = sha256()
            for part, receipt in zip(content['parts'], manifest['parts']):
                check()
                if (type(receipt) is not dict or set(receipt) != {'name', 'identity', 'pcm_sha256'}
                        or receipt['name'] != part['name']):
                    raise ValueError('Invalid measured part receipt.')
                header = _header(pcm, part['frames'])
                expected_bytes = part['frames'] * pcm['channels'] * 2
                if by_name[part['name']].file_size != len(header) + expected_bytes:
                    raise ValueError('A measured part differs from its exact frame byte count.')
                identity, part_pcm = sha256(), sha256()
                with archive.open(part['name']) as stream:
                    if stream.read(len(header)) != header:
                        raise ValueError('A measured part has a different PCM container or frame count.')
                    identity.update(header)
                    remaining = expected_bytes
                    while remaining:
                        check()
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError('A measured audio part is truncated.')
                        remaining -= len(block)
                        identity.update(block)
                        part_pcm.update(block)
                        whole_pcm.update(block)
                    if stream.read(1):
                        raise ValueError('A measured audio part contains extra data.')
                if (receipt['identity'] != {'size': len(header) + expected_bytes, 'sha256': identity.hexdigest()}
                        or part_pcm.hexdigest() != receipt['pcm_sha256']):
                    raise ValueError('A measured part differs from its exact checksum.')
            if whole_pcm.hexdigest() != pcm['pcm_sha256']:
                raise ValueError('Concatenated measured parts differ from the reviewed original PCM.')
    check()
    return manifest


def context(service, project, timeline, paths, proof, book, index, source_guard, check):
    """Capture exact metadata through the current source readers."""
    from .publishing import _read
    if not callable(source_guard):
        raise ValueError('Open measured splitting from the current retained Book review.')
    check()
    source_guard()
    check()
    bound = bind_book(project, book, index)
    plan_path = service.root / ('plan-' + proof['job_id'] + '.json')
    raw = _read(plan_path, {'size': regular_file(plan_path).st_size,
                           'sha256': proof['plan_file_sha256']}, check, collect=True)
    timeline_raw = _read(paths['timeline.json'][0], paths['timeline.json'][1], check, collect=True)
    documents = metadata(raw, timeline_raw, book)
    source = dict(proof, book=bound)
    validate_metadata(documents, source, check)
    return source, documents


def review(service, run_key, output, *, book, part_index, maximum_ms, name_template,
           source_guard, cancelled=lambda: False, progress=None, approval=None):
    from contextlib import ExitStack
    from time import monotonic
    from .jobs import checked_directory, project_lock
    from ..research.export_file import ExportDestination, capture_export_destination
    options(maximum_ms, name_template)
    if not isinstance(book, BookPlan) or not callable(source_guard):
        raise ValueError('Open measured splitting from the current retained Book review.')
    book.job_id(part_index)
    started, lease = monotonic(), None
    def check():
        if cancelled() or (lease is not None and lease.cancelled()):
            raise PublicationCancelled('Measured audio review cancelled.')
        if monotonic() - started >= service.timeout:
            raise TimeoutError('Measured audio review exceeded its selected time budget.')
    def stopped():
        check()
        return False
    def notify(message):
        if progress is not None:
            try:
                progress(message)
            except Exception:
                pass
    check()
    source_guard()
    selected = capture_export_destination(output, private_root=service.private_root, cancelled=stopped)
    if approval is not None and (type(approval) is not ExportDestination or approval != selected):
        raise ValueError('The selected measured publication destination changed.')
    if (not selected.require_absent or selected.target.suffix.casefold() != '.zip'
            or selected.target.is_relative_to(service.root)):
        raise ValueError('Choose a new .zip outside the source audio project.')
    with project_lock(service.root), ExitStack() as resources:
        if service.broker is not None:
            from ..operations import ResourceBroker, ResourceRequest
            if not isinstance(service.broker, ResourceBroker):
                raise ValueError('Use the existing library resource broker for measured review.')
            lease = resources.enter_context(service.broker.reserve(
                ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2, disk_bytes=0),
                operation_id=service.operation_id or 'publication-review:' + book.job_id(part_index),
                cancelled=cancelled, timeout=min(60, service.timeout), run_timeout=service.timeout))
        project, timeline, paths, proof, _ = service._source(run_key, check, notify)
        source, documents = context(service, project, timeline, paths, proof, book, part_index, source_guard, check)
        pcm = pcm_info(paths['master.wav'][0], timeline, project, check)
        content = content_review(project, timeline, source, pcm, maximum_ms, name_template, check)
        if service._source(run_key, check, lambda _: None)[3] != proof:
            raise ValueError('The completed source changed during measured review.')
        source_guard()
        check()
        if capture_export_destination(selected.requested, private_root=service.private_root,
                                      cancelled=stopped) != selected:
            raise ValueError('The measured publication destination changed during review.')
        return review_document(content, selected)
