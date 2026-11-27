"""Durable orchestration of the supplied, explicitly trusted local worker.

This is a file-protocol adapter, not an ASR implementation. It reuses the
application's atomic state, bounded subprocess runner and resource broker.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
from uuid import uuid4

from ..operations.process import ProcessBudget, ProcessCancelled, ProcessError, publish_file, run_process
from ..durable_state import (atomic_bytes, atomic_json, digest_file, encode, local_path,
                             read_json, regular, state_lock, sync_directory)
from ..research.export_file import write_binary_export
from .models import (AUDIO_EXTENSIONS, FORMATS, MAX_RESULT_BYTES, encoded_outputs,
                     fingerprint, settings as validate_settings, validated_result,
                     worker_settings, validate_effective_settings)
from .runtime import file_identity, inspect_runtime, stopped, _stamp
from . import verified_protocol

SCHEMA = 'resonance-transcription-job/1'
MAX_SOURCES = 512
MAX_SOURCE_BYTES = 32 * 1024**3
MAX_JOB_BYTES = 64 * 1024**3
ACTIVE = frozenset(('running', 'pausing', 'cancelling'))


def _id(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Invalid transcription identity.')
    return value


def _label(value, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('Enter a valid transcription name.')
    return value.strip()


def _protocol_digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _open_worker_status(path):
    """Read dynamic status with delete sharing on Windows, never a reparse target."""
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if os.name != 'nt':
        return os.open(path, flags)
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class Attributes(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = (
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    # FILE_SHARE_READ|WRITE|DELETE lets the producer atomically replace status.
    # OPEN_REPARSE_POINT avoids following a link swapped in after lstat.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = Attributes()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attributes),
                                                   ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & (0x400 | 0x10):
            raise ValueError('Worker status must use a regular local file without links.')
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return descriptor  # fdopen owns and closes this handle.


def _observe_worker_status(path):
    """Take one bounded observation of an atomically replaced progress file.

    An unlinked/replaced inode is a missed observation, not a failed job.
    Persistent links, reparse points, oversized files and stable malformed JSON
    still fail. Immutable receipts and final completion retain strict readers.
    """
    path = Path(path)
    path = local_path(path.parent, exists=True) / path.name
    maximum = 256 * 1024

    def ordinary(info):
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_nlink > 1):
            raise ValueError('Worker status must use a regular local file without links.')
        if not 0 <= info.st_size <= maximum:
            raise ValueError('The worker status exceeds its byte budget.')
        return info.st_nlink == 1

    def binding(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_nlink

    for _ in range(3):
        try:
            before = path.lstat()
            if not ordinary(before):
                continue
            with os.fdopen(_open_worker_status(path), 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not ordinary(opened) or binding(opened) != binding(before):
                    continue
                raw = stream.read(maximum + 1)
                after_open = os.fstat(stream.fileno())
                after = path.lstat()
                if not ordinary(after_open) or not ordinary(after):
                    continue
                # Compare each stat API to itself, with stable identity binding.
                if (len(raw) != before.st_size or len(raw) > maximum
                        or _stamp(opened) != _stamp(after_open)
                        or _stamp(before) != _stamp(after)
                        or binding(after_open) != binding(after)):
                    continue
            value = verified_protocol.decode(raw)
            if type(value) is not dict:
                raise ValueError('The worker returned invalid status.')
            return value
        except FileNotFoundError:
            continue
    return None


def expand_sources(paths, *, recursive=False, cancel=None):
    if not isinstance(paths, (list, tuple)) or not paths or type(recursive) is not bool:
        raise ValueError('Select local audio/video files or folders.')
    found, pending, visited = {}, [], 0
    for value in paths:
        path = local_path(value, exists=True)
        if path.is_dir():
            pending.append(path)
        elif path.suffix.lower() in AUDIO_EXTENSIONS:
            found[str(path)] = path
        else:
            raise ValueError('The selected file is not a supported audio/video container.')
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                stopped(cancel)
                visited += 1
                if visited > 100000:
                    raise ValueError('The selected folder exceeds its inventory budget.')
                path = local_path(entry.path, exists=True)
                if path.is_dir():
                    if recursive:
                        pending.append(path)
                elif path.suffix.lower() in AUDIO_EXTENSIONS:
                    found[str(path)] = path
                if len(found) > MAX_SOURCES:
                    raise ValueError(f'A transcription job supports at most {MAX_SOURCES} sources.')
    result = tuple(found[key] for key in sorted(found))
    if not result or len(result) > MAX_SOURCES:
        raise ValueError('No supported sources were selected, or the source limit was exceeded.')
    return result


def _snapshot(source, target, *, cancel=None, maximum=MAX_SOURCE_BYTES):
    before = regular(source)
    allowed = min(MAX_SOURCE_BYTES, maximum)
    if not 0 < before.st_size <= allowed:
        raise ValueError('Source audio exceeds the remaining retained-job byte budget.')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.pending-source-', dir=target.parent)
    temporary = Path(temporary)
    digest, count = sha256(), 0
    try:
        with source.open('rb') as incoming, os.fdopen(descriptor, 'wb') as outgoing:
            if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
                raise ValueError('The audio changed while opening.')
            while block := incoming.read(1024 * 1024):
                stopped(cancel)
                count += len(block)
                if count > allowed:
                    raise ValueError('The audio grew beyond its byte budget.')
                outgoing.write(block)
                digest.update(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
            if _stamp(os.fstat(incoming.fileno())) != _stamp(before):
                raise ValueError('The audio changed while retaining its original bytes.')
        if count != before.st_size or _stamp(regular(source)) != _stamp(before):
            raise ValueError('The audio changed while retaining its original bytes.')
        expected = {'sha256': digest.hexdigest(), 'size': count}
        if file_identity(temporary, cancel=cancel) != expected:
            raise ValueError('The retained audio copy could not be verified.')
        publish_file(temporary, target, cancelled=cancel or (lambda: False))
        temporary.unlink()
        return expected
    finally:
        temporary.unlink(missing_ok=True)


def _published_identity(path):
    """Verify this call's published file even if its staging hardlink remains.

    This narrow post-publication check does not admit linked inputs. The
    product export writer raises on incomplete staging cleanup; it returns no receipt.
    """
    path = Path(path)
    local_path(path.parent, exists=True)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        raise ValueError('The published export is not a regular file.')
    digest, size = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The published export changed before verification.')
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > 32 * 1024**2:
                raise ValueError('The published export exceeds its byte budget.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('The published export changed during verification.')
    if _stamp(path.lstat()) != _stamp(before):
        raise ValueError('The published export changed during verification.')
    return {'sha256': digest.hexdigest(), 'size': size}


class TranscriptionService:
    def __init__(self, root, *, broker=None, budget=None, stop_grace=2.0):
        self.root = local_path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profiles_root = self.root / 'profiles'
        self.jobs_root = self.root / 'jobs'
        self.profiles_root.mkdir(exist_ok=True)
        self.jobs_root.mkdir(exist_ok=True)
        self.broker = broker
        self.budget = budget or ProcessBudget(timeout=24 * 3600, max_inputs=MAX_SOURCES,
            max_output_bytes=8 * 1024**3, max_staging_files=100000,
            reserve_disk_bytes=64 * 1024**2)
        if type(stop_grace) not in (int, float) or not .05 <= stop_grace <= 30:
            raise ValueError('Invalid cooperative stop allowance.')
        self.stop_grace = float(stop_grace)

    @property
    def library_root(self):
        return self.root.parent.parent if self.root.parent.name == 'state' else self.root

    inspect_runtime = staticmethod(inspect_runtime)
    expand_sources = staticmethod(expand_sources)

    def register_runtime(self, name, python_executable, worker_path, model_directory, *,
                         owner_trusted=False, expected_fingerprint=None, cancel=None, progress=None):
        name = _label(name)
        if type(owner_trusted) is not bool:
            raise ValueError('Runtime trust must be an explicit owner decision.')
        inspected = inspect_runtime(python_executable, worker_path, model_directory,
                                    cancel=cancel, progress=progress)
        if owner_trusted and inspected['fingerprint'] != expected_fingerprint:
            raise ValueError('Inspect and explicitly approve these exact current runtime/model bytes first.')
        identity = inspected['fingerprint'][:32]
        profile = {**inspected, 'id': identity, 'name': name, 'trusted': owner_trusted,
                   'qualification': 'unqualified', 'registered': time.time(), 'restored': False}
        stopped(cancel)
        with state_lock(self.root, 'inventory'), state_lock(self.profiles_root, 'profiles'):
            target = self.profiles_root / (identity + '.json')
            atomic_json(target, profile, replace=target.exists())
        return profile

    def _profile(self, identity):
        value = read_json(self.profiles_root / (_id(identity) + '.json'))
        if value.get('id') != identity or value.get('schema') != 'resonance-transcription-runtime/1':
            raise ValueError('Invalid local transcription profile.')
        return value

    def list_runtimes(self):
        return tuple(self._profile(path.stem) for path in sorted(self.profiles_root.glob('*.json')))

    def _verify_profile(self, profile, *, cancel=None, progress=None):
        if profile.get('trusted') is not True:
            raise PermissionError('Review and explicitly trust the local runtime/model profile before execution.')
        current = inspect_runtime(profile['python_executable'], profile['worker_path'],
                                  profile['model_directory'], cancel=cancel, progress=progress)
        if current['fingerprint'] != profile['fingerprint']:
            raise ValueError('Runtime or model files changed; inspect and register the exact new bytes.')
        return profile

    def _job_root(self, identity):
        return local_path(self.jobs_root / _id(identity), root=self.root)

    @staticmethod
    def _plan(state):
        return fingerprint({name: state[name] for name in ('id', 'profile_id', 'profile_fingerprint', 'sources', 'settings')})

    def _load(self, identity):
        value = read_json(self._job_root(identity) / 'job.json')
        if value.get('schema') != SCHEMA or value.get('id') != identity or value.get('plan_fingerprint') != self._plan(value):
            raise ValueError('Invalid or modified transcription job plan.')
        validate_settings(value['settings'])
        if not isinstance(value['sources'], list) or not 0 < len(value['sources']) <= MAX_SOURCES:
            raise ValueError('Invalid retained transcription sources.')
        if len({_id(source['id']) for source in value['sources']}) != len(value['sources']):
            raise ValueError('Duplicate transcription source identity.')
        return value

    def _save(self, state):
        state['updated'] = time.time()
        target = self._job_root(state['id']) / 'job.json'
        atomic_json(target, state, replace=target.exists())

    def create_job(self, profile_id, sources, *, settings=None, title='Transcription',
                   recursive=False, cancel=None, progress=None):
        with state_lock(self.root, 'inventory'):
            return self._create_job(profile_id, sources, settings=settings, title=title,
                recursive=recursive, cancel=cancel, progress=progress)

    def _create_job(self, profile_id, sources, *, settings=None, title='Transcription',
                    recursive=False, cancel=None, progress=None):
        profile = self._profile(profile_id)
        selected = expand_sources(sources, recursive=recursive, cancel=cancel)
        options = validate_settings(settings)
        total_bytes = sum(regular(path).st_size for path in selected)
        if total_bytes > MAX_JOB_BYTES:
            raise ValueError('The selected sources exceed the 64 GiB job budget.')
        if shutil.disk_usage(self.root).free < total_bytes + self.budget.reserve_disk_bytes:
            raise OSError('Not enough free disk space to retain the selected original audio.')
        identity = uuid4().hex
        job_root = self._job_root(identity)
        job_root.mkdir()
        retained, retained_bytes = [], 0
        try:
            for index, source in enumerate(selected):
                stopped(cancel)
                source_id = uuid4().hex
                relative = f'inputs/{source_id}/source{source.suffix.lower()}'
                if progress:
                    progress({'phase': 'Retaining original audio', 'status': 'running',
                              'done': index, 'total': len(selected), 'current': source.name, 'indeterminate': False})
                value = _snapshot(source, job_root / relative, cancel=cancel,
                                  maximum=MAX_JOB_BYTES - retained_bytes)
                retained_bytes += value['size']
                if retained_bytes > MAX_JOB_BYTES:
                    raise ValueError('Retained sources exceed the complete job byte budget.')
                retained.append({'id': source_id, 'original_path': str(source), 'name': source.name,
                                 'retained_path': relative, **value})
            state = {'schema': SCHEMA, 'id': identity, 'title': _label(title), 'status': 'pending',
                     'profile_id': profile_id, 'profile_fingerprint': profile['fingerprint'],
                     'sources': retained, 'settings': options, 'results': [], 'edits': {},
                     'done': 0, 'total': len(retained), 'error': '', 'created': time.time(),
                     'recovery_review_required': False, 'restored': False}
            state['plan_fingerprint'] = self._plan(state)
            self._save(state)
            atomic_json(job_root / 'control.json', {'action': 'run'})
            return state
        except BaseException:
            # This is a new uncommitted private staging directory only; originals
            # and every previously committed job remain untouched.
            if not (job_root / 'job.json').exists():
                shutil.rmtree(job_root)
            raise

    def get_job(self, identity):
        value = self._load(identity)
        if value['status'] in ACTIVE:
            try:
                with state_lock(self._job_root(identity), 'run'):
                    value = self._load(identity)
                    if value['status'] in ACTIVE:
                        value['status'] = 'interrupted'
                        value['recovery_review_required'] = True
                        value['error'] = 'The previous runner ended without closure. Confirm its worker stopped before resuming.'
                        self._save(value)
            except RuntimeError:
                pass  # A live process still owns the durable run lock.
        return value

    def list_jobs(self):
        return tuple(self.get_job(path.parent.name) for path in sorted(self.jobs_root.glob('*/job.json')))

    get = get_job
    jobs = list_jobs

    def _control(self, identity):
        path = self._job_root(identity) / 'control.json'
        value = read_json(path) if path.exists() else {'action': 'run'}
        if value.get('action') not in ('run', 'pause', 'cancel'):
            raise ValueError('Invalid transcription control state.')
        return value['action']

    def _set_control(self, identity, action):
        self._load(identity)
        job_root = self._job_root(identity)
        with state_lock(job_root, 'control'):
            atomic_json(job_root / 'control.json', {'action': action}, replace=True)
        value = self._load(identity)
        value['status'] = 'pausing' if action == 'pause' else 'cancelling'
        return value

    def pause(self, identity):
        return self._set_control(identity, 'pause')

    def cancel(self, identity):
        return self._set_control(identity, 'cancel')

    def acknowledge_interruption(self, identity, *, worker_stopped=False):
        if worker_stopped is not True:
            raise ValueError('Confirm the previous external worker stopped before retrying this job.')
        with state_lock(self._job_root(identity), 'run'):
            state = self._load(identity)
            state['recovery_review_required'] = False
            if state['status'] != 'complete':
                state['status'] = 'paused'
            self._save(state)
            return state

    def rebind_runtime(self, identity, profile_id):
        job_root = self._job_root(identity)
        with state_lock(job_root, 'run'):
            state = self._load(identity)
            if state['status'] in ACTIVE or (state.get('recovery_review_required') and not state.get('restored')):
                raise RuntimeError('Confirm the interrupted external worker stopped before rebinding this job.')
            profile = self._profile(profile_id)
            if profile.get('trusted') is not True:
                raise PermissionError('Choose an explicitly reviewed local runtime first.')
            previous = self._profile(state['profile_id'])
            if (profile['model_content_fingerprint'] != previous['model_content_fingerprint'] or
                    profile['worker_identity'] != previous['worker_identity'] or
                    profile.get('protocol') != previous.get('protocol') or
                    profile.get('verified_implementation', [])[:2] != previous.get('verified_implementation', [])[:2]):
                raise ValueError('Resuming requires the same model content and worker; create a new job for a different engine/model.')
            if profile['fingerprint'] != previous['fingerprint'] or state.get('restored'):
                worker = job_root / 'worker'
                if os.path.lexists(worker):
                    regular(local_path(worker, root=job_root, exists=True), directory=True)
                    history = local_path(job_root / 'worker-history', root=job_root)
                    history.mkdir(exist_ok=True)
                    local_path(history, root=job_root, exists=True)
                    archived = history / (previous['id'] + '-' + uuid4().hex)
                    if os.path.lexists(archived):
                        raise FileExistsError('The previous worker workspace history already exists.')
                    # The worker checkpoint does not report every base/native
                    # support file in our admission profile. Never relabel its
                    # old raw cache with a newly reviewed profile. Keep it for
                    # recovery evidence; accepted artifacts stay in place.
                    worker.rename(archived)
                    sync_directory(history)
                    sync_directory(job_root)
            state['profile_id'], state['profile_fingerprint'] = profile_id, profile['fingerprint']
            state['plan_fingerprint'] = self._plan(state)
            if state.get('restored'):
                state['recovery_review_required'] = False
            self._save(state)
            return state

    def continue_job(self, identity):
        job_root = self._job_root(identity)
        with state_lock(job_root, 'run'):
            state = self._load(identity)
            if state['status'] in ACTIVE:
                state.update(status='interrupted', recovery_review_required=True)
                self._save(state)
                raise RuntimeError('The prior runner did not close; confirm its worker stopped before resuming.')
            if state.get('recovery_review_required') and not state.get('restored'):
                raise RuntimeError('Confirm the interrupted worker stopped before resuming.')
            atomic_json(job_root / 'control.json', {'action': 'run'}, replace=True)
            for name in ('pause.flag', 'stop.flag', 'cancel.flag', 'job_status.json'):
                path = job_root / 'worker' / name
                if path.exists() or path.is_symlink():
                    local_path(path, root=job_root, exists=True).unlink()
            if state['status'] != 'complete':
                state['status'] = 'pending'
                state['error'] = ''
                self._save(state)
            return state

    def resume(self, identity, *, progress=None, cancel=None):
        self.continue_job(identity)
        return self.run(identity, progress=progress, cancel=cancel)

    def _source_path(self, state, source):
        return local_path(self._job_root(state['id']) / source['retained_path'],
                          root=self._job_root(state['id']), exists=True)

    def _verify_source(self, state, source, *, cancel=None):
        if file_identity(self._source_path(state, source), cancel=cancel, maximum=MAX_SOURCE_BYTES) != {
                'sha256': source['sha256'], 'size': source['size']}:
            raise ValueError('Retained source audio changed; it was not silently replaced.')

    def _receipt(self, state, result):
        root = self._job_root(state['id'])
        path = local_path(root / result['receipt'], root=root, exists=True)
        if digest_file(path) != result['sha256']:
            raise ValueError('The saved transcript receipt changed.')
        receipt = read_json(path)
        if receipt.get('schema') != 'resonance-transcription-result/1' or receipt.get('source_id') != result['source_id']:
            raise ValueError('Invalid saved transcript receipt.')
        source = next((x for x in state['sources'] if x['id'] == result['source_id']), None)
        if source is None or receipt['provenance']['source_sha256'] != source['sha256']:
            raise ValueError('The transcript belongs to a different retained source.')
        for record in receipt['files'].values():
            target = local_path(root / record['path'], root=root, exists=True)
            if file_identity(target, maximum=MAX_RESULT_BYTES) != {'sha256': record['sha256'], 'size': record['size']}:
                raise ValueError('A completed transcript export changed; the existing file was preserved.')
        return receipt

    def _publish_result(self, state, source, result, provenance):
        if state['settings']['include_headers']:
            provenance = {**provenance, 'text_headers': 'derived-v1'}
        root = self._job_root(state['id'])
        folder = root / 'artifacts' / source['id'] / uuid4().hex
        folder.mkdir(parents=True)
        files = {}
        for format, payload in encoded_outputs(result, provenance).items():
            if not state['settings']['output_' + format]:
                continue
            if len(payload) > MAX_RESULT_BYTES:
                raise ValueError('The transcript export exceeds its byte budget.')
            target = folder / ('transcript.' + format)
            atomic_bytes(target, payload)
            actual = file_identity(target, maximum=MAX_RESULT_BYTES)
            expected = {'sha256': sha256(payload).hexdigest(), 'size': len(payload)}
            if actual != expected:
                raise OSError('A transcript export failed its durable byte verification.')
            files[format] = {'path': target.relative_to(root).as_posix(), **actual}
        receipt = {'schema': 'resonance-transcription-result/1', 'source_id': source['id'],
                   'result': result, 'provenance': provenance, 'files': files,
                   'created': time.time(), 'status': 'complete'}
        target = folder / 'receipt.json'
        atomic_json(target, receipt)
        record = {'source_id': source['id'], 'receipt': target.relative_to(root).as_posix(),
                  'sha256': digest_file(target), 'formats': list(files), 'language': result['language'],
                  'duration': result['duration'], 'segments': len(result['segments'])}
        self._receipt(state, record)
        state['results'].append(record)
        state['done'] = len(state['results'])
        self._save(state)

    def _verified_reader(self, state, profile, expected_settings, *, sources=None, status=None):
        worker_root = self._job_root(state['id']) / 'worker'
        if status is None:
            status, _ = verified_protocol.read_json(worker_root / 'job_status.json', maximum=256 * 1024)
        selected = state['sources'] if sources is None else sources
        bound = {str(self._source_path(state, source)): source for source in selected}
        return verified_protocol.ReceiptReader(worker_root,
            verified_protocol.result_directory(worker_root, status), bound, expected_settings, profile)

    def _admit_verified_results(self, state, profile, expected_settings, *, status=None):
        worker_root = self._job_root(state['id']) / 'worker'
        if not (worker_root / 'job_status.json').exists():
            return  # No guessed result namespace or loose-cache adoption.
        accepted = {result['source_id'] for result in state['results']}
        sources = [source for source in state['sources'] if source['id'] not in accepted]
        if not sources:
            return
        reader = self._verified_reader(state, profile, expected_settings, sources=sources, status=status)
        by_id = {source['id']: source for source in sources}
        for checkpoint in reader.checkpoints():
            source = by_id[checkpoint.source_id]
            provenance = {
                'derived': True, 'citation_authority': False, 'quality': 'unqualified',
                'source_sha256': source['sha256'], 'source_bytes': source['size'],
                'profile_id': profile['id'], 'profile_fingerprint': profile['fingerprint'],
                'model_content_fingerprint': profile['model_content_fingerprint'],
                'worker_sha256': profile['worker_identity']['sha256'],
                'python_sha256': profile['python_identity']['sha256'],
                'runtime_identity_sha256': fingerprint(checkpoint.request['runtime']),
                'worker_checkpoint_sha256': checkpoint.cache['sha256'],
                'worker_export_receipt_sha256': checkpoint.receipt['sha256'],
                'worker_protocol': verified_protocol.PROTOCOL,
                'settings': state['settings'],
                'effective_settings': {**expected_settings, **checkpoint.execution},
                'translation_target': 'en' if expected_settings['task'] == 'translate' else None,
                'diarization': False,
            }
            self._publish_result(state, source, checkpoint.result, provenance)

    def _admit_results(self, state, profile, expected_settings):
        """Admit atomic worker recognition checkpoints into app-owned receipts.

        Accepted artifacts live outside cache, so ordinary data backup preserves
        them even if the optional worker cache is omitted.
        """
        if profile.get('protocol') == verified_protocol.PROTOCOL:
            return self._admit_verified_results(state, profile, expected_settings)
        if profile.get('protocol') != 'local-file-job/1':
            raise ValueError('Unsupported reviewed transcription worker protocol.')
        root = self._job_root(state['id'])
        cache_root = root / 'worker' / 'logs' / 'cache'
        if not cache_root.exists():
            return
        accepted = {result['source_id'] for result in state['results']}
        sources = {str(self._source_path(state, source)): source for source in state['sources'] if source['id'] not in accepted}
        count = 0
        for marker in cache_root.rglob('*.cache.json'):
            count += 1
            if count > MAX_SOURCES * 16:
                raise ValueError('The worker cache exceeds its bounded checkpoint inventory.')
            marker = local_path(marker, root=cache_root, exists=True)
            checkpoint = read_json(marker)
            identity = checkpoint.get('identity', {})
            source = sources.get(identity.get('source', {}).get('path'))
            if source is None or source['id'] in accepted:
                continue
            if identity.get('settings') != expected_settings:
                continue  # An older, differently configured checkpoint is not reused.
            key = marker.name[:-len('.cache.json')]
            if not re.fullmatch('[0-9a-f]{64}', key) or key != _protocol_digest(identity):
                raise ValueError('The worker checkpoint does not match its content identity.')
            if (identity.get('source', {}).get('sha256') != source['sha256'] or
                    identity.get('source', {}).get('size') != source['size']):
                raise ValueError('The worker checkpoint used different source audio.')
            model, runtime = identity.get('model', {}), identity.get('runtime', {})
            if (model.get('files') != profile['model_files'] or model.get('name') != profile['model_name']
                    or model.get('root') != profile['model_directory']
                    or runtime.get('worker') != profile['worker_identity']
                    or runtime.get('executable') != profile['python_identity']
                    or runtime.get('packages') != profile['runtime_distributions']):
                raise ValueError('The worker checkpoint does not match the selected runtime/model identity.')
            self._verify_source(state, source)
            text_path, meta_path = marker.with_name(key + '.txt'), marker.with_name(key + '.json')
            if (file_identity(text_path, maximum=MAX_RESULT_BYTES) != checkpoint.get('text') or
                    file_identity(meta_path, maximum=MAX_RESULT_BYTES) != checkpoint.get('meta')):
                raise ValueError('A worker recognition checkpoint was modified or incomplete.')
            meta = read_json(meta_path)
            result = validated_result(meta, text_path.read_text(encoding='utf-8'))
            effective = meta.get('effective_settings', meta.get('settings'))
            validate_effective_settings(effective, state['settings'], profile['model_name'])
            provenance = {'derived': True, 'citation_authority': False, 'quality': 'unqualified',
                          'source_sha256': source['sha256'], 'source_bytes': source['size'],
                          'profile_id': profile['id'], 'profile_fingerprint': profile['fingerprint'],
                          'model_content_fingerprint': profile['model_content_fingerprint'],
                          'worker_sha256': profile['worker_identity']['sha256'],
                          'python_sha256': profile['python_identity']['sha256'],
                          'runtime_identity_sha256': fingerprint(runtime),
                          'worker_checkpoint_sha256': digest_file(marker),
                          'settings': state['settings'], 'effective_settings': effective,
                          'translation_target': 'en' if expected_settings['task'] == 'translate' else None,
                          'diarization': False}
            self._publish_result(state, source, result, provenance)
            accepted.add(source['id'])

    def run(self, identity, *, progress=None, cancel=None):
        from ..operations import ResourceBroker, ResourceRequest
        observer = progress
        def progress(event):
            # Notifications have no admission or publication authority. A UI
            # failure must not turn a durable accepted result into failed work.
            if observer is not None:
                try:
                    observer(event)
                except Exception:
                    pass
        job_root = self._job_root(identity)
        with state_lock(job_root, 'run'):
            state = self._load(identity)
            if state['status'] in ACTIVE:
                state.update(status='interrupted', recovery_review_required=True)
                self._save(state)
                raise RuntimeError('The prior runner did not close; confirm its worker stopped before resuming.')
            for result in state['results']:
                self._receipt(state, result)
            if state['status'] == 'complete':
                return state
            if state.get('recovery_review_required') and not state.get('restored'):
                raise RuntimeError('Confirm the interrupted external worker stopped before resuming.')
            profile = self._profile(state['profile_id'])
            stop_started, last_report, last_admit = None, None, 0.0
            worker_run_id = None
            verified = profile.get('protocol') == verified_protocol.PROTOCOL
            expected_settings = worker_settings(state['settings'], profile['model_name'])
            worker_root = job_root / 'worker'
            worker_root.mkdir(exist_ok=True)
            for name in ('results', 'logs'):
                (worker_root / name).mkdir(exist_ok=True)

            def report(phase, *, terminal=False):
                nonlocal last_report
                event = {'phase': phase, 'status': state['status'], 'done': state['done'],
                         'total': state['total'], 'current': state.get('current', ''),
                         'indeterminate': not terminal and state['done'] == state['total']}
                key = (phase, event['status'], event['done'], event['current'])
                if progress and key != last_report:
                    progress(event)
                    last_report = key

            def action():
                selected = self._control(identity)
                if selected == 'run' and cancel is not None and cancel():
                    atomic_json(job_root / 'control.json', {'action': 'cancel'}, replace=True)
                    selected = 'cancel'
                return selected

            def poll():
                nonlocal stop_started, last_admit, worker_run_id
                selected = action()
                if selected != 'run':
                    state['status'] = 'pausing' if selected == 'pause' else 'cancelling'
                    if stop_started is None:
                        stop_started = time.monotonic()
                        atomic_bytes(worker_root / 'stop.flag', b'1', replace=(worker_root / 'stop.flag').exists())
                        if selected == 'pause':
                            atomic_bytes(worker_root / 'pause.flag', b'1', replace=(worker_root / 'pause.flag').exists())
                        elif verified:
                            atomic_bytes(worker_root / 'cancel.flag', b'1', replace=(worker_root / 'cancel.flag').exists())
                        self._save(state)
                    report('Stopping at the next worker checkpoint')
                    return time.monotonic() - stop_started >= self.stop_grace
                status_path = worker_root / 'job_status.json'
                current = _observe_worker_status(status_path)
                if current is not None:
                    if type(current) is not dict or not isinstance(current.get('state'), str):
                        raise ValueError('The worker returned invalid status.')
                    for name in ('done', 'failed', 'total'):
                        if type(current.get(name)) is not int or not 0 <= current[name] <= len(state['sources']):
                            raise ValueError('The worker returned invalid progress counts.')
                    if verified:
                        run_id = current.get('run_id')
                        if not isinstance(run_id, str) or not re.fullmatch('[0-9a-f]{32}', run_id):
                            raise ValueError('The verified worker must identify its current run.')
                        if worker_run_id is not None and worker_run_id != run_id:
                            raise ValueError('The worker run identity changed during execution.')
                        worker_run_id = run_id
                    state['current'] = str(current.get('current', ''))[:2048]
                if time.monotonic() - last_admit >= .25:
                    if verified:
                        if current is not None:
                            self._admit_verified_results(state, profile, expected_settings, status=current)
                    else:
                        self._admit_results(state, profile, expected_settings)
                    last_admit = time.monotonic()
                report('Transcribing locally; verified files are retained')
                return False

            try:
                if state['settings']['device'] == 'cuda':
                    if os.environ.get('CUDA_VISIBLE_DEVICES') not in (None, '0') or os.environ.get('CUDA_DEVICE_ORDER'):
                        raise ValueError('CUDA execution requires the default unmasked device order for the cuda:0 reservation; review CUDA_VISIBLE_DEVICES/CUDA_DEVICE_ORDER.')
                self._verify_profile(profile, cancel=lambda: action() != 'run', progress=progress)
                if profile['fingerprint'] != state['profile_fingerprint']:
                    raise ValueError('The job is bound to a different reviewed runtime profile.')
                for source in state['sources']:
                    self._verify_source(state, source, cancel=lambda: action() != 'run')
                self._admit_results(state, profile, expected_settings)
                pending = [source for source in state['sources'] if source['id'] not in {x['source_id'] for x in state['results']}]
                if verified and not pending:
                    status_path = worker_root / 'job_status.json'
                    previous_status = read_json(status_path) if status_path.exists() else {}
                    if previous_status.get('result_state') == 'completed' and previous_status.get('completion') is not None:
                        planned_ids = state.get('worker_plan_sources', [source['id'] for source in state['sources']])
                        if (type(planned_ids) is not list or not planned_ids or
                                len(set(planned_ids)) != len(planned_ids)):
                            raise ValueError('Invalid previous worker source plan.')
                        planned = [source for source in state['sources'] if source['id'] in planned_ids]
                        if len(planned) != len(planned_ids):
                            raise ValueError('Previous worker plan refers to an unknown retained input.')
                        reader = self._verified_reader(state, profile, expected_settings,
                                                       sources=planned, status=previous_status)
                        _, completion = reader.completion(previous_status, job_name=state['title'])
                        state['worker_completion'] = {'run_id': previous_status['run_id'], 'receipt': completion,
                            'source_ids': planned_ids, 'profile_fingerprint': profile['fingerprint']}
                    else:
                        # All sealed inputs can survive a failed final write.
                        # Re-run the cached handoff to produce actual completion.
                        pending = list(state['sources'])
                if not pending:
                    state.update(status='complete', done=state['total'], error='')
                    self._save(state)
                    report('All requested transcript exports verified', terminal=True)
                    return state
                for name in ('pause.flag', 'stop.flag', 'cancel.flag', 'job_status.json'):
                    path = worker_root / name
                    if path.exists() or path.is_symlink():
                        local_path(path, root=worker_root, exists=True).unlink()
                configuration = {'job_name': state['title'],
                    'folders': [str(self._source_path(state, source).parent) for source in pending],
                    'models_dir': str(Path(profile['model_directory']).parent),
                    'results_root': str(worker_root / 'results'), 'settings': expected_settings}
                atomic_json(worker_root / 'job_config.json', configuration, replace=(worker_root / 'job_config.json').exists())
                atomic_json(worker_root / 'settings_live.json', {}, replace=(worker_root / 'settings_live.json').exists())
                if verified:
                    state['worker_plan_sources'] = [source['id'] for source in pending]
                state.update(status='running', error='', recovery_review_required=False, restored=False)
                self._save(state)
                report('Waiting for local CPU, memory and disk resources')
                broker = self.broker or ResourceBroker.for_app(self.library_root)
                device = state['settings']['device']
                memory = max(256 * 1024**2, profile['model_bytes'] * 2 + 128 * 1024**2)
                disk = min(self.budget.max_output_bytes, sum(x['size'] for x in pending) + len(pending) * 16 * 1024**2)
                request = ResourceRequest(cpu_slots=state['settings']['parallel_files'], memory_bytes=memory,
                    gpu_device=None if device == 'cpu' else 'cuda:0',
                    gpu_memory_bytes=0 if device == 'cpu' else memory,
                    disk_bytes=disk, disk_path=job_root, devices=('heavy-media',))
                environment = dict(os.environ)
                for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'):
                    environment.pop(name, None)
                if verified:
                    for name in tuple(environment):
                        if name in ('LD_PRELOAD', 'LD_LIBRARY_PATH', '__PYVENV_LAUNCHER__', '_PYTHON_PROJECT_BASE') or name.startswith('DYLD_'):
                            environment.pop(name, None)
                environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                                   HF_HUB_DISABLE_TELEMETRY='1', PYTHONUTF8='1')
                process_active = False
                with broker.reserve(request, operation_id='asr:' + identity,
                                    cancelled=lambda: not process_active and action() != 'run', timeout=60) as lease:
                    # Admission can wait behind another task. Revalidate after
                    # that wait so changed code/weights never inherit old trust.
                    self._verify_profile(profile, cancel=lambda: action() != 'run', progress=progress)
                    process_active = True
                    def checked_poll():
                        lease.checkpoint()
                        return poll()
                    # Isolated mode ignores PYTHON* environment variables.
                    # Set UTF-8 explicitly for protocol paths and worker pipes.
                    if verified:
                        from .verified_runtime import launch_arguments
                        arguments = launch_arguments(profile, worker_root, nonce=uuid4().hex)
                    else:
                        arguments = [profile['python_executable'], '-I', '-X', 'utf8',
                                     profile['worker_path'], '--job-dir', str(worker_root)]
                    run_process(arguments, budget=self.budget,
                                cancelled=checked_poll, watch=(worker_root / 'logs', worker_root / 'results'),
                                env=environment, cwd=worker_root)
                selected = action()
                if selected != 'run':
                    state['status'] = 'paused' if selected == 'pause' else 'cancelled'
                else:
                    self._verify_profile(profile)
                    self._admit_results(state, profile, expected_settings)
                    status = read_json(worker_root / 'job_status.json')
                    if verified:
                        reader = self._verified_reader(state, profile, expected_settings,
                                                       sources=pending, status=status)
                        _, completion = reader.completion(status, run_id=worker_run_id, job_name=state['title'])
                        state['worker_completion'] = {'run_id': status['run_id'], 'receipt': completion,
                            'source_ids': [source['id'] for source in pending],
                            'profile_fingerprint': profile['fingerprint']}
                    if status.get('state') != 'завершено' or status.get('failed') != 0 or state['done'] != state['total']:
                        raise ValueError('The worker exited without a complete set of verified requested results.')
                    state['status'] = 'complete'
                state['error'] = ''
            except Exception as error:
                selected = action()
                if selected != 'run':
                    state['status'] = 'paused' if selected == 'pause' else 'cancelled'
                    state['error'] = 'Completed verified files are retained; resume restarts only unfinished sources.'
                else:
                    try:
                        self._verify_profile(profile)
                        self._admit_results(state, profile, expected_settings)
                    except Exception as invalid:
                        state['error'] = str(error) + ' Checkpoint admission: ' + str(invalid)
                    state['status'] = 'partial' if state['results'] else 'failed'
                    state['error'] = state['error'] or str(error)
            self._save(state)
            report('Transcript job ' + state['status'], terminal=True)
            return state

    def read_transcript(self, identity, source_id=None, *, edited=False):
        state = self._load(identity)
        if source_id is None:
            if len(state['results']) != 1:
                raise ValueError('Choose the source transcript to read.')
            source_id = state['results'][0]['source_id']
        source = next((source for source in state['sources'] if source['id'] == source_id), None)
        result = next((result for result in state['results'] if result['source_id'] == source_id), None)
        if source is None or result is None:
            raise FileNotFoundError('This source does not yet have a verified transcript.')
        self._verify_source(state, source)
        receipt = self._receipt(state, result)
        if edited:
            record = state.get('edits', {}).get(source_id)
            if record is None:
                raise FileNotFoundError('No edited reading copy exists for this transcript.')
            path = local_path(self._job_root(identity) / record['path'], root=self._job_root(identity), exists=True)
            if digest_file(path) != record['sha256']:
                raise ValueError('The saved edited reading copy changed.')
            return read_json(path)
        return {'text': receipt['result']['text'], 'source_id': source_id, 'edited': False,
                'provenance': receipt['provenance'], 'segments': receipt['result']['segments'],
                'language': receipt['result']['language'], 'duration': receipt['result']['duration']}

    def save_edit(self, identity, source_id, text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_RESULT_BYTES or '\x00' in text:
            raise ValueError('The edited reading copy exceeds its text budget.')
        with state_lock(self._job_root(identity), 'run'):
            original = self.read_transcript(identity, source_id)
            state = self._load(identity)
            edit_id = uuid4().hex
            relative = f'edits/{_id(source_id)}/{edit_id}.json'
            value = {'id': edit_id, 'source_id': source_id, 'text': text, 'edited': True,
                     'provenance': {**original['provenance'], 'revision_kind': 'user-edited-transcript',
                                    'original_transcript_sha256': sha256(original['text'].encode('utf-8')).hexdigest()},
                     'created': time.time(), 'path': relative}
            target = self._job_root(identity) / relative
            atomic_json(target, value)
            checksum = digest_file(target)
            state.setdefault('edits', {})[source_id] = {'id': edit_id, 'path': relative, 'sha256': checksum}
            self._save(state)
            return {**value, 'sha256': checksum}

    def export(self, identity, directory, *, formats=None, include_edits=True, cancel=None):
        state = self._load(identity)
        selected = tuple(formats) if formats is not None else tuple(name for name in FORMATS if state['settings']['output_' + name])
        if not selected or len(set(selected)) != len(selected) or any(name not in FORMATS for name in selected):
            raise ValueError('Choose distinct TXT, SRT, VTT or JSON exports.')
        destination = local_path(directory, exists=True)
        regular(destination, directory=True)
        if destination.is_relative_to(self.library_root) or self.library_root.is_relative_to(destination):
            raise ValueError('Choose a separate export directory outside the entire private library.')
        files, warnings = [], []
        for result in state['results']:
            stopped(cancel)
            receipt = self._receipt(state, result)
            source = next(source for source in state['sources'] if source['id'] == result['source_id'])
            self._verify_source(state, source, cancel=cancel)
            for name in selected:
                if name not in receipt['files']:
                    raise ValueError('This format was not requested for the job; create a reviewed export plan first.')
                record = receipt['files'][name]
                path = self._job_root(identity) / record['path']
                target = destination / f"transcript-{identity[:8]}-{result['source_id'][:12]}.{name}"
                write_binary_export(target, path.read_bytes(), private_root=self.library_root,
                                                cancelled=cancel or (lambda: False), require_absent=True)
                if _published_identity(target) != {'sha256': record['sha256'], 'size': record['size']}:
                    raise OSError('An exported transcript failed byte verification.')
                files.append({'path': str(target), 'source_id': result['source_id'], 'format': name,
                              'sha256': record['sha256'], 'size': record['size'], 'edited': False})
            if include_edits and result['source_id'] in state.get('edits', {}):
                edited = self.read_transcript(identity, result['source_id'], edited=True)
                payload = edited['text'].encode('utf-8')
                target = destination / f"transcript-{identity[:8]}-{result['source_id'][:12]}-edited.txt"
                write_binary_export(target, payload, private_root=self.library_root,
                                           cancelled=cancel or (lambda: False), require_absent=True)
                if _published_identity(target) != {'sha256': sha256(payload).hexdigest(), 'size': len(payload)}:
                    raise OSError('The edited transcript export failed byte verification.')
                files.append({'path': str(target), 'source_id': result['source_id'], 'format': 'txt',
                              'sha256': sha256(payload).hexdigest(), 'size': len(payload), 'edited': True,
                              'provenance': edited['provenance']})
        if not files:
            raise ValueError('No verified transcript results are available to export.')
        manifest = {'schema': 'resonance-transcription-export/1', 'job_id': identity,
                    'status': 'complete', 'job_status': state['status'], 'files': files,
                    'derived': True, 'citation_authority': False, 'warnings': warnings}
        target = destination / f'transcripts-{identity[:12]}-manifest.json'
        write_binary_export(target, encode(manifest), private_root=self.library_root,
                                     cancelled=cancel or (lambda: False), require_absent=True)
        manifest['manifest_path'] = str(target)
        manifest['manifest_sha256'] = _published_identity(target)['sha256']
        return manifest


def prepare_restored_state(state_root, destination_root=None):
    """Revoke external execution trust and mark restored work for explicit review.

    ``state_root`` is the complete staged state directory. No external runtime,
    model or original path is opened, copied, executed or modified here.
    """
    root = local_path(Path(state_root) / 'transcription', exists=True)
    profiles = jobs = 0
    for path in sorted((root / 'profiles').glob('*.json')):
        value = read_json(path)
        if value.get('schema') != 'resonance-transcription-runtime/1':
            raise ValueError('Invalid restored transcription profile.')
        value.update(trusted=False, restored=True)
        atomic_json(path, value, replace=True)
        profiles += 1
    for path in sorted((root / 'jobs').glob('*/job.json')):
        value = read_json(path)
        if value.get('schema') != SCHEMA or value.get('plan_fingerprint') != TranscriptionService._plan(value):
            raise ValueError('Invalid restored transcription job.')
        if value['status'] in ACTIVE or value['status'] in ('pending', 'preparing', 'interrupted'):
            value['status'] = 'interrupted'
        value.update(restored=True, recovery_review_required=True)
        if value['status'] != 'complete':
            value['error'] = 'Restored job: review and register the local runtime/model before resuming.'
        atomic_json(path, value, replace=True)
        atomic_json(path.parent / 'control.json', {'action': 'run'}, replace=(path.parent / 'control.json').exists())
        workspaces = [path.parent / 'worker']
        history = path.parent / 'worker-history'
        if os.path.lexists(history):
            regular(local_path(history, root=root, exists=True), directory=True)
            for count, archived in enumerate(history.iterdir(), 1):
                if count > 1024 or not re.fullmatch('[0-9a-f]{32}-[0-9a-f]{32}', archived.name):
                    raise ValueError('Invalid or excessive restored worker workspace history.')
                regular(local_path(archived, root=root, exists=True), directory=True)
                workspaces.append(archived)
        for worker in workspaces:
            for name in ('job_config.json', 'settings_live.json', 'job_status.json', 'pause.flag', 'stop.flag', 'cancel.flag'):
                candidate = worker / name
                if candidate.exists() or candidate.is_symlink():
                    local_path(candidate, root=root, exists=True).unlink()
        jobs += 1
    return {'profiles_revoked': profiles, 'jobs_reviewed': jobs, 'external_files_executed': False}
