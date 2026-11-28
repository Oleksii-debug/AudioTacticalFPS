"""Measured part publication for a completed item in the existing audio queue.

The queue, Studio renderer, native-process owner and resource broker remain the
authorities. This adapter saves an immutable review and independent accepted
PCM, then publishes all parts as one no-replace directory. A cancelled attempt
can repeat the same approved review; a verified complete result is reused.
"""
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re
import shutil
import tempfile
import time

from ..operations import ResourceRequest
from ..security.admission import admitted_operation
from . import media_conversion as media
from .batch_models import (AudioPreset, MAX_INPUT_BYTES, MAX_OUTPUTS, MAX_STATE_BYTES,
                           canonical, count, digest, exact, hash_value, identity,
                           output_names)
from .cleanup import AudioCleanupPending, wait_for_cleanup
from .ffmpeg import wav_info
from .jobs import (atomic_json, checked_directory, check_cancelled, file_identity,
                   publish_new, publish_noreplace, read_json, regular_file,
                   sha256_file, verified_reader, _sync_directory)
from .managed import ManagedProjects

DIRECTORY = 'duration'
REVIEW_SCHEMA = 'carr-book-duration-review/1'
RECEIPT_SCHEMA = 'carr-book-duration-receipt/1'
MAX_REVIEWS = 1000
PENDING = re.compile(r'\.pending-duration-([0-9a-f]{32})-[A-Za-z0-9_-]+\Z')
PART = re.compile(r'[0-9]{4}-.+-[0-9a-f]{32}-duration-[0-9]+-part-[0-9]{4,5}\.(?:wav|flac|mp3|opus|m4a|m4b|aiff|ogg|aac|wma)\Z')


def requested_duration(value, rate):
    """Convert reviewed decimal seconds to an integer number of source frames."""
    if type(value) not in (str, int, float) or isinstance(value, bool):
        raise ValueError('Choose a bounded decimal duration in seconds.')
    raw = str(value)
    if re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,6})?', raw) is None:
        raise ValueError('Duration needs plain seconds with at most six decimal places.')
    try:
        seconds = Decimal(raw)
    except InvalidOperation:
        raise ValueError('Invalid decimal duration.') from None
    if not Decimal('0.05') <= seconds <= Decimal('86400'):
        raise ValueError('Each duration part must be between 0.05 and 86400 seconds.')
    frames = int(seconds * count(rate, 'Source sample rate', 8000, 192000))
    if frames < 1:
        raise ValueError('The requested duration contains no complete source sample.')
    return format(seconds.normalize(), 'f'), frames


def intervals(frames, part_frames):
    count(frames, 'Accepted source frames', 1)
    count(part_frames, 'Part source frames', 1)
    number = (frames + part_frames - 1) // part_frames
    if number > MAX_OUTPUTS:
        raise ValueError('The duration review exceeds the 10000-part allowance.')
    return tuple((first, min(frames, first + part_frames))
                 for first in range(0, frames, part_frames))


def part_names(item, preset, part_frames, number):
    base = output_names(item.id, item.ordinal, item.title, preset)[0].rsplit('.', 1)[0]
    return tuple(f'{base}-duration-{part_frames}-part-{n:04d}.{preset.output_format}'
                 for n in range(1, number + 1))


