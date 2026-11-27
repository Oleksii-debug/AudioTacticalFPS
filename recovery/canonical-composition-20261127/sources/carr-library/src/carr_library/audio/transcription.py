"""Durable adapter for the supplied, separately installed Whisper worker.

Inference remains in the owner's reviewed worker. Every invocation gets fresh
private staging, one immutable audio snapshot, fixed settings and no old TXT or
worker cache. Only independently verified outputs become reusable receipts.
The canonical JSON store, subprocess budgets, cancellation and backup tree are
reused; no faster-whisper dependency is imported into the desktop process.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, finite, integer
from .cleanup import retained_state_lock
from .process import run_process
from .dsp import _digest as _file_digest
from .supplied_whisper import (PREPARATION_VERSION, SOURCE_SHA256, HARDENED_SHA256,
                              HARDENED_PROTOCOL, prepare)
from ..durable_state import (atomic_bytes, atomic_json, digest_file, encode,
                            local_path, read_json, regular, state_lock)

FORMATS = ('txt', 'srt', 'vtt', 'json')
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.ogg', '.opus', '.flac',
                    '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'}
MAX_RESULT_BYTES = 32 * 1024**2
MAX_SOURCE_BYTES = 32 * 1024**3
MAX_SEGMENTS = 250000
_RUNTIME_PACKAGES = ('faster_whisper', 'ctranslate2', 'av', 'onnxruntime',
                     'numpy', 'huggingface_hub', 'tokenizers')


def _cancel(cancelled):
    if cancelled():
        raise AudioCancelled('Transcription cancelled; verified completed files remain available.')


def _digest(value):
    return sha256(encode(value)).hexdigest()


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('An exact SHA256 identity is required.')
    return value


def _bounded_bytes(path, maximum=MAX_RESULT_BYTES):
    path = local_path(path, exists=True)
    if regular(path).st_size > maximum:
        raise ValueError('The worker result exceeds its byte allowance.')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError('The worker result grew beyond its byte allowance.')
    return raw


def _copy_snapshot(source, target, cancelled, maximum=MAX_SOURCE_BYTES):
    source = local_path(source, exists=True)
    before = regular(source)
    if not 0 < before.st_size <= maximum:
        raise ValueError('Choose a nonempty local audio file within the input byte allowance.')
    digest, count = sha256(), 0
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        if stamp(os.fstat(incoming.fileno())) != stamp(before):
            raise ValueError('The audio source changed before copying.')
        while block := incoming.read(1024 * 1024):
            _cancel(cancelled)
            count += len(block)
            if count > before.st_size:
                raise ValueError('The audio source grew during copying.')
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        if stamp(os.fstat(incoming.fileno())) != stamp(before):
            raise ValueError('The audio source changed during copying.')
    if stamp(regular(source)) != stamp(before) or count != before.st_size:
        raise ValueError('The audio source changed during copying.')
    return digest.hexdigest(), count


def _inventory(root, files, cancelled, *, runtime=False):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    if not isinstance(files, dict) or not files or len(files) > 100000:
        raise ValueError('A bounded complete installation file inventory is required.')
    names = set()
    count = 0
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                _cancel(cancelled)
                count += 1
                if count > 150000:
                    raise ValueError('The installation exceeds its directory inventory allowance.')
                path = Path(entry.path)
                if runtime and entry.name == '__pycache__':
                    regular(path, directory=True)
                    continue
                if runtime and path.suffix == '.pyc':
                    raise ValueError('Unreviewed standalone Python bytecode is not an accepted runtime file.')
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                else:
                    regular(path)
                    names.add(path.relative_to(root).as_posix())
    if names != set(files):
        raise ValueError('The installed files differ from the reviewed complete inventory.')
    for name, expected in files.items():
        relative = PurePosixPath(name) if isinstance(name, str) else None
        if (relative is None or relative.is_absolute() or str(relative) != name
                or any(part in ('', '.', '..') for part in relative.parts)
                or '\\' in name or ':' in name):
            raise ValueError('Unsafe installation manifest path.')
        _sha(expected)
        _cancel(cancelled)
        if _file_digest(local_path(root / name, root=root, exists=True), cancelled) != expected:
            raise ValueError('An installed runtime or model file failed its SHA256 check.')
    return root


@dataclass(frozen=True)
class TranscriptionSettings:
    language: str = 'auto'
    task: str = 'transcribe'
    beam_size: int = 5
    vad_filter: bool = True
    condition_on_previous_text: bool = True
    multilingual: bool = False
    device: str = 'cpu'
    compute_type: str = 'int8'

    def __post_init__(self):
        if not isinstance(self.language, str) or not re.fullmatch('auto|[a-z]{2,3}', self.language):
            raise ValueError('Choose auto or an explicit supported Whisper language code.')
        if self.task not in ('transcribe', 'translate'):
            raise ValueError('Whisper supports transcription or translation into English.')
        integer(self.beam_size, 'Recognition beam size', 1, 10)
        for name in ('vad_filter', 'condition_on_previous_text', 'multilingual'):
            if type(getattr(self, name)) is not bool:
                raise ValueError('Recognition flags must be boolean values.')
        if self.device not in ('cpu', 'cuda'):
            raise ValueError('Select CPU or CUDA explicitly; no automatic device fallback is used.')
        if self.compute_type not in ('int8', 'int8_float16', 'int8_float32', 'float16', 'float32'):
            raise ValueError('Select an explicit supported recognition compute type.')

    def worker(self, model, formats=FORMATS):
        return {**asdict(self), 'model': model, 'parallel_files': 1,
                'output_mode': 'separate', 'include_headers': False,
                'translation_target': 'en',
                **{'output_' + f: f in formats or f == 'json' for f in FORMATS}}


class SuppliedWhisperRuntime:
    """An explicit reviewed local installation; never an automatic downloader.

    The manifest sits outside the model and runtime directories. Hashing a local
    inventory identifies it, but does not establish its rights or quality. The
    separate review flags are required, and the unmodified supplied source hash
    is pinned in application code rather than trusted from the manifest.
    """
    def __init__(self, manifest):
        self.manifest_path = local_path(manifest, exists=True)
        self._configure(read_json(self.manifest_path))

    @classmethod
    def from_reviewed_data(cls, data, *, source_manifest_sha256, python_version):
        """Explicit compatibility enrollment; all ordinary review checks apply.

        The caller retains the separately pinned legacy manifest. No review
        flags are inferred from a hash, installed files or provenance notices.
        """
        _sha(source_manifest_sha256)
        if not isinstance(python_version, str) or re.fullmatch(r'\d+\.\d+\.\d+', python_version) is None:
            raise ValueError('The reviewed interpreter version is invalid.')
        runtime = cls.__new__(cls)
        runtime.manifest_path = None
        runtime._configure(data)
        runtime._compatibility = {'manifest_sha256': source_manifest_sha256,
                                  'python_version': python_version}
        return runtime

    def _configure(self, data):
        # Own an immutable value snapshot rather than the caller's nested maps.
        self.data = json.loads(encode(data))
        self._compatibility = None
        fields = {'schema', 'engine', 'python', 'python_sha256', 'worker',
                  'runtime_directory', 'runtime_files', 'models_directory',
                  'model', 'model_files', 'rights_reviewed', 'runtime_reviewed', 'rights_notice'}
        if (not isinstance(self.data, dict) or set(self.data) != fields
                or self.data['schema'] != 1 or self.data['engine'] != 'supplied-whisper-v43'
                or self.data['rights_reviewed'] is not True or self.data['runtime_reviewed'] is not True):
            raise ValueError('A reviewed supplied Whisper runtime manifest is required.')
        if (not isinstance(self.data['rights_notice'], str) or not self.data['rights_notice'].strip()
                or len(self.data['rights_notice']) > 8000):
            raise ValueError('Record the local runtime and model rights review.')
        if not isinstance(self.data['model'], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', self.data['model']):
            raise ValueError('Select one simple local model directory name.')
        _sha(self.data['python_sha256'])
        for name in ('python', 'worker', 'runtime_directory', 'models_directory'):
            if not isinstance(self.data[name], str) or not Path(self.data[name]).is_absolute():
                raise ValueError('Installation paths must be explicit absolute local paths.')

    def verify(self, cancelled=lambda: False):
        _cancel(cancelled)
        data = self.data
        python = local_path(data['python'], exists=True)
        if _file_digest(python, cancelled) != data['python_sha256']:
            raise ValueError('The selected Python executable changed.')
        raw = _bounded_bytes(data['worker'], maximum=128 * 1024)
        prepared = prepare(raw)
        runtime_root = _inventory(data['runtime_directory'], data['runtime_files'], cancelled, runtime=True)
        if any(name + '/__init__.py' not in data['runtime_files'] for name in _RUNTIME_PACKAGES):
            raise ValueError('The reviewed runtime inventory is missing an ASR dependency.')
        model_root = _inventory(Path(data['models_directory']) / data['model'], data['model_files'], cancelled)
        if not {'model.bin', 'config.json', 'tokenizer.json'} <= set(data['model_files']):
            raise ValueError('The reviewed local Whisper model is incomplete.')
        identity = {'schema': 1, 'adapter': PREPARATION_VERSION,
                    'source_sha256': sha256(raw).hexdigest(), 'prepared_sha256': sha256(prepared).hexdigest(),
                    'python_sha256': data['python_sha256'], 'runtime_files': data['runtime_files'],
                    'model_files': data['model_files'], 'model': data['model']}
        if getattr(self, '_compatibility', None) is not None:
            identity['compatibility'] = self._compatibility
        package = Path(__file__).parent.parent
        identity['adapter_files'] = {name: _file_digest(package / name, cancelled)
            for name in ('audio/transcription.py', 'audio/transcription_protocol.py',
                         'audio/supplied_whisper.py', 'audio/process.py', 'audio/windows_job.py', 'audio/cleanup.py',
                         'audio/models.py', 'audio/dsp.py', 'durable_state.py')}
        return _digest(identity), prepared, python, runtime_root, model_root


def _validate_request(request):
    if (not isinstance(request, dict)
            or set(request) != {'schema', 'runtime', 'settings', 'formats', 'inputs'}
            or request['schema'] != 1):
        raise ValueError('Invalid transcription request schema.')
    _sha(request['runtime'])
    if not isinstance(request['settings'], dict):
        raise ValueError('Invalid transcription request settings.')
    TranscriptionSettings(**request['settings'])
    formats = request['formats']
    if (not isinstance(formats, list) or not formats or len(formats) != len(set(formats))
            or any(f not in FORMATS for f in formats)):
        raise ValueError('Invalid transcription request formats.')
    inputs = request['inputs']
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 1000:
        raise ValueError('Invalid transcription input inventory.')
    names = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {'sha256', 'bytes', 'file'}:
            raise ValueError('Invalid transcription input record.')
        _sha(item['sha256'])
        integer(item['bytes'], 'Retained source bytes', 1, MAX_SOURCE_BYTES)
        name = item['file']
        if (not isinstance(name, str) or Path(name).suffix not in AUDIO_EXTENSIONS
                or name != item['sha256'] + Path(name).suffix or name in names):
            raise ValueError('Invalid retained source filename.')
        names.add(name)


def validate_saved_job(record):
    """Validate saved declarations without looking up an engine or model.

    Backup recovery uses this pure boundary before staging a saved job. Artifact
    bytes must additionally be checked against the retained input and output
    receipts; a valid declaration alone does not establish completed audio.
    """
    required = {'schema', 'id', 'request', 'status', 'completed', 'error',
                'external_dependencies'}
    optional = {'notices', 'cleanup_pending', 'recovery_review_required', 'source_labels', 'resource_plan'}
    if (not isinstance(record, dict) or not required <= set(record)
            or set(record) - required - optional
            or record['schema'] != 'resonance-transcription-job/1'):
        raise ValueError('Invalid durable transcription job schema.')
    _validate_request(record['request'])
    if record['id'] != _digest(record['request']):
        raise ValueError('Invalid durable transcription job identity.')
    if record['status'] not in ('queued', 'running', 'paused', 'cancelled', 'failed', 'complete'):
        raise ValueError('Invalid durable transcription state.')
    expected = {item['file'] for item in record['request']['inputs']}
    completed = record['completed']
    if (not isinstance(completed, list) or any(not isinstance(v, str) or v not in expected for v in completed)
            or len(set(completed)) != len(completed)
            or (record['status'] == 'complete' and set(completed) != expected)):
        raise ValueError('Invalid transcription completion inventory.')
    if record['error'] is not None and (not isinstance(record['error'], str) or len(record['error']) > 2100):
        raise ValueError('Invalid saved transcription error.')
    if record['external_dependencies'] != {
            'runtime_identity': record['request']['runtime'],
            'runtime_and_model_included': False,
            'resume_requires_reviewed_local_installation': True}:
        raise ValueError('Invalid external transcription dependency declaration.')
    for name in ('notices', 'cleanup_pending'):
        values = record.get(name, [])
        if (not isinstance(values, list) or len(values) > 10000
                or any(not isinstance(v, str) or not v or len(v) > 2000 for v in values)):
            raise ValueError('Invalid saved transcription notice.')
    if ('recovery_review_required' in record
            and record['recovery_review_required'] is not True):
        raise ValueError('Invalid transcription recovery review declaration.')
    if 'source_labels' in record:
        labels = record['source_labels']
        if (not isinstance(labels, dict) or set(labels) != expected
                or any(not isinstance(name, str) or not 1 <= len(name) <= 500 or '\x00' in name
                       or '/' in name or '\\' in name for name in labels.values())):
            raise ValueError('Invalid saved transcription source labels.')
    if 'resource_plan' in record:
        from ..operations.models import ResourceRequest
        plan = record['resource_plan']
        fields = {'cpu_memory_basis', 'disk_basis', 'gpu_basis'}
        if (not isinstance(plan, dict) or set(plan) != {'schema', 'request', *fields}
                or type(plan['schema']) is not int or plan['schema'] != 1
                or any(not isinstance(plan[name], str) or not 1 <= len(plan[name]) <= 2000
                       for name in fields)):
            raise ValueError('Invalid saved transcription resource declaration.')
        # Pure validation only: these are historical estimates, never resumed
        # reservations or authority to open paths from the earlier installation.
        ResourceRequest.from_dict(plan['request'])
    return record

def restored_job(record):
    """Make a detached recovery record inert without probing declared paths."""
    validate_saved_job(record)
    result = json.loads(encode(record))
    if result['status'] in ('queued', 'running'):
        result['status'] = 'paused'
    result['recovery_review_required'] = True
    return result

def validate_saved_snapshot(directory, record, *, cancelled=lambda: False):
    """Check copied job bytes without reading any external runtime declaration."""
    validate_saved_job(record)
    directory = local_path(directory, exists=True)
    regular(directory, directory=True)
    if directory.name != record['id']:
        raise ValueError('The saved transcription directory has another job identity.')
    if read_json(directory / 'job.json') != record:
        raise ValueError('The saved transcription declaration changed during validation.')
    control = read_json(directory / 'control.json')
    if (not isinstance(control, dict) or set(control) != {'command'}
            or control['command'] not in ('run', 'pause', 'cancel')):
        raise ValueError('Invalid saved transcription control.')
    expected = {'job.json', 'control.json'}
    for item in record['request']['inputs']:
        _cancel(cancelled)
        relative = 'inputs/' + item['file']
        source = local_path(directory / relative, root=directory, exists=True)
        if regular(source).st_size != item['bytes'] or _file_digest(source, cancelled) != item['sha256']:
            raise ValueError('A saved transcription input failed its identity check.')
        expected.add(relative)
        receipt = TranscriptionService._receipt(directory, item, record['request'], cancelled=cancelled)
        if receipt is None:
            if item['file'] in record['completed']:
                raise ValueError('A saved completed transcription input has no valid receipt.')
            continue
        prefix = 'outputs/' + item['file'] + '/'
        expected.add(prefix + 'receipt.json')
        expected.update(prefix + 'transcript.' + format for format in record['request']['formats'])
    # Only the producer's durable format belongs to its recovery namespace.
    # Incomplete private .pending-asr directories are excluded by the backup
    # producer before this validation, rather than accepted as finished data.
    actual, pending, count = set(), [directory], 0
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                _cancel(cancelled)
                count += 1
                if count > 100000:
                    raise ValueError('The saved transcription exceeds its inventory allowance.')
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    regular(path, directory=True)
                    pending.append(path)
                else:
                    regular(path)
                    actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ValueError('The saved transcription contains missing or undeclared files.')
    return record


def _time(seconds):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def _text(value):
    if not isinstance(value, str) or len(value) > 100000 or '\x00' in value:
        raise ValueError('The worker returned invalid transcript text.')
    return value.strip()


def _verified_result(meta, source, settings):
    if not isinstance(meta, dict):
        raise ValueError('The supplied worker returned invalid transcript metadata.')
    if (meta.get('source') != str(source) or meta.get('settings') != settings
            or meta.get('selected_language') != settings['language']
            or meta.get('v43_attempt') != 'original' or meta.get('v43_attempt_no') != 1):
        raise ValueError('The worker result differs from the selected source or recognition settings.')
    return _normalized_result(meta, settings)


def _normalized_result(meta, settings):
    """Validate timed text shared by the two explicitly pinned worker protocols."""
    language = meta.get('detected_language')
    if not isinstance(language, str) or not re.fullmatch('[a-z]{2,3}', language):
        raise ValueError('The supplied worker did not return a valid language.')
    if settings['language'] != 'auto' and language != settings['language']:
        raise ValueError('The worker substituted a different recognition language.')
    duration = finite(meta.get('duration'), 'Audio duration', 0, 365 * 86400)
    segments = meta.get('segments')
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ValueError('The transcript exceeds its segment allowance.')
    normalized, previous = [], 0.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('The worker returned an invalid transcript segment.')
        start = finite(segment.get('start'), 'Segment start', 0, duration + .1)
        end = finite(segment.get('end'), 'Segment end', start, duration + .1)
        if start < previous:
            raise ValueError('The transcript timestamps are not in order.')
        previous = start
        normalized.append({'start': start, 'end': end, 'text': _text(segment.get('text'))})
        if 'raw_model_timestamps' in segment:
            raw = segment['raw_model_timestamps']
            if not isinstance(raw, dict) or set(raw) != {'start', 'end'}:
                raise ValueError('Invalid raw model timestamp warning.')
            raw_start = finite(raw['start'], 'Raw segment start', 0, 365 * 86400)
            raw_end = finite(raw['end'], 'Raw segment end', raw_start, 365 * 86400)
            normalized[-1]['raw_model_timestamps'] = {'start': raw_start, 'end': raw_end}
            normalized[-1]['timestamp_warning'] = _text(segment.get('timestamp_warning'))
    text = '\n'.join(line.strip() for line in '\n'.join(s['text'] for s in normalized).splitlines()
                     if line.strip())
    if meta.get('text') != text:
        raise ValueError('The transcript text differs from its timed segments.')
    result = {'language': language, 'duration': duration, 'text': text,
              'segments': normalized, 'word_timestamps': False, 'diarization': False}
    warnings = meta.get('warnings', [])
    if (not isinstance(warnings, list) or len(warnings) > 1000
            or any(not isinstance(value, str) or len(value) > 2000 for value in warnings)):
        raise ValueError('Invalid worker transcript warnings.')
    if warnings:
        result['warnings'] = warnings
    return result


def _outputs(result):
    # Preserve the supplied worker's segment numbering, including empty segments.
    srt, vtt = [], []
    for index, segment in enumerate(result['segments'], 1):
        if not segment['text']:
            continue
        times = _time(segment['start']) + ' --> ' + _time(segment['end'])
        srt.extend((str(index), times, segment['text'], ''))
        vtt.extend((times.replace(',', '.'), segment['text'], ''))
    return {
        'txt': (result['text'] + ('\n' if result['text'] else '')).encode('utf-8'),
        'srt': ('\n'.join(srt).strip() + ('\n' if srt else '')).encode('utf-8'),
        'vtt': ('WEBVTT\n\n' + '\n'.join(vtt).strip() + ('\n' if vtt else '')).encode('utf-8'),
    }


_BOOTSTRAP = r'''
import json, os, pathlib, runpy, sys
worker, job, runtime, *version = sys.argv[1:]
if version and version[0] and '.'.join(map(str, sys.version_info[:3])) != version[0]:
    raise RuntimeError('The selected transcription Python version changed.')
stage = pathlib.Path(job).resolve()
sys.path.insert(0, runtime)
def audit(event, args):
    if event.startswith('socket.') or event in ('subprocess.Popen','os.system','os.posix_spawn'):
        raise PermissionError('The supplied local transcription worker denies network and child commands.')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
            raise PermissionError('Transcription writes only to its private staging directory.')
sys.addaudithook(audit)
sys.argv = [worker, '--job-dir', job]
runpy.run_path(worker, run_name='__main__')
'''


_HARDENED_BOOTSTRAP = r'''
import importlib.util, os, pathlib, socket, sys
worker, job, runtime, *version = sys.argv[1:]
if version and version[0] and '.'.join(map(str, sys.version_info[:3])) != version[0]:
    raise RuntimeError('The selected transcription Python version changed.')
stage = pathlib.Path(job).resolve()
sys.path.insert(0, runtime)
def audit(event, args):
    # multiprocessing's local duplex Pipe uses AF_UNIX on POSIX. Internet
    # sockets and name resolution remain denied in the supervisor and child.
    if event.startswith('socket.'):
        local = (event == 'socket.__new__' and args[1] == getattr(socket, 'AF_UNIX', -1))
        local = local or (event in ('socket.bind', 'socket.connect')
                         and getattr(args[0], 'family', None) == getattr(socket, 'AF_UNIX', -1))
        if not local:
            raise PermissionError('The local transcription worker denies network access.')
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn'):
        raise PermissionError('The local transcription worker denies child commands.')
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not pathlib.Path(os.fsdecode(args[0])).resolve().is_relative_to(stage):
            raise PermissionError('Transcription writes only to its private staging directory.')
sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location('_carr_reviewed_worker', worker)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
# spawn reimports this public bootstrap as __mp_main__. It installs the same
# audit policy and private module identity before unpickling the worker target.
if __name__ == '__main__':
    result = module.Worker(pathlib.Path(job)).run()
    raise SystemExit({'succeeded': 0, 'partial': 1, 'failed': 1, 'cancelled': 3}[result['state']])
'''


class TranscriptionService:
    """Typed ASR operations below the existing application state/backup tree."""
    def __init__(self, state_root, runtime=None, *, budget=None, broker=None):
        if runtime is not None and not isinstance(runtime, SuppliedWhisperRuntime):
            raise TypeError('Use a reviewed supplied-worker runtime.')
        self.root = local_path(Path(state_root) / 'transcription')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime = runtime
        self.budget = budget or AudioBudget()
        self._broker = broker

    @property
    def broker(self):
        return self._admission_broker()

    def _admission_broker(self, cancelled=lambda: False):
        from ..operations import ResourceBroker, ResourceStateError
        app_root = self.root.parent.parent
        if self._broker is None:
            self._broker = ResourceBroker.for_app(app_root, cancelled=cancelled)
        if (self._broker.root != app_root / 'runtime' / 'resources'
                or self._broker.policy_path != app_root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The transcription resource policy belongs to another data folder.')
        return self._broker

    def _resource_plan(self, item, request, settings, cancelled=lambda: False):
        from ..operations import ResourceRequest, ResourceUnavailable
        limits = self._admission_broker(cancelled).limits
        if not limits.memory_bytes:
            raise ResourceUnavailable('Configure a positive declared memory allowance before transcription.')
        # The supplied runtime exposes a CUDA setting, not a validated physical
        # device index. Reserve a generic pool and a shared accelerator token.
        device = 'cuda' if settings.device == 'cuda' else None
        capacity = dict(limits.gpu_devices).get(device, 0) if device else 0
        if device and not capacity:
            raise ResourceUnavailable('The selected transcription CUDA pool needs an explicit shared resource capacity (cuda).')
        # The supplied worker owns its native thread/model configuration. Until
        # its peak is measured, one inference reserves the whole declared CPU
        # and RAM pool, plus the explicitly selected whole GPU capacity.
        disk = min(self.budget.max_output_bytes,
                   item['bytes'] + MAX_RESULT_BYTES * (2 * len(request['formats']) + 1) + 1024**2)
        resource = ResourceRequest(cpu_slots=limits.cpu_slots, memory_bytes=limits.memory_bytes,
            gpu_device=device, gpu_memory_bytes=capacity,
            devices=('accelerator:cuda',) if device is not None else (),
            disk_path=self.root, disk_bytes=disk)
        return resource, {'schema': 1, 'request': resource.to_dict(),
            'cpu_memory_basis': 'whole configured CPU/RAM pool; supplied worker thread count and model peak are not measured',
            'disk_basis': 'retained input staging and bounded transcript copies; existing worker output budget remains in force',
            'gpu_basis': 'whole explicitly configured generic CUDA pool; no physical device index or VRAM measurement is inferred'}

    def _directory(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch('[0-9a-f]{64}', job_id):
            raise ValueError('Invalid transcription job identity.')
        return local_path(self.root / job_id, root=self.root, exists=True)

    def folder_inputs(self, folder, *, existing=(), cancelled=lambda: False, max_files=1000):
        """Select bounded direct folder contents; never recurse into subfolders."""
        integer(max_files, 'Audio file selection limit', 1, 1000)
        if not isinstance(existing, (tuple, list)) or len(existing) > max_files:
            raise ValueError('Select at most 1000 local audio files.')
        folder = local_path(folder, exists=True)
        regular(folder, directory=True)
        selected = dict.fromkeys(str(local_path(path, exists=True)) for path in existing)
        with os.scandir(folder) as entries:
            for count, entry in enumerate(entries, 1):
                _cancel(cancelled)
                if count > 20000:
                    raise ValueError('The selected folder exceeds the bounded directory scan; choose a smaller folder.')
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False) or path.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue
                selected[str(local_path(path, root=folder, exists=True))] = None
                if len(selected) > max_files:
                    raise ValueError('The selected audio exceeds the file-count limit; choose fewer files.')
        total = 0
        for name in selected:
            _cancel(cancelled)
            path = local_path(name, exists=True)
            size = regular(path).st_size
            if path.suffix.lower() not in AUDIO_EXTENSIONS or not 0 < size <= MAX_SOURCE_BYTES:
                raise ValueError('Choose nonempty supported local audio files within the input allowance.')
            total += size
            if total > self.budget.max_output_bytes:
                raise AudioError('The selected audio exceeds the configured job disk allowance.')
        return sorted(selected, key=lambda value: (value.casefold(), value))

    def create(self, sources, *, settings=None, formats=FORMATS, cancelled=lambda: False):
        if self.runtime is None:
            raise ValueError('Select a reviewed local transcription runtime before creating a job.')
        settings = settings or TranscriptionSettings()
        if not isinstance(settings, TranscriptionSettings):
            raise TypeError('Use validated transcription settings.')
        if (not isinstance(formats, (tuple, list)) or not formats
                or any(f not in FORMATS for f in formats) or len(set(formats)) != len(formats)):
            raise ValueError('Select distinct TXT, SRT, VTT or JSON output formats.')
        formats = tuple(f for f in FORMATS if f in formats)
        if not isinstance(sources, (list, tuple)) or not 1 <= len(sources) <= 1000:
            raise ValueError('Select from 1 to 1000 local audio files.')
        runtime_id, _, _, _, _ = self.runtime.verify(cancelled)
        with state_lock(self.root, 'create'):
            temporary = Path(tempfile.mkdtemp(prefix='.pending-asr-', dir=self.root))
            try:
                inputs = temporary / 'inputs'
                inputs.mkdir()
                records, labels, total = [], {}, 0
                for index, source in enumerate(sources):
                    _cancel(cancelled)
                    source = local_path(source, exists=True)
                    suffix = source.suffix.lower()
                    if suffix not in AUDIO_EXTENSIONS:
                        raise ValueError('Select a supported local audio or video file.')
                    staged = inputs / (str(index) + suffix)
                    remaining = min(MAX_SOURCE_BYTES, self.budget.max_output_bytes - total)
                    selected_size = regular(source).st_size
                    if shutil.disk_usage(inputs).free < selected_size + self.budget.reserve_disk_bytes:
                        raise AudioError('Not enough free space to retain the selected audio safely.')
                    checksum, size = _copy_snapshot(source, staged, cancelled, maximum=remaining)
                    total += size
                    if total > self.budget.max_output_bytes:
                        raise AudioError('The retained audio exceeds the configured job disk allowance.')
                    name = checksum + suffix
                    target = inputs / name
                    if target.exists():
                        staged.unlink()
                    else:
                        staged.rename(target)
                        records.append({'sha256': checksum, 'bytes': size, 'file': name})
                        labels[name] = source.name
                identity = {'schema': 1, 'runtime': runtime_id, 'settings': asdict(settings),
                            'formats': list(formats), 'inputs': records}
                job_id = _digest(identity)
                destination = self.root / job_id
                if destination.exists():
                    existing = self.load(job_id)
                    if existing['request'] != identity:
                        raise ValueError('The transcription identity conflicts with an existing job.')
                    return job_id
                record = {'schema': 'resonance-transcription-job/1', 'id': job_id,
                          'request': identity, 'status': 'queued', 'completed': [], 'error': None,
                          'source_labels': labels,
                          'external_dependencies': {'runtime_identity': runtime_id,
                              'runtime_and_model_included': False,
                              'resume_requires_reviewed_local_installation': True}}
                atomic_json(temporary / 'job.json', record)
                atomic_json(temporary / 'control.json', {'command': 'run'})
                _cancel(cancelled)
                temporary.rename(destination)
                from ..durable_state import sync_directory
                sync_directory(self.root)
                return job_id
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def _record(self, job_id):
        value = read_json(self._directory(job_id) / 'job.json')
        if (not isinstance(value, dict) or value.get('schema') != 'resonance-transcription-job/1'
                or value.get('id') != job_id or _digest(value.get('request')) != job_id
                or value.get('status') not in ('queued', 'running', 'paused', 'cancelled', 'failed', 'complete')
                or not isinstance(value.get('completed'), list)):
            raise ValueError('Invalid durable transcription job.')
        _validate_request(value['request'])
        ids = {item['file'] for item in value['request']['inputs']}
        if (any(not isinstance(identity, str) or identity not in ids for identity in value['completed'])
                or len(set(value['completed'])) != len(value['completed'])):
            raise ValueError('Invalid transcription completion inventory.')
        return value

    def load(self, job_id, *, cancelled=lambda: False):
        value = self._record(job_id)
        ids = {item['file'] for item in value['request']['inputs']}
        if value['status'] == 'complete':
            if set(value['completed']) != ids:
                raise ValueError('A completed transcription job has missing completion records.')
            directory = self._directory(job_id)
            for item in value['request']['inputs']:
                if self._receipt(directory, item, value['request'], cancelled=cancelled) is None:
                    raise ValueError('A completed transcription job has a missing artifact receipt.')
        return value

    def recorded_status(self, job_id):
        """Validated journal summary; output receipts are not verified by this view."""
        value = self._record(job_id)
        if value['status'] == 'complete' and set(value['completed']) != {
                item['file'] for item in value['request']['inputs']}:
            raise ValueError('A completed transcription journal has missing completion records.')
        return {'id': value['id'], 'status': value['status'],
                'completed': len(value['completed']), 'total': len(value['request']['inputs'])}

    def jobs(self, *, cancelled=lambda: False, limit=1000):
        """Bounded summaries; damaged jobs stay visible instead of claiming success."""
        integer(limit, 'Saved transcription job limit', 1, 1000)
        rows = []
        for path in self.root.iterdir():
            _cancel(cancelled)
            if not re.fullmatch('[0-9a-f]{64}', path.name):
                continue
            if len(rows) >= limit:
                raise ValueError('More than the selected saved-job limit; use the CLI to inspect a job by identity.')
            try:
                value = self.load(path.name, cancelled=cancelled)
                rows.append({'id': path.name, 'status': value['status'],
                             'completed': len(value['completed']),
                             'total': len(value['request']['inputs']), 'error': value.get('error')})
            except (OSError, ValueError) as exc:
                rows.append({'id': path.name, 'status': 'invalid', 'completed': 0,
                             'total': None, 'error': str(exc)})
        return sorted(rows, key=lambda row: row['id'])

    def details(self, job_id, *, cancelled=lambda: False):
        """Inspect valid request metadata, clearly marking damaged results for review.

        Unlike load(), this view keeps user reading drafts reachable when an
        original generated output was changed or removed. It never reports a
        damaged result as complete and does not modify the durable job.
        """
        _cancel(cancelled)
        record = self._record(job_id)
        directory = self._directory(job_id)
        outputs, errors = [], []
        labels = record.get('source_labels', {})
        if (record['status'] == 'complete' and set(record['completed']) !=
                {item['file'] for item in record['request']['inputs']}):
            errors.append('A completed job has missing completion records.')
        for index, item in enumerate(record['request']['inputs'], 1):
            _cancel(cancelled)
            if item['file'] not in record['completed']:
                continue
            try:
                receipt = self._receipt(directory, item, record['request'], cancelled=cancelled)
                if receipt is None:
                    raise ValueError('The completed output is missing its receipt.')
            except (OSError, ValueError) as exc:
                receipt = None
                errors.append(f'Audio {index}: {exc}')
            label = labels.get(item['file']) if isinstance(labels, dict) else None
            outputs.append({'file': item['file'],
                            'label': label if isinstance(label, str) else f'Audio {index}',
                            'directory': str(directory / 'outputs' / item['file']),
                            'formats': list(receipt['outputs']) if receipt else record['request']['formats'],
                            'checksums': receipt['outputs'] if receipt else {},
                            'verified': receipt is not None})
        if errors:
            record = {**record, 'status': 'invalid',
                      'error': (' '.join(errors[:5]) +
                                (f' {len(errors) - 5} more damaged outputs.' if len(errors) > 5 else ''))}
        return {'record': record, 'outputs': outputs}

    def interrupted_details(self, job_id):
        """Bounded request metadata after stop; do not restart a receipt scan."""
        record = self._record(job_id)
        if record['status'] == 'complete':
            record = {**record, 'status': 'unverified',
                      'error': 'Result validation stopped; recorded completion has not been rechecked.'}
        return {'record': record, 'outputs': [], 'validation_cancelled': True}

    def transcript_text(self, job_id, source_file, *, cancelled=lambda: False):
        """Read verified TXT as a derivative, bounded to the existing reader budget."""
        detail = self.details(job_id, cancelled=cancelled)
        output = next((row for row in detail['outputs'] if row['file'] == source_file), None)
        if output is None or not output['verified'] or 'txt' not in output['formats']:
            raise ValueError('Select a completed TXT transcript to edit its reading copy.')
        path = local_path(Path(output['directory']) / 'transcript.txt', exists=True)
        _cancel(cancelled)
        raw = _bounded_bytes(path, maximum=8_000_000)
        expected = output['checksums']['txt']
        if len(raw) != expected['bytes'] or sha256(raw).hexdigest() != expected['sha256']:
            raise ValueError('The transcript changed while opening; it is preserved for review.')
        text = raw.decode('utf-8')
        if len(text) > 2_000_000:
            raise ValueError('This transcript exceeds the reading-copy character budget.')
        return text, output['label']

    def control(self, job_id, command):
        if command not in ('run', 'pause', 'cancel'):
            raise ValueError('Select run, pause or cancel.')
        if command == 'run':
            value = self.load(job_id)
            if value.pop('recovery_review_required', False):
                atomic_json(self._directory(job_id) / 'job.json', value, replace=True)
        atomic_json(self._directory(job_id) / 'control.json', {'command': command}, replace=True)

    @staticmethod
    def _receipt(directory, item, request, *, cancelled=lambda: False):
        receipt_path = directory / 'outputs' / item['file'] / 'receipt.json'
        if not receipt_path.exists():
            return None
        receipt = read_json(receipt_path)
        expected_identity = {'source_sha256': item['sha256'], 'source_bytes': item['bytes'],
                             'source_format': Path(item['file']).suffix,
                             'runtime': request['runtime'], 'settings': request['settings'],
                             'formats': request['formats']}
        if (not isinstance(receipt, dict) or receipt.get('schema') != 1
                or receipt.get('status') != 'complete' or receipt.get('identity') != expected_identity
                or not isinstance(receipt.get('outputs'), dict)
                or set(receipt['outputs']) != set(request['formats'])):
            raise ValueError('A completed transcription receipt is invalid.')
        for format, entry in receipt['outputs'].items():
            if not isinstance(entry, dict) or set(entry) != {'sha256', 'bytes'}:
                raise ValueError('A completed transcription artifact record is invalid.')
            _sha(entry['sha256'])
            integer(entry['bytes'], 'Transcript bytes', 0, MAX_RESULT_BYTES)
            path = receipt_path.parent / ('transcript.' + format)
            if regular(local_path(path, exists=True)).st_size != entry['bytes'] or _file_digest(path, cancelled) != entry['sha256']:
                raise ValueError('An existing transcript was changed; it is preserved for review.')
        return receipt

    def run(self, job_id, *, cancelled=lambda: False, on_status=lambda text: None):
        from ..operations import ResourceCancelled
        if self.runtime is None:
            raise ValueError('Select a reviewed local transcription runtime before resuming a job.')
        directory = self._directory(job_id)
        with retained_state_lock(directory, 'job'):
            record = self._record(job_id)
            if record.get('recovery_review_required'):
                raise ValueError('Review the restored job and its local runtime, then explicitly resume it.')
            request = record['request']
            command = 'run'
            def stopped():
                nonlocal command
                # Flush GUI pause/cancel requests on this worker thread before
                # reading the durable command; no control fsync on the wx thread.
                external_cancelled = cancelled()
                control = read_json(directory / 'control.json')
                command = control.get('command') if isinstance(control, dict) else None
                if command not in ('run', 'pause', 'cancel'):
                    raise ValueError('Invalid transcription control record.')
                return external_cancelled or command != 'run'
            def save(status, error=None):
                record.update(status=status, error=error)
                atomic_json(directory / 'job.json', record, replace=True)
            def notify(message):
                try:
                    on_status(message)
                except Exception as exc:
                    record.setdefault('notices', []).append('Progress observer failed: ' + type(exc).__name__)
            try:
                _cancel(stopped)
                record = self.load(job_id, cancelled=stopped)
                runtime_id, prepared, python, runtime_root, model_root = self.runtime.verify(stopped)
                if runtime_id != request['runtime']:
                    raise ValueError('The selected runtime or model changed; create a new reviewed job.')
                settings = TranscriptionSettings(**request['settings'])
                with retained_state_lock(self.root, 'runtime-' + runtime_id):
                    save('running')
                    completed = []
                    for index, item in enumerate(request['inputs'], 1):
                        _cancel(stopped)
                        source = local_path(directory / 'inputs' / item['file'], root=directory / 'inputs', exists=True)
                        if regular(source).st_size != item['bytes'] or _file_digest(source, stopped) != item['sha256']:
                            raise ValueError('The retained audio snapshot failed its identity check.')
                        receipt = self._receipt(directory, item, request, cancelled=stopped)
                        if receipt is None:
                            resource, plan = self._resource_plan(item, request, settings, stopped)
                            record['resource_plan'] = plan
                            save('queued')
                            notify('Waiting for shared local transcription resources.')
                            try:
                                with ExitStack() as admission:
                                    lease = admission.enter_context(self.broker.reserve(resource,
                                        operation_id='asr:' + job_id, cancelled=stopped, timeout=60))
                                    save('running')
                                    notify(f'Transcribing file {index} of {len(request["inputs"])} locally.')
                                    try:
                                        notice = self._run_one(directory, item, request, source, settings,
                                            prepared, python, runtime_root, model_root, lease.cancelled)
                                    except AudioCleanupPending as pending:
                                        # The existing lease remains owned until the worker's
                                        # native tree has actually stopped, including descendants.
                                        retained = admission.pop_all()
                                        pending.cleanup_owner.add_quiescent_callback(retained.close)
                                        raise
                            except ResourceCancelled as exc:
                                raise AudioCancelled('Transcription stopped while waiting for shared resources.') from exc
                            if lease.cleanup_pending:
                                record.setdefault('notices', []).append('Resource reservation metadata cleanup is pending.')
                            if notice is not None:
                                record.setdefault('cleanup_pending', []).append(notice)
                            receipt = self._receipt(directory, item, request, cancelled=stopped)
                            if receipt is None:
                                raise AudioError('The completed worker output has no durable receipt.')
                        completed.append(item['file'])
                        record['completed'] = completed[:]
                        save('running')
                    # All requested artifacts have durable verified receipts.
                    save('complete')
                    notify('Transcription complete; every requested output has been verified.')
                    return record
            except (AudioCancelled, ResourceCancelled) as exc:
                if record['status'] != 'complete':
                    save('paused' if command == 'pause' else 'cancelled')
                if isinstance(exc, ResourceCancelled):
                    raise AudioCancelled('Transcription stopped while waiting for shared resources.') from exc
                raise
            except KeyboardInterrupt:
                if record['status'] != 'complete':
                    save('cancelled')
                raise
            except Exception as exc:
                if record['status'] != 'complete':
                    try:
                        save('failed', type(exc).__name__ + ': ' + str(exc)[:2000])
                    except BaseException as status_error:
                        if not isinstance(exc, AudioCleanupPending):
                            raise
                        exc.add_note('Cleanup status could not be saved: ' + type(status_error).__name__)
                        exc.status_publication_error = status_error
                raise

    def _run_one(self, directory, item, request, source, settings, prepared,
                 python, runtime_root, model_root, cancelled):
        current_id, prepared, python, runtime_root, model_root = self.runtime.verify(cancelled)
        if current_id != request['runtime']:
            raise ValueError('The runtime or model changed before worker admission.')
        stage = Path(tempfile.mkdtemp(prefix='.pending-asr-', dir=directory))
        committed = False
        active_error = None
        cleanup_pending = None
        try:
            worker = stage / 'worker.py'
            atomic_bytes(worker, prepared)
            incoming = stage / 'input'
            incoming.mkdir()
            snapshot = incoming / item['file']
            checksum, size = _copy_snapshot(source, snapshot, cancelled)
            if checksum != item['sha256'] or size != item['bytes']:
                raise ValueError('The retained input changed before worker admission.')
            worker_settings = settings.worker(self.runtime.data['model'], request['formats'])
            config = {'job_name': 'Resonance transcription', 'folders': [str(incoming)],
                      'results_root': str(stage / 'results'), 'models_dir': str(model_root.parent),
                      'settings': worker_settings}
            hardened = sha256(prepared).hexdigest() == HARDENED_SHA256
            if hardened:
                config.update(protocol_version=1, job_id=directory.name)
            atomic_json(stage / 'job_config.json', config)
            env = {name: os.environ[name] for name in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'TEMP', 'TMP') if name in os.environ}
            env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1',
                       HF_HUB_DISABLE_TELEMETRY='1', PYTHONDONTWRITEBYTECODE='1',
                       TEMP=str(stage), TMP=str(stage), TMPDIR=str(stage),
                       HF_HOME=str(stage / 'hf-cache'), XDG_CACHE_HOME=str(stage / 'cache'))
            entrypoint = ['-c', _BOOTSTRAP]
            if hardened:
                bootstrap = stage / 'host_bootstrap.py'
                atomic_bytes(bootstrap, _HARDENED_BOOTSTRAP.encode('utf-8'))
                entrypoint = [str(bootstrap)]
            try:
                run_process([str(python), '-I', '-S', '-B', '-X', 'utf8',
                             '-X', 'pycache_prefix=' + str(stage / 'bytecode'), *entrypoint,
                             str(worker), str(stage), str(runtime_root),
                             (getattr(self.runtime, '_compatibility', None) or {}).get('python_version', '')],
                            budget=self.budget, cancelled=cancelled, watch=(stage,), env=env, cwd=stage,
                            owned_tree=hardened)
            except (AudioCancelled, AudioCleanupPending):
                raise
            except AudioError as exc:
                # This worker records inference exceptions in its terminal
                # receipt and stdout. Preserve a bounded useful explanation
                # before private staging is removed; an exit code is not a
                # successful or reusable recognition result.
                detail = None
                if hardened:
                    try:
                        failed = read_json(stage / 'job_result.json')
                        if (isinstance(failed, dict) and failed.get('protocol') == HARDENED_PROTOCOL
                                and failed.get('job_id') == directory.name
                                and failed.get('state') in ('failed', 'partial')):
                            messages = list(failed.get('errors', []))
                            for row in failed.get('files', []):
                                if isinstance(row, dict):
                                    messages.append(row.get('error'))
                            detail = '; '.join(value for value in messages if isinstance(value, str) and value)[:2000]
                    except (ValueError, OSError, TypeError):
                        pass
                if detail:
                    raise AudioError('The supplied local worker could not transcribe this input: ' + detail) from exc
                raise
            if self.runtime.verify(cancelled)[0] != request['runtime']:
                raise ValueError('The runtime or model changed during transcription.')
            status = read_json(stage / 'job_status.json')
            if (not isinstance(status, dict) or status.get('state') != 'завершено'
                    or any(type(status.get(name)) is not int for name in ('done','total','failed','active_count'))
                    or status.get('done') != 1 or status.get('total') != 1
                    or status.get('failed') != 0 or status.get('active_count') != 0):
                raise AudioError('The supplied worker did not finish the requested file successfully.')
            if hardened:
                from .transcription_protocol import verify_hardened_result
                meta, worker_outputs = verify_hardened_result(stage, snapshot, item,
                    worker_settings, self.runtime.data, status, job_id=directory.name,
                    worker_sha256=HARDENED_SHA256, cancelled=cancelled)
                result = _normalized_result(meta, worker_settings)
                result['worker_protocol'] = HARDENED_PROTOCOL
            else:
                base = stage / 'results' / 'input' / item['sha256']
                meta = read_json(base.with_suffix('.json'))
                result = _verified_result(meta, snapshot, worker_settings)
                worker_outputs = {format: base.with_suffix('.' + format) for format in ('txt', 'srt', 'vtt')}
            payloads = _outputs(result)
            for format in ('txt', 'srt', 'vtt'):
                if format not in request['formats']:
                    continue
                if _bounded_bytes(worker_outputs[format]) != payloads[format]:
                    raise AudioError('A required worker transcript format is missing or inconsistent.')
            _cancel(cancelled)
            identity = {'source_sha256': item['sha256'], 'source_bytes': item['bytes'],
                        'source_format': Path(item['file']).suffix,
                        'runtime': request['runtime'], 'settings': request['settings'],
                        'formats': request['formats']}
            payloads['json'] = encode({'schema': 'resonance-transcript/1', **identity, **result,
                                      'translation_target': 'en' if settings.task == 'translate' else None})
            output = directory / 'outputs' / item['file']
            output.parent.mkdir(parents=True, exist_ok=True)
            local_path(output.parent, root=directory, exists=True)
            if os.path.lexists(output):
                raise ValueError('An existing transcript directory has no verified receipt; it was preserved.')
            publication = stage / 'publication'
            publication.mkdir()
            artifacts = {}
            for format in request['formats']:
                _cancel(cancelled)
                target = publication / ('transcript.' + format)
                raw = payloads[format]
                atomic_bytes(target, raw)
                if _bounded_bytes(target) != raw:
                    raise AudioError('A staged transcript did not verify before publication.')
                artifacts[format] = {'sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}
            _cancel(cancelled)
            atomic_json(publication / 'receipt.json', {'schema': 1, 'status': 'complete',
                        'identity': identity, 'outputs': artifacts})
            _cancel(cancelled)
            if os.path.lexists(output):
                raise ValueError('The transcript destination appeared before publication; it was preserved.')
            # The job lock serializes cooperating writers. All closed/fsynced
            # artifacts and their receipt become visible in one same-filesystem
            # rename; cancellation/crash before it leaves no partial final file.
            publication.rename(output)
            from ..durable_state import sync_directory
            sync_directory(output.parent)
            committed = True
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            if isinstance(active_error, AudioCleanupPending):
                # The still-owned process may hold or write these files. Neither
                # a GUI future nor a failed native wait authorizes their removal.
                active_error.cleanup_owner.add_quiescent_callback(
                    lambda: shutil.rmtree(stage, ignore_errors=True))
            else:
                try:
                    shutil.rmtree(stage)
                except OSError:
                    if committed:
                        cleanup_pending = stage.name
                    elif active_error is not None:
                        active_error.add_note('Private temporary audio cleanup remains necessary: ' + stage.name)
                    else:
                        raise
                    # Preserve durable successful completion. The retained .pending
                    # staging is deliberately excluded by the existing backup policy.
        return cleanup_pending
