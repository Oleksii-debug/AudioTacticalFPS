"""Explicitly bake two adjacent retained clips through the existing PCM owner.

Sources remain independent project assets. A small typed receipt records the
two exact source hashes and controls; it never activates a runtime or a path.
"""
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, fields, replace
from hashlib import sha256
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from ..backup_files import optional_present
from . import ffmpeg as audio
from .clip_audio import CLIP_MIX_REVISION, clip_window, process_clip
from .cleanup import AudioCleanupPending, wait_for_cleanup
from .recording_process import CaptureBudget, run_process
from .jobs import (ChunkCache, check_cancelled, checked_directory, file_identity,
                   project_lock, publish_noreplace, read_json, regular_file,
                   sha256_file, verified_reader, _sync_directory)
from .models import Clip, SAMPLE_RATES, MAX_CLIP_SECONDS, canonical, digest


SCHEMA = 'carr-clip-consolidation/1'
PREFIX = 'clip-consolidation-'
MAX_RECEIPT_BYTES = 32 * 1024
MAX_RECEIPTS = 1000
MAX_ROOT_ENTRIES = 1000000
MAX_SOURCE_BYTES = 8 * 1024**3
_RECIPE = {'schema', 'clip_revision', 'sample_rate', 'processor', 'inputs'}
_AUDIO = {'asset', 'bytes', 'sha256', 'sample_rate', 'channels', 'frames'}
_CLIP = {field.name for field in fields(Clip)}


def _owned(args, *, budget, cancelled, watch=()):
    try:
        return run_process(args, budget=budget, cancelled=cancelled,
                           watch=watch, owned_tree=True)
    except AudioCleanupPending as error:
        # Keep the current stage, readers and resource lease alive while the
        # existing native owner settles every descendant. Do not create a
        # second process family or unwind a pending-cleanup scope.
        wait_for_cleanup(error)
        raise


def _processor_identity(path, *, cancelled):
    result = _owned([str(path), '-version'],
                    budget=CaptureBudget(timeout=10, max_output_bytes=65536,
                                         max_log_bytes=1024 * 1024, reserve_disk_bytes=0),
                    cancelled=cancelled)
    raw = result.stdout + result.stderr
    lines = raw[:16384].decode('utf-8', 'replace').splitlines()
    if not lines or not lines[0].startswith('ffmpeg version '):
        raise RuntimeError('The selected consolidation executable did not identify itself as FFmpeg.')
    return {'binary_sha256': sha256_file(path, cancelled=cancelled), 'version': lines[0],
            'version_sha256': sha256(raw).hexdigest()}


def _process_pcm(ffmpeg, arguments, *, cancelled):
    target = Path(arguments[-1])
    result = _owned([str(ffmpeg), '-hide_banner', '-nostdin', '-loglevel', 'error',
                     '-max_alloc', str(128 * 1024**2), '-filter_threads', '1',
                     '-filter_complex_threads', '1', *arguments],
                    budget=CaptureBudget(timeout=3600, max_output_bytes=MAX_SOURCE_BYTES,
                        max_log_bytes=1024 * 1024, reserve_disk_bytes=16 * 1024**2,
                        max_staging_files=128, max_alloc_bytes=128 * 1024**2, threads=1),
                    cancelled=cancelled, watch=(target,))
    return result.stderr.decode('utf-8', errors='replace')


def is_receipt_name(name):
    return isinstance(name, str) and re.fullmatch(PREFIX + r'[0-9a-f]{64}\.json', name) is not None


