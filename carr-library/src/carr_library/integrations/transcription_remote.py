"""Explicit optional remote transcription on the existing retained job service.

The desktop's worker owns this entire operation. One bounded existing process
runner owns network execution, including DNS/TLS waits. A completed immutable
result directory is reconciled locally; it is never an invitation to resend.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from time import time
from uuid import uuid4

from ..assistant.models import canonical
from ..assistant.profiles import ProfileStore, ProviderProfile
from ..assistant.providers import Authorization, money
from ..assistant.transcription_provider import (OPERATION, PROTOCOL, MODEL, RETENTION,
    MAX_INPUT_BYTES, AudioUpload, read_audio, request)
from ..audio.models import AudioBudget
from ..audio.process import run_process
from ..audio.voice_transport import retained_context
from ..durable_state import (atomic_bytes, atomic_json, local_path, read_json, regular,
                             state_lock, sync_directory, encode)
from ..file_publication import publish_directory_no_replace
from ..ingest.writer_guard import exclusive_writer
from . import transcription_remote_values as values

MAX_NETWORK_SECONDS = 120.0
MAX_JOB_DISK_BYTES = 96 * 1024**2

# A source-loaded app and an installed app must execute their actual current
# package, never whichever unrelated version this Python happens to discover.
# -I/-B remain; only the already loaded package and support roots are admitted.
WORKER_BOOTSTRAP = r'''import hashlib, importlib, importlib.util, pathlib, runpy, sys
if len(sys.argv) != 9:
    raise ValueError("Invalid remote-STT package entry arguments")
package_name, package_sha, support_name, support_sha, worker_name, worker_sha, request_name, request_sha = sys.argv[1:]
package = pathlib.Path(package_name).resolve(strict=True)
support = pathlib.Path(support_name).resolve(strict=True)
worker = pathlib.Path(worker_name).resolve(strict=True)
if (package.name != "__init__.py" or package.parent.name != "carr_library"
        or support.name != "__init__.py" or support.parent.name != "bootstrap"
        or worker != package.parent / "assistant" / "transcription_worker.py"):
    raise ValueError("The current remote-STT package entry changed")
for path, expected in ((package, package_sha), (support, support_sha), (worker, worker_sha)):
    raw = path.read_bytes()
    if len(raw) > 2 * 1024**2 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("The current remote-STT source bytes changed")
sys.path.insert(0, str(package.parent.parent))
if support.parent.parent != package.parent.parent:
    sys.path.insert(1, str(support.parent.parent))
for name, expected in (("carr_library", package), ("bootstrap", support)):
    selected = importlib.import_module(name)
    if pathlib.Path(selected.__file__).resolve(strict=True) != expected:
        raise ValueError("The remote-STT child imported another package")
spec = importlib.util.find_spec("carr_library.assistant.transcription_worker")
if spec is None or pathlib.Path(spec.origin).resolve(strict=True) != worker:
    raise ValueError("The remote-STT child selected another worker")
sys.argv = [str(worker), request_name, request_sha]
runpy.run_module("carr_library.assistant.transcription_worker", run_name="__main__")
'''


def _worker_command(request_path, request_sha):
    import carr_library
    import bootstrap
    from ..assistant import transcription_worker
    package = local_path(Path(carr_library.__file__).resolve(strict=True), exists=True)
    support = local_path(Path(bootstrap.__file__).resolve(strict=True), exists=True)
    worker = local_path(Path(transcription_worker.__file__).resolve(strict=True), exists=True)
    if worker != package.parent / 'assistant' / 'transcription_worker.py':
        raise ValueError('The remote-STT worker differs from the current application package.')
    pinned = []
    for path in (package, support, worker):
        if not 0 < regular(path).st_size <= 2 * 1024**2:
            raise ValueError('The current remote-STT package entry exceeds its source allowance.')
        pinned.extend((str(path), sha256(path.read_bytes()).hexdigest()))
    return [str(Path(sys.executable).resolve(strict=True)), '-I', '-B', '-c',
            WORKER_BOOTSTRAP, *pinned, str(request_path), request_sha]


class RemoteTranscriptionCommitted(RuntimeError):
    """Immutable transcript bytes committed; checkpoint/cleanup needs attention."""
    def __init__(self, result):
        self.result = result
        super().__init__('The remote transcript was saved. Verify this job to reconcile its checkpoint; do not send the recording again.')


@dataclass(frozen=True)
class RemoteApproval:
    """Ephemeral explicit confirmation; this object is never saved in the job."""
    review_json: str
    authorization: Authorization


def _copy_review(value):
    if type(value) is not dict:
        raise ValueError('Review the selected recording before approving a remote call.')
    captured = json.loads(canonical(value))
    required = {'schema', 'id', 'workspace', 'title', 'source_path', 'audio',
                'profile', 'profile_sha256', 'language', 'max_cost', 'request_sha256',
                'operation_id', 'created', 'retention', 'review_sha256', 'library_root', 'library_identity'}
    values.fields(captured, required)
    if captured['schema'] != 'resonance-remote-transcription-review/1' or captured['retention'] != RETENTION:
        raise ValueError('The remote transcription disclosure changed.')
    values.identifier(captured['id'])
    values.identifier(captured['operation_id'])
    values.text(captured['workspace'], 200)
    values.text(captured['title'], 200)
    values.text(captured['source_path'], 32768)
    values.text(captured['library_root'], 32768)
    if (type(captured['library_identity']) is not list or len(captured['library_identity']) != 2
            or any(type(number) is not int or number < 0 for number in captured['library_identity'])):
        raise ValueError('Invalid reviewed library directory identity.')
    values.finite(captured['created'], 10**12)
    values.checksum(captured['profile_sha256'])
    profile = ProviderProfile(**captured['profile'])
    if captured['profile'] != values.snapshot(profile):
        raise ValueError('The reviewed public profile cannot carry an account secret.')
    _, body, _ = request(profile, {'audio': captured['audio'], 'language': captured['language']})
    if values.fingerprint(body) != captured['request_sha256']:
        raise ValueError('The reviewed recording or provider parameters changed.')
    if str(money(captured['max_cost'])) != captured['max_cost']:
        raise ValueError('Review an explicit maximum remote cost.')
    if values.fingerprint({key: item for key, item in captured.items() if key != 'review_sha256'}) != captured['review_sha256']:
        raise ValueError('The remote transcription review changed.')
    return captured


def approve(review, *, consent=False):
    """Call only after the explicit UI/CLI privacy and budget confirmation."""
    captured = _copy_review(review)
    if consent is not True or not captured['created'] <= time() <= captured['created'] + 900:
        raise PermissionError('Confirm this current recording, provider retention and explicit cost bound.')
    authorization = Authorization(uuid4().hex, captured['id'],
        ((captured['profile']['id'], captured['profile']['endpoint']),), (OPERATION,),
        ('selected-audio',), captured['max_cost'], 1, min(time() + 600, captured['created'] + 900),
        (captured['request_sha256'],))
    return RemoteApproval(canonical(captured), authorization)


def _authority(approval, saved_job=None):
    if not isinstance(approval, RemoteApproval):
        raise PermissionError('A new explicit in-memory transcription approval is required.')
    review = _copy_review(json.loads(approval.review_json))
    auth = approval.authorization
    if (not isinstance(auth, Authorization) or type(auth) is not Authorization
            or auth.expires_at <= time() or auth.task_id != review['id']
            or tuple(auth.profiles) != ((review['profile']['id'], review['profile']['endpoint']),)
            or tuple(auth.operations) != (OPERATION,) or tuple(auth.data_classes) != ('selected-audio',)
            or tuple(auth.request_hashes) != (review['request_sha256'],) or auth.max_calls != 1
            or auth.budget != review['max_cost']):
        raise PermissionError('Remote transcription approval does not match the reviewed recording.')
    if saved_job is not None:
        values.job(saved_job, identity=review['id'], workspace=review['workspace'])
        binding = saved_job['remote']
        if (binding['restored'] or binding['approval_sha256'] != values.fingerprint(asdict(auth))
                or binding['authorization_id'] != auth.id
                or binding['request_sha256'] != review['request_sha256']
                or binding['profile_sha256'] != review['profile_sha256']
                or binding['operation_id'] != review['operation_id']):
            raise PermissionError('This saved or restored job needs a new review; its old consent is inactive.')
    return review, auth


class RemoteTranscription:
    def __init__(self, service):
        self.service = service
        self.app, self.workspace = service.app, service.workspace
        self.root = local_path(service.app.root, exists=True)
        info = regular(self.root, directory=True)
        self.identity = (info.st_dev, info.st_ino)

    def guard(self, cancelled=lambda: False):
        if cancelled():
            raise InterruptedError('Remote transcription cancelled before the next effect.')
        if Path(self.app.root) != self.root or Path(self.service._library_root) != self.root:
            raise PermissionError('The remote transcription belongs to another library.')
        info = regular(local_path(self.root, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != self.identity:
            raise PermissionError('The selected library directory changed.')
        self.service._authorize()
        after = regular(local_path(self.root, exists=True), directory=True)
        if (after.st_dev, after.st_ino) != self.identity:
            raise PermissionError('The selected library directory changed during authorization.')

    def profiles(self):
        self.guard()
        store = ProfileStore(self.root / 'state' / 'providers')
        result = tuple(profile for profile in store.list() if profile.enabled
            and profile.adapter == 'elevenlabs' and profile.capabilities == (OPERATION,)
            and profile.model == MODEL and profile.credential_ref is not None)
        self.guard()
        return result

    def _profile(self, identity, checksum=None, *, cancelled=lambda: False):
        self.guard(cancelled)
        profile = ProfileStore(self.root / 'state' / 'providers').get(identity)
        if not profile.enabled or not profile.credential_ref:
            raise PermissionError('Explicitly enable and reconnect the selected transcription profile.')
        if checksum is not None and values.fingerprint(profile.to_dict()) != checksum:
            raise PermissionError('The selected provider profile changed after review.')
        self.guard(cancelled)
        return profile

    def review(self, selected, *, profile_id, title='Remote transcription',
               language='auto', max_cost=None, cancelled=lambda: False):
        self.guard(cancelled)
        if max_cost is None:
            raise PermissionError('Approve an explicit maximum cost; no billed price is guessed.')
        values.text(title, 200)
        profile = self._profile(profile_id, cancelled=cancelled)
        path, audio, _ = read_audio(selected, cancelled=lambda: self.stopped(cancelled))
        _, body, _ = request(profile, {'audio': audio, 'language': language})
        captured = dict(schema='resonance-remote-transcription-review/1', id=uuid4().hex,
            workspace=self.workspace, title=title, source_path=str(path), audio=audio,
            library_root=str(self.root), library_identity=list(self.identity),
            profile=values.snapshot(profile), profile_sha256=values.fingerprint(profile.to_dict()),
            language=language, max_cost=str(money(max_cost)), request_sha256=values.fingerprint(body),
            operation_id=uuid4().hex, created=time(), retention=RETENTION)
        captured['review_sha256'] = values.fingerprint(captured)
        self.guard(cancelled)
        return _copy_review(captured)

    def _retire_pending(self, path, identity):
        # A moved/replaced library must never turn cleanup into deletion of a
        # replacement tree. The original pending data stays with its old root.
        if not path.exists():
            return
        try:
            root = regular(local_path(self.root, exists=True), directory=True)
            current = regular(local_path(path, exists=True), directory=True)
        except (OSError, ValueError):
            return
        if ((root.st_dev, root.st_ino) == self.identity
                and (current.st_dev, current.st_ino) == identity):
            shutil.rmtree(path)

    def stopped(self, cancelled):
        self.guard(cancelled)
        return False

    def submit(self, approval, *, cancelled=lambda: False):
        review, auth = _authority(approval)
        self.guard(cancelled)
        if (review['workspace'] != self.workspace or review['library_root'] != str(self.root)
                or tuple(review['library_identity']) != self.identity):
            raise PermissionError('The reviewed recording belongs to another workspace.')
        self._profile(review['profile']['id'], review['profile_sha256'], cancelled=cancelled)
        from ..transcription import _copy
        # All admission checks precede state_lock's possible directory creation.
        self.guard(cancelled)
        with state_lock(self.service.jobs_root, 'submit'):
            self.guard(cancelled)
            target = self.service._path(review['id'])
            if os.path.lexists(target):
                raise FileExistsError('This remote transcription job already exists.')
            pending = self.service.jobs_root / ('.pending-' + review['id'])
            pending.mkdir(mode=0o700)
            pending_info = regular(pending, directory=True)
            pending_identity = (pending_info.st_dev, pending_info.st_ino)
            (pending / 'input').mkdir(mode=0o700)
            try:
                selected, exact, _ = read_audio(review['source_path'], expected=review['audio'],
                    cancelled=lambda: self.stopped(cancelled))
                name = '001-' + uuid4().hex + exact['suffix']
                copied = _copy(selected, pending / 'input' / name, maximum=MAX_INPUT_BYTES,
                               cancelled=lambda: self.stopped(cancelled))
                if copied != {key: exact[key] for key in ('sha256', 'size_bytes')}:
                    raise ValueError('Selected audio changed while retaining the approved original.')
                source = dict(filename=name, original_name=selected.name, **copied)
                binding = dict(profile=review['profile'], profile_sha256=review['profile_sha256'],
                    request_sha256=review['request_sha256'], approval_sha256=values.fingerprint(asdict(auth)),
                    authorization_id=auth.id, operation_id=review['operation_id'], restored=False, consent=values.REVIEW_REQUIRED,
                    payload={'audio': exact, 'language': review['language']}, max_cost=review['max_cost'])
                saved = dict(version=values.JOB_VERSION, protocol=PROTOCOL, id=review['id'],
                    workspace=self.workspace, title=review['title'], created=time(), updated=time(),
                    status='ready', sources=[source],
                    settings=dict(language=review['language'], model=MODEL, max_attempts=1, word_timestamps=True),
                    done=0, failed=0, receipts=[], artifacts=[], run_context=None, last_error='', remote=binding)
                values.job(saved)
                self.guard(cancelled)
                atomic_json(pending / 'job.json', saved)
                with exclusive_writer(self.root):
                    self.guard(cancelled)
                    publish_directory_no_replace(pending, target)
                    sync_directory(self.service.jobs_root)
                return saved['id']
            finally:
                self._retire_pending(pending, pending_identity)

    def reconcile(self, identity, *, cancelled=lambda: False):
        self.guard(cancelled)
        path = self.service._path(identity)
        saved = self.service.get(identity)
        values.job(saved, workspace=self.workspace, identity=identity)
        self.guard(cancelled)
        with state_lock(path, 'transcribe'):
            self.guard(cancelled)
            saved = self.service.get(identity)
            recovered = values.verify_files(path, saved, cancelled=lambda: self.stopped(cancelled))
            if recovered['status'] != 'completed':
                raise ValueError('No completed remote receipt exists. Review a new call explicitly; an interrupted call may already have been charged.')
            if recovered != saved:
                with exclusive_writer(self.root):
                    self.guard(cancelled)
                    self.service._save(path, recovered)
            return recovered

    def run(self, identity, approval, *, cancelled=lambda: False, progress=None,
            timeout=MAX_NETWORK_SECONDS):
        if type(timeout) not in (int, float) or not 0.05 <= timeout <= MAX_NETWORK_SECONDS:
            raise ValueError('A remote transcription call requires a finite allowance up to 120 seconds.')
        self.guard(cancelled)
        path = self.service._path(identity)
        saved = self.service.get(identity)
        review, auth = _authority(approval, saved)
        if review['library_root'] != str(self.root) or tuple(review['library_identity']) != self.identity:
            raise PermissionError('This approval belongs to another library directory.')
        self._profile(review['profile']['id'], review['profile_sha256'], cancelled=cancelled)
        if (path / 'results').exists():
            return self.reconcile(identity, cancelled=cancelled)
        if saved['status'] != 'ready':
            raise PermissionError('An attempted remote call is never automatically retried; review charge state and make a new explicit request.')
        completed = None
        try:
            self.guard(cancelled)
            with retained_context(state_lock(path, 'transcribe')):
                self.guard(cancelled)
                saved = self.service.get(identity)
                _authority(approval, saved)
                if saved['status'] != 'ready' or (path / 'results').exists():
                    raise PermissionError('The remote transcription changed before execution.')
                from ..operations import ResourceRequest
                broker = self.service._admission_broker(cancelled)
                request_budget = ResourceRequest(cpu_slots=1, memory_bytes=192 * 1024**2,
                    disk_bytes=MAX_JOB_DISK_BYTES, disk_path=path)
                with retained_context(broker.reserve(request_budget, operation_id='remote-asr:' + identity,
                                                     cancelled=cancelled, timeout=min(timeout, 60), run_timeout=timeout)) as lease:
                    def stopped():
                        lease.checkpoint()
                        self.guard(cancelled)
                        return False
                    self.service._sources(path, saved, stopped)
                    self._profile(review['profile']['id'], review['profile_sha256'], cancelled=stopped)
                    saved['status'] = 'running'
                    with exclusive_writer(self.root):
                        self.guard(cancelled)
                        self.service._save(path, saved)
                    if progress is not None:
                        progress({'phase': 'remote transcription', 'state': 'one explicitly approved transfer'})
                    with retained_context(TemporaryDirectory(prefix='carr-remote-stt-')) as temporary:
                        stage = Path(temporary).resolve(strict=True)
                        response_path = stage / 'response.json'
                        message = dict(schema='resonance-remote-transcription-call/1',
                            library_root=str(self.root), library_identity=list(self.identity),
                            workspace=self.workspace, job=identity,
                            job_sha256=values.fingerprint(saved),
                            authorization=asdict(auth), output=str(response_path),
                            timeout=float(timeout))
                        request_path = stage / 'request.json'
                        raw = encode(message)
                        atomic_bytes(request_path, raw)
                        command = _worker_command(request_path, sha256(raw).hexdigest())
                        # The same owned process supervisor closes network/DNS/TLS
                        # stalls at this total deadline and settles native ownership.
                        result = run_process(command, budget=AudioBudget(timeout=timeout,
                            threads=1, max_inputs=1, max_output_bytes=32 * 1024**2,
                            max_log_bytes=65536, max_staging_files=8),
                            cancelled=stopped, watch=(stage,), cwd=stage,
                            owned_windows_job=os.name == 'nt')
                        if request_path.read_bytes() != raw:
                            raise ValueError('The ephemeral remote approval request changed.')
                        returned = read_json(response_path, maximum=values.MAX_RESULT_BYTES)
                        proof = json.loads(result.stdout)
                        if proof != {'schema': 'resonance-remote-transcription-return/1',
                                     'sha256': sha256(encode(returned)).hexdigest()}:
                            raise ValueError('The remote worker result lost its exact output binding.')
                        self.guard(cancelled)
                        self.service._sources(path, saved, stopped)
                        receipt, encoded = values.make_receipt(saved, returned)
                        values.verify_usage(path, saved, receipt)
                        prepared = path / ('.pending-remote-' + uuid4().hex)
                        prepared.mkdir(mode=0o700)
                        prepared_info = regular(prepared, directory=True)
                        prepared_identity = (prepared_info.st_dev, prepared_info.st_ino)
                        try:
                            for kind, data in encoded.items():
                                self.guard(cancelled)
                                atomic_bytes(prepared / (saved['sources'][0]['filename'] + '.' + kind), data)
                            atomic_json(prepared / (values.fingerprint(receipt) + '.result.json'), receipt)
                            with exclusive_writer(self.root):
                                self.guard(cancelled)
                                values.verify_usage(path, saved, receipt)
                                publish_directory_no_replace(prepared, path / 'results')
                                completed = values.completed_job(saved, receipt)
                                sync_directory(path)
                                # The immutable result already exists. Any later
                                # checkpoint/lease/temp cleanup error carries it.
                                self.service._save(path, completed)
                        finally:
                            self._retire_pending(prepared, prepared_identity)
                    if progress is not None:
                        progress({'phase': 'remote transcription', 'state': 'completed', 'job': identity})
            return completed
        except BaseException as error:
            if completed is not None:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    error.remote_transcription_result = completed
                    raise
                raise RemoteTranscriptionCommitted(completed) from error
            # Never overwrite the first error if the original root disappeared.
            try:
                self.guard()
                current = self.service.get(identity)
                if current['status'] == 'running':
                    current.update(status='stopped' if isinstance(error, InterruptedError) else 'failed',
                        failed=1, last_error='Remote recognition did not finish. The provider may have received the recording or charged the account; no automatic retry was made.')
                    with exclusive_writer(self.root):
                        self.guard()
                        self.service._save(path, current)
            except BaseException:
                pass
            raise