def validate_review(value, batch, item):
    exact(value, ('schema', 'id', 'sha256', 'batch_id', 'item_id', 'batch_plan_sha256',
          'item_sha256', 'source_sha256', 'queue_receipt_sha256', 'render',
          'accepted_completion_sha256', 'source', 'requested_seconds', 'part_frames',
          'preset', 'parts'), 'measured book duration review')
    expected = {'schema': REVIEW_SCHEMA, 'batch_id': batch.id, 'item_id': item.id,
                'batch_plan_sha256': batch.plan_sha256, 'item_sha256': item.fingerprint,
                'source_sha256': item.source.sha256}
    if item.kind != 'book' or any(value[key] != wanted for key, wanted in expected.items()):
        raise ValueError('The duration review belongs to another exact audiobook item.')
    identity(value['id'])
    for key in ('sha256', 'queue_receipt_sha256', 'accepted_completion_sha256'):
        hash_value(value[key])
    render = exact(value['render'], ('run_key', 'plan_sha256', 'source_sha256'), 'accepted Studio render')
    for field in render.values():
        hash_value(field)
    source = exact(value['source'], ('name', 'bytes', 'sha256', 'sample_rate', 'channels', 'frames'),
                   'independent accepted PCM')
    if source['name'] != 'source.wav':
        raise ValueError('The accepted PCM is outside the fixed duration source namespace.')
    hash_value(source['sha256'])
    count(source['bytes'], 'Accepted PCM bytes', 44, MAX_INPUT_BYTES)
    count(source['sample_rate'], 'Accepted PCM sample rate', 8000, 192000)
    count(source['channels'], 'Accepted PCM channels', 1, 2)
    count(source['frames'], 'Accepted PCM frames', 1, source['bytes'] // (2 * source['channels']))
    requested, part_frames = requested_duration(value['requested_seconds'], source['sample_rate'])
    if (type(value['requested_seconds']) is not str or requested != value['requested_seconds']
            or type(value['part_frames']) is not int or part_frames != value['part_frames']):
        raise ValueError('The saved duration is not its exact reviewed source-frame interval.')
    preset = AudioPreset.from_dict(value['preset'])
    if (preset.split != 'whole' or preset.sample_rate != source['sample_rate']
            or preset.channels != source['channels']
            or preset.max_seconds < part_frames / source['sample_rate']):
        raise ValueError('Duration delivery must retain the accepted PCM sample rate and channel count.')
    bounds = intervals(source['frames'], part_frames)
    names = part_names(item, preset, part_frames, len(bounds))
    wanted = [{'name': name, 'first_frame': first, 'end_frame': last}
              for name, (first, last) in zip(names, bounds)]
    if (type(value['parts']) is not list
            or any(type(row) is not dict or set(row) != {'name', 'first_frame', 'end_frame'}
                   or type(row['first_frame']) is not int or type(row['end_frame']) is not int
                   for row in value['parts']) or value['parts'] != wanted):
        raise ValueError('Reviewed output names or source sample intervals changed.')
    body = {key: entry for key, entry in value.items() if key not in ('id', 'sha256')}
    fingerprint = digest(body)
    if value['sha256'] != fingerprint or value['id'] != fingerprint[:32]:
        raise ValueError('The duration review lost its exact immutable fingerprint.')
    if len(canonical(value)) > MAX_STATE_BYTES:
        raise ValueError('The duration review exceeds its metadata allowance.')
    return value


def validate_receipt(value, review):
    exact(value, ('schema', 'status', 'review_id', 'review_sha256', 'source_sha256', 'files'),
          'completed duration publication')
    if (value['schema'] != RECEIPT_SCHEMA or value['status'] != 'complete'
            or value['review_id'] != review['id'] or value['review_sha256'] != review['sha256']
            or value['source_sha256'] != review['source']['sha256']):
        raise ValueError('The duration receipt belongs to another accepted PCM or review.')
    if type(value['files']) is not list or len(value['files']) != len(review['parts']):
        raise ValueError('The completed duration publication lost a reviewed part.')
    preset = AudioPreset.from_dict(review['preset'])
    for row, part in zip(value['files'], review['parts']):
        exact(row, ('name', 'bytes', 'sha256', 'sample_rate', 'channels', 'frames',
                    'source_first_frame', 'source_end_frame', 'pcm_frames'),
              'verified duration output')
        wanted = part['end_frame'] - part['first_frame']
        if (row['name'] != part['name'] or type(row['source_first_frame']) is not int
                or type(row['source_end_frame']) is not int
                or row['source_first_frame'] != part['first_frame']
                or row['source_end_frame'] != part['end_frame']
                or type(row['pcm_frames']) is not int or row['pcm_frames'] != wanted):
            raise ValueError('A duration output lost its exact accepted source sample interval.')
        hash_value(row['sha256'])
        count(row['bytes'], 'Duration output bytes', 1, 32 * 1024**3)
        count(row['frames'], 'Actually decoded output frames', 1, wanted + preset.sample_rate)
        if (type(row['sample_rate']) is not int or row['sample_rate'] != preset.sample_rate
                or type(row['channels']) is not int or row['channels'] != preset.channels):
            raise ValueError('A duration output changed the reviewed PCM rate or channel count.')
        if preset.output_format in ('wav', 'flac', 'aiff') and row['frames'] != wanted:
            raise ValueError('Lossless duration output must preserve every reviewed PCM frame.')
    return value


def _item(service, batch_id, item_id, cancelled):
    batch = service.load(batch_id)
    identity(item_id)
    item = next((row for row in batch.items if row.id == item_id), None)
    if item is None or item.kind != 'book' or item.state != 'complete':
        raise ValueError('Choose an already completed saved audiobook item.')
    service.verify_sources(batch, item, cancelled=cancelled)
    receipt = service.verified_receipt(batch, item, cancelled=cancelled, durable=True)
    if receipt is None:
        raise ValueError('The selected audiobook has no verified completed output.')
    return batch, item, receipt


def _root(service, batch_id, review_id):
    identity(review_id)
    return service.path(batch_id) / DIRECTORY / review_id


def _read(service, batch, item, review_id, receipt, cancelled):
    root = checked_directory(_root(service, batch.id, review_id))
    review = validate_review(read_json(root / 'review.json', limit=MAX_STATE_BYTES), batch, item)
    if (review['id'] != review_id or review['queue_receipt_sha256'] != digest(receipt)
            or review['render'] != receipt['render']):
        raise ValueError('The completed queue changed since this duration review.')
    if {path.name for path in root.iterdir()} - {'review.json', 'source.wav', 'outputs'}:
        raise ValueError('The duration review contains undeclared files.')
    source = root / 'source.wav'
    service._verify_file(source, review['source']['bytes'], review['source']['sha256'], cancelled)
    info = wav_info(source)
    if (info.rate, info.channels, info.frames) != tuple(
            review['source'][key] for key in ('sample_rate', 'channels', 'frames')):
        raise ValueError('The accepted PCM differs from its reviewed measured duration.')
    return root, review


def _completed(service, root, review, cancelled, *, durable=False):
    output = root / 'outputs'
    if not os.path.lexists(output):
        return None
    checked_directory(output)
    value = validate_receipt(read_json(output / 'receipt.json', limit=MAX_STATE_BYTES), review)
    if {path.name for path in output.iterdir()} != {row['name'] for row in review['parts']} | {'receipt.json'}:
        raise ValueError('The completed duration directory contains missing or undeclared files.')
    for record in value['files']:
        path = output / record['name']
        service._verify_file(path, record['bytes'], record['sha256'], cancelled)
        if review['preset']['output_format'] == 'wav':
            info = wav_info(path)
            if (info.rate, info.channels, info.frames) != (
                    record['sample_rate'], record['channels'], record['frames']):
                raise ValueError('The completed duration WAV differs from its sample receipt.')
    if durable:
        _sync_directory(output)
        _sync_directory(root)
    return value


def _discard(service, batch):
    from ..audio_batch_backup import duration_pending
    for path in tuple(service.path(batch.id).iterdir()):
        if PENDING.fullmatch(path.name):
            duration_pending(path, batch)
            shutil.rmtree(path)
    _sync_directory(service.path(batch.id))


def review(service, batch_id, item_id, seconds, *, output_format=None,
           cancelled=lambda: False, on_status=lambda message: None):
    from .batches import producer_guard
    from .publishing import PublicationService
    with producer_guard(service.library):
        batch, item, receipt = _item(service, batch_id, item_id, cancelled)
        _discard(service, batch)
        _, _, audio = ManagedProjects(service.library).paths(item.id)
        deadline = time.monotonic() + item.preset.timeout_seconds
        def check():
            check_cancelled(cancelled)
            if time.monotonic() >= deadline:
                raise TimeoutError('Accepted audiobook verification exceeded its review time allowance.')
        publication = PublicationService(audio, private_root=service.library,
            expected_plan_sha256=receipt['render']['plan_sha256'])
        plan, timeline, paths, proof, info = publication._source(receipt['render']['run_key'], check, on_status)
        if plan.source_sha256 != receipt['render']['source_sha256']:
            raise ValueError('The accepted master differs from this queue exact source plan.')
        master, record = paths['master.wav']
        if not 44 <= record['size'] <= MAX_INPUT_BYTES:
            raise ValueError('The accepted duration source exceeds its retained PCM allowance.')
        requested, part_frames = requested_duration(seconds, info.rate)
        bounds = intervals(info.frames, part_frames)
        preset = replace(item.preset, label='Reviewed measured book parts', split='whole',
                         output_format=output_format or item.preset.output_format,
                         sample_rate=info.rate, channels=info.channels,
                         max_seconds=max(.05, part_frames / info.rate))
        body = {'schema': REVIEW_SCHEMA, 'batch_id': batch.id, 'item_id': item.id,
                'batch_plan_sha256': batch.plan_sha256, 'item_sha256': item.fingerprint,
                'source_sha256': item.source.sha256, 'queue_receipt_sha256': digest(receipt),
                'render': receipt['render'], 'accepted_completion_sha256': proof['job_sha256'],
                'source': {'name': 'source.wav', 'bytes': record['size'], 'sha256': record['sha256'],
                           'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames},
                'requested_seconds': requested, 'part_frames': part_frames, 'preset': preset.to_dict(),
                'parts': [{'name': name, 'first_frame': first, 'end_frame': last}
                          for name, (first, last) in zip(
                              part_names(item, preset, part_frames, len(bounds)), bounds)]}
        fingerprint = digest(body)
        value = validate_review({**body, 'id': fingerprint[:32], 'sha256': fingerprint}, batch, item)
        destination = _root(service, batch.id, value['id'])
        if os.path.lexists(destination):
            _, existing = _read(service, batch, item, value['id'], receipt, cancelled)
            if existing != value:
                raise ValueError('A saved duration review conflicts with this accepted master.')
            return existing
        base = checked_directory(destination.parent, create=True)
        for index, path in enumerate(base.iterdir(), 1):
            identity(path.name)
            checked_directory(path)
            if index >= MAX_REVIEWS:
                raise ValueError('This audio queue already has the maximum saved duration reviews.')
        def review_stop():
            check()
            return False
        request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                                  disk_bytes=record['size'] + 2 * MAX_STATE_BYTES, disk_path=base)
        with service.broker.reserve(request, operation_id='audio-duration-review:' + value['id'],
                                    cancelled=review_stop, timeout=60) as lease:
            with tempfile.TemporaryDirectory(prefix='.pending-duration-' + value['id'] + '-',
                                             dir=service.path(batch.id)) as temporary:
                stage = Path(temporary)
                atomic_json(stage / 'owner.json', {'schema': REVIEW_SCHEMA, 'action': 'review',
                    'batch_id': batch.id, 'item_id': item.id, 'review_id': value['id']})
                publish = checked_directory(stage / 'publish', create=True)
                original = file_identity(regular_file(master))
                with verified_reader(master, expected_identity=original):
                    service._verify_file(master, record['size'], record['sha256'], lease.cancelled)
                    publish_new(master, publish / 'source.wav', cancelled=lease.cancelled,
                                expected_identity=original, expected_sha256=record['sha256'])
                    service._verify_file(publish / 'source.wav', record['size'], record['sha256'], lease.cancelled)
                    atomic_json(publish / 'review.json', value)
                    if _item(service, batch.id, item.id, lease.cancelled)[2] != receipt:
                        raise ValueError('The completed queue receipt changed during accepted PCM capture.')
                    check_cancelled(lease.cancelled)
                    publish_noreplace(publish, destination, cancelled=lease.cancelled)
        _read(service, batch, item, value['id'], receipt, lambda: False)
        return value


def describe(service, batch_id, item_id, review_id, *, cancelled=lambda: False):
    batch, item, receipt = _item(service, batch_id, item_id, cancelled)
    root, value = _read(service, batch, item, review_id, receipt, cancelled)
    completed = _completed(service, root, value, cancelled)
    return {'status': 'complete' if completed is not None else 'reviewed', 'review': value,
            'receipt': completed, 'outputs': [str(root / 'outputs' / row['name']) for row in value['parts']]}


@contextmanager
def _settled_native():
    try:
        yield
    except AudioCleanupPending as error:
        wait_for_cleanup(error)
        raise


def publish(service, batch_id, item_id, review_id, *, approved_sha256,
            cancelled=lambda: False, on_status=lambda message: None):
    from .batches import producer_guard
    hash_value(approved_sha256)
    with producer_guard(service.library):
        batch, item, receipt = _item(service, batch_id, item_id, cancelled)
        _discard(service, batch)
        root, value = _read(service, batch, item, review_id, receipt, cancelled)
        if approved_sha256 != value['sha256']:
            raise ValueError('Approve the exact displayed duration review before publishing its parts.')
        previous = _completed(service, root, value, cancelled, durable=True)
        if previous is not None:
            return previous
        # An explicit publish resumes this one reviewed derived operation. Old
        # queue completions remain immutable; newly requested controls still stop it.
        control = service._continue(batch.id)
        preset = AudioPreset.from_dict(value['preset'])
        deadline = time.monotonic() + preset.timeout_seconds
        def stop():
            service._scope()
            current = service._control(batch.id)
            if current.sequence < control.sequence:
                raise ValueError('The durable audio control sequence moved backwards.')
            if time.monotonic() >= deadline:
                raise TimeoutError('Duration publication exceeded its total native time allowance.')
            return cancelled() or current.action in ('pause', 'cancel')
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
            disk_bytes=value['source']['bytes'] * 4 + len(value['parts']) * 1024**2 + 2 * MAX_STATE_BYTES,
            disk_path=root)
        with admitted_operation(service.trust_controller, 'audio.render'):
            with service.broker.reserve(request, operation_id='audio-duration:' + value['id'],
                                        cancelled=stop, timeout=60) as lease:
                with tempfile.TemporaryDirectory(prefix='.pending-duration-' + value['id'] + '-',
                                                 dir=service.path(batch.id)) as temporary:
                    stage = Path(temporary)
                    atomic_json(stage / 'owner.json', {'schema': REVIEW_SCHEMA, 'action': 'publish',
                        'batch_id': batch.id, 'item_id': item.id, 'review_id': value['id']})
                    output = checked_directory(stage / 'publish', create=True)
                    work = checked_directory(stage / 'work', create=True)
                    def bounded_stop():
                        if lease.cancelled():
                            return True
                        total, files = 0, 0
                        for directory in (output, work):
                            for path in directory.iterdir():
                                info = regular_file(path)
                                files += 1
                                total += info.st_size
                                if info.st_nlink != 1 or files > len(value['parts']) + 4 or total > request.disk_bytes:
                                    raise ValueError('Duration scratch exceeded its owned file or disk allowance.')
                        return False
                    source = root / 'source.wav'
                    original = file_identity(regular_file(source))
                    try:
                        with media.selected_runtime(service.ffmpeg, batch.runtime, cancelled=bounded_stop):
                            with verified_reader(source, expected_identity=original), _settled_native():
                                service._verify_file(source, value['source']['bytes'],
                                                     value['source']['sha256'], bounded_stop)
                                files = []
                                for index, part in enumerate(value['parts'], 1):
                                    on_status(f'Publishing reviewed duration part {index} of {len(value["parts"])}.')
                                    decoded = work / 'decoded.wav'
                                    info = media.decode(source, decoded, demuxer='wav', stream_index=0,
                                        ffmpeg=service.ffmpeg, preset=preset, cancelled=bounded_stop,
                                        sample_bounds=(part['first_frame'], part['end_frame']))
                                    frames = part['end_frame'] - part['first_frame']
                                    if info.frames != frames:
                                        raise ValueError('The selected native trim did not preserve exact source sample bounds.')
                                    target = output / part['name']
                                    media.encode(decoded, target, ffmpeg=service.ffmpeg, preset=preset,
                                                 cancelled=bounded_stop)
                                    decoded.unlink()
                                    encoded = media.verify_encoded(target, work / 'verified.wav',
                                        ffmpeg=service.ffmpeg, preset=preset, cancelled=bounded_stop)
                                    files.append({**encoded, 'source_first_frame': part['first_frame'],
                                                  'source_end_frame': part['end_frame'], 'pcm_frames': frames})
                                service._verify_file(source, value['source']['bytes'],
                                                     value['source']['sha256'], bounded_stop)
                                if _item(service, batch.id, item.id, bounded_stop)[2] != receipt:
                                    raise ValueError('The completed queue receipt changed during duration publication.')
                                result = validate_receipt({'schema': RECEIPT_SCHEMA, 'status': 'complete',
                                    'review_id': value['id'], 'review_sha256': value['sha256'],
                                    'source_sha256': value['source']['sha256'], 'files': files}, value)
                                atomic_json(output / 'receipt.json', result)
                                check_cancelled(bounded_stop)
                                publish_noreplace(output, root / 'outputs', cancelled=bounded_stop)
                    except AudioCleanupPending as error:
                        # Retain source readers, scratch and reservation until the
                        # existing native owner settles; media._run also settles
                        # its own child before propagating this retained failure.
                        wait_for_cleanup(error)
                        raise
        return _completed(service, root, value, lambda: False, durable=True)


