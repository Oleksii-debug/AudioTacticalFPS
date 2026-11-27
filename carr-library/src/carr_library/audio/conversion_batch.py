"""Reviewed ordered audio conversions on the existing FFmpeg runtime.

This is synchronous batch glue, not another queue or renderer. The existing
resource broker owns admission; AudioRuntime.convert owns encoding and its
create-only publication. An external selected-folder manifest records each
validated candidate before publication and can recover only those exact bytes.
"""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from .effect_presets import checked_chain, read_effect
from .models import AudioCancelled, AudioError, AudioInfo, finite, integer
from ..durable_state import (atomic_json, encode, local_path, read_json_snapshot,
                             regular, state_lock, sync_directory, _open_json_read)
from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled, ResourceTimeout

MAX_FILES = 64
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_SOURCE_TOTAL = 32 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
MANIFEST = 'conversion-manifest.json'
FORMATS = ('wav', 'flac', 'mp3', 'm4a', 'm4b', 'ogg', 'opus', 'aac', 'aiff', 'wma')


def _stamp(value):
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _identity(path, *, maximum, cancelled=lambda: False):
    """Bound and bind the actual descriptor and path around every binary read."""
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size > maximum:
        raise ValueError('A selected audio file exceeds its byte budget.')
    digest, count = sha256(), 0
    with _open_json_read(path) as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A selected audio file changed while opening.')
        while True:
            if cancelled():
                raise AudioCancelled('Audio batch verification was cancelled.')
            block = stream.read(min(1024**2, maximum + 1 - count))
            if not block:
                break
            count += len(block)
            if count > maximum:
                raise ValueError('A selected audio file grew beyond its byte budget.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A selected audio file changed during verification.')
    if count != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('A selected audio file changed during verification.')
    return {'size_bytes': count, 'sha256': digest.hexdigest(), 'stamp': _stamp(before)}


def _settings(*, effects=(), sample_rate=24000, channels=2, start=0, duration=None,
              bit_depth=16, bitrate=192):
    integer(sample_rate, 'Output sample rate', 8000, 192000)
    integer(channels, 'Output channels', 1, 2)
    if type(bit_depth) is not int or bit_depth not in (16, 24, 32):
        raise ValueError('PCM bit depth must be 16, 24 or 32.')
    integer(bitrate, 'Encoding bitrate', 32, 512)
    start = finite(start, 'Selection start', 0)
    duration = None if duration is None else finite(duration, 'Selection duration', .000001)
    chain = checked_chain(effects, sample_rate)
    return {'effects': [item.to_dict() for item in chain], 'sample_rate': sample_rate,
            'channels': channels, 'start': start, 'duration': duration,
            'bit_depth': bit_depth, 'bitrate': bitrate}


def _options(value):
    if type(value) is not dict or set(value) != {
            'effects', 'sample_rate', 'channels', 'start', 'duration', 'bit_depth', 'bitrate'}:
        raise ValueError('Unsupported audio batch settings.')
    result = _settings(**dict(value, effects=tuple(read_effect(item) for item in value['effects'])))
    if result != value:
        raise ValueError('The saved audio settings changed their normalized identity.')
    return dict(result, effects=tuple(read_effect(item) for item in result['effects']))


def _name(index, source, extension):
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', Path(source).stem).strip(' .')[:100] or 'audio'
    # An explicit ordinal disambiguates equal basenames and Windows reserved names.
    return f'{index + 1:03d}-{label}.{extension}'


def _plan_id(value):
    return sha256(encode({key: item for key, item in value.items() if key != 'id'})).hexdigest()


def _notify(callback, done, total, phase):
    if callback is not None:
        try:
            callback(done, total, phase)
        except Exception:
            pass  # Observation cannot reverse or duplicate a durable output.


