"""Owned snapshots and a bounded supervisor for the supplied private worker."""
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

from ..audio.engines import _stop
from ..audio.jobs import atomic_json, checked_directory, project_lock
from ..audio.model_manifest import local_path
from ..file_publication import publish_directory_no_replace
from .files import (canonical, copy_snapshot, file_digest, read_bytes, read_json,
                    regular, sync_parent, write_new)
from .models import (INPUT_SUFFIXES, PROTOCOL, PreparedTranscription,
    TranscriptionBudget, TranscriptionControl, TranscriptionOptions, TranscriptionProgress,
    TranscriptionReceipt, checksum, identity)
from .protocol import preflight_outputs, verify_outputs
from .runtime import TranscriptionRuntime, environment
from .validation import (RESTORED_BYTES, RESTORED_NAME, validate_request,
                         validate_receipt, validate_restored)


MAX_REQUEST_BYTES = 8 * 1024**2
MAX_STATUS_BYTES = 2 * 1024**2
MAX_PROCESS_LOG_BYTES = 1024**2


class _Guard:
    def __init__(self, service, seconds, cancelled):
        self.service, self.cancelled = service, cancelled
        self.deadline = time.monotonic() + seconds
        self.stopped = False

    def check(self):
        self.service._bound()
        self.stopped = self.stopped or bool(self.cancelled())
        self.service._bound()  # A callback may itself change the data root.
        if self.stopped:
            raise InterruptedError('Transcription stopped; completed worker checkpoints remain available.')
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Transcription exceeded the approved time budget.')

    def remaining(self):
        self.check()
        return max(.001, self.deadline - time.monotonic())