def export(service, batch_id, item_id, review_id, filename, destination, *, approval,
           cancelled=lambda: False):
    from ..research.export_file import capture_export_destination, ExportDestination
    batch, item, receipt = _item(service, batch_id, item_id, cancelled)
    root, review = _read(service, batch, item, review_id, receipt, cancelled)
    complete = _completed(service, root, review, cancelled, durable=True)
    if complete is None:
        raise ValueError('Publish and verify the reviewed duration parts before exporting one.')
    row = next((record for record in complete['files'] if record['name'] == filename), None)
    if row is None:
        raise ValueError('Choose an exact completed duration output filename.')
    destination = Path(destination).absolute()
    if type(approval) is not ExportDestination or not approval.require_absent:
        raise ValueError('Capture a new explicit export filename; duration exports never replace a file.')
    def stop():
        if cancelled():
            return True
        if capture_export_destination(destination, private_root=service.library) != approval:
            raise ValueError('The approved duration export destination changed.')
        return False
    check_cancelled(stop)
    source = root / 'outputs' / filename
    original = file_identity(regular_file(source))
    request = ResourceRequest(cpu_slots=1, memory_bytes=64 * 1024**2,
                              disk_bytes=row['bytes'] + 1024**2, disk_path=destination.parent)
    with service.broker.reserve(request, operation_id='audio-duration-export:' + review_id,
                                cancelled=stop, timeout=60) as lease:
        publish_new(source, destination, cancelled=lease.cancelled, expected_identity=original,
                    expected_sha256=row['sha256'])
    service._verify_file(destination, row['bytes'], row['sha256'])
    return {'status': 'complete', 'output': str(destination), 'sha256': row['sha256'],
            'source_batch': batch_id, 'source_item': item_id, 'review_id': review_id}