class AudioConversionBatch:
    """One explicit selected batch, with no background work of its own."""
    def __init__(self, runtime, *, library_root, broker=None, scope_guard=lambda: None):
        from .dsp import AudioRuntime
        if not isinstance(runtime, AudioRuntime) or not callable(scope_guard):
            raise TypeError('Batch conversion requires the existing audio runtime and scope guard.')
        self.runtime = runtime
        self.library_root = local_path(library_root, exists=True)
        regular(self.library_root, directory=True)
        self.broker = broker or ResourceBroker.for_app(self.library_root)
        if (not isinstance(self.broker, ResourceBroker)
                or self.broker.root != self.library_root / 'runtime' / 'resources'
                or self.broker.policy_path != self.library_root / 'state' / 'operations' / 'resources.json'):
            raise ValueError('Audio batch admission belongs to another library.')
        self.scope_guard = scope_guard

    def _check(self, cancelled):
        self.scope_guard()
        if cancelled():
            raise AudioCancelled('Audio batch cancelled; completed files were retained.')

    def _directory(self, value):
        target = local_path(value)
        if (target == self.runtime.root or not target.is_relative_to(self.runtime.root)
                or target.is_relative_to(self.library_root)):
            raise ValueError('Choose a separate output folder outside the library data directory.')
        parent = local_path(target.parent, exists=True)
        before = regular(parent, directory=True)
        return target, [before.st_dev, before.st_ino]

    def _tools(self, cancelled):
        return {name: {'path': getattr(self.runtime, name),
                       **_identity(getattr(self.runtime, name), maximum=512 * 1024**2,
                                   cancelled=cancelled)}
                for name in ('ffmpeg', 'ffprobe')}

    @contextmanager
    def _admit(self, identity, cancelled, *, disk_bytes=0, directory=None):
        self._check(cancelled)
        threads = min(self.runtime.budget.threads, self.broker.limits.cpu_slots)
        request = ResourceRequest(cpu_slots=threads,
            memory_bytes=self.runtime.budget.max_alloc_bytes * threads + 64 * 1024**2,
            disk_bytes=disk_bytes, disk_path=directory if disk_bytes else None)
        with self.broker.reserve(request, operation_id='audio-convert:' + identity,
                                 cancelled=cancelled, timeout=min(60, self.runtime.budget.timeout)) as lease:
            original = self.runtime.budget
            self.runtime.budget = replace(original, threads=threads)
            try:
                self._check(lease.cancelled)
                yield lease
            finally:
                self.runtime.budget = original

    def prepare(self, sources, directory, *, format='wav', resume=False, cancelled=lambda: False,
                progress=None, **settings):
        """Inspect selected inputs off the UI thread; create no output or job."""
        self._check(cancelled)
        if (type(sources) not in (list, tuple) or not 1 <= len(sources) <= MAX_FILES
                or format not in FORMATS or type(resume) is not bool):
            raise ValueError('Choose one to 64 local inputs, a supported format and an explicit resume option.')
        chosen = _settings(**settings)
        destination, parent_identity = self._directory(directory)
        if os.path.lexists(destination):
            regular(destination, directory=True)
            if not resume:
                raise FileExistsError('The batch folder already exists; explicitly review resume or choose a new name.')
        rows, total, estimate = [], 0, 0
        with self._admit(uuid4().hex, cancelled) as lease:
            tools = self._tools(lease.cancelled)
            for index, selected in enumerate(sources):
                self._check(lease.cancelled)
                source = self.runtime._input(selected)
                identity = _identity(source, maximum=min(MAX_SOURCE_BYTES, MAX_SOURCE_TOTAL - total),
                                     cancelled=lease.cancelled)
                total += identity['size_bytes']
                _notify(progress, index, len(sources), 'Inspecting ' + source.name)
                error, information, output_bytes = '', None, 0
                try:
                    info = self.runtime.inspect(source, cancelled=lease.cancelled)
                    selected_duration = info.duration - chosen['start'] if chosen['duration'] is None else chosen['duration']
                    if (chosen['start'] >= info.duration or selected_duration <= 0
                            or selected_duration > info.duration - chosen['start'] + 1 / info.sample_rate):
                        raise ValueError('The selected interval is outside this source.')
                    tempo = math.prod(float(item['parameters'].get('factor', 1))
                        for item in chosen['effects'] if item['kind'] == 'tempo' and item['enabled'])
                    duration = selected_duration / tempo
                    information = {key: item for key, item in info.to_dict().items()
                                   if key not in ('path', 'sha256')}
                    output_bytes = math.ceil(max(
                        duration * chosen['sample_rate'] * chosen['channels'] * 4,
                        duration * chosen['bitrate'] * 125) * 1.2) + 1024**2
                    if output_bytes > self.runtime.budget.max_output_bytes:
                        raise ValueError('This converted item exceeds the selected output budget.')
                except (AudioCancelled, InterruptedError):
                    raise
                except (AudioError, ValueError, OSError) as exc:
                    error, information, output_bytes = str(exc)[:2000], None, 0
                if _identity(source, maximum=MAX_SOURCE_BYTES, cancelled=lease.cancelled) != identity:
                    raise ValueError('A selected source changed during the batch review.')
                estimate += output_bytes
                rows.append({'index': index, 'source': str(source), 'identity': identity,
                             'output': _name(index, source, format), 'information': information,
                             'estimated_bytes': output_bytes, 'inspection_error': error})
            if self._tools(lease.cancelled) != tools:
                raise ValueError('The selected audio runtime changed during review.')
        estimate += max((row['estimated_bytes'] for row in rows), default=0)
        if estimate > self.runtime.budget.max_output_bytes:
            raise ValueError('The selected batch exceeds the aggregate output budget; choose fewer files or review a larger budget.')
        plan = {'schema': 'carr-audio-conversion-plan/1', 'directory': str(destination),
                'parent_identity': parent_identity, 'format': format, 'settings': chosen,
                'tools': tools, 'sources': rows, 'estimated_bytes': estimate}
        plan['id'] = _plan_id(plan)
        if resume:
            self._read_manifest(plan)
        self._check(cancelled)
        _notify(progress, len(rows), len(rows), 'Selected audio batch is ready for explicit review')
        return _copy(plan)

    def _validate_plan(self, plan):
        if type(plan) is not dict or set(plan) != {
                'schema', 'id', 'directory', 'parent_identity', 'format', 'settings',
                'tools', 'sources', 'estimated_bytes'}:
            raise ValueError('Invalid audio batch plan.')
        if plan['schema'] != 'carr-audio-conversion-plan/1' or plan['id'] != _plan_id(plan):
            raise ValueError('The reviewed batch identity changed.')
        _options(plan['settings'])
        if type(plan['sources']) is not list or not 1 <= len(plan['sources']) <= MAX_FILES:
            raise ValueError('The reviewed batch exceeds its input bound.')
        if type(plan['directory']) is not str or not 0 < len(plan['directory']) <= 32768:
            raise ValueError('Invalid reviewed batch directory.')
        if type(plan['tools']) is not dict or set(plan['tools']) != {'ffmpeg', 'ffprobe'}:
            raise ValueError('The batch lost its selected audio runtime identities.')
        def identity(value, maximum):
            if (type(value) is not dict or set(value) != {'size_bytes', 'sha256', 'stamp'}
                    or type(value['size_bytes']) is not int or not 0 <= value['size_bytes'] <= maximum
                    or type(value['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
                    or type(value['stamp']) is not list or len(value['stamp']) != 5
                    or any(type(part) is not int or abs(part) > 2**64 for part in value['stamp'])
                    or value['stamp'][2] != value['size_bytes']):
                raise ValueError('Invalid reviewed audio file identity.')
        for tool in plan['tools'].values():
            if (type(tool) is not dict or set(tool) != {'path', 'size_bytes', 'sha256', 'stamp'}
                    or type(tool['path']) is not str or not 0 < len(tool['path']) <= 32768):
                raise ValueError('Invalid selected audio executable identity.')
            identity({key: item for key, item in tool.items() if key != 'path'}, 512 * 1024**2)
        total = 0
        for index, row in enumerate(plan['sources']):
            if (type(row) is not dict or set(row) != {'index', 'source', 'identity', 'output',
                    'information', 'estimated_bytes', 'inspection_error'}
                    or type(row['index']) is not int or row['index'] != index
                    or type(row['source']) is not str or not 0 < len(row['source']) <= 32768
                    or row['output'] != _name(index, row['source'], plan['format'])
                    or type(row['inspection_error']) is not str or len(row['inspection_error']) > 2000):
                raise ValueError('Invalid reviewed audio source row.')
            identity(row['identity'], MAX_SOURCE_BYTES)
            integer(row['estimated_bytes'], 'Reviewed output allowance', 0, self.runtime.budget.max_output_bytes)
            total += row['identity']['size_bytes']
            if row['inspection_error']:
                if row['information'] is not None or row['estimated_bytes'] != 0:
                    raise ValueError('A refused audio source cannot carry a prepared output estimate.')
            elif type(row['information']) is not dict or set(row['information']) != {
                    'duration', 'sample_rate', 'channels', 'codec', 'format', 'frames', 'size'}:
                raise ValueError('A reviewed audio source lost its measured metadata.')
        estimate = sum(row['estimated_bytes'] for row in plan['sources']) + max(
            row['estimated_bytes'] for row in plan['sources'])
        if total > MAX_SOURCE_TOTAL or plan['estimated_bytes'] != estimate:
            raise ValueError('Invalid reviewed aggregate batch allowance.')
        integer(plan['estimated_bytes'], 'Reviewed aggregate output', 0, self.runtime.budget.max_output_bytes)
        destination, parent_identity = self._directory(plan['directory'])
        if parent_identity != plan['parent_identity'] or plan['format'] not in FORMATS:
            raise ValueError('The reviewed output directory or format changed.')
        return destination

    def _read_manifest(self, plan):
        destination = self._validate_plan(plan)
        value, checksum = read_json_snapshot(destination / MANIFEST, max_bytes=MAX_MANIFEST_BYTES)
        if (type(value) is not dict or set(value) != {
                'schema', 'plan', 'status', 'done', 'failed', 'updated', 'items', 'warning'}
                or value['schema'] != 'carr-audio-conversion-batch/1' or value['plan'] != plan
                or value['status'] not in ('ready', 'running', 'completed', 'partial', 'failed',
                                            'cancelled', 'timed_out', 'attention')
                or type(value['items']) is not list or len(value['items']) != len(plan['sources'])
                or type(value['warning']) is not str or len(value['warning']) > 3000):
            raise ValueError('The selected folder has no matching reviewed batch manifest.')
        finite(value['updated'], 'Saved batch update', 0)
        for index, (row, source) in enumerate(zip(value['items'], plan['sources'])):
            if (type(row) is not dict or set(row) != {'index', 'output', 'status', 'candidate', 'error'}
                    or row['index'] != index or row['output'] != source['output']
                    or row['status'] not in ('pending', 'prepared', 'completed', 'failed', 'cancelled')
                    or type(row['error']) is not str or len(row['error']) > 3000):
                raise ValueError('Invalid per-file conversion outcome.')
            if row['candidate'] is not None:
                self._validate_candidate(row['candidate'], destination / row['output'])
            if row['status'] in ('prepared', 'completed') and row['candidate'] is None:
                raise ValueError('A prepared conversion lost its exact output receipt.')
        done = sum(row['status'] == 'completed' for row in value['items'])
        failed = sum(row['status'] == 'failed' for row in value['items'])
        if (type(value['done']) is not int or value['done'] != done
                or type(value['failed']) is not int or value['failed'] != failed
                or value['status'] == 'completed' and done != len(value['items'])):
            raise ValueError('Saved batch counters disagree with their per-file receipts.')
        return value, checksum

    @staticmethod
    def _validate_candidate(value, output):
        if (type(value) is not dict or set(value) != {
                'path', 'duration', 'sample_rate', 'channels', 'codec', 'format', 'frames', 'size', 'sha256', 'staging'}
                or value['path'] != str(output)
                or type(value['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', value['sha256'])
                or type(value['codec']) is not str or not 0 < len(value['codec']) <= 128
                or type(value['format']) is not str or not 0 < len(value['format']) <= 128):
            raise ValueError('Invalid prepared audio output identity.')
        finite(value['duration'], 'Prepared audio duration', 0)
        integer(value['sample_rate'], 'Prepared audio sample rate', 8000, 192000)
        integer(value['channels'], 'Prepared audio channels', 1, 2)
        integer(value['size'], 'Prepared audio bytes', 1, 2**63 - 1)
        if value['frames'] is not None:
            integer(value['frames'], 'Prepared audio frames', 0, 2**63 - 1)
        staging = value['staging']
        if (type(staging) is not dict or set(staging) != {'relative', 'device', 'inode', 'size', 'mtime_ns'}
                or type(staging['relative']) is not str
                or re.fullmatch(r'\.audio-[A-Za-z0-9_-]{6,64}/render\.' + re.escape(output.suffix[1:]),
                                staging['relative']) is None
                or any(type(staging[key]) is not int or abs(staging[key]) > 2**64
                       for key in ('device', 'inode', 'size', 'mtime_ns'))
                or staging['size'] != value['size']):
            raise ValueError('A prepared conversion lost its exact private staging identity.')

    @staticmethod
    def _owned_directory(destination, identity):
        current = regular(local_path(destination, exists=True), directory=True)
        if identity is not None and (current.st_dev, current.st_ino) != identity:
            raise ValueError('The selected batch directory was replaced during the operation.')

    def _save(self, destination, value, *, expected=None, directory_identity=None):
        self._owned_directory(destination, directory_identity)
        if expected is not None:
            _current, checksum = read_json_snapshot(destination / MANIFEST, max_bytes=MAX_MANIFEST_BYTES)
            if checksum != expected:
                raise ValueError('The selected batch manifest changed outside its owned operation.')
        value['done'] = sum(row['status'] == 'completed' for row in value['items'])
        value['failed'] = sum(row['status'] == 'failed' for row in value['items'])
        value['updated'] = time.time()
        raw = encode(value)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('The audio batch receipt exceeds its byte budget.')
        atomic_json(destination / MANIFEST, value, replace=expected is not None)
        return sha256(raw).hexdigest()

    def _verify_source(self, row, cancelled):
        source = self.runtime._input(row['source'])
        if _identity(source, maximum=MAX_SOURCE_BYTES, cancelled=cancelled) != row['identity']:
            raise ValueError('A reviewed original changed; select and review a new batch.')
        return source

    def _retire_published_staging(self, destination, output, candidate, cancelled):
        """Retire only the exact known second link from an interrupted publisher.

        Global regular-file readers stay strict. A crash after link but before
        TemporaryDirectory cleanup can leave exactly these two owned names.
        Unknown links, replacement staging, changed bytes or extra links refuse.
        """
        saved = candidate['staging']
        staged = destination / saved['relative']
        local_path(staged.parent, exists=True)
        names = (output, staged)
        before = [path.lstat() for path in names]
        expected = (saved['device'], saved['inode'], saved['size'], saved['mtime_ns'])
        def accepted(info):
            return (stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
                    and info.st_nlink == 2
                    and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == expected)
        if not all(accepted(info) for info in before):
            raise ValueError('An output hard link has no exact owned staging counterpart.')
        digest, count = sha256(), 0
        with _open_json_read(output) as stream:
            if not accepted(os.fstat(stream.fileno())):
                raise ValueError('The committed audio link changed while opening.')
            while True:
                if cancelled():
                    raise AudioCancelled('Committed audio recovery was cancelled.')
                data = stream.read(min(1024**2, saved['size'] + 1 - count))
                if not data:
                    break
                count += len(data)
                if count > saved['size']:
                    raise ValueError('Committed audio grew beyond its prepared identity.')
                digest.update(data)
            if not accepted(os.fstat(stream.fileno())):
                raise ValueError('Committed audio changed during recovery.')
        if (count != saved['size'] or digest.hexdigest() != candidate['sha256']
                or any(_stamp(path.lstat()) != _stamp(info) for path, info in zip(names, before))):
            raise ValueError('Committed audio or its private staging link changed.')
        staged.unlink()
        sync_directory(staged.parent)
        # The exact alias is gone before normal strict readers are used again.
        after = regular(output)
        if ((after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != expected
                or after.st_nlink != 1):
            raise ValueError('Committed audio ownership changed while retiring staging.')
        try:
            staged.parent.rmdir()
        except OSError:
            pass  # Preserve any unrelated file; it never grants completion.

    def _recover(self, destination, row, *, cancelled=lambda: False, directory_identity=None):
        self._owned_directory(destination, directory_identity)
        output = destination / row['output']
        if not os.path.lexists(output):
            if row['status'] == 'completed':
                raise ValueError('A completed conversion output is missing; no replacement was written.')
            return False
        candidate = row['candidate']
        if candidate is None:
            raise FileExistsError('An unbound existing output was preserved: ' + row['output'])
        self._validate_candidate(candidate, output)
        if output.lstat().st_nlink != 1:
            self._retire_published_staging(destination, output, candidate, cancelled)
        identity = _identity(output, maximum=self.runtime.budget.max_output_bytes, cancelled=cancelled)
        if identity['size_bytes'] != candidate['size'] or identity['sha256'] != candidate['sha256']:
            raise ValueError('A prepared/completed output changed; it was not overwritten.')
        row.update(status='completed', error='')
        return True

    def run(self, reviewed, *, resume=False, cancelled=lambda: False, progress=None):
        """Run serially under one reservation, preserving every committed item."""
        plan = _copy(reviewed)
        destination = self._validate_plan(plan)
        # A serialized plan never grants read authority: use the exact caller-
        # selected runtime/input roots and recompute the selected plan first.
        fresh = self.prepare([row['source'] for row in plan['sources']], destination,
            format=plan['format'], resume=resume, cancelled=cancelled, **_options(plan['settings']))
        if fresh != plan:
            raise ValueError('Sources, settings, runtime or output location changed after review.')
        self._check(cancelled)
        if resume:
            regular(destination, directory=True)
        else:
            destination.mkdir(mode=0o700)
        opened_directory = regular(destination, directory=True)
        directory_identity = (opened_directory.st_dev, opened_directory.st_ino)
        with state_lock(destination, 'conversion'):
            self._owned_directory(destination, directory_identity)
            if resume:
                value, checksum = self._read_manifest(plan)
            else:
                value = {'schema': 'carr-audio-conversion-batch/1', 'plan': plan,
                    'status': 'ready', 'done': 0, 'failed': 0, 'updated': time.time(), 'warning': '',
                    'items': [{'index': row['index'], 'output': row['output'], 'status': 'pending',
                               'candidate': None, 'error': ''} for row in plan['sources']]}
                checksum = self._save(destination, value, directory_identity=directory_identity)
            deadline = time.monotonic() + self.runtime.budget.timeout
            timed_out = lambda: time.monotonic() >= deadline
            stopped = lambda: cancelled() or timed_out()
            try:
                with self._admit(plan['id'], stopped, disk_bytes=max(4096, plan['estimated_bytes']),
                                 directory=destination) as lease:
                    def execution_cancelled():
                        self._owned_directory(destination, directory_identity)
                        self.scope_guard()
                        return lease.cancelled()
                    self._check(execution_cancelled)
                    if self._tools(lease.cancelled) != plan['tools']:
                        raise ValueError('The selected audio runtime changed while waiting for resources.')
                    value.update(status='running', warning='')
                    checksum = self._save(destination, value, expected=checksum, directory_identity=directory_identity)
                    for source, row in zip(plan['sources'], value['items']):
                        self._check(lease.cancelled)
                        self._verify_source(source, lease.cancelled)
                        if self._recover(destination, row, cancelled=execution_cancelled, directory_identity=directory_identity):
                            checksum = self._save(destination, value, expected=checksum, directory_identity=directory_identity)
                            _notify(progress, value['done'], len(value['items']), 'Verified existing ' + row['output'])
                            continue
                        if source['inspection_error']:
                            row.update(status='failed', error=source['inspection_error'])
                            checksum = self._save(destination, value, expected=checksum, directory_identity=directory_identity)
                            _notify(progress, value['done'], len(value['items']), 'Could not convert ' + row['output'])
                            continue
                        target = destination / row['output']
                        def prepared(info, staged):
                            nonlocal checksum
                            self._check(lease.cancelled)
                            self._verify_source(source, lease.cancelled)
                            if self._tools(lease.cancelled) != plan['tools']:
                                raise ValueError('The selected audio runtime changed before publication.')
                            candidate = info.to_dict()
                            staged_info = regular(staged)
                            candidate['staging'] = {'relative': staged.relative_to(destination).as_posix(),
                                'device': staged_info.st_dev, 'inode': staged_info.st_ino,
                                'size': staged_info.st_size, 'mtime_ns': staged_info.st_mtime_ns}
                            self._validate_candidate(candidate, target)
                            row.update(status='prepared', candidate=candidate, error='')
                            checksum = self._save(destination, value, expected=checksum, directory_identity=directory_identity)
                        try:
                            old_budget = self.runtime.budget
                            remaining = max(.05, deadline - time.monotonic())
                            self.runtime.budget = replace(old_budget, timeout=min(old_budget.timeout, remaining))
                            try:
                                self.runtime.convert(source['source'], target, **_options(plan['settings']),
                                    cancelled=execution_cancelled, before_publish=prepared)
                            finally:
                                self.runtime.budget = old_budget
                            if not self._recover(destination, row, directory_identity=directory_identity):
                                raise AudioError('The validated conversion was not durably published.')
                        except (AudioError, InterruptedError, OSError, ValueError, KeyboardInterrupt) as exc:
                            # A post-link flush/cleanup/cancellation exception
                            # cannot erase exact committed output. Reconcile only
                            # this row's previously persisted candidate.
                            if not self._recover(destination, row, directory_identity=directory_identity):
                                row.update(status='cancelled' if stopped() or isinstance(
                                    exc, (AudioCancelled, InterruptedError, KeyboardInterrupt)) else 'failed',
                                    error=str(exc)[:3000])
                            elif not isinstance(exc, (AudioCancelled, InterruptedError, KeyboardInterrupt)):
                                value['warning'] = ('A converted file was committed; a later cleanup/reporting step failed: '
                                                    + str(exc))[:3000]
                        try:
                            checksum = self._save(destination, value, expected=checksum, directory_identity=directory_identity)
                        except (OSError, ValueError) as exc:
                            if row['status'] == 'completed':
                                value.update(status='attention',
                                    warning=('Converted output is committed and matches its prepared receipt; '
                                             'updating the batch status needs recovery: ' + str(exc))[:3000])
                                value['done'] = sum(item['status'] == 'completed' for item in value['items'])
                                return _copy(value)
                            raise
                        _notify(progress, value['done'], len(value['items']), row['status'] + ': ' + row['output'])
                        if row['status'] == 'cancelled':
                            raise AudioCancelled('Audio batch stopped after retaining completed outputs.')
                    value['status'] = ('completed' if value['done'] == len(value['items'])
                                       else 'partial' if value['done'] else 'failed')
            except (AudioCancelled, InterruptedError, KeyboardInterrupt, ResourceCancelled, ResourceTimeout) as exc:
                value['status'] = 'timed_out' if timed_out() or isinstance(exc, ResourceTimeout) else 'cancelled'
            except (OSError, ValueError, RuntimeError) as exc:
                value.update(status='partial' if value['done'] else 'failed', warning=str(exc)[:3000])
            try:
                self._save(destination, value, expected=checksum, directory_identity=directory_identity)
            except (OSError, ValueError) as exc:
                if value['done']:
                    value.update(status='attention',
                        warning=('Completed converted outputs are retained; receipt update needs recovery: '
                                 + str(exc))[:3000])
                else:
                    raise
            _notify(progress, value['done'], len(value['items']), value['status'])
            return _copy(value)

    def verify(self, reviewed, *, cancelled=lambda: False):
        """Verify saved output receipts without running FFmpeg or opening inputs."""
        plan = _copy(reviewed)
        destination = self._validate_plan(plan)
        value, _checksum = self._read_manifest(plan)
        for row in value['items']:
            if row['status'] == 'completed':
                self._recover(destination, row, cancelled=cancelled)
        return _copy(value)