class TranscriptionService:
    """Each instance is bound to one existing host and physical data directory.

Worker files are private implementation state. Only an independently validated
receipt exposes output names; recognized text is always a derived asset.
"""
    def __init__(self, app):
        from ..search.federated import _data_identity
        self.app = app
        self.root = checked_directory(app.root)
        info = self.root.stat()
        self._root_identity = info.st_dev, info.st_ino
        self._data_identity = _data_identity(app)
        self.jobs_root = self.root / 'transcription-jobs'

    def _bound(self):
        from ..search.federated import _data_identity
        try:
            current = checked_directory(self.app.root)
            info = current.stat()
            valid = (current == self.root and (info.st_dev, info.st_ino) == self._root_identity
                     and _data_identity(self.app) == self._data_identity)
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise PermissionError('The application data directory changed; reopen transcription.')

    def _workspace(self, workspace):
        self._bound()
        if not isinstance(workspace, str) or not workspace:
            raise ValueError('Select an existing library for the transcription job.')
        with self.app.read(workspace):
            pass

    def _directory(self, job_id):
        self._bound()
        return checked_directory(self.jobs_root / identity(job_id))

    @staticmethod
    def _emit(callback, phase, completed, total, state='running'):
        callback(TranscriptionProgress(phase, completed, total, state))

    def prepare(self, workspace, manifest, sources, *, options=None, budget=None,
                cancelled=lambda: False, on_progress=lambda event: None, _display_names=None):
        """Snapshot explicit files. This does not run recognition or edit originals."""
        options = TranscriptionOptions() if options is None else options
        budget = TranscriptionBudget() if budget is None else budget
        if type(options) is not TranscriptionOptions or type(budget) is not TranscriptionBudget:
            raise TypeError('Use typed transcription options and budgets.')
        if type(sources) not in (tuple, list) or not 1 <= len(sources) <= budget.max_sources:
            raise ValueError('Choose a bounded explicit list of audio/video files.')
        if _display_names is not None and (type(_display_names) is not tuple or len(_display_names) != len(sources)
                or any(not isinstance(name, str) or not 1 <= len(name) <= 512
                       or any(ord(c) < 32 for c in name) for name in _display_names)):
            raise ValueError('Invalid retained source display names.')
        self._workspace(workspace)
        guard = _Guard(self, budget.max_seconds, cancelled)
        guard.check()
        runtime = TranscriptionRuntime.load(manifest)
        runtime.validate_options(options)
        if runtime.manifest_path.is_relative_to(self.jobs_root):
            raise ValueError('Install the reviewed runtime separately from job-controlled files.')
        checked_directory(self.jobs_root, create=True)
        job_id = uuid4().hex
        destination = self.jobs_root / job_id
        stage = Path(tempfile.mkdtemp(prefix='.preparing-', dir=self.jobs_root))
        try:
            for directory in ('inputs', 'results', 'logs', 'cache', 'temp', 'worker-lock'):
                (stage / directory).mkdir(mode=0o700)
            self._emit(on_progress, 'Verifying the selected local installation', 0, len(sources))
            runtime.verify(check=guard.check, timeout=min(30, guard.remaining()), probe_directory=stage)
            snapshots, seen, total = [], set(), 0
            for number, source in enumerate(sources, 1):
                guard.check()
                path = local_path(source)
                info = regular(path)
                key = info.st_dev, info.st_ino
                if key in seen:
                    raise ValueError('Select each physical input file only once.')
                seen.add(key)
                if path.suffix.lower() not in INPUT_SUFFIXES:
                    raise ValueError('The selected file format is not supported by this worker protocol.')
                if (len(path.name) > 512 or any(ord(c) < 32 for c in path.name)
                        or path.is_relative_to(stage)):
                    raise ValueError('Invalid selected source name.')
                name = f'source-{number:05d}' + path.suffix.lower()
                self._emit(on_progress, 'Copying an explicit source snapshot', number - 1, len(sources))
                payload = copy_snapshot(path, stage / 'inputs' / name,
                                        budget.max_input_bytes - total, guard.check)
                total += payload['bytes']
                display_name = _display_names[number - 1] if _display_names is not None else path.name
                snapshots.append({'name': name, 'display_name': display_name, **payload})
            request = {'schema': 'resonance-transcription-request/1', 'protocol': PROTOCOL,
                'id': job_id, 'data_root': str(self.root), 'workspace': workspace,
                'profile_path': str(runtime.manifest_path), 'profile_sha256': runtime.manifest_sha256,
                'model_id': runtime.model_id, 'model_revision': runtime.revision,
                'options': options.to_dict(), 'budget': budget.to_dict(), 'sources': snapshots}
            raw = canonical(request)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError('The approved request exceeds the metadata budget.')
            write_new(stage / 'request.json', raw)
            self._tree_budget(stage, budget, guard.check)
            self._workspace(workspace)
            guard.check()
            publish_directory_no_replace(stage, destination)
            stage = None
            # No callback or cancellation may hide the now-durable prepared job.
            warnings = sync_parent(destination)
            return self._prepared(request, raw, warnings=warnings)
        finally:
            if stage is not None:
                shutil.rmtree(stage)

    @staticmethod
    def _prepared(request, raw, *, warnings=(), requires_reapproval=False):
        return PreparedTranscription(request['id'], sha256(raw).hexdigest(), request['workspace'],
            len(request['sources']), sum(row['bytes'] for row in request['sources']),
            request['model_id'], request['model_revision'], TranscriptionOptions.from_dict(request['options']),
            tuple(row['display_name'] for row in request['sources']), request['profile_path'], request['profile_sha256'],
            warnings, requires_reapproval, TranscriptionBudget.from_dict(request['budget']))

    def _request(self, job_id, expected=None, check=lambda: None, *, allow_restored=False):
        directory = self._directory(job_id)
        value, raw = read_json(directory / 'request.json', MAX_REQUEST_BYTES, check)
        options, budget = validate_request(value, job_id)
        restored = directory / RESTORED_NAME
        if restored.exists():
            marker, _ = read_json(restored, RESTORED_BYTES, check)
            validate_restored(marker, job_id, sha256(raw).hexdigest())
            if not allow_restored:
                raise PermissionError('Restored transcription is inert. Choose a current profile and prepare a new reviewed request.')
        elif value['data_root'] != str(self.root):
            raise PermissionError('This transcription request belongs to another data directory.')
        if expected is not None and checksum(expected) != sha256(raw).hexdigest():
            raise PermissionError('The reviewed transcription request changed; review it again.')
        self._workspace(value['workspace'])
        return directory, value, raw, options, budget

    def review(self, job_id):
        """Read the exact saved request for a fresh, explicit resume decision."""
        directory, request, raw, _, _ = self._request(job_id, allow_restored=True)
        return self._prepared(request, raw, requires_reapproval=(directory / RESTORED_NAME).exists()), request

    def jobs(self, *, workspace=None, cancelled=lambda: False):
        self._bound()
        if not self.jobs_root.exists():
            return ()
        root = checked_directory(self.jobs_root)
        guard = _Guard(self, 60, cancelled)
        paths = []
        with os.scandir(root) as entries:
            for count, entry in enumerate(entries, 1):
                guard.check()
                if count > 20000:
                    raise ValueError('The transcription job directory exceeds its listing budget.')
                if entry.name.startswith('.'):
                    continue
                if len(paths) >= 10000:
                    raise ValueError('The transcription job listing exceeds its budget.')
                paths.append(Path(entry.path))
        result = []
        for path in sorted(paths):
            guard.check()
            if path.name.startswith('.'):
                continue
            if workspace is not None:
                preliminary, _ = read_json(path / 'request.json', MAX_REQUEST_BYTES, guard.check)
                if type(preliminary) is dict and preliminary.get('workspace') != workspace:
                    continue
            directory, request, raw, _, _ = self._request(identity(path.name), check=guard.check, allow_restored=True)
            result.append(self._prepared(request, raw, requires_reapproval=(directory / RESTORED_NAME).exists()))
        return tuple(result)

    def prepare_recovered(self, job_id, manifest, *, approved_source_request_sha256,
                          options=None, budget=None, cancelled=lambda: False, on_progress=lambda event: None):
        """Create a new request from owned restored inputs and a current profile.

        Original bytes, fingerprints and path strings remain untouched. Old
        caches, profile paths and approvals never become execution authority.
        """
        directory, request, _, old_options, old_budget = self._request(job_id,
            checksum(approved_source_request_sha256), allow_restored=True)
        if not (directory / RESTORED_NAME).exists():
            raise ValueError('Choose a restored inert transcription request.')
        selected_budget = old_budget if budget is None else budget
        if type(selected_budget) is not TranscriptionBudget:
            raise TypeError('Use a typed transcription budget.')
        guard = _Guard(self, selected_budget.max_seconds, cancelled)
        with project_lock(directory):
            self._inputs(directory, request, guard.check)
            return self.prepare(request['workspace'], manifest,
                [directory / 'inputs' / row['name'] for row in request['sources']],
                options=old_options if options is None else options, budget=selected_budget,
                cancelled=lambda: (guard.check() or False), on_progress=on_progress,
                _display_names=tuple(row['display_name'] for row in request['sources']))

    @staticmethod
    def _tree_budget(root, budget, check, *, active=False):
        total = files = entries = 0
        pending = [(Path(root), 0)]
        while pending:
            path, depth = pending.pop()
            check()
            checked_directory(path)
            if depth > 16:
                raise ValueError('Transcription job nesting exceeds its budget.')
            with os.scandir(path) as children:
                for entry in children:
                    check()
                    entries += 1
                    if entries > budget.max_files * 2 + 128:
                        raise ValueError('Transcription job entries exceed their budget.')
                    try:
                        # Windows DirEntry.stat() leaves st_nlink/st_ino/st_dev
                        # at zero. Fetch current filesystem metadata instead of
                        # treating that cached placeholder as an external link.
                        info = os.stat(entry.path, follow_symlinks=False)
                    except FileNotFoundError:
                        if active:
                            continue  # A worker may just have renamed its temporary file.
                        raise
                    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('Transcription job links and reparse points are not accepted.')
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((Path(entry.path), depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        # The supplied producer briefly hard-links its own stage
                        # during exclusive publication. At rest, links are refused.
                        if info.st_nlink != 1 and not active:
                            raise ValueError('Transcription job files must not have external links.')
                        files += 1
                        total += info.st_size
                        if files > budget.max_files or total > budget.max_job_bytes:
                            raise ValueError('Transcription exceeded its approved working-disk budget.')
                    else:
                        raise ValueError('Transcription jobs require ordinary local files.')
        return total

    @staticmethod
    def _inputs(directory, request, check):
        root = checked_directory(directory / 'inputs')
        if set(p.name for p in root.iterdir()) != {row['name'] for row in request['sources']}:
            raise ValueError('The retained source snapshot inventory changed.')
        for row in request['sources']:
            path = root / row['name']
            if regular(path).st_size != row['bytes'] or file_digest(path, check) != row['sha256']:
                raise ValueError('A retained source snapshot changed.')

    @staticmethod
    def _control_file(path, enabled):
        if path.exists():
            if read_bytes(path, 16) != b'1\n':
                raise ValueError('The worker control file was changed.')
            if not enabled:
                path.unlink()
        elif enabled:
            write_new(path, b'1\n')

    @staticmethod
    def _fixed_file(path, raw):
        if path.exists():
            if read_bytes(path, MAX_REQUEST_BYTES) != raw:
                raise ValueError('The fixed worker configuration changed.')
        else:
            write_new(path, raw)

    def _supervise(self, directory, runtime, settings, request, budget, guard, control, on_progress):
        config = canonical({'job_name': request['id'], 'folders': [str(directory / 'inputs')],
                            'results_root': str(directory / 'results'), 'models_dir': str(runtime.models),
                            'settings': settings})
        live = canonical(settings)
        self._fixed_file(directory / 'job_config.json', config)
        self._fixed_file(directory / 'settings_live.json', live)
        self._control_file(directory / 'stop.flag', False)
        self._control_file(directory / 'pause.flag', control.paused)
        # The wrapper holds this separate OS lock for its entire lifetime. An
        # orphaned child therefore prevents a restarted host launching a second
        # worker against the same job, even after the parent lock was released.
        with project_lock(directory / 'worker-lock'):
            pass
        options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
                   if os.name == 'nt' else {'start_new_session': True})
        reason, stop_deadline, last_scan, last_status = None, None, 0.0, None
        with tempfile.TemporaryFile(dir=directory / 'temp') as outgoing, tempfile.TemporaryFile(dir=directory / 'temp') as errors:
            guard.check()
            process = subprocess.Popen([str(item) for item in runtime.command(directory)],
                stdin=subprocess.DEVNULL, stdout=outgoing, stderr=errors, shell=False,
                env=environment(directory), cwd=runtime.worker.parent, **options)
            try:
                while True:
                    try:
                        guard.check()
                        if control.stopped:
                            raise InterruptedError('Transcription stop requested; completed worker checkpoints were retained.')
                    except (InterruptedError, TimeoutError, PermissionError) as exc:
                        if reason is None:
                            reason, stop_deadline = exc, time.monotonic() + budget.stop_grace_seconds
                            self._control_file(directory / 'stop.flag', True)
                    if process.poll() is not None:
                        break
                    if reason is not None:
                        if time.monotonic() >= stop_deadline:
                            break
                        time.sleep(.03)
                        continue
                    self._control_file(directory / 'pause.flag', control.paused)
                    if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_PROCESS_LOG_BYTES:
                        raise ValueError('The transcription worker exceeded its diagnostic byte budget.')
                    if time.monotonic() - last_scan >= .2:
                        self._fixed_file(directory / 'job_config.json', config)
                        self._fixed_file(directory / 'settings_live.json', live)
                        self._tree_budget(directory, budget, guard.check, active=True)
                        last_scan = time.monotonic()
                        status = directory / 'job_status.json'
                        if status.exists():
                            try:
                                value, _ = read_json(status, MAX_STATUS_BYTES, guard.check)
                                done = value.get('done') if type(value) is dict else None
                                state = value.get('state') if type(value) is dict else None
                                if type(done) is int and 0 <= done <= len(request['sources']):
                                    current = done, state == 'paused', control.paused
                                    if current != last_status:
                                        phase = ('Paused between inference steps' if current[1] else
                                                 'Pause requested; waiting for an inference boundary' if current[2] else
                                                 'Processing; exports still require independent verification')
                                        self._emit(on_progress, phase, done, len(request['sources']),
                                                   'paused' if current[1] else 'running')
                                        last_status = current
                            except (ValueError, OSError, UnicodeError):
                                # A producer atomically replacing a transient
                                # status file does not grant or revoke authority.
                                pass
                    time.sleep(.03)
                if reason is not None:
                    raise reason
                if max(os.fstat(outgoing.fileno()).st_size, os.fstat(errors.fileno()).st_size) > MAX_PROCESS_LOG_BYTES:
                    raise ValueError('The transcription worker exceeded its diagnostic byte budget.')
                if process.returncode != 0:
                    raise RuntimeError(f'The local transcription worker did not complete (exit {process.returncode}).')
            finally:
                _stop(process)
                if process.poll() is None:
                    raise RuntimeError('The owned worker has not confirmed exit; its job remains locked.')
        # Child status is never sufficient for completion, but success must also
        # agree with the exact number of approved sources.
        self._fixed_file(directory / 'job_config.json', config)
        self._fixed_file(directory / 'settings_live.json', live)
        status, _ = read_json(directory / 'job_status.json', MAX_STATUS_BYTES, guard.check)
        if (type(status) is not dict or status.get('state') != 'завершено'
                or type(status.get('done')) is not int or status['done'] != len(request['sources'])
                or type(status.get('total')) is not int or status['total'] != len(request['sources'])
                or type(status.get('failed')) is not int or status['failed'] != 0):
            raise ValueError('The worker completion status does not match the approved input count.')

    def run(self, job_id, *, approved_request_sha256, control=None,
            cancelled=lambda: False, on_progress=lambda event: None):
        """Run/resume only the exact reviewed request; no implicit model download."""
        checksum(approved_request_sha256)
        directory, request, raw, options, budget = self._request(job_id, approved_request_sha256)
        control = TranscriptionControl() if control is None else control
        if type(control) is not TranscriptionControl:
            raise TypeError('Use an operation-owned transcription control.')
        guard = _Guard(self, budget.max_seconds, lambda: cancelled() or control.stopped)
        with project_lock(directory):
            if (directory / 'receipt.json').exists():
                raise FileExistsError('This job already has a completed receipt; verify it instead of overwriting it.')
            guard.check()
            self._tree_budget(directory, budget, guard.check)
            self._inputs(directory, request, guard.check)
            runtime = TranscriptionRuntime.load(request['profile_path'], expected_sha256=request['profile_sha256'])
            runtime.validate_options(options)
            if (runtime.model_id, runtime.revision) != (request['model_id'], request['model_revision']):
                raise ValueError('The selected model identity changed.')
            self._emit(on_progress, 'Verifying local runtime before recognition', 0, len(request['sources']))
            runtime.verify(check=guard.check, timeout=min(30, guard.remaining()), probe_directory=directory)
            settings = options.worker_settings(runtime.model)
            preflight_outputs(directory, request['sources'], settings, runtime,
                              budget.max_output_bytes, guard.check)
            try:
                self._supervise(directory, runtime, settings, request, budget, guard, control, on_progress)
                if control.stopped:
                    raise InterruptedError('Transcription stopped before derivative verification.')
                guard.check()
                self._emit(on_progress, 'Verifying source snapshots, runtime and every requested export',
                           len(request['sources']), len(request['sources']))
                runtime.verify(check=guard.check, timeout=min(30, guard.remaining()), probe_directory=directory)
                self._inputs(directory, request, guard.check)
                outputs, observed = verify_outputs(directory, request['sources'], settings, runtime,
                                                    budget.max_output_bytes, guard.check)
                self._tree_budget(directory, budget, guard.check)
                self._workspace(request['workspace'])
                if read_bytes(directory / 'request.json', MAX_REQUEST_BYTES, guard.check) != raw:
                    raise ValueError('The approved request changed during transcription.')
                if control.stopped:
                    raise InterruptedError('Transcription stopped before publication.')
                guard.check()
                receipt = {'schema': 'resonance-transcription-receipt/1', 'id': job_id,
                    'request_sha256': approved_request_sha256, 'profile_sha256': runtime.manifest_sha256,
                    'quotation_authority': False, 'accuracy_status': 'not_qualified',
                    'derivative_kind': 'english_translation' if options.task == 'translate' else 'transcript',
                    'sources': request['sources'], 'outputs': outputs, 'observed': observed}
                encoded = canonical(receipt)
                if len(encoded) > MAX_REQUEST_BYTES:
                    raise ValueError('The completed transcript receipt exceeds its metadata budget.')
                write_new(directory / 'receipt.json', encoded)
                warnings = sync_parent(directory / 'receipt.json')
                result = TranscriptionReceipt(job_id, approved_request_sha256, sha256(encoded).hexdigest(),
                    len(request['sources']), tuple(row['path'] for row in outputs
                    if not row['path'].endswith('.v43-output.json')), warnings=warnings)
                try:
                    self._emit(on_progress, 'Verified derivatives published; speech accuracy requires review',
                               len(request['sources']), len(request['sources']), 'completed')
                except Exception:
                    pass  # An optional observer cannot hide a completed artifact.
                return result
            except (InterruptedError, TimeoutError):
                # A partial worker export never becomes a completed host receipt.
                raise

    def verify(self, job_id, *, expected_receipt_sha256, cancelled=lambda: False):
        """Reopen only a hash-bound receipt; installation absence does not erase derivatives."""
        checksum(expected_receipt_sha256)
        directory, request, raw, options, budget = self._request(job_id, allow_restored=True)
        guard = _Guard(self, budget.max_seconds, cancelled)
        with project_lock(directory):
            value, encoded = read_json(directory / 'receipt.json', MAX_REQUEST_BYTES, guard.check)
            if sha256(encoded).hexdigest() != expected_receipt_sha256:
                raise ValueError('The retained transcription receipt changed.')
            outputs = validate_receipt(value, request, raw)
            for row in outputs:
                path = directory / row['path']
                if regular(path).st_size != row['bytes'] or file_digest(path, guard.check) != row['sha256']:
                    raise ValueError('A retained transcript output changed.')
            self._inputs(directory, request, guard.check)
            self._tree_budget(directory, budget, guard.check)
            guard.check()
            return TranscriptionReceipt(job_id, value['request_sha256'], expected_receipt_sha256,
                len(request['sources']), tuple(row['path'] for row in outputs
                if not row['path'].endswith('.v43-output.json')))

    def read_text(self, receipt, *, cancelled=lambda: False):
        """Return the verified JSON transcript text as a derivative, never a quote."""
        if type(receipt) is not TranscriptionReceipt:
            raise TypeError('Use the exact completed or freshly verified transcript receipt.')
        self.verify(receipt.id, expected_receipt_sha256=receipt.receipt_sha256, cancelled=cancelled)
        directory, request, _, _, budget = self._request(receipt.id, receipt.request_sha256, allow_restored=True)
        guard = _Guard(self, budget.max_seconds, cancelled)
        recorded, encoded = read_json(directory / 'receipt.json', MAX_REQUEST_BYTES, guard.check)
        if sha256(encoded).hexdigest() != receipt.receipt_sha256:
            raise ValueError('The retained transcription receipt changed.')
        expected = {row['path']: row for row in recorded['outputs']}
        chunks = []
        for row in request['sources']:
            name = 'results/inputs/' + Path(row['name']).stem + '.json'
            value, raw = read_json(directory / name, budget.max_output_bytes, guard.check)
            if len(raw) != expected[name]['bytes'] or sha256(raw).hexdigest() != expected[name]['sha256']:
                raise ValueError('The transcript changed after receipt verification.')
            chunks.append((row['display_name'], value['text']))
        guard.check()
        return tuple(chunks)
