"""Explicit serial batches over the existing reviewed audio conversion service.

Naming and complete per-file inventories reuse the reviewed conversion-batch
design. The existing converter remains the only renderer and publisher. An
intent is never a commit receipt, including when an existing file has the same
bytes. No scheduler, private job store, automatic repair or pruning is created.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unicodedata
from uuid import uuid4

from ..durable_state import atomic_json, encode, local_path, read_json, regular, state_lock
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
from ..research.export_file import capture_export_destination
from ..runtime_settings import audio_runtime
from .conversion import (FORMATS, ConversionResult, ConversionSettings, _directory,
                         _stamp, convert_audio, review_conversion)
from .effect_presets import read_effect
from .models import AudioBudget, AudioCancelled, finite, integer

MAX_FILES = 64
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_SOURCE_TOTAL = 32 * 1024**3
MAX_MANIFEST_BYTES = 4 * 1024**2
MANIFEST = 'conversion-manifest.json'
SCHEMA = 'carr-audio-conversion-batch/1'
PLAN_SCHEMA = 'carr-audio-conversion-plan/1'
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_STATES = frozenset(('pending', 'intent', 'completed', 'uncertain', 'failed', 'cancelled'))


def _copy(value):
    return json.loads(encode(value))


def _notify(callback, done, total, phase):
    if callback is not None:
        try:
            callback(done, total, phase)
        except Exception:
            pass  # A progress observer cannot reverse a committed output.


def _name(index, source, extension):
    label = unicodedata.normalize('NFC', Path(source).stem)
    label = ''.join('_' if char in '<>:"/\\|?*' or ord(char) < 32
                    or 127 <= ord(char) < 160 else char for char in label)
    label = label.strip(' .')[:100].rstrip(' .') or 'audio'
    return f'{index + 1:03d}-{label}.{extension}'


def _opened_identity(info):
    """Compare descriptor/path metadata without Windows suffix-derived execute bits.

    CPython adds synthetic execute bits to a named .exe/.com/.bat/.cmd stat.
    A descriptor has no filename. All real file type, identity, link count,
    size and write/create timestamps remain part of this comparison.
    """
    stamp = list(_stamp(info))
    if os.name == 'nt':
        stamp[2] &= ~0o111
    return tuple(stamp)


def _file_identity(path, maximum, check):
    """Read at most the captured size plus one; bind descriptor and live path."""
    check()
    path = local_path(path, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('A selected file exceeds the reviewed byte allowance.')
    checksum, count = sha256(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if _opened_identity(opened) != _opened_identity(before):
            raise ValueError('A selected file changed before verification.')
        while True:
            check()
            block = stream.read(min(1024**2, before.st_size + 1 - count))
            if not block:
                break
            count += len(block)
            if count > before.st_size:
                raise ValueError('A selected file grew during verification.')
            checksum.update(block)
        if count != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(opened):
            raise ValueError('A selected file changed during verification.')
    check()
    if _stamp(regular(path)) != _stamp(before):
        raise ValueError('A selected file changed after verification.')
    return {'size': count, 'sha256': checksum.hexdigest(), 'stamp': list(_stamp(before))}


def _settings(value):
    if not isinstance(value, ConversionSettings):
        raise ValueError('Choose typed conversion settings.')
    return {'start': value.start, 'duration': value.duration,
            'sample_rate': value.sample_rate, 'channels': value.channels,
            'bit_depth': value.bit_depth, 'bitrate': value.bitrate,
            'effects': [effect.to_dict() for effect in value.effects]}


def _options(value):
    if type(value) is not dict or set(value) != {
            'start', 'duration', 'sample_rate', 'channels', 'bit_depth', 'bitrate', 'effects'}:
        raise ValueError('Unsupported saved batch settings.')
    if type(value['effects']) is not list or len(value['effects']) > 32:
        raise ValueError('Unsupported saved effect chain.')
    result = ConversionSettings(**dict(value, effects=tuple(read_effect(row) for row in value['effects'])))
    if encode(_settings(result)) != encode(value):
        raise ValueError('The saved batch settings changed their typed identity.')
    return result


def _digest(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError('A batch digest is invalid.')
    return value


def _receipt(value, plan, index):
    """Pure receipt validation; paths in saved records never open a source."""
    keys = {'output', 'source_sha256', 'effects_sha256', 'audio', 'measurement',
            'durability_confirmed', 'cleanup_warning'}
    if type(value) is not dict or set(value) != keys:
        raise ValueError('Unsupported conversion receipt.')
    if (value['output'] != plan['sources'][index]['output']
            or value['source_sha256'] != plan['sources'][index]['identity']['sha256']
            or value['effects_sha256'] != sha256(encode(plan['settings']['effects'])).hexdigest()):
        raise ValueError('The conversion receipt does not match this reviewed item.')
    _digest(value['source_sha256']); _digest(value['effects_sha256'])
    audio = value['audio']
    if type(audio) is not dict or set(audio) != {
            'duration', 'sample_rate', 'channels', 'codec', 'format', 'frames', 'size', 'sha256'}:
        raise ValueError('Unsupported measured audio receipt.')
    finite(audio['duration'], 'Measured duration', .000001)
    integer(audio['sample_rate'], 'Measured sample rate', 8000, 192000)
    integer(audio['channels'], 'Measured channels', 1, 2)
    if (audio['sample_rate'] != plan['settings']['sample_rate']
            or audio['channels'] != plan['settings']['channels']):
        raise ValueError('The measured output does not match the reviewed sample format.')
    integer(audio['size'], 'Measured file bytes', 1, plan['budget']['max_output_bytes'])
    _digest(audio['sha256'])
    if audio['frames'] is not None:
        integer(audio['frames'], 'Measured frames', 0, 2**63 - 1)
    for name in ('codec', 'format'):
        if type(audio[name]) is not str or not 0 < len(audio[name]) <= 256 or '\x00' in audio[name]:
            raise ValueError('Invalid measured codec information.')
    measure = value['measurement']
    names = {'integrated_lufs', 'true_peak_dbfs', 'loudness_range_lu', 'threshold_lufs', 'silent'}
    if type(measure) is not dict or set(measure) != names or type(measure['silent']) is not bool:
        raise ValueError('Unsupported measured loudness receipt.')
    for name in names - {'silent'}:
        if measure[name] is not None:
            finite(measure[name], 'Measured loudness')
    if type(value['durability_confirmed']) is not bool:
        raise ValueError('Invalid receipt durability.')
    if (value['cleanup_warning'] is not None
            and (type(value['cleanup_warning']) is not str or len(value['cleanup_warning']) > 1024)):
        raise ValueError('Invalid receipt cleanup notice.')
    return value


@dataclass(frozen=True)
class BatchReview:
    """A fresh explicit selection, never an instruction loaded from a manifest."""
    plan: dict
    resume: bool
    manifest_stamp: tuple | None

    @property
    def fingerprint(self):
        return self.plan['id']

    def to_dict(self):
        return {'plan': _copy(self.plan), 'resume': self.resume}


class AudioConversionBatch:
    def __init__(self, library_root, *, scope_guard=lambda: None):
        if not callable(scope_guard):
            raise TypeError('Batch conversion requires a current-library guard.')
        self.library_root = local_path(library_root, exists=True)
        self.library_identity = _directory(self.library_root)
        self.scope_guard = scope_guard

    def _check(self, cancelled):
        self.scope_guard()
        if _directory(self.library_root) != self.library_identity:
            raise ValueError('The selected library changed. Review the batch again.')
        if cancelled():
            raise AudioCancelled('Batch cancelled; committed audio files were retained.')
        return False

    def _folder(self, directory, expected=None):
        directory = local_path(directory, exists=True)
        identity = _directory(directory)
        # Reuse the current destination route/private-root guard with a basename
        # that is never written. Reading a stored source path is not involved.
        capture_export_destination(directory / '.audio-batch-route-check',
                                   private_root=self.library_root)
        if expected is not None and identity != tuple(expected):
            raise ValueError('The selected output directory changed.')
        return directory, identity

    @contextmanager
    def _admit_verification(self, check):
        broker = ResourceBroker.for_app(self.library_root, cancelled=check)
        request = ResourceRequest(cpu_slots=1, memory_bytes=16 * 1024**2)
        try:
            with broker.reserve(request, operation_id='audio-batch-review:' + uuid4().hex,
                                cancelled=check, timeout=60):
                check()
                yield
        except ResourceCancelled as error:
            raise AudioCancelled('Batch verification cancelled while waiting for resources.') from error

    def _tools(self, runtime, check):
        return {name: {'path': str(local_path(getattr(runtime, name), exists=True)),
                       **_file_identity(getattr(runtime, name), 512 * 1024**2, check)}
                for name in ('ffmpeg', 'ffprobe')}

    def _runtime_identity(self, budget, check):
        with TemporaryDirectory(prefix='carr-batch-tools-') as temporary:
            runtime = audio_runtime(temporary, state_root=self.library_root / 'state', budget=budget)
            return self._tools(runtime, check)

    def prepare(self, sources, directory, *, format='wav', settings=None, budget=None,
                resume=False, cancelled=lambda: False, progress=None):
        """Hash an explicit selection on the existing worker; write no outputs."""
        check = lambda: self._check(cancelled)
        check()
        if (type(sources) not in (list, tuple) or not 1 <= len(sources) <= MAX_FILES
                or type(format) is not str or '.' + format not in FORMATS
                or type(resume) is not bool):
            raise ValueError('Choose 1 to 64 explicit files, one format and whether to resume.')
        settings = ConversionSettings() if settings is None else settings
        budget = AudioBudget() if budget is None else budget
        if not isinstance(budget, AudioBudget):
            raise ValueError('Choose an explicit audio budget.')
        chosen = _settings(settings)
        folder, folder_identity = self._folder(directory)
        rows, total, seen = [], 0, set()
        with self._admit_verification(check):
            tools = self._runtime_identity(budget, check)
            for index, value in enumerate(sources):
                check()
                path = local_path(value, exists=True)
                if not 0 < len(str(path)) <= 32768:
                    raise ValueError('A selected path exceeds the supported length.')
                current = _file_identity(path, min(MAX_SOURCE_BYTES, budget.max_output_bytes,
                                                   MAX_SOURCE_TOTAL - total), check)
                inode = tuple(current['stamp'][:2])
                if inode in seen:
                    raise ValueError('Select each source file once.')
                seen.add(inode); total += current['size']
                rows.append({'source': str(path), 'identity': current,
                             'output': _name(index, path, format)})
                _notify(progress, index + 1, len(sources), 'Verifying selected source bytes')
        plan = {'schema': PLAN_SCHEMA, 'library_binding': sha256(encode({
                    'root': str(self.library_root), 'identity': list(self.library_identity)})).hexdigest(),
                'directory': str(folder), 'directory_identity': list(folder_identity),
                'format': format, 'settings': chosen, 'budget': asdict(budget),
                'tools': tools, 'sources': rows}
        plan['id'] = sha256(encode(plan)).hexdigest()
        check()
        self._folder(folder, folder_identity)
        stamp = None
        with state_lock(folder, 'audio-conversion-batch'):
            if resume:
                _saved, stamp = self._read(plan)
            elif os.path.lexists(folder / MANIFEST):
                raise FileExistsError('A batch manifest already exists. Explicitly choose Resume or another folder.')
            else:
                for row in rows:
                    if os.path.lexists(folder / row['output']):
                        raise FileExistsError('A planned output already exists. Choose another folder; nothing is overwritten.')
        check()
        return BatchReview(_copy(plan), resume, stamp)

    def _validate_review(self, review):
        if not isinstance(review, BatchReview) or type(review.resume) is not bool:
            raise ValueError('Prepare and explicitly review this exact batch first.')
        plan = review.plan
        if type(plan) is not dict or set(plan) != {
                'schema', 'id', 'library_binding', 'directory', 'directory_identity',
                'format', 'settings', 'budget', 'tools', 'sources'}:
            raise ValueError('Invalid reviewed batch.')
        if (plan['schema'] != PLAN_SCHEMA or _digest(plan['id']) !=
                sha256(encode({key: value for key, value in plan.items() if key != 'id'})).hexdigest()):
            raise ValueError('The reviewed batch fingerprint changed.')
        expected = sha256(encode({'root': str(self.library_root),
                                  'identity': list(self.library_identity)})).hexdigest()
        if plan['library_binding'] != expected:
            raise ValueError('A restored or different library needs a new batch review and output folder.')
        options = _options(plan['settings'])
        if type(plan['budget']) is not dict or set(plan['budget']) != set(asdict(AudioBudget())):
            raise ValueError('Invalid batch resource allowance.')
        budget = AudioBudget(**plan['budget'])
        if encode(asdict(budget)) != encode(plan['budget']):
            raise ValueError('The batch resource allowance changed.')
        if (type(plan['directory']) is not str or not 0 < len(plan['directory']) <= 32768
                or type(plan['directory_identity']) is not list or len(plan['directory_identity']) != 2
                or any(type(part) is not int for part in plan['directory_identity'])
                or type(plan['format']) is not str or '.' + plan['format'] not in FORMATS
                or type(plan['sources']) is not list or not 1 <= len(plan['sources']) <= MAX_FILES):
            raise ValueError('Invalid explicit batch selection.')
        if type(plan['tools']) is not dict or set(plan['tools']) != {'ffmpeg', 'ffprobe'}:
            raise ValueError('The reviewed runtime identity is missing.')
        def identity(value, maximum):
            if (type(value) is not dict or set(value) != {'size', 'sha256', 'stamp'}
                    or type(value['size']) is not int or not 0 < value['size'] <= maximum
                    or type(value['stamp']) is not list or len(value['stamp']) != 7
                    or any(type(part) is not int for part in value['stamp'])
                    or value['stamp'][4] != value['size']):
                raise ValueError('Invalid selected file identity.')
            _digest(value['sha256'])
        for value in plan['tools'].values():
            if (type(value) is not dict or set(value) != {'path', 'size', 'sha256', 'stamp'}
                    or type(value['path']) is not str or not 0 < len(value['path']) <= 32768):
                raise ValueError('Invalid selected runtime path.')
            identity({key: item for key, item in value.items() if key != 'path'}, 512 * 1024**2)
        total = 0
        for index, row in enumerate(plan['sources']):
            if (type(row) is not dict or set(row) != {'source', 'identity', 'output'}
                    or type(row['source']) is not str or not 0 < len(row['source']) <= 32768
                    or row['output'] != _name(index, row['source'], plan['format'])):
                raise ValueError('Invalid selected source or output name.')
            identity(row['identity'], min(MAX_SOURCE_BYTES, budget.max_output_bytes))
            total += row['identity']['size']
        if total > MAX_SOURCE_TOTAL:
            raise ValueError('The selected source bytes exceed the batch allowance.')
        return options, budget

    def _read(self, plan):
        folder, _identity = self._folder(plan['directory'], plan['directory_identity'])
        path = folder / MANIFEST
        before = _stamp(regular(path))
        value = read_json(path, maximum=MAX_MANIFEST_BYTES)
        if _stamp(regular(path)) != before:
            raise ValueError('The batch manifest changed while reading.')
        if (type(value) is not dict or set(value) != {'schema', 'plan', 'items'}
                or value['schema'] != SCHEMA or encode(value['plan']) != encode(plan)
                or type(value['items']) is not list or len(value['items']) != len(plan['sources'])):
            raise ValueError('The saved batch does not match the explicit current review.')
        total = 0
        for index, row in enumerate(value['items']):
            if (type(row) is not dict or set(row) != {'index', 'status', 'candidate', 'receipt', 'error'}
                    or type(row['index']) is not int or row['index'] != index
                    or type(row['status']) is not str or row['status'] not in _STATES
                    or type(row['error']) is not str or len(row['error']) > 2048):
                raise ValueError('Invalid saved batch item.')
            if row['candidate'] is not None:
                _receipt(row['candidate'], plan, index)
            if row['receipt'] is not None:
                _receipt(row['receipt'], plan, index)
            if ((row['status'] == 'completed') != (row['receipt'] is not None)
                    or row['status'] in ('intent', 'uncertain') and row['candidate'] is None
                    or row['status'] == 'pending' and (row['candidate'] is not None or row['error'])):
                raise ValueError('An intent cannot stand in for a successful conversion receipt.')
            if row['receipt'] is not None:
                if row['candidate'] is None or any(encode(row['candidate'][name]) != encode(row['receipt'][name])
                        for name in ('output', 'source_sha256', 'effects_sha256', 'audio', 'measurement')):
                    raise ValueError('A completion receipt lost its pre-publication candidate binding.')
                total += row['receipt']['audio']['size']
        if total > plan['budget']['max_output_bytes']:
            raise ValueError('The saved batch exceeds its reviewed output allowance.')
        return value, (before, sha256(encode(value)).hexdigest())

    def _write(self, plan, value, expected):
        """Write only this exact selected manifest; no post-commit user callback."""
        folder, _identity = self._folder(plan['directory'], plan['directory_identity'])
        path = folder / MANIFEST
        raw = encode(value)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('The batch receipt exceeds its bounded manifest allowance.')
        if expected is None:
            if os.path.lexists(path):
                raise FileExistsError('Another batch already owns this output manifest.')
        else:
            _current, stamp = self._read(plan)
            if stamp != expected:
                raise ValueError('The selected batch manifest changed. Review it again.')
        try:
            atomic_json(path, value, replace=expected is not None)
        except FileExistsError:
            raise
        except OSError:
            # This reconciles only metadata for an already known conversion
            # result. It never infers an audio commit from matching output bytes.
            actual, stamp = self._read(plan)
            if encode(actual) != raw:
                raise
            return stamp, False
        actual, stamp = self._read(plan)
        if encode(actual) != raw:
            raise ValueError('The batch manifest changed after publication.')
        return stamp, True

    def _completed_receipt(self, result, plan, index):
        if not isinstance(result, ConversionResult):
            raise ValueError('The existing converter did not return a completion result.')
        audio = result.audio.to_dict()
        if Path(audio.pop('path')) != Path(plan['directory']) / plan['sources'][index]['output']:
            raise ValueError('The converter returned another destination.')
        value = {'output': plan['sources'][index]['output'],
                 'source_sha256': result.source_sha256, 'effects_sha256': result.effects_sha256,
                 'audio': audio, 'measurement': result.measurement.to_dict(),
                 'durability_confirmed': result.durability_confirmed,
                 'cleanup_warning': result.cleanup_warning}
        return _copy(_receipt(value, plan, index))

    def run(self, review, *, cancelled=lambda: False, progress=None):
        """Run serially; only returned, durably recorded successes are reusable."""
        options, budget = self._validate_review(review)
        plan = _copy(review.plan)
        check = lambda: self._check(cancelled)
        check()
        folder, _identity = self._folder(plan['directory'], plan['directory_identity'])
        with state_lock(folder, 'audio-conversion-batch'):
            check()
            if review.resume:
                document, expected = self._read(plan)
                if expected != review.manifest_stamp:
                    raise ValueError('The batch changed since review. Review Resume again.')
            else:
                document = {'schema': SCHEMA, 'plan': plan, 'items': [
                    {'index': index, 'status': 'pending', 'candidate': None, 'receipt': None, 'error': ''}
                    for index in range(len(plan['sources']))]}
                expected, durable = self._write(plan, document, None)
                if not durable:
                    return self._summary(plan, document, 'attention', True,
                        'The manifest is visible but its durability is unconfirmed. Review Resume before encoding.')
            done = 0
            for index, source in enumerate(plan['sources']):
                row = document['items'][index]
                target = folder / source['output']
                try:
                    check()
                    self._folder(folder, plan['directory_identity'])
                    if row['status'] == 'completed':
                        receipt = row['receipt']
                        with self._admit_verification(check):
                            actual = _file_identity(target, receipt['audio']['size'], check)
                        if (actual['size'], actual['sha256']) != (
                                receipt['audio']['size'], receipt['audio']['sha256']):
                            raise ValueError('A previously completed output changed; it will not be overwritten.')
                        done += 1
                        _notify(progress, done, len(plan['sources']), 'Verified saved completion receipt')
                        continue
                    if os.path.lexists(target):
                        # Even a matching candidate is not historical commit
                        # proof. Do not hash/adopt/delete an unreceipted target.
                        return self._summary(plan, document, 'attention', True,
                            'An output exists without a durable successful conversion receipt. '
                            'Review it explicitly; this batch will not adopt, overwrite or duplicate it.')
                    with self._admit_verification(check):
                        current = _file_identity(source['source'], source['identity']['size'], check)
                    if current != source['identity']:
                        raise ValueError('A selected source changed after review.')
                    used = sum(item['receipt']['audio']['size'] for item in document['items']
                               if item['receipt'] is not None)
                    remaining = budget.max_output_bytes - used
                    if remaining < 4096:
                        raise ValueError('The remaining reviewed batch output allowance is exhausted.')
                    single = review_conversion(self.library_root, source['source'], target,
                        settings=options, budget=AudioBudget(**dict(asdict(budget), max_output_bytes=remaining)),
                        guard=lambda: self._check(cancelled))
                    row.update(status='pending', candidate=None, receipt=None, error='')
                    def runtime_guard(runtime):
                        check()
                        if self._tools(runtime, check) != plan['tools']:
                            raise ValueError('The configured audio runtime changed after batch review.')
                    def checkpoint(candidate, staged, runtime):
                        nonlocal expected
                        runtime_guard(runtime)
                        receipt = self._completed_receipt(candidate, plan, index)
                        actual = _file_identity(staged, remaining, check)
                        if (actual['size'], actual['sha256']) != (
                                receipt['audio']['size'], receipt['audio']['sha256']):
                            raise ValueError('The measured conversion candidate changed.')
                        row.update(status='intent', candidate=receipt, error='')
                        expected, durable = self._write(plan, document, expected)
                        if not durable:
                            raise OSError('Candidate receipt durability is unconfirmed; audio publication did not start.')
                    result = convert_audio(single, cancelled=cancelled, guard=self.scope_guard,
                                           before_publish=checkpoint, before_runtime=runtime_guard)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as error:
                    # No converted success returned: a visible target is never
                    # reconciled to completion, even when candidate bytes match.
                    visible = os.path.lexists(target)
                    status = 'uncertain' if visible and row['candidate'] is not None else (
                        'cancelled' if isinstance(error, (AudioCancelled, InterruptedError)) else 'failed')
                    if row['status'] != 'completed':
                        row.update(status=status, error=str(error)[:2048])
                    try:
                        expected, durable = self._write(plan, document, expected)
                        persisted = True
                    except Exception:
                        durable, persisted = False, False
                    return self._summary(plan, document,
                        'attention' if visible or not durable else ('cancelled' if status == 'cancelled' else 'partial'),
                        persisted, 'Existing outputs are retained. Review the incomplete item before resuming.')
                # convert_audio has returned an actual committed result. Persist
                # that result without another source/cancel/observer callback.
                try:
                    row.update(status='completed', receipt=self._completed_receipt(result, plan, index), error='')
                except Exception:
                    answer = self._summary(plan, document, 'attention', False,
                        'Audio was saved, but its successful receipt needs explicit review. No retry will adopt it.')
                    answer['unrecorded_commits'] = [source['output']]
                    return answer
                try:
                    expected, durable = self._write(plan, document, expected)
                except Exception:
                    answer = self._summary(plan, document, 'attention', False,
                        'Audio was saved, but its successful receipt could not be persisted. '
                        'Review the existing output; automatic resume will not adopt it.')
                    answer['unrecorded_commits'] = [source['output']]
                    return answer
                done += 1
                _notify(progress, done, len(plan['sources']), 'Saved converted audio')
                if not durable or not result.durability_confirmed or result.cleanup_owner is not None:
                    return self._summary(plan, document, 'attention', True,
                        'Audio is saved; durability or owned temporary cleanup still needs review.')
            notices = any(not item['receipt']['durability_confirmed'] or item['receipt']['cleanup_warning']
                          for item in document['items'])
            return self._summary(plan, document, 'attention' if notices else 'completed', True,
                'Saved outputs have a retained durability or cleanup notice; review the receipts.' if notices else None)

    def _summary(self, plan, document, status, receipt_persisted, warning):
        return {'schema': SCHEMA, 'id': plan['id'], 'manifest': str(Path(plan['directory']) / MANIFEST),
                'status': status, 'done': sum(row['status'] == 'completed' for row in document['items']),
                'total': len(document['items']), 'receipt_persisted': receipt_persisted,
                'warning': warning, 'items': _copy(document['items'])}
