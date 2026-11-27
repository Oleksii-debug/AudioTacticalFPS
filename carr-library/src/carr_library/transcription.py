"""Library-scoped jobs for the supplied local Whisper worker.

The reviewed owner worker remains a separately selected private component.
This service preserves inputs, validates its durable receipts, and reuses the
application's state, process runner and operation progress infrastructure.
No model is downloaded and a protocol test is never acoustic qualification.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from .audio.models import AudioBudget, AudioCancelled, AudioError, finite
from .audio.process import run_process
from .durable_state import (atomic_bytes, atomic_json, digest_file, local_path,
                            read_json, regular, state_lock, sync_directory)
from .state_backup import relative_name

MAX_FILES = 64
MAX_SOURCE_BYTES = 8 * 1024**3
MAX_BATCH_BYTES = 32 * 1024**3
MAX_TEXT_BYTES = 16 * 1024**2
MEDIA_SUFFIXES = frozenset(('.wav', '.flac', '.mp3', '.m4a', '.mp4', '.mkv',
                           '.webm', '.ogg', '.opus', '.aac', '.wma', '.aiff',
                           '.aif', '.m4b', '.mov', '.avi'))
RECOGNITION_KEYS = ('task', 'language', 'model', 'model_revision', 'device',
                    'compute_type', 'beam_size', 'parallel_files', 'vad_filter',
                    'multilingual', 'condition_on_previous_text',
                    'word_timestamps', 'max_attempts')
EXPORT_KEYS = ('output_mode', 'output_txt', 'output_srt', 'output_vtt',
               'output_json', 'include_headers', 'word_timestamps')
DEFAULT_SETTINGS = {
    'output_mode': 'separate', 'task': 'transcribe', 'translation_target': 'en',
    'language': 'auto', 'model': 'small', 'model_revision': None,
    'device': 'cpu', 'compute_type': 'int8', 'beam_size': 1, 'parallel_files': 1,
    'vad_filter': True, 'include_headers': False, 'output_txt': True,
    'output_srt': True, 'output_vtt': True, 'output_json': True,
    'multilingual': False, 'condition_on_previous_text': True,
    'word_timestamps': True, 'max_attempts': 1,
}


def settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError('Unknown transcription setting.')
    result = {**DEFAULT_SETTINGS, **value}
    if result['output_mode'] != 'separate' or result['task'] != 'transcribe':
        raise ValueError('Library transcription requires separate original-language outputs.')
    if not isinstance(result['language'], str) or not re.fullmatch(r'auto|[a-z]{2,3}', result['language']):
        raise ValueError('Choose auto or a supported Whisper language code.')
    if not isinstance(result['model'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', result['model']):
        raise ValueError('Choose a local model name, not a URL or path.')
    revision = result['model_revision']
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision)):
        raise ValueError('Use an exact 40-character local model commit or no revision.')
    if result['device'] not in ('cpu', 'cuda') or result['compute_type'] not in (
            'int8', 'float32', 'float16', 'int8_float16', 'int8_float32', 'auto'):
        raise ValueError('Unsupported local inference device or compute type.')
    for key, low, high in (('beam_size', 1, 20), ('parallel_files', 1, 8), ('max_attempts', 1, 3)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Invalid bounded transcription setting: ' + key)
    for key in ('vad_filter', 'include_headers', 'output_txt', 'output_srt',
                'output_vtt', 'output_json', 'multilingual',
                'condition_on_previous_text', 'word_timestamps'):
        if type(result[key]) is not bool:
            raise ValueError('A transcription option must be boolean: ' + key)
    # All four base formats are the editable, portable library contract.
    if not all(result[key] for key in ('output_txt', 'output_srt', 'output_vtt', 'output_json')):
        raise ValueError('Library jobs retain TXT, SRT, VTT and JSON together.')
    return result


def _identity(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid recognition identity.')
    raw = json.dumps({k: v for k, v in value.items() if k != 'sha256'},
                     sort_keys=True, ensure_ascii=True, allow_nan=False,
                     separators=(',', ':')).encode('utf-8')
    actual = sha256(raw).hexdigest()
    if value.get('sha256') != actual:
        raise ValueError('Recognition identity checksum does not match.')
    return actual


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('Invalid transcription job ID.')
    return value


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _copy(source, destination, *, maximum, cancelled):
    source = local_path(source, exists=True)
    before = regular(source)
    if before.st_size > maximum:
        raise ValueError('The selected file exceeds this operation size budget.')
    count, digest = 0, sha256()
    destination = local_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open('rb') as read, destination.open('xb') as write:
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise ValueError('The selected file changed before copying.')
        while chunk := read.read(min(1024 * 1024, maximum - count + 1)):
            if cancelled():
                raise AudioCancelled('Transcription input preparation was cancelled.')
            count += len(chunk)
            if count > before.st_size or count > maximum:
                raise ValueError('The selected file changed or exceeded its size budget.')
            write.write(chunk)
            digest.update(chunk)
        write.flush()
        os.fsync(write.fileno())
        if _stamp(os.fstat(read.fileno())) != _stamp(before):
            raise ValueError('The selected file changed during copying.')
    if count != before.st_size or _stamp(regular(source)) != _stamp(before):
        raise ValueError('The selected file changed during copying.')
    # Compare actual bytes too; metadata alone is insufficient on Windows.
    verify = sha256()
    remaining = count
    with source.open('rb') as read:
        while chunk := read.read(min(1024 * 1024, remaining + 1)):
            if cancelled():
                raise AudioCancelled('Transcription input verification was cancelled.')
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError('The selected file grew during verification.')
            verify.update(chunk)
    if remaining or verify.digest() != digest.digest() or _stamp(regular(source)) != _stamp(before):
        raise ValueError('The selected file changed during verification.')
    return {'size_bytes': count, 'sha256': digest.hexdigest()}


def _checked_digest(path, size, cancelled):
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size != size:
        raise ValueError('A retained file changed size.')
    digest, count = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A retained file changed before verification.')
        while chunk := stream.read(min(1024 * 1024, size - count + 1)):
            if cancelled():
                raise AudioCancelled('Transcription verification was cancelled.')
            count += len(chunk)
            if count > size:
                raise ValueError('A retained file grew during verification.')
            digest.update(chunk)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A retained file changed during verification.')
    if count != size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('A retained file changed during verification.')
    return digest.hexdigest()


# Installed model code is explicitly trusted local code. These guards constrain
# Python-visible network, child commands and writes; they are not an OS sandbox
# for arbitrary native libraries. The worker must also require offline assets.
_WORKER_BOOTSTRAP = r'''
import os, pathlib, runpy, sys
job = pathlib.Path(sys.argv[2]).resolve(strict=True)
worker = pathlib.Path(sys.argv[1]).resolve(strict=True)
def writable(value):
    if isinstance(value, int):
        return
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = pathlib.Path.cwd() / path
    if not path.resolve().is_relative_to(job):
        raise PermissionError('Transcription writes must remain inside its private job.')
def audit(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo',
                 'subprocess.Popen', 'os.system', 'os.posix_spawn') or event.startswith('os.spawn'):
        raise PermissionError('Transcription cannot use network or child commands.')
    if event == 'open':
        mode = args[1] or ''
        flags = args[2]
        if any(c in mode for c in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            writable(args[0])
    elif event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.chmod', 'os.utime'):
        writable(args[0])
    elif event in ('os.rename', 'os.link', 'os.symlink'):
        writable(args[0])
        writable(args[1])
sys.dont_write_bytecode = True
sys.addaudithook(audit)
sys.argv = [str(worker), '--job-dir', str(job)]
runpy.run_path(str(worker), run_name='__main__')
'''


class TranscriptionService:
    """The same retained-input and receipt boundary for native UI and CLI."""

    def __init__(self, app, workspace):
        self.app, self.workspace = app, workspace
        self._authorize()
        self.root = local_path(app.root / 'state' / 'transcription')
        self.jobs_root = self.root / sha256(workspace.encode('utf-8')).hexdigest() / 'jobs'

    def _authorize(self):
        with self.app.read(self.workspace):
            pass

    def configure(self, python, worker, models_dir):
        self._authorize()
        python = local_path(Path(python).resolve(strict=True), exists=True)
        worker = local_path(worker, exists=True)
        models_dir = local_path(models_dir, exists=True)
        regular(models_dir, directory=True)
        regular(python)
        regular(worker)
        if worker.suffix.lower() != '.py' or regular(worker).st_size > 4 * 1024**2:
            raise ValueError('Select the reviewed local Whisper worker Python file.')
        if models_dir.is_relative_to(self.app.root):
            raise ValueError('Keep model/runtime assets outside the backed-up library data directory.')
        value = {'version': 1, 'python': str(python), 'worker': str(worker),
                 'models_dir': str(models_dir), 'python_sha256': digest_file(python),
                 'worker_sha256': digest_file(worker),
                 'network': 'local-files-only; Python network calls denied',
                 'inference_qualified': False}
        with state_lock(self.root, 'engine'):
            atomic_json(self.root / 'engine.json', value, replace=True)
        return value

    def configuration(self, *, verify=False):
        self._authorize()
        path = self.root / 'engine.json'
        if not path.exists():
            raise ValueError('Configure the supplied local Whisper worker and offline models first.')
        value = read_json(path)
        if value.get('version') != 1:
            raise ValueError('Unsupported transcription engine configuration.')
        if verify:
            for key in ('python', 'worker'):
                if digest_file(local_path(value[key], exists=True)) != value[key + '_sha256']:
                    raise ValueError('The configured transcription runtime changed; select and review it again.')
            regular(local_path(value['models_dir'], exists=True), directory=True)
        return value

    def _path(self, identity):
        return local_path(self.jobs_root / _identifier(identity))

    def _save(self, path, value):
        self._authorize()
        value['updated'] = time.time()
        atomic_json(path / 'job.json', value, replace=True)

    def get(self, identity):
        self._authorize()
        value = read_json(self._path(identity) / 'job.json')
        if (value.get('version') != 1 or value.get('id') != identity
                or value.get('workspace') != self.workspace):
            raise ValueError('Transcription job belongs to another library or schema.')
        if not isinstance(value.get('sources'), list) or not 1 <= len(value['sources']) <= MAX_FILES:
            raise ValueError('Invalid retained transcription source manifest.')
        settings(value['settings'])
        names = set()
        for item in value['sources']:
            name = item.get('filename')
            if (not isinstance(name, str) or relative_name(name).name != name or name in names
                    or type(item.get('size_bytes')) is not int or not 0 <= item['size_bytes'] <= MAX_SOURCE_BYTES
                    or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', ''))):
                raise ValueError('Invalid retained transcription source identity.')
            names.add(name)
        return value

    def list(self):
        self._authorize()
        if not self.jobs_root.exists():
            return ()
        result = []
        for path in sorted(self.jobs_root.iterdir()):
            if path.name.startswith('.'):
                continue
            regular(path, directory=True)
            if len(result) >= 10000:
                raise ValueError('Transcription job listing exceeds its budget.')
            result.append(self.get(path.name))
        return tuple(sorted(result, key=lambda x: (x['created'], x['id']), reverse=True))

    def submit_folder(self, folder, *, title='Transcription', options=None, cancelled=lambda: False):
        """Retain one explicitly selected folder through the shared bounded scanner."""
        from .ingest.batch_scan import scan_paths
        self._authorize()
        settings(options)
        if cancelled():
            raise AudioCancelled('Transcription folder intake was cancelled.')
        folder = local_path(folder, exists=True)
        regular(folder, directory=True)
        total = 0
        def media_manifest(path):
            nonlocal total
            size = regular(path).st_size
            if not 0 < size <= MAX_SOURCE_BYTES:
                raise ValueError('A media file is empty or exceeds the transcription source budget.')
            total += size
            if total > MAX_BATCH_BYTES:
                raise ValueError('The selected folder exceeds the transcription batch size budget.')
            return {'size': size, 'mime': 'application/octet-stream',
                    'sha256': _checked_digest(path, size, cancelled)}
        try:
            rows = scan_paths([folder], cancelled=cancelled, supported=MEDIA_SUFFIXES,
                              file_manifest=media_manifest, max_files=MAX_FILES,
                              skip_unsupported=True)
        except InterruptedError as exc:
            raise AudioCancelled('Transcription folder intake was cancelled.') from exc
        rejected = next((row for row in rows if row['state'] == 'rejected'), None)
        if rejected is not None:
            raise ValueError('Folder intake stopped at ' + rejected['file'] + ': ' +
                             rejected['message'] + ' No transcription job was created.')
        accepted = [row for row in rows if row['state'] in ('queued', 'duplicate')]
        files = [row['source'] for row in accepted]
        source_manifests = [{'size_bytes': row['manifest']['size'],
                             'sha256': row['manifest']['sha256']} for row in accepted]
        excluded = sum(row['state'] == 'excluded' for row in rows)
        if not files:
            raise ValueError(f'No supported audio or video files were found; {excluded} unsupported files excluded.')
        identity = self.submit(files, title=title, options=options, cancelled=cancelled,
                               source_root=folder, source_manifests=source_manifests)
        return {'id': identity, 'files': len(files), 'excluded_files': excluded}

    def submit(self, files, *, title='Transcription', options=None, cancelled=lambda: False,
               source_root=None, source_manifests=None):
        self._authorize()
        chosen = settings(options)
        files = tuple(files)
        if not 1 <= len(files) <= MAX_FILES:
            raise ValueError('Choose between 1 and 64 local audio or video files.')
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or '\0' in title:
            raise ValueError('Enter a transcription title of 1 to 200 characters.')
        if source_root is not None:
            source_root = local_path(source_root, exists=True)
            regular(source_root, directory=True)
        if source_manifests is not None and (
                not isinstance(source_manifests, (tuple, list)) or len(source_manifests) != len(files)
                or any(not isinstance(item, dict) or set(item) != {'size_bytes', 'sha256'}
                       or type(item['size_bytes']) is not int or not 0 <= item['size_bytes'] <= MAX_SOURCE_BYTES
                       or not isinstance(item['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])
                       for item in source_manifests)):
            raise ValueError('Invalid transcription source manifests.')
        identity = uuid4().hex
        with state_lock(self.jobs_root, 'submit'):
            pending = self.jobs_root / ('.pending-' + identity)
            pending.mkdir(mode=0o700)
            (pending / 'input').mkdir(mode=0o700)
            total, sources = 0, []
            try:
                for index, original in enumerate(files):
                    original = local_path(original, root=source_root, exists=True)
                    expected = source_manifests[index] if source_manifests is not None else None
                    if expected is not None and regular(original).st_size != expected['size_bytes']:
                        raise ValueError('A source changed after the folder scan; select the folder again.')
                    suffix = original.suffix.lower()
                    if suffix not in MEDIA_SUFFIXES:
                        raise ValueError('Unsupported audio/video extension: ' + suffix)
                    name = f'{index + 1:03d}-{uuid4().hex}{suffix}'
                    record = _copy(original, pending / 'input' / name,
                        maximum=min(MAX_SOURCE_BYTES, MAX_BATCH_BYTES - total), cancelled=cancelled)
                    if expected is not None and record != expected:
                        raise ValueError('A source changed after the folder scan; select the folder again.')
                    total += record['size_bytes']
                    original_name = (original.relative_to(source_root).as_posix()
                                     if source_root is not None else original.name)
                    sources.append({'filename': name, 'original_name': original_name, **record})
                value = {'version': 1, 'id': identity, 'workspace': self.workspace,
                         'title': title.strip(), 'created': time.time(), 'updated': time.time(),
                         'status': 'ready', 'sources': sources, 'settings': chosen,
                         'done': 0, 'failed': 0, 'receipts': [], 'artifacts': [],
                         'run_context': None, 'last_error': ''}
                if cancelled():
                    raise AudioCancelled('Transcription input preparation was cancelled before publication.')
                self._authorize()
                atomic_json(pending / 'job.json', value)
                os.rename(pending, self._path(identity))
                sync_directory(self.jobs_root)
            finally:
                if pending.exists():
                    shutil.rmtree(pending)
        return identity

    def _sources(self, path, job, cancelled):
        expected = {item['filename']: item for item in job['sources']}
        actual = {item.name for item in (path / 'input').iterdir()}
        if actual != set(expected):
            raise ValueError('Retained transcription inputs changed.')
        for name, record in expected.items():
            if cancelled():
                raise AudioCancelled('Transcription source verification was cancelled.')
            source = local_path(path / 'input' / name, exists=True)
            if regular(source).st_size != record['size_bytes'] or _checked_digest(source, record['size_bytes'], cancelled) != record['sha256']:
                raise ValueError('A retained original changed; recognition cannot resume.')
        return expected

    def _receipts(self, path, job, status, cancelled=lambda: False):
        context = job.get('run_context')
        if not isinstance(context, dict) or not re.fullmatch(r'[a-f0-9]{32}', status.get('run_id', '')):
            raise ValueError('No current worker run identity was recorded.')
        rows = status.get('receipts')
        if not isinstance(rows, list) or len(rows) > len(job['sources']) or any(not isinstance(row, str) for row in rows) or len(set(rows)) != len(rows):
            raise ValueError('Invalid current-run transcription receipt list.')
        expected = self._sources(path, job, cancelled)
        required = {'txt', 'srt', 'vtt', 'json'}
        if job['settings']['word_timestamps']:
            required |= {'words.srt', 'words.vtt'}
        seen, seen_outputs, accepted, artifacts = set(), set(), [], []
        for receipt_name in rows:
            relative = relative_name(receipt_name)
            if len(relative.parts) != 2 or relative.parts[0] != 'receipts':
                raise ValueError('A worker receipt escaped its job receipt folder.')
            receipt = read_json(local_path(path / relative, root=path, exists=True))
            if (receipt.get('version') != 1 or receipt.get('state') != 'complete'
                    or receipt.get('run_id') != status['run_id']):
                raise ValueError('A stale or incomplete transcription receipt was returned.')
            identity = receipt.get('identity')
            _identity(identity)
            _identity(identity.get('model'))
            _identity(identity.get('runtime'))
            source = identity.get('source', {})
            name = source.get('relative')
            record = expected.get(name)
            if (record is None or name in seen or source.get('size_bytes') != record['size_bytes']
                    or source.get('sha256') != record['sha256']
                    or source.get('root') != context['input_root']
                    or source.get('path') != os.path.normcase(str(Path(context['input_root']) / name))):
                raise ValueError('A transcription receipt does not match its retained original.')
            if (identity['runtime'].get('worker_sha256') != context['worker_sha256']
                    or identity['runtime'].get('executable', {}).get('sha256') != context['python_sha256']
                    or identity['model'].get('selected') != job['settings']['model']):
                raise ValueError('A transcription receipt belongs to another engine or model.')
            if identity.get('recognition') != {key: job['settings'][key] for key in RECOGNITION_KEYS}:
                raise ValueError('Recognition settings changed without a new job.')
            if receipt.get('requested_exports') != {key: job['settings'][key] for key in EXPORT_KEYS}:
                raise ValueError('The worker did not complete the requested export profile.')
            if receipt.get('outcome') not in ('speech', 'no_speech'):
                raise ValueError('Unrecognized transcription outcome.')
            outputs = receipt.get('artifacts')
            if not isinstance(outputs, list) or len(outputs) != len(required):
                raise ValueError('A required transcription export is missing.')
            formats = set()
            for output in outputs:
                relative_output = relative_name(output.get('path'))
                name_output = str(relative_output)
                if name_output in seen_outputs:
                    raise ValueError('Different source receipts share an output.')
                kind = next((key for key in ('words.srt', 'words.vtt', 'srt', 'vtt', 'txt', 'json')
                             if name_output.endswith('.' + key)), None)
                if kind not in required or kind in formats:
                    raise ValueError('A required transcription export is missing or duplicated.')
                target = local_path(path / 'results' / relative_output, root=path / 'results', exists=True)
                if (type(output.get('size_bytes')) is not int
                        or regular(target).st_size != output['size_bytes']
                        or not 0 <= output['size_bytes'] <= 512 * 1024**2
                        or _checked_digest(target, output['size_bytes'], cancelled) != output.get('sha256')):
                    raise ValueError('A transcription output is incomplete or changed.')
                if kind == 'json' and output['sha256'] != receipt.get('result_metadata_sha256'):
                    raise ValueError('The timed transcription metadata checksum does not match its receipt.')
                formats.add(kind)
                seen_outputs.add(name_output)
                artifacts.append({**output, 'source': name, 'format': kind,
                                  'recognition_identity': identity['sha256']})
            if formats != required:
                raise ValueError('A required transcription export is missing.')
            seen.add(name)
            accepted.append(receipt_name)
        if (type(status.get('done')) is not int or type(status.get('total')) is not int
                or type(status.get('failed')) is not int or status['failed'] < 0
                or status['done'] != len(accepted) or status['total'] != len(expected)):
            raise ValueError('Worker completion counters do not match verified receipts.')
        return accepted, artifacts

    def run(self, identity, *, cancelled=lambda: False, progress=None, timeout=86400):
        finite(timeout, 'Transcription time allowance', .05, 365 * 86400)
        path = local_path(self._path(identity), exists=True)
        regular(path, directory=True)
        with state_lock(path, 'transcribe'):
            job = self.get(identity)
            config = self.configuration(verify=True)
            self._sources(path, job, cancelled)
            if job['status'] == 'completed':
                self.verify(identity)
                return job
            for name in ('stop.flag', 'pause.flag', 'settings_live.json'):
                candidate = path / name
                if candidate.exists():
                    regular(candidate)
                    candidate.unlink()
            prior = path / 'job_status.json'
            if prior.exists():
                regular(prior)
                prior.unlink()
            context = {'input_root': str((path / 'input').resolve()),
                       'worker_sha256': config['worker_sha256'],
                       'python_sha256': config['python_sha256']}
            job.update(status='running', done=0, failed=0, receipts=[], artifacts=[],
                       run_context=context, last_error='')
            self._save(path, job)
            for name in ('results', 'temporary', 'runtime-home'):
                (path / name).mkdir(exist_ok=True, mode=0o700)
            atomic_json(path / 'job_config.json', {
                'job_name': job['title'], 'folders': [context['input_root']],
                'results_root': str(path / 'results'), 'models_dir': config['models_dir'],
                'settings': job['settings']}, replace=True)
            environment = {key: os.environ[key] for key in (
                'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'LANG', 'LC_ALL',
                'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE') if key in os.environ}
            environment.update({
                'PATH': os.pathsep.join((str(Path(config['python']).parent), os.defpath)),
                'HOME': str(path / 'runtime-home'), 'USERPROFILE': str(path / 'runtime-home'),
                'TEMP': str(path / 'temporary'), 'TMP': str(path / 'temporary'),
                'TMPDIR': str(path / 'temporary'), 'HF_HOME': str(path / 'runtime-home'),
                'XDG_CACHE_HOME': str(path / 'runtime-home'),
                'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
                'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2'})
            stop_at, notice_at = None, 0.0
            reported_done, observer_available = 0, progress is not None
            def notify(done, total, phase):
                nonlocal reported_done, observer_available
                # Worker-reported files are distinct from verified job.done.
                # Stop defers receipt verification and must not rewind measured
                # progress or promote saved files into accepted exports.
                if type(done) is int and 0 <= done <= len(job['sources']):
                    reported_done = max(reported_done, done)
                if observer_available:
                    try:
                        progress(reported_done, total, phase)
                    except Exception:
                        # A detached UI must not kill the worker or reverse a
                        # durable result. Disable only this optional observer.
                        observer_available = False
            def poll():
                nonlocal stop_at, notice_at
                now = time.monotonic()
                if cancelled() and stop_at is None:
                    atomic_bytes(path / 'stop.flag', b'stop\n', replace=True)
                    stop_at = now
                if observer_available and now - notice_at >= .5 and prior.exists():
                    notice_at = now
                    try:
                        current = read_json(prior)
                        notify(current.get('done', 0), len(job['sources']),
                               str(current.get('state', 'running'))[:200])
                    except (ValueError, OSError):
                        pass
                return stop_at is not None and now - stop_at >= 3
            failure = None
            from .audio.music import _SITE_BOOTSTRAP
            try:
                run_process([config['python'], '-I', '-S', '-B', '-X', 'utf8', '-c',
                    _SITE_BOOTSTRAP + _WORKER_BOOTSTRAP, config['worker'], str(path)],
                    cwd=path, env=environment, cancelled=poll, watch=(path,),
                    budget=AudioBudget(timeout=timeout,
                        max_output_bytes=(sum(x['size_bytes'] for x in job['sources'])
                            + sum(sorted((x['size_bytes'] for x in job['sources']), reverse=True)[:job['settings']['parallel_files']])
                            + 512 * 1024**2),
                        max_staging_files=10000))
            except (AudioError, OSError, ValueError, KeyboardInterrupt) as exc:
                failure = exc
            # A stop must not trigger a second potentially multi-gigabyte read.
            # Keep worker cache/receipts on disk for the next explicit resume;
            # do not present these unverified files as completed exports yet.
            if stop_at is not None or isinstance(failure, (AudioCancelled, KeyboardInterrupt)) or cancelled():
                job.update(status='stopped', done=0, failed=0, receipts=[], artifacts=[],
                           last_error='Stopped. Saved worker results will be verified on the next resume.')
                self._save(path, job)
                notify(0, len(job['sources']), 'stopped')
                return job
            try:
                status = read_json(prior) if prior.exists() else {}
                receipts, artifacts = self._receipts(path, job, status, cancelled)
                complete = (failure is None and stop_at is None
                            and status.get('result_state') == 'completed'
                            and len(receipts) == len(job['sources'])
                            and status.get('failed') == 0)
                job.update(done=len(receipts), failed=len(job['sources']) - len(receipts),
                           receipts=receipts, artifacts=artifacts,
                           status='completed' if complete else (
                               'stopped' if stop_at is not None or isinstance(failure, (AudioCancelled, KeyboardInterrupt))
                               else 'partial' if receipts else 'failed'),
                           worker_run_id=status.get('run_id'))
                if not complete:
                    job['last_error'] = str(failure or 'The worker did not complete every requested export.')[:3000]
            except AudioCancelled:
                job.update(status='stopped', done=0, failed=0, receipts=[], artifacts=[],
                           last_error='Verification stopped. Saved worker results remain available for resume.')
            except (ValueError, OSError, KeyError, TypeError) as exc:
                job.update(status='stopped' if stop_at is not None else 'failed',
                           last_error=str(failure or exc)[:3000])
            self._save(path, job)
            notify(job['done'], len(job['sources']), job['status'])
            return job

    def verify(self, identity, *, cancelled=lambda: False):
        path, job = self._path(identity), self.get(identity)
        status = read_json(path / 'job_status.json')
        if status.get('run_id') != job.get('worker_run_id'):
            raise ValueError('The recognized transcript no longer belongs to the saved run.')
        receipts, artifacts = self._receipts(path, job, status, cancelled)
        if receipts != job['receipts'] or artifacts != job['artifacts']:
            raise ValueError('The saved transcription result changed.')
        return job

    def text(self, identity, source, *, cancelled=lambda: False):
        job = self.verify(identity, cancelled=cancelled)
        row = next((x for x in job['artifacts'] if x['source'] == source and x['format'] == 'txt'), None)
        if row is None:
            raise ValueError('No verified text export exists for this selected source.')
        path = self._path(identity) / 'results' / relative_name(row['path'])
        if regular(path).st_size > MAX_TEXT_BYTES:
            raise ValueError('The transcript exceeds the editor size allowance.')
        return path.read_text(encoding='utf-8-sig')

    def save_edit(self, identity, source, text, *, cancelled=lambda: False):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_TEXT_BYTES or '\0' in text:
            raise ValueError('Edited transcript exceeds its text allowance.')
        path = local_path(self._path(identity), exists=True)
        regular(path, directory=True)
        with state_lock(path, 'transcribe'):
            job = self.verify(identity, cancelled=cancelled)
            artifact = next((x for x in job['artifacts'] if x['source'] == source and x['format'] == 'txt'), None)
            if artifact is None:
                raise ValueError('Choose a verified original transcript before editing.')
            original = next(x for x in job['sources'] if x['filename'] == source)
            revision = uuid4().hex
            value = {'version': 1, 'id': revision, 'job': identity, 'source': source,
                     'source_sha256': original['sha256'],
                     'recognition_identity': artifact['recognition_identity'],
                     'original_text_sha256': artifact['sha256'],
                     'created': time.time(), 'text': text,
                     'kind': 'user-edited derivative; source and timed recognition remain unchanged'}
            if cancelled():
                raise AudioCancelled('Saving the transcript edit was cancelled.')
            atomic_json(path / 'edits' / (revision + '.json'), value)
            return revision

    def edits(self, identity):
        path = self._path(identity) / 'edits'
        self.get(identity)
        if not path.exists():
            return ()
        result = []
        for child in sorted(path.iterdir()):
            if len(result) >= 10000 or child.suffix != '.json':
                raise ValueError('Invalid transcript edit history.')
            _identifier(child.stem)
            value = read_json(child)
            if value.get('job') != identity or value.get('id') != child.stem:
                raise ValueError('Transcript edit history belongs to another job.')
            result.append(value)
        return tuple(sorted(result, key=lambda x: (x['created'], x['id'])))

    def export(self, identity, destination, *, cancelled=lambda: False):
        path = local_path(self._path(identity), exists=True)
        regular(path, directory=True)
        with state_lock(path, 'transcribe'):
            job = self.verify(identity, cancelled=cancelled)
            destination = local_path(destination)
            if os.path.lexists(destination) or destination.is_relative_to(self.app.root):
                raise ValueError('Choose a new export folder outside the library data directory.')
            regular(local_path(destination.parent, exists=True), directory=True)
            temporary = destination.parent / ('.pending-' + uuid4().hex)
            temporary.mkdir(mode=0o700)
            try:
                exported = []
                edits = self.edits(identity)
                for artifact in job['artifacts']:
                    relative = relative_name(artifact['path'])
                    record = _copy(path / 'results' / relative, temporary / relative,
                                   maximum=512 * 1024**2, cancelled=cancelled)
                    if record['sha256'] != artifact['sha256'] or record['size_bytes'] != artifact['size_bytes']:
                        raise ValueError('A transcription export changed while copying.')
                    exported.append({'filename': str(relative), **record})
                for edit in edits:
                    name = 'edited/' + edit['id'] + '.txt'
                    atomic_bytes(temporary / name, edit['text'].encode('utf-8'))
                atomic_json(temporary / 'transcription-manifest.json', {
                    'version': 1, 'job': identity, 'title': job['title'],
                    'status': job['status'], 'sources': job['sources'],
                    'settings': job['settings'], 'artifacts': exported,
                    'edits': edits,
                    'notice': 'Machine transcription and separately labelled user edits; original media is retained in the private library.'})
                self._authorize()
                if cancelled():
                    raise AudioCancelled('Transcription export was cancelled.')
                os.rename(temporary, destination)
                sync_directory(destination.parent)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return destination
