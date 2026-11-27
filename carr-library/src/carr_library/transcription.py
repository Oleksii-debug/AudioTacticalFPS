"""Explicit local transcription on the reviewed external worker protocol.

This module neither distributes the private worker nor downloads engines/models.
Its durable records use the existing application-state primitives. Only an exact
reviewed worker digest can run; test fixtures explicitly replace that allowlist.
A worker's terminal claim becomes public state only after source and output bytes
have been checked. Recognition quality is not inferred from a valid protocol.
"""
from dataclasses import asdict, dataclass
from contextlib import ExitStack
from hashlib import sha256
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

from .durable_state import (atomic_bytes, atomic_json, local_path,
                            read_json, regular, state_lock, sync_directory)
from .speech._io import stop_process
from .state_backup import relative_name

PROTOCOL = 'carr-asr-v43-hardened-1'
# Reviewed external v43 worker. Its source and models are not distributed here.
REVIEWED_WORKER_SHA256 = frozenset({
    '5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c',
})
MAX_INPUT_FILES = 128
MAX_INPUT_BYTES = 2 * 1024**3
MAX_JOB_INPUT_BYTES = 8 * 1024**3
MAX_ARTIFACT_BYTES = 32 * 1024**2
MAX_OUTPUT_BYTES = 256 * 1024**2
MAX_WORKER_BYTES = 4 * 1024**2
MAX_DIAGNOSTIC_BYTES = 2 * 1024**2
MAX_JOBS = 1000
MAX_REVISIONS = 1000
AUDIO_EXTENSIONS = frozenset(('.wav', '.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.opus',
    '.aac', '.wma', '.webm', '.mkv', '.3gp', '.amr', '.m4b'))
FORMATS = ('txt', 'srt', 'vtt', 'json')
_TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled'}
_SHA = re.compile(r'[0-9a-f]{64}')
_ID = re.compile(r'[0-9a-f]{32}')