def _hash(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('A consolidation receipt requires an exact byte digest.')
    return value


def _audio(value, *, output=False):
    if type(value) is not dict or set(value) != _AUDIO:
        raise ValueError('Invalid consolidated audio identity.')
    Clip('receipt', value['asset'])
    _hash(value['sha256'])
    if (type(value['bytes']) is not int or not 0 < value['bytes'] <= MAX_SOURCE_BYTES
            or type(value['sample_rate']) is not int or not 1 <= value['sample_rate'] <= 384000
            or type(value['channels']) is not int or value['channels'] not in (1, 2)
            or type(value['frames']) is not int or not 0 < value['frames'] <= MAX_SOURCE_BYTES // 2):
        raise ValueError('Consolidation PCM bounds are invalid.')
    if output and (value['asset'] != value['sha256'] + '.wav' or value['channels'] != 2):
        raise ValueError('Consolidated PCM must be an independent named stereo asset.')
    return audio.WaveInfo(value['sample_rate'], value['channels'], value['frames'],
                          0, value['frames'] * value['channels'] * 2)


def decode_receipt(value, *, name=None):
    """Pure strict schema/timeline validation, safe for restored inert bytes."""
    if (type(value) is not dict or set(value) != _RECIPE | {'output'}
            or value['schema'] != SCHEMA or type(value['clip_revision']) is not int
            or value['clip_revision'] != CLIP_MIX_REVISION
            or type(value['sample_rate']) is not int or value['sample_rate'] not in SAMPLE_RATES):
        raise ValueError('Unsupported clip consolidation receipt.')
    processor = value['processor']
    if (type(processor) is not dict or set(processor) != {'binary_sha256', 'version', 'version_sha256'}
            or type(processor['version']) is not str or not 0 < len(processor['version']) <= 16384
            or any(char in processor['version'] for char in '\r\n\0')):
        raise ValueError('Invalid retained consolidation processor identity.')
    _hash(processor['binary_sha256'])
    _hash(processor['version_sha256'])
    rows = value['inputs']
    if type(rows) is not list or len(rows) != 2:
        raise ValueError('Consolidation records exactly two source clips.')
    clips, windows = [], []
    for row in rows:
        if (type(row) is not dict or set(row) != {'clip', 'audio'}
                or type(row['clip']) is not dict or set(row['clip']) != _CLIP):
            raise ValueError('Invalid retained source clip controls.')
        clip = Clip(**row['clip'])
        info = _audio(row['audio'])
        if clip.asset != row['audio']['asset']:
            raise ValueError('Consolidated clip controls do not name their retained source.')
        clips.append(clip)
        windows.append(clip_window(clip, info, value['sample_rate']))
    a, b = windows
    if (clips[0].id == clips[1].id or clips[0].track != clips[1].track
            or a.start_frame + a.frames != b.start_frame):
        raise ValueError('Consolidation requires adjacent clips in one track.')
    total = a.frames + b.frames
    if total > MAX_CLIP_SECONDS * value['sample_rate']:
        raise ValueError('The consolidated clip exceeds the selected clip duration profile.')
    output = _audio(value['output'], output=True)
    if (output.rate, output.frames) != (value['sample_rate'], total):
        raise ValueError('Consolidated audio does not preserve the exact project sample count.')
    if name is not None and name != PREFIX + digest({key: value[key] for key in _RECIPE}) + '.json':
        raise ValueError('The consolidation receipt does not match its filename.')
    if len(canonical(value).encode('utf-8')) + 1 > MAX_RECEIPT_BYTES:
        raise ValueError('Consolidation provenance exceeds its metadata allowance.')
    return value


def _asset(root, record, *, cancelled, expected_identity=None):
    path = checked_directory(root / 'assets') / record['asset']
    source_info = regular_file(path)
    if source_info.st_nlink != 1:
        raise ValueError('Retained consolidation audio cannot have hard-link aliases.')
    stamp = file_identity(source_info)
    if expected_identity is not None and stamp != expected_identity:
        raise ValueError('A retained consolidation input changed.')
    with verified_reader(path, expected_identity=stamp):
        info = audio.wav_info(path)
        actual = {'asset': path.name, 'bytes': regular_file(path).st_size,
                  'sha256': sha256_file(path, cancelled=cancelled, expected_identity=stamp),
                  'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames}
        if actual != record:
            raise ValueError('Consolidation provenance differs from its retained audio bytes.')
    return path, stamp


def verify_receipt(root, path, *, cancelled=lambda: False):
    """Check all three retained audio identities without invoking FFmpeg."""
    root, path = checked_directory(root), Path(path)
    if path.parent != root or not is_receipt_name(path.name):
        raise ValueError('Choose a retained consolidation receipt in this project.')
    if regular_file(path).st_nlink != 1:
        raise ValueError('A consolidation receipt cannot have hard-link aliases.')
    value = decode_receipt(read_json(path, limit=MAX_RECEIPT_BYTES), name=path.name)
    for row in value['inputs']:
        check_cancelled(cancelled)
        _asset(root, row['audio'], cancelled=cancelled)
    _asset(root, value['output'], cancelled=cancelled)
    check_cancelled(cancelled)
    return value


def retained_receipts(root, *, cancelled=lambda: False):
    """One bounded inventory used by source snapshots and restored projects."""
    root = checked_directory(root)
    result = {}
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            check_cancelled(cancelled)
            if count > MAX_ROOT_ENTRIES:
                raise ValueError('The audio project exceeds its bounded root inventory.')
            if not entry.name.startswith(PREFIX):
                continue
            if not is_receipt_name(entry.name):
                raise ValueError('Unexpected consolidation receipt filename.')
            if len(result) >= MAX_RECEIPTS:
                raise ValueError('Too many retained consolidation receipts.')
            path = root / entry.name
            result[path.name] = (path, verify_receipt(root, path, cancelled=cancelled))
    return result


def _source(root, clip, rate, cancelled):
    path = checked_directory(root / 'assets') / clip.asset
    source_info = regular_file(path)
    if source_info.st_nlink != 1:
        raise ValueError('Retained consolidation audio cannot have hard-link aliases.')
    stamp = file_identity(source_info)
    with verified_reader(path, expected_identity=stamp):
        info = audio.wav_info(path)
        if not 0 < regular_file(path).st_size <= MAX_SOURCE_BYTES:
            raise ValueError('Consolidation needs bounded retained PCM sources.')
        row = {'asset': clip.asset, 'bytes': regular_file(path).st_size,
               'sha256': sha256_file(path, cancelled=cancelled, expected_identity=stamp),
               'sample_rate': info.rate, 'channels': info.channels, 'frames': info.frames}
        window = clip_window(clip, info, rate)
    return path, stamp, row, window


def consolidated_clip(value):
    """The edited clip plays baked controls once; original controls stay recorded."""
    value = decode_receipt(value)
    first = Clip(**value['inputs'][0]['clip'])
    return replace(first, asset=value['output']['asset'], start=first.start,
                   trim_start=0, duration=None, gain_db=0, pan=0, fade_in=0, fade_out=0)


def consolidate_clips(project_dir, first, second, *, ffmpeg, sample_rate,
                      disk_budget_bytes=32 * 1024**3, cancelled=lambda: False,
                      broker=None, operation_id=None, trust_controller=None):
    """Checked reusable operation for UI or an explicitly scoped local tool.

    Caller selects two typed clips; only their existing project assets are read.
    One project lock and the existing resource lease cover all owned children,
    verification, provenance publication and private-stage cleanup.
    """
    from ..security.admission import checked_controller
    trust_controller = checked_controller(trust_controller)
    root = checked_directory(project_dir)
    if (type(first) is not Clip or type(second) is not Clip or first.id == second.id
            or first.track != second.track or type(sample_rate) is not int or sample_rate not in SAMPLE_RATES):
        raise ValueError('Choose two adjacent clips in the same track.')
    if type(disk_budget_bytes) is not int or disk_budget_bytes <= 0:
        raise ValueError('Choose a positive project disk allowance.')
    with project_lock(root):
        return _consolidate(root, first, second, ffmpeg=ffmpeg, sample_rate=sample_rate,
                            disk_budget_bytes=disk_budget_bytes, cancelled=cancelled,
                            broker=broker, operation_id=operation_id, trust_controller=trust_controller)


def _consolidate(root, first, second, *, ffmpeg, sample_rate, disk_budget_bytes,
                 cancelled, broker, operation_id, trust_controller):
    from .clips import _retain_pcm_asset

    root_stamp = checked_directory(root).stat()
    root_identity = root_stamp.st_dev, root_stamp.st_ino
    ordered = sorted(((first, _source(root, first, sample_rate, cancelled)),
                      (second, _source(root, second, sample_rate, cancelled))),
                     key=lambda pair: pair[1][3].start_frame)
    (first, a), (second, b) = ordered
    if a[3].start_frame + a[3].frames != b[3].start_frame:
        raise ValueError('Consolidate clips that are exactly adjacent on the project sample timeline.')
    frames = a[3].frames + b[3].frames
    if frames > MAX_CLIP_SECONDS * sample_rate or frames * 4 + 65536 > MAX_SOURCE_BYTES:
        raise ValueError('The resulting independent PCM asset exceeds its bounded source profile.')
    binary = Path(audio.trusted_ffmpeg(ffmpeg))
    binary_stamp = file_identity(regular_file(binary))
    with verified_reader(binary, expected_identity=binary_stamp):
        processor = _processor_identity(binary, cancelled=cancelled)
    recipe = {'schema': SCHEMA, 'clip_revision': CLIP_MIX_REVISION,
              'sample_rate': sample_rate, 'processor': processor,
              'inputs': [{'clip': asdict(clip), 'audio': item[2]} for clip, item in ordered]}
    if len(canonical(recipe).encode('utf-8')) + 1024 > MAX_RECEIPT_BYTES:
        raise ValueError('Consolidation provenance exceeds its metadata allowance.')
    name = PREFIX + digest(recipe) + '.json'
    destination = root / name
    if optional_present(destination):
        value = verify_receipt(root, destination, cancelled=cancelled)
        if {key: value[key] for key in _RECIPE} != recipe:
            raise ValueError('Existing consolidation has different source controls.')
        _sync_directory(root / 'assets')
        _sync_directory(root)
        return {'clip': consolidated_clip(value), 'receipt': name, 'reused': True}
    # Bound the receipt namespace even when the existing project contains many
    # saved plans. No previous recipe is executed or loaded as a runtime.
    count = 0
    with os.scandir(root) as entries:
        for total, entry in enumerate(entries, 1):
            check_cancelled(cancelled)
            if total > MAX_ROOT_ENTRIES:
                raise ValueError('The audio project exceeds its bounded root inventory.')
            if entry.name.startswith(PREFIX):
                if not is_receipt_name(entry.name):
                    raise ValueError('Unexpected consolidation receipt filename.')
                regular_file(root / entry.name)
                count += 1
                if count >= MAX_RECEIPTS:
                    raise ValueError('The project already has its maximum consolidation receipts.')
    # Two processed clips, their exact concatenation, an independent retained
    # copy and the existing copy publisher's stage can coexist.
    estimate = frames * 4 * 4 + 5 * 65536 + MAX_RECEIPT_BYTES
    cache = ChunkCache(root / 'cache', cancelled=cancelled, budget_root=root,
                       disk_budget_bytes=disk_budget_bytes)
    cache.reserve(estimate)
    admission = nullcontext(None)
    if broker is not None:
        from ..operations import ResourceBroker, ResourceRequest
        if not isinstance(broker, ResourceBroker):
            raise TypeError('Use the shared typed resource coordinator.')
        if broker.limits.memory_bytes <= 0:
            raise ValueError('Configure a positive declared memory allowance.')
        request = ResourceRequest(cpu_slots=broker.limits.cpu_slots,
            memory_bytes=broker.limits.memory_bytes, disk_bytes=estimate, disk_path=root)
        admission = broker.reserve(request,
            operation_id=operation_id or 'studio:' + digest(str(root))[:32],
            cancelled=cancelled, timeout=60)
    with admission as lease:
        stopping = lease.cancelled if lease is not None else cancelled
        def check():
            check_cancelled(stopping)
            stamp = checked_directory(root).stat()
            if (stamp.st_dev, stamp.st_ino) != root_identity:
                raise ValueError('The audio project changed during consolidation.')
            return False

        check()
        cache.cancelled = stopping
        cache.reserve(estimate)
        from ..security.admission import admit_new_operation
        authorization = admit_new_operation(trust_controller, 'audio.render')
        with TemporaryDirectory(prefix='.clip-consolidation-', dir=root) as temporary:
            stage = checked_directory(temporary)
            parts = [stage / 'first.wav', stage / 'second.wav']
            joined = stage / 'joined.wav'
            with ExitStack() as readers:
                readers.enter_context(verified_reader(binary, expected_identity=binary_stamp))
                if sha256_file(binary, cancelled=check) != processor['binary_sha256']:
                    raise ValueError('The selected consolidation processor changed.')
                for _, item in ordered:
                    readers.enter_context(verified_reader(item[0], expected_identity=item[1]))
                    if sha256_file(item[0], cancelled=check, expected_identity=item[1]) != item[2]['sha256']:
                        raise ValueError('A consolidation input changed while waiting for resources.')
                for (clip, item), target in zip(ordered, parts):
                    process_clip(item[0], target, clip, ffmpeg=binary, sample_rate=sample_rate,
                                 cancelled=check, expected_identity=item[1], runner=_process_pcm)
                audio.concatenate(parts, joined, cancelled=check)
                info = audio.wav_info(joined)
                if (info.rate, info.channels, info.frames) != (sample_rate, 2, frames):
                    raise RuntimeError('Consolidation changed the selected project sample intervals.')
                content = sha256_file(joined, cancelled=check)
                output = {'asset': content + '.wav', 'bytes': regular_file(joined).st_size,
                          'sha256': content, 'sample_rate': info.rate,
                          'channels': info.channels, 'frames': info.frames}
                value = decode_receipt({**recipe, 'output': output}, name=name)
                check()
            # All final source/runtime reader checks finish before retaining an
            # accepted asset or its receipt. The originals are never overwritten.
            retained, _ = _retain_pcm_asset(joined, root, cancelled=check,
                expected_sha256=content, disk_budget_bytes=disk_budget_bytes)
            if retained != output['asset']:
                raise RuntimeError('The retained consolidation identity changed.')
            raw = (canonical(value) + '\n').encode('utf-8')
            checkpoint = stage / 'receipt.json'
            with checkpoint.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            check()
            publish_noreplace(checkpoint, destination, cancelled=check)
            # Receipt publication is the durable operation boundary. A later
            # Stop cannot turn a verified completed operation into a partial one.
            return {'clip': consolidated_clip(value), 'receipt': name, 'reused': False}