def saved(service, batch_id, item_id, *, cancelled=lambda: False):
    """List typed passive reviews; selecting one performs full byte verification."""
    batch, item, receipt = _item(service, batch_id, item_id, cancelled)
    base = service.path(batch.id) / DIRECTORY
    if not os.path.lexists(base):
        return ()
    checked_directory(base)
    result = []
    for index, path in enumerate(base.iterdir()):
        check_cancelled(cancelled)
        if index >= MAX_REVIEWS:
            raise ValueError('The saved duration review inventory exceeds its allowance.')
        identity(path.name)
        checked_directory(path)
        value = read_json(path / 'review.json', limit=MAX_STATE_BYTES)
        if type(value) is not dict or value.get('item_id') not in {row.id for row in batch.items}:
            raise ValueError('A duration review belongs to an unknown queue item.')
        selected = next(row for row in batch.items if row.id == value['item_id'])
        validate_review(value, batch, selected)
        if value['id'] != path.name:
            raise ValueError('A saved duration review is under another identity.')
        if selected.id == item.id:
            if value['queue_receipt_sha256'] != digest(receipt) or value['render'] != receipt['render']:
                raise ValueError('A saved duration review lost its exact completed source.')
            result.append({'id': value['id'], 'sha256': value['sha256'],
                           'seconds': value['requested_seconds'], 'parts': len(value['parts']),
                           'format': value['preset']['output_format']})
    return tuple(sorted(result, key=lambda row: row['id']))