def _hex(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def _text(value, name, maximum=500):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Choose a short readable ' + name + '.')
    return value.strip()


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Transcription was cancelled.')


def _bounded_bytes(path, maximum):
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size > maximum:
        raise ValueError('The local file exceeds its byte budget.')
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The local file changed while opening it.')
        raw = stream.read(maximum + 1)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The local file changed while reading it.')
    if len(raw) != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('The local file changed while reading it.')
    return raw


def _relative(value):
    if type(value) is not str or len(value) > 1024:
        raise ValueError('Invalid transcription artifact path.')
    return relative_name(value).as_posix()


@dataclass(frozen=True)
class EngineSpec:
    python_path: str
    worker_path: str
    worker_sha256: str
    models_dir: str

    def validate(self):
        if not _hex(self.worker_sha256) or self.worker_sha256 not in REVIEWED_WORKER_SHA256:
            raise ValueError('This worker has not been admitted by the application release review.')
        if any(not isinstance(value, (str, Path)) for value in (self.python_path, self.worker_path, self.models_dir)):
            raise ValueError('Choose explicit local paths for Python, the reviewed worker and installed models.')
        python = local_path(self.python_path, exists=True)
        worker = local_path(self.worker_path, exists=True)
        models = local_path(self.models_dir, exists=True)
        regular(python)
        regular(worker)
        regular(models, directory=True)
        if sha256(_bounded_bytes(worker, MAX_WORKER_BYTES)).hexdigest() != self.worker_sha256:
            raise ValueError('The selected worker bytes do not match the reviewed SHA-256.')
        return EngineSpec(str(python), str(worker), self.worker_sha256, str(models))


@dataclass(frozen=True)
class TranscriptionSettings:
    model: str = 'tiny.en'
    device: str = 'cpu'
    compute_type: str = 'int8'
    language: str = 'auto'
    task: str = 'transcribe'
    translation_target: str = 'en'
    beam_size: int = 1
    parallel_files: int = 1
    vad_filter: bool = True
    condition_on_previous_text: bool = True
    multilingual: bool = False
    output_mode: str = 'separate'
    output_txt: bool = True
    output_srt: bool = True
    output_vtt: bool = True
    output_json: bool = True
    include_headers: bool = False

    def __post_init__(self):
        if (type(self.model) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', self.model)
                or type(self.language) is not str or not re.fullmatch(r'auto|[a-z]{2,3}', self.language)):
            raise ValueError('Choose a local model name and an explicit language code or auto.')
        allowed = {'device': {'cpu', 'cuda', 'auto'},
            'compute_type': {'default', 'auto', 'int8', 'int8_float16', 'int8_float32',
                             'int8_bfloat16', 'int16', 'float16', 'float32', 'bfloat16'},
            'task': {'transcribe', 'translate'},
            'output_mode': {'separate', 'one_per_folder', 'one_whole_job'}}
        for name, values in allowed.items():
            if type(getattr(self, name)) is not str or getattr(self, name) not in values:
                raise ValueError('Unsupported transcription ' + name + '.')
        if self.translation_target != 'en':
            raise ValueError('Whisper translation supports an English target only.')
        if self.model.endswith('.en') and self.language not in {'auto', 'en'}:
            raise ValueError('An English-only .en model supports English or auto; choose a local multilingual model for another language.')
        if not _integer(self.beam_size, 1, 20) or type(self.parallel_files) is not int or self.parallel_files != 1:
            raise ValueError('Choose beam size 1–20; the reviewed engine processes one file at a time.')
        for name in ('vad_filter', 'condition_on_previous_text', 'multilingual', 'include_headers',
                     'output_txt', 'output_srt', 'output_vtt', 'output_json'):
            if type(getattr(self, name)) is not bool:
                raise ValueError('Transcription switches must be boolean.')
        if not any(getattr(self, 'output_' + fmt) for fmt in FORMATS):
            raise ValueError('Choose at least one transcript output format.')

    @property
    def formats(self):
        return {fmt for fmt in FORMATS if getattr(self, 'output_' + fmt)}


class TranscriptionService:
    """Private jobs under ``library.root / 'state' / 'transcription'``."""
    def __init__(self, directory):
        self.directory = local_path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_path(self.directory, exists=True)
        self.jobs = self.directory / 'jobs'
        self.jobs.mkdir(exist_ok=True, mode=0o700)
        local_path(self.jobs, root=self.directory, exists=True)

    def _job_dir(self, identity):
        if type(identity) is not str or not _ID.fullmatch(identity):
            raise ValueError('Invalid transcription job identifier.')
        return local_path(self.jobs / identity, root=self.jobs, exists=True)

    def configure_engine(self, spec):
        if not isinstance(spec, EngineSpec):
            raise ValueError('Choose an explicit reviewed engine profile.')
        spec = spec.validate()
        with state_lock(self.directory, 'engine'):
            atomic_json(self.directory / 'engine.json', {'schema': 1,
                'selected_for': str(self.directory), 'engine': asdict(spec)}, replace=True)
        return spec

    def engine(self):
        path = self.directory / 'engine.json'
        if not path.exists():
            raise ValueError('Configure the reviewed local Python, worker and model folder first.')
        value = read_json(path)
        if (type(value) is not dict or value.get('schema') != 1
                or value.get('selected_for') != str(self.directory) or type(value.get('engine')) is not dict):
            raise ValueError('Select the reviewed engine again for this library location after restore or move.')
        try:
            return EngineSpec(**value['engine']).validate()
        except TypeError as exc:
            raise ValueError('The saved engine profile is invalid.') from exc

    def capability(self):
        try:
            spec = self.engine()
            return {'state': 'ready', 'worker_sha256': spec.worker_sha256,
                    'message': 'Worker digest verified. Python/runtime and local model qualification are separate; no models are downloaded.'}
        except (ValueError, OSError) as exc:
            return {'state': 'waiting-input', 'message': str(exc)}

    def submit(self, files, settings=None, *, title='Transcription', cancelled=lambda: False,
               progress=lambda *args: None):
        settings = TranscriptionSettings() if settings is None else settings
        if not isinstance(settings, TranscriptionSettings):
            raise ValueError('Explicit validated transcription settings are required.')
        title = _text(title, 'job name')
        if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= MAX_INPUT_FILES:
            raise ValueError('Choose 1–' + str(MAX_INPUT_FILES) + ' explicit local audio files.')
        _cancel(cancelled)
        paths, total, seen = [], 0, set()
        for value in files:
            source = local_path(value, exists=True)
            before = regular(source)
            if source.suffix.casefold() not in AUDIO_EXTENSIONS or not 0 < before.st_size <= MAX_INPUT_BYTES:
                raise ValueError('Choose a supported nonempty audio file within the per-file byte budget.')
            if source in seen:
                raise ValueError('Choose each source file only once.')
            seen.add(source)
            total += before.st_size
            if total > MAX_JOB_INPUT_BYTES:
                raise ValueError('Selected transcription inputs exceed the job byte budget.')
            paths.append((source, before))
        identity = uuid4().hex
        with state_lock(self.directory, 'jobs'):
            if sum(1 for p in self.jobs.iterdir() if _ID.fullmatch(p.name)) >= MAX_JOBS:
                raise ValueError('The transcription job count exceeds its budget.')
            pending = self.jobs / ('.pending-' + identity)
            pending.mkdir(mode=0o700)
            try:
                (pending / 'inputs').mkdir(mode=0o700)
                records, digests = [], set()
                for index, (source, before) in enumerate(paths):
                    _cancel(cancelled)
                    target = pending / 'inputs' / ('copy-' + str(index) + source.suffix.lower())
                    digest, count = sha256(), 0
                    with source.open('rb') as incoming, target.open('xb') as outgoing:
                        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
                            raise ValueError('A selected input changed before its snapshot.')
                        while block := incoming.read(1024 * 1024):
                            _cancel(cancelled)
                            count += len(block)
                            if count > before.st_size or count > MAX_INPUT_BYTES:
                                raise ValueError('A selected input grew while being snapshotted.')
                            digest.update(block)
                            outgoing.write(block)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                        if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
                            raise ValueError('A selected input changed while being snapshotted.')
                    if count != before.st_size or _stamp(regular(source)) != _stamp(before):
                        raise ValueError('A selected input changed while being snapshotted.')
                    content = digest.hexdigest()
                    if content in digests:
                        raise ValueError('Identical source content was selected more than once.')
                    digests.add(content)
                    name = 'inputs/' + content + source.suffix.lower()
                    target.rename(pending / name)
                    (pending / name).chmod(stat.S_IREAD if os.name == 'nt' else 0o400)
                    records.append({'path': name, 'name': source.name[:255], 'sha256': content, 'bytes': count})
                    progress(index + 1, len(paths), 'Copying selected audio into private job inputs')
                _cancel(cancelled)
                job = {'schema': 'carr-transcription/1', 'id': identity, 'title': title,
                    'created_ns': time.time_ns(), 'state': 'queued', 'settings': asdict(settings),
                    'inputs': records, 'receipt': None, 'revisions': [], 'error': None}
                atomic_json(pending / 'job.json', job)
                pending.rename(self.jobs / identity)
                sync_directory(self.jobs)
                return identity
            finally:
                if pending.exists():
                    for child in pending.rglob('*'):
                        if child.is_file() and not child.is_symlink():
                            child.chmod(0o600)
                    shutil.rmtree(pending)

    def _load(self, identity):
        root = self._job_dir(identity)
        job = read_json(root / 'job.json')
        if (type(job) is not dict or job.get('schema') != 'carr-transcription/1' or job.get('id') != identity
                or not {'title', 'settings', 'receipt', 'revisions', 'error'} <= job.keys()
                or type(job.get('state')) is not str or job['state'] not in _TERMINAL | {'queued', 'running'}
                or not _integer(job.get('created_ns'), 1, 2**63 - 1)
                or type(job.get('inputs')) is not list or not 1 <= len(job['inputs']) <= MAX_INPUT_FILES
                or type(job.get('revisions')) is not list or len(job['revisions']) > MAX_REVISIONS):
            raise ValueError('The transcription job record is invalid.')
        _text(job.get('title'), 'job name')
        try:
            settings = TranscriptionSettings(**job['settings'])
        except (KeyError, TypeError) as exc:
            raise ValueError('The saved transcription settings are invalid.') from exc
        seen, total = set(), 0
        for row in job['inputs']:
            if (type(row) is not dict or set(row) != {'path', 'name', 'sha256', 'bytes'}
                    or not _hex(row['sha256']) or not _integer(row['bytes'], 1, MAX_INPUT_BYTES)
                    or type(row['name']) is not str or not row['name'] or len(row['name']) > 255):
                raise ValueError('The managed input manifest is invalid.')
            name = _relative(row['path'])
            if (Path(name).parent.as_posix() != 'inputs' or Path(name).stem != row['sha256']
                    or Path(name).suffix not in AUDIO_EXTENSIONS or row['sha256'] in seen):
                raise ValueError('The managed input identity is invalid or duplicated.')
            seen.add(row['sha256'])
            total += row['bytes']
        if total > MAX_JOB_INPUT_BYTES:
            raise ValueError('The managed inputs exceed the job byte budget.')
        revision_ids = set()
        for row in job['revisions']:
            if (type(row) is not dict or set(row) != {'id', 'path', 'original', 'created_ns', 'bytes', 'sha256', 'format'}
                    or type(row['id']) is not str or not _ID.fullmatch(row['id']) or row['id'] in revision_ids
                    or row['path'] != 'revisions/' + row['id'] + '.txt' or row['format'] != 'txt'
                    or not _hex(row['sha256']) or not _integer(row['bytes'], 0, MAX_ARTIFACT_BYTES)
                    or not _integer(row['created_ns'], 1, 2**63 - 1) or type(row['original']) is not dict):
                raise ValueError('The transcript revision manifest is invalid.')
            revision_ids.add(row['id'])
        return root, job, settings

    def _verify_inputs(self, root, job, cancelled=lambda: False):
        for row in job['inputs']:
            _cancel(cancelled)
            path = local_path(root / row['path'], root=root, exists=True)
            before = regular(path)
            if before.st_size != row['bytes']:
                raise ValueError('A managed input differs from its accepted source bytes.')
            digest, count = sha256(), 0
            with path.open('rb') as stream:
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise ValueError('A managed input changed while opening it.')
                while block := stream.read(1024 * 1024):
                    _cancel(cancelled)
                    count += len(block)
                    if count > row['bytes']:
                        raise ValueError('A managed input grew during verification.')
                    digest.update(block)
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise ValueError('A managed input changed during verification.')
            if (count != row['bytes'] or digest.hexdigest() != row['sha256']
                    or _stamp(regular(path)) != _stamp(before)):
                raise ValueError('A managed input differs from its accepted source bytes.')

    def _artifact(self, root, value, *, prefix='', cancelled=lambda: False):
        _cancel(cancelled)
        if (type(value) is not dict or set(value) != {'path', 'format', 'bytes', 'sha256'}
                or value['format'] not in FORMATS or not _hex(value['sha256'])
                or not _integer(value['bytes'], 0, MAX_ARTIFACT_BYTES)):
            raise ValueError('A transcript artifact receipt is invalid.')
        name = _relative(value['path'])
        if Path(name).suffix != '.' + value['format']:
            raise ValueError('A transcript artifact has an inconsistent format.')
        path = local_path(root / prefix / name, root=root, exists=True)
        if (regular(path).st_size != value['bytes']
                or sha256(_bounded_bytes(path, MAX_ARTIFACT_BYTES)).hexdigest() != value['sha256']):
            raise ValueError('Transcript artifact bytes do not match their receipt.')
        _cancel(cancelled)
        return {**value, 'path': (Path(prefix) / name).as_posix()}

    def _validate_result(self, root, job, settings, value, *, returncode, run_id, cancelled=lambda: False):
        _cancel(cancelled)
        if (type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('protocol') != PROTOCOL or value.get('job_id') != job['id']
                or type(run_id) is not str or not _ID.fullmatch(run_id) or value.get('run_id') != run_id
                or type(value.get('state')) is not str or value['state'] not in _TERMINAL
                or type(value.get('effective_parallel_files')) is not int
                or value['effective_parallel_files'] != 1 or type(value.get('files')) is not list
                or type(value.get('artifacts')) is not list or len(value['files']) > len(job['inputs'])
                or len(value['artifacts']) > MAX_INPUT_FILES * len(FORMATS)):
            raise ValueError('The worker did not return the reviewed terminal protocol.')
        total = len(job['inputs'])
        if (value.get('total') != total or type(value.get('total')) is not int
                or not _integer(value.get('done'), 0, total) or not _integer(value.get('failed'), 0, total)
                or value.get('results_dir') != str(root / 'results')):
            raise ValueError('The worker terminal counts or result directory are inconsistent.')
        expected = {str(root / row['path']): row for row in job['inputs']}
        seen, artifacts, records = set(), {}, []
        for row in value['files']:
            _cancel(cancelled)
            if (type(row) is not dict or type(row.get('source')) is not dict
                    or type(row['source'].get('path')) is not str or row['source']['path'] not in expected
                    or row['source']['path'] in seen or type(row.get('state')) is not str or row['state'] not in
                    {'succeeded', 'failed', 'cancelled', 'paused', 'recognized'}
                    or type(row.get('artifacts')) is not list or len(row['artifacts']) > len(FORMATS)):
                raise ValueError('The worker returned an unknown or duplicated source record.')
            seen.add(row['source']['path'])
            source = expected[row['source']['path']]
            supplied = row['source']
            if (set(supplied) - {'path', 'sha256', 'bytes'} or
                    ('sha256' in supplied and supplied['sha256'] != source['sha256']) or
                    ('bytes' in supplied and (type(supplied['bytes']) is not int or supplied['bytes'] != source['bytes']))):
                raise ValueError('A worker source identity differs from the managed input.')
            if row['state'] == 'succeeded' and (set(supplied) != {'path', 'sha256', 'bytes'} or not _hex(row.get('cache_key'))):
                raise ValueError('A completed source lacks its verified identity or cache key.')
            if row.get('cache_key') is not None and not _hex(row['cache_key']):
                raise ValueError('Invalid worker recognition identity.')
            admitted, formats = [], set()
            for item in row['artifacts']:
                checked = self._artifact(root, item, prefix='results', cancelled=cancelled)
                key = checked['path'].casefold()
                if checked['format'] in formats or (key in artifacts and artifacts[key] != checked):
                    raise ValueError('Conflicting or duplicated transcript output receipts.')
                formats.add(checked['format'])
                artifacts[key] = checked
                admitted.append(checked)
            if row['state'] == 'succeeded' and formats != settings.formats:
                raise ValueError('The worker did not complete every selected export format.')
            if not formats <= settings.formats:
                raise ValueError('The worker produced an unselected export format.')
            error = row.get('error')
            if error is not None and (type(error) is not str or len(error) > 8000):
                raise ValueError('Invalid worker failure description.')
            warnings = row.get('warnings', [])
            if (type(warnings) is not list or len(warnings) > 100
                    or any(type(w) is not str or len(w) > 8000 for w in warnings)):
                raise ValueError('Invalid worker warning description.')
            records.append({'source': dict(source), 'state': row['state'], 'cache_key': row.get('cache_key'),
                'artifacts': admitted, 'error': error, 'warnings': warnings})
        listed, names = {}, set()
        for item in value['artifacts']:
            checked = self._artifact(root, item, prefix='results', cancelled=cancelled)
            key = checked['path'].casefold()
            if key in names:
                raise ValueError('The terminal output list contains duplicate artifacts.')
            names.add(key)
            listed[key] = checked
        if listed != artifacts or sum(item['bytes'] for item in artifacts.values()) > MAX_OUTPUT_BYTES:
            raise ValueError('The terminal output list is incomplete or exceeds its byte budget.')
        done = sum(row['state'] == 'succeeded' for row in records)
        failed = sum(row['state'] == 'failed' for row in records)
        state = value['state']
        if (done != value['done'] or failed != value['failed']
                or state == 'succeeded' and (done != total or failed or returncode != 0)
                or state == 'partial' and not 0 < done < total or state == 'failed' and done):
            raise ValueError('Worker success does not match the verified completed files.')
        errors = value.get('errors', [])
        if type(errors) is not list or len(errors) > MAX_INPUT_FILES or any(type(e) is not str or len(e) > 8000 for e in errors):
            raise ValueError('Invalid worker terminal diagnostics.')
        self._verify_inputs(root, job, cancelled)
        return {'schema_version': 1, 'protocol': PROTOCOL, 'job_id': job['id'], 'run_id': run_id, 'state': state,
            'done': done, 'failed': failed, 'total': total, 'effective_parallel_files': 1,
            'files': records, 'artifacts': list(artifacts.values()), 'errors': errors}

    def get(self, identity, *, verify=True, cancelled=lambda: False):
        _cancel(cancelled)
        root, job, settings = self._load(identity)
        if job['receipt'] is not None:
            receipt = job['receipt']
            if (type(receipt) is not dict or receipt.get('protocol') != PROTOCOL
                    or receipt.get('job_id') != identity or type(receipt.get('state')) is not str or receipt['state'] not in _TERMINAL
                    or type(receipt.get('artifacts')) is not list or len(receipt['artifacts']) > MAX_INPUT_FILES * len(FORMATS)
                    or type(receipt.get('files')) is not list or len(receipt['files']) > len(job['inputs'])
                    or job['state'] == 'succeeded' and receipt['state'] != 'succeeded'):
                raise ValueError('The saved terminal transcription receipt is invalid.')
            if verify:
                # Restore never trusts previously absolute worker paths. Rebuild
                # the protocol view from confined relative records, then apply
                # the same complete graph and byte verifier as a fresh run.
                def original_artifact(item):
                    if type(item) is not dict or type(item.get('path')) is not str or not item['path'].startswith('results/'):
                        raise ValueError('A saved original transcript is outside results.')
                    return {**item, 'path': _relative(item['path'])[len('results/'):]}
                files = []
                for row in receipt['files']:
                    if (type(row) is not dict or type(row.get('source')) is not dict
                            or type(row.get('artifacts')) is not list or row['source'] not in job['inputs']):
                        raise ValueError('A saved transcript source does not match the input manifest.')
                    source = row['source']
                    files.append({**row, 'source': {'path': str(root / source['path']),
                        'sha256': source['sha256'], 'bytes': source['bytes']},
                        'artifacts': [original_artifact(item) for item in row['artifacts']]})
                candidate = {**receipt, 'files': files, 'results_dir': str(root / 'results'),
                             'artifacts': [original_artifact(item) for item in receipt['artifacts']]}
                validated = self._validate_result(root, job, settings, candidate, returncode=0,
                                                  run_id=receipt.get('run_id'), cancelled=cancelled)
                if validated != receipt:
                    raise ValueError('The saved transcript receipt is inconsistent.')
        elif job['state'] == 'succeeded':
            raise ValueError('A successful job requires a verified terminal receipt.')
        return job

    def list(self):
        paths = sorted(self.jobs.iterdir(), key=lambda path: path.name)
        if len(paths) > MAX_JOBS + 10:
            raise ValueError('The transcription job directory exceeds its entry budget.')
        rows = [self.get(path.name, verify=False) for path in paths if _ID.fullmatch(path.name)]
        return sorted(rows, key=lambda row: row['created_ns'], reverse=True)

    def set_paused(self, identity, paused):
        if type(paused) is not bool:
            raise ValueError('Pause must be boolean.')
        root, job, _ = self._load(identity)
        if job['state'] != 'running':
            raise ValueError('Start or explicitly resume the job before pausing it.')
        flag = local_path(root / 'pause.flag', root=root)
        if paused:
            atomic_bytes(flag, b'pause\n', replace=True)
        elif flag.exists():
            regular(flag)
            flag.unlink()

    def _config(self, root, job, settings, engine):
        model = local_path(Path(engine.models_dir) / settings.model, root=engine.models_dir, exists=True)
        regular(model, directory=True)
        regular(local_path(model / 'model.bin', root=model, exists=True))
        return {'protocol_version': 1, 'job_id': job['id'], 'job_name': job['title'],
            'files': [str(root / row['path']) for row in job['inputs']], 'folders': [],
            'results_root': str(root / 'results'), 'models_dir': engine.models_dir,
            'settings': asdict(settings)}

    def run(self, identity, *, engine=None, cancelled=lambda: False, progress=lambda *args: None,
            timeout=24 * 3600, stop_grace=3):
        for value, maximum in ((timeout, 7 * 24 * 3600), (stop_grace, 30)):
            if type(value) not in (int, float) or not 0 < value <= maximum or not math.isfinite(value):
                raise ValueError('Choose a bounded positive transcription time budget.')
        _cancel(cancelled)
        engine = self.engine() if engine is None else engine
        if not isinstance(engine, EngineSpec):
            raise ValueError('Choose an explicit reviewed engine profile.')
        engine = engine.validate()
        root = self._job_dir(identity)
        with state_lock(root, 'transcription'):
            root, job, settings = self._load(identity)
            self._verify_inputs(root, job, cancelled)
            if job['state'] == 'succeeded':
                return self.get(identity, cancelled=cancelled)
            config = self._config(root, job, settings, engine)
            # Executing a private, byte-verified copy closes the script pathname
            # replacement window. It is never a public repo artifact or backup.
            worker = _bounded_bytes(engine.worker_path, MAX_WORKER_BYTES)
            if sha256(worker).hexdigest() != engine.worker_sha256:
                raise ValueError('The reviewed worker changed before launch.')
            for name in ('stop.flag', 'pause.flag', 'job_result.json', 'job_status.json'):
                path = local_path(root / name, root=root)
                if path.exists():
                    regular(path)
                    path.unlink()
            atomic_json(root / 'job_config.json', config, replace=True)
            atomic_json(root / 'settings_live.json', asdict(settings), replace=True)
            job['state'], job['error'] = 'running', None
            atomic_json(root / 'job.json', job, replace=True)
            process, stopping, cancelled_run, observed_run_id = None, None, False, None
            try:
                with tempfile.TemporaryDirectory(prefix='carr-asr-reviewed-') as runner, \
                        tempfile.TemporaryFile() as outgoing, tempfile.TemporaryFile() as errors, ExitStack() as children:
                    runtime = local_path(runner, exists=True)
                    library_root = (self.directory.parent.parent if self.directory.parent.name == 'state'
                                    else self.directory)
                    if runtime.is_relative_to(library_root):
                        raise ValueError('The private worker runtime must be outside the backed-up library root.')
                    entrypoint = atomic_bytes(Path(runner) / 'worker.py', worker)
                    names = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL',
                             'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOME', 'PATHEXT')
                    environment = {name: os.environ[name] for name in names if name in os.environ}
                    environment.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                        HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
                    options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                        | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt'
                        else {'start_new_session': True})
                    process = subprocess.Popen([engine.python_path, '-I', '-B', str(entrypoint), '--job-dir', str(root)],
                        cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=outgoing,
                        stderr=errors, shell=False, **options)
                    # Stop owned children before their private runtime directory
                    # and diagnostic files leave their context, even on error.
                    children.callback(stop_process, process)
                    started, status_stamp = time.monotonic(), None
                    progress(0, len(job['inputs']), 'Starting reviewed local transcription')
                    while process.poll() is None:
                        now = time.monotonic()
                        if cancelled() and stopping is None:
                            atomic_bytes(root / 'stop.flag', b'stop\n', replace=True)
                            stopping, cancelled_run = now, True
                            progress(None, len(job['inputs']), 'Stopping local transcription')
                        if stopping is not None and now - stopping >= stop_grace:
                            stop_process(process)
                            break
                        if now - started > timeout:
                            raise TimeoutError('The reviewed transcription worker exceeded its time budget.')
                        if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_DIAGNOSTIC_BYTES:
                            raise ValueError('The transcription worker exceeded its diagnostic byte budget.')
                        status = root / 'job_status.json'
                        if status.exists():
                            current_stamp = _stamp(regular(status))
                            if current_stamp != status_stamp:
                                update = read_json(status)
                                if (type(update) is not dict or update.get('protocol') != PROTOCOL
                                        or update.get('job_id') != identity
                                        or type(update.get('run_id')) is not str or not _ID.fullmatch(update['run_id'])
                                        or not _integer(update.get('done'), 0, len(job['inputs']))):
                                    raise ValueError('The worker progress protocol is invalid.')
                                if observed_run_id is not None and observed_run_id != update['run_id']:
                                    raise ValueError('The worker execution identity changed during this run.')
                                observed_run_id = update['run_id']
                                phase = update.get('phase')
                                if phase not in {'starting', 'identity', 'recognizing', 'exporting', 'paused', 'terminal'}:
                                    phase = 'Working locally'
                                progress(update['done'], len(job['inputs']), phase)
                                status_stamp = current_stamp
                        time.sleep(.04)
                    process.wait(timeout=5)
                    if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_DIAGNOSTIC_BYTES:
                        raise ValueError('The transcription worker exceeded its diagnostic byte budget.')
                    result_path = root / 'job_result.json'
                    if result_path.exists():
                        terminal_status = read_json(root / 'job_status.json')
                        if (type(terminal_status) is not dict or terminal_status.get('protocol') != PROTOCOL
                                or terminal_status.get('job_id') != identity
                                or terminal_status.get('phase') != 'terminal'
                                or (observed_run_id is not None and terminal_status.get('run_id') != observed_run_id)):
                            raise ValueError('The terminal worker status does not match this execution.')
                        receipt = self._validate_result(root, job, settings, read_json(result_path),
                            returncode=process.returncode, run_id=terminal_status.get('run_id'),
                            cancelled=cancelled)
                        if terminal_status.get('result_state') != receipt['state']:
                            raise ValueError('The terminal worker status and result disagree.')
                        # A cancellation requested after final byte verification
                        # cannot erase a genuinely completed terminal result.
                        job['receipt'], job['state'] = receipt, receipt['state']
                        job['worker_sha256'] = engine.worker_sha256
                    elif cancelled_run:
                        job['state'], job['error'] = 'cancelled', 'Stopped before a valid terminal receipt; completed files remain on disk.'
                    else:
                        raise RuntimeError('The reviewed worker exited without a valid terminal result (exit ' + str(process.returncode) + ').')
                    atomic_json(root / 'job.json', job, replace=True)
                    if job['state'] == 'succeeded':
                        progress(len(job['inputs']), len(job['inputs']), 'Verified every requested transcript output')
                    return job
            except Exception as exc:
                job['state'], job['error'] = ('cancelled' if isinstance(exc, InterruptedError) else 'failed'), str(exc)[:8000]
                atomic_json(root / 'job.json', job, replace=True)
                if isinstance(exc, InterruptedError):
                    # Keep completed bytes and any earlier admitted receipt.
                    # This execution remains unverified until explicit resume.
                    return job
                raise
            finally:
                if process is not None:
                    stop_process(process)

    def _selected_artifact(self, root, job, name, *, cancelled=lambda: False):
        name = _relative(name)
        receipt = job.get('receipt')
        if not isinstance(receipt, dict):
            raise ValueError('No verified transcript outputs are available yet.')
        if not name.startswith('results/') or type(receipt.get('artifacts')) is not list:
            raise ValueError('Choose one verified original transcript in results.')
        matches = [row for row in receipt['artifacts'] if row['path'] == name]
        if len(matches) != 1:
            raise ValueError('Choose one verified transcript artifact.')
        return self._artifact(root, matches[0], cancelled=cancelled)

    def read_transcript(self, identity, artifact, *, cancelled=lambda: False):
        _cancel(cancelled)
        root, job, _ = self._load(identity)
        selected = self._selected_artifact(root, job, artifact, cancelled=cancelled)
        raw = _bounded_bytes(root / selected['path'], MAX_ARTIFACT_BYTES)
        _cancel(cancelled)
        if sha256(raw).hexdigest() != selected['sha256']:
            raise ValueError('The selected transcript changed before display.')
        return raw.decode('utf-8')

    def revise(self, identity, artifact, text):
        if type(text) is not str or '\x00' in text or len(text) > 2_000_000:
            raise ValueError('An edited transcript must be bounded plain text.')
        raw = text.encode('utf-8')
        root = self._job_dir(identity)
        with state_lock(root, 'transcription'):
            root, job, _ = self._load(identity)
            original = self._selected_artifact(root, job, artifact)
            if original['format'] != 'txt':
                raise ValueError('Choose a TXT transcript before saving a text revision.')
            if len(job['revisions']) >= MAX_REVISIONS:
                raise ValueError('This transcription job reached its revision budget.')
            revision = {'id': uuid4().hex, 'original': dict(original), 'created_ns': time.time_ns(),
                        'bytes': len(raw), 'sha256': sha256(raw).hexdigest(), 'format': 'txt'}
            revision['path'] = 'revisions/' + revision['id'] + '.txt'
            atomic_bytes(root / revision['path'], raw)
            atomic_json(root / ('revisions/' + revision['id'] + '.json'), revision)
            job['revisions'].append(revision)
            atomic_json(root / 'job.json', job, replace=True)
            return revision

    def export(self, identity, artifact, destination, *, revision_id=None):
        root, job, _ = self._load(identity)
        selected = self._selected_artifact(root, job, artifact)
        if revision_id is not None:
            matches = [row for row in job['revisions'] if row.get('id') == revision_id]
            if len(matches) != 1 or matches[0].get('original') != selected:
                raise ValueError('Choose a revision of this exact original transcript.')
            selected = self._artifact(root, {key: matches[0][key] for key in ('path', 'format', 'bytes', 'sha256')})
        destination = local_path(destination)
        if destination.is_relative_to(self.directory):
            raise ValueError('Export to a new file outside managed transcription state.')
        if destination.suffix.casefold() != '.' + selected['format']:
            raise ValueError('Use the selected transcript format extension.')
        raw = _bounded_bytes(root / selected['path'], MAX_ARTIFACT_BYTES)
        if sha256(raw).hexdigest() != selected['sha256']:
            raise ValueError('The selected transcript changed before export.')
        return atomic_bytes(destination, raw)
