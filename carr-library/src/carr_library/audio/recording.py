"""Explicit local microphone capture into the selected Audio Studio project.

Selectively reuses PR149 906aa1a95a8f40ef119682d2d5c805c017c327f4 recorder
(blob f921c81cf836669191730626e4979890c0fc0e35) and PR112 resource admission.
Current runtime, ResourceBroker, project locks and no-replace publication remain
authority. Inputs/receipts belong to the existing separate audio snapshot;
no canonical state/studio tree, device fallback or new renderer is activated.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp
import threading
import time
from uuid import uuid4

from .capture_types import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, AudioUnavailable, integer
from .capture_process import publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular
from .jobs import project_lock
from ..progress import recording_admission_ready, recording_audio_notice, set_recording_active

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def _digest(path, cancelled=lambda: False):
    before = regular(path)
    digest = sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise AudioCancelled('Recording verification cancelled.')
            digest.update(block)
        after = os.fstat(stream.fileno())
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stamp(before) != stamp(after) or stamp(before) != stamp(regular(path)):
        raise ValueError('Recording input changed while being verified.')
    return digest.hexdigest()


@dataclass(frozen=True)
class RecordingDevice:
    id: str
    name: str
    alternative: str

    def __post_init__(self):
        _text(self.name, 'microphone name')
        _text(self.alternative, 'DirectShow device identity', 2048)
        if (not self.alternative.startswith('@device_')
                or any(char in self.alternative for char in ':=\"\'')
                or self.id != sha256(self.alternative.encode('utf-8')).hexdigest()):
            raise ValueError('The microphone needs an unambiguous DirectShow alternative identity.')


def parse_devices(raw):
    if not isinstance(raw, bytes) or len(raw) > 512 * 1024:
        raise ValueError('Microphone discovery exceeded its diagnostic allowance.')
    found, selected, section = {}, None, None
    for line in raw.decode('utf-8', errors='strict').splitlines():
        if 'DirectShow audio devices' in line:
            section = 'audio'
        elif 'DirectShow video devices' in line:
            section = 'video'
        match = re.search(r'"([^"\r\n]+)"\s*\((audio|video)\)\s*$', line)
        legacy = re.search(r'\]\s+"([^"\r\n]+)"\s*$', line)
        if match is not None:
            selected = match[1] if match[2] == 'audio' else None
        elif 'Alternative name' in line:
            alternative = re.search(r'Alternative name\s+"([^"\r\n]+)"\s*$', line)
            if selected is not None:
                if alternative is None:
                    raise ValueError('The microphone inventory has an invalid alternative identity.')
                value = alternative[1]
                device = RecordingDevice(sha256(value.encode('utf-8')).hexdigest(), selected, value)
                if device.id in found:
                    raise ValueError('The microphone inventory repeats a device identity.')
                found[device.id] = device
                if len(found) > 128:
                    raise ValueError('The microphone inventory exceeds its device allowance.')
            selected = None
        elif legacy is not None:
            selected = legacy[1] if section == 'audio' else None
    return tuple(found.values())


@dataclass(frozen=True)
class RecordingOptions:
    max_seconds: int = 600
    sample_rate: int = 48000
    channels: int = 1
    max_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        integer(self.max_seconds, 'Recording duration limit', 1, 21600)
        if type(self.sample_rate) is not int or self.sample_rate not in (16000, 24000, 44100, 48000):
            raise ValueError('Choose a supported recording sample rate.')
        integer(self.channels, 'Recording channels', 1, 2)
        integer(self.max_bytes, 'Recording byte allowance', 4096, 2 * 1024**3)
        if self.max_seconds * self.sample_rate * self.channels * 2 + 4096 > self.max_bytes:
            raise ValueError('The selected recording duration exceeds its PCM byte allowance.')


def validate_recording_receipt(value, *, recording_id=None):
    """Validate a saved recording declaration without opening files or devices."""
    identity = value.get('id') if type(value) is dict else None
    if (type(identity) is not str or _ID.fullmatch(identity) is None
            or (recording_id is not None and recording_id != identity)):
        raise ValueError('Invalid recording receipt identity.')
    if (set(value) != {'schema', 'id', 'filename', 'title', 'created',
            'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
            or value['schema'] != 'carr-recording/1'
            or value['filename'] != identity + '.wav'
            or type(value['limits']) is not dict
            or set(value['limits']) != {'max_seconds', 'sample_rate', 'channels', 'max_bytes'}
            or type(value['ffmpeg_sha256']) is not str
            or _HASH.fullmatch(value['ffmpeg_sha256']) is None
            or value['completion'] not in ('stopped', 'duration-limit', 'device-ended')):
        raise ValueError('Invalid recording receipt.')
    _text(value['title'], 'recording title')
    source = value['source']
    if (type(source) is not dict or set(source) != {'kind', 'backend', 'device_id', 'name'}
            or source['kind'] != 'microphone' or source['backend'] != 'dshow'
            or type(source['device_id']) is not str or _HASH.fullmatch(source['device_id']) is None):
        raise ValueError('Invalid recording source receipt.')
    _text(source['name'], 'recording device name')
    options = RecordingOptions(**value['limits'])
    _text(value['created'], 'recording timestamp', 100)
    try:
        created = datetime.fromisoformat(value['created'])
    except ValueError:
        raise ValueError('Invalid recording timestamp.') from None
    if created.utcoffset() is None or created.utcoffset().total_seconds() != 0:
        raise ValueError('The recording timestamp must identify UTC.')
    audio = value['audio']
    if (type(audio) is not dict or set(audio) != {'sha256', 'size_bytes', 'sample_rate',
            'channels', 'frames', 'sample_width', 'duration_seconds'}
            or type(audio['sha256']) is not str or _HASH.fullmatch(audio['sha256']) is None
            or any(type(audio[key]) is not int for key in ('size_bytes', 'sample_rate', 'channels', 'frames', 'sample_width'))
            or not 44 <= audio['size_bytes'] <= options.max_bytes
            or audio['sample_rate'] != options.sample_rate or audio['channels'] != options.channels
            or audio['sample_width'] != 2 or not 0 < audio['frames'] <= options.max_seconds * options.sample_rate
            or audio['size_bytes'] < 44 + audio['frames'] * audio['channels'] * audio['sample_width']
            or type(audio['duration_seconds']) not in (int, float)
            or audio['duration_seconds'] != audio['frames'] / options.sample_rate):
        raise ValueError('Invalid recording audio receipt.')
    return value


def unverified_recording_files(project_root):
    """List final UUID WAVs without receipts; never open audio or a device."""
    project = local_path(project_root, exists=True)
    assets = local_path(project / 'assets', root=project)
    receipts = local_path(project / 'recordings', root=project)
    if not assets.exists():
        return ()
    regular(assets, directory=True)
    result = []
    for count, entry in enumerate(assets.iterdir()):
        if count >= 10000:
            raise ValueError('The recording inventory exceeds its item allowance.')
        if entry.suffix != '.wav' or _ID.fullmatch(entry.stem) is None:
            continue
        if os.path.lexists(receipts / (entry.stem + '.json')):
            continue
        path = local_path(entry, root=assets, exists=True)
        regular(path)
        result.append(path)
    return tuple(sorted(result))


_PENDING_CAPTURES = set()
_PENDING_CAPTURE_LOCK = threading.Lock()


class _CaptureReservation:
    """Retain project, capture and shared resource ownership until native silence."""
    def __init__(self, root):
        from filelock import FileLock, Timeout
        self.root = local_path(root, exists=True)
        self.directory = self.owner = self.lock = None
        self.project_context = self.resource_context = self.lease = None
        self.deferred = self.closed = self.release_retry_required = False
        self.cleanup_error = None
        self._guard = threading.Lock()
        with _PENDING_CAPTURE_LOCK:
            retained = tuple(_PENDING_CAPTURES)
        release_pending = False
        for reservation in retained:
            if reservation.release_retry_required and not reservation.close():
                release_pending = True
        if not _CAPTURE.acquire(blocking=False):
            if release_pending:
                raise RuntimeError('Previous recording cleanup could not release its admission lock. '
                                   'Retry Start; no microphone was opened.')
            notice = recording_audio_notice()
            raise RuntimeError(notice or 'Another microphone recording owns capture admission. Confirm it has stopped first.')
        try:
            path = self.root / '.recording.lock'
            if os.path.lexists(path):
                regular(path)
            self.lock = FileLock(path, timeout=0, mode=0o600, thread_local=False)
            try:
                self.lock.acquire()
            except Timeout:
                raise RuntimeError('Another microphone recording or cleanup owns this project.') from None
            self.project_context = project_lock(self.root)
            self.project_context.__enter__()
        except BaseException:
            if self.lock is not None:
                self.lock.release()
            _CAPTURE.release()
            raise
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.add(self)
        set_recording_active(self, True)

    def admit(self, broker, request, *, cancelled, timeout):
        context = broker.reserve(request, operation_id='recording:' + uuid4().hex,
                                 cancelled=cancelled, timeout=timeout)
        lease = context.__enter__()
        self.resource_context, self.lease = context, lease
        return lease

    def staging(self):
        if self.directory is None:
            self.directory = local_path(mkdtemp(prefix='.pending-recording-', dir=self.root), exists=True)
        return self.directory

    def retain(self, owner):
        with self._guard:
            self.owner, self.deferred = owner, True
        set_recording_active(self, True, cleanup_pending=True)
        # The process runner owns cleanup; this callback never touches wx.
        owner.add_quiescent_callback(self.close)

    def close(self):
        with self._guard:
            if self.closed:
                return True
            if self.owner is not None and not self.owner.quiescent:
                return False
            try:
                if self.directory is not None and os.path.lexists(self.directory):
                    local_path(self.directory, root=self.root, exists=True)
                    regular(self.directory, directory=True)
                    shutil.rmtree(self.directory)
            except (OSError, ValueError) as exc:
                self.cleanup_error = str(exc)  # Quiet, inert staging stays owned.
            try:
                if self.lease is not None:
                    self.lease.release()
                if self.resource_context is not None:
                    self.resource_context.__exit__(None, None, None)
                    self.resource_context = None
                if self.project_context is not None:
                    self.project_context.__exit__(None, None, None)
                    self.project_context = None
                self.lock.release()
            except Exception as exc:
                self.cleanup_error, self.release_retry_required = str(exc), True
                set_recording_active(self, True, cleanup_pending=True)
                return False
            self.release_retry_required, self.closed = False, True
            set_recording_active(self, False)
            _CAPTURE.release()
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


class RecordingService:
    def __init__(self, library_root, project_root, *, ffmpeg=None, runtime=None, broker=None):
        self.library_root = local_path(library_root, exists=True)
        self.project_root = local_path(project_root, exists=True)
        regular(self.project_root, directory=True)
        if self.project_root.is_relative_to(self.library_root):
            raise ValueError('Choose the existing audio project folder outside the private library.')
        self.root = local_path(self.project_root / 'recordings')
        self.assets = local_path(self.project_root / 'assets')
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.assets.mkdir(exist_ok=True, mode=0o700)
        self.ffmpeg, self.runtime, self._broker = ffmpeg, runtime, broker
        self._devices, self._engine_hash = {}, None

    @property
    def broker(self):
        from ..operations import ResourceBroker, ResourceStateError
        if self._broker is None:
            self._broker = ResourceBroker.for_app(self.library_root)
        if (not isinstance(self._broker, ResourceBroker)
                or self._broker.root != self.library_root / 'runtime' / 'resources'
                or self._broker.policy_path != self.library_root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The recording resource policy belongs to another data folder.')
        return self._broker

    def _runtime(self):
        from types import SimpleNamespace
        from .runtime import find_ffmpeg, trusted_executable
        if self.runtime is None:
            selected = find_ffmpeg(self.ffmpeg)
            if selected is None:
                raise AudioUnavailable('Choose an installed local FFmpeg runtime before microphone discovery.')
            self.runtime = SimpleNamespace(ffmpeg=str(selected))
        # A test/runtime injection remains an explicit trusted local executable.
        trusted_executable(self.runtime.ffmpeg)
        return self.runtime

    def devices(self, *, cancelled=lambda: False):
        self._devices, self._engine_hash = {}, None
        if os.name != 'nt':
            raise AudioUnavailable('Microphone recording currently requires Windows DirectShow and a configured local FFmpeg build.')
        self._runtime()
        identity = _digest(self.runtime.ffmpeg, cancelled)
        result = run_process([self.runtime.ffmpeg, '-hide_banner', '-nostdin', '-list_devices', 'true',
                              '-f', 'dshow', '-i', 'dummy'],
            budget=AudioBudget(timeout=15, max_log_bytes=512 * 1024),
            cancelled=cancelled, allow_failure=True, owned_tree=True)
        if result.returncode not in (0, 1):
            raise AudioUnavailable('The local FFmpeg device inspection failed. Check the configured Windows runtime.')
        devices = parse_devices(result.stderr)
        if not devices:
            raise AudioUnavailable('No identifiable microphone was reported. Check Windows microphone permission and the FFmpeg DirectShow build, then refresh devices.')
        if _digest(self.runtime.ffmpeg, cancelled) != identity:
            raise ValueError('The FFmpeg runtime changed during device discovery. Refresh devices.')
        self._devices, self._engine_hash = {item.id: item for item in devices}, identity
        return devices

    @staticmethod
    def _input_arguments(device):
        return ['-thread_queue_size', '128', '-rtbufsize', '16M', '-f', 'dshow',
                '-i', 'audio=' + device.alternative]

    def capture(self, device_id, *, title='Microphone recording', options=None,
                stop_requested=lambda: False, cancelled=lambda: False, progress=None,
                resource_timeout=60):
        from ..operations import ResourceCancelled, ResourceRequest
        options = options or RecordingOptions()
        if not isinstance(options, RecordingOptions):
            raise ValueError('Validated recording options are required.')
        _text(title, 'recording title')
        if not callable(stop_requested) or not callable(cancelled):
            raise ValueError('Recording requires explicit stop and cancel callbacks.')
        device = self._devices.get(device_id)
        if device is None or self._engine_hash is None:
            raise ValueError('Refresh microphones and explicitly select a current device before Start.')
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        engine_hash = self._engine_hash
        reservation = _CaptureReservation(self.project_root)
        started = False
        def stopping():
            return bool(cancelled()) or (not started and bool(stop_requested()))
        try:
            budget = AudioBudget(threads=1)
            request = ResourceRequest(cpu_slots=1, memory_bytes=2 * budget.max_alloc_bytes,
                disk_path=self.project_root,
                disk_bytes=options.max_bytes + budget.reserve_disk_bytes + 65536,
                devices=('microphone:' + device.id,))
            lease = reservation.admit(self.broker, request, cancelled=stopping, timeout=resource_timeout)
            lease.checkpoint()
            if self._devices.get(device_id) != device or self._engine_hash != engine_hash:
                raise ValueError('The microphone inventory changed while waiting. Refresh and select it again.')
            quiet_deadline = time.monotonic() + 3
            while not recording_admission_ready(reservation):
                lease.checkpoint()
                if time.monotonic() >= quiet_deadline:
                    raise AudioError('Application audio has not confirmed silence; no microphone was opened.')
                time.sleep(.025)
            lease.checkpoint()
            started = True  # Stop now requests fixed q; Cancel still discards unfinished PCM.
            return self._capture(device, title, options, stop_requested, lease.cancelled, progress, reservation)
        except ResourceCancelled as exc:
            raise AudioCancelled('Recording stopped while waiting for shared resources; no microphone was opened.') from exc
        except AudioCleanupPending as exc:
            reservation.retain(exc.cleanup_owner)
            exc.recording_reservation = reservation
            raise
        finally:
            if not reservation.deferred:
                reservation.close()

    def _capture(self, device, title, options, stop_requested, cancelled, progress, reservation):
        identity = uuid4().hex
        output, receipt = self.assets / (identity + '.wav'), self.root / (identity + '.json')
        if os.path.lexists(output) or os.path.lexists(receipt):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed. Refresh microphones before recording.')
        budget = AudioBudget(timeout=options.max_seconds + 20, max_output_bytes=options.max_bytes,
                             max_log_bytes=512 * 1024, max_staging_files=4, threads=1)
        if shutil.disk_usage(self.root).free < options.max_seconds * options.sample_rate * options.channels * 2 + budget.reserve_disk_bytes:
            raise AudioError('There is not enough free space for the selected recording allowance.')

        def report(seconds):
            nonlocal progress
            if progress is not None:
                try:
                    progress(seconds)
                except Exception:
                    progress = None  # An observer cannot destroy captured audio.

        temporary = reservation.staging()
        staged = Path(temporary) / 'capture.wav'
        command = [self.runtime.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
            '-max_alloc', str(budget.max_alloc_bytes), '-threads', '1', '-stats_period', '0.1',
            '-progress', 'pipe:1', *self._input_arguments(device), '-map', '0:a:0', '-vn',
            '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
            '-t', str(options.max_seconds), '-f', 'wav', '-rf64', 'never', str(staged)]
        result = run_process(command, budget=budget, cancelled=cancelled,
            stop_requested=stop_requested, watch=(staged,), progress=report, cwd=temporary, owned_tree=True)
        audio = self._audio(staged, options, cancelled)
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
        value = {'schema': 'carr-recording/1', 'id': identity, 'filename': output.name,
            'title': title, 'created': datetime.now(timezone.utc).isoformat(),
            'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
            'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
            'completion': 'stopped' if result.stopped else 'duration-limit' if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended'}
        # This is the commit boundary. A late Stop/Cancel cannot erase a
        # validated published original. A receipt failure leaves that WAV
        # available for explicit recovery, never claims successful capture.
        validate_recording_receipt(value, recording_id=identity)
        publish_file(staged, output, cancelled=cancelled)
        try:
            atomic_json(receipt, value)
        except (OSError, ValueError) as exc:
            raise AudioError(f'The recorded WAV was retained at {output}, but its receipt could not be saved. Preserve the file for recovery.') from exc
        return {**value, 'path': str(output)}

    @staticmethod
    def _audio(path, options, cancelled):
        from .ffmpeg import wav_info
        info = regular(path)
        if cancelled():
            raise AudioCancelled('Recording PCM verification cancelled.')
        if info.st_size > options.max_bytes:
            raise ValueError('The recording exceeds its byte allowance.')
        pcm = wav_info(path)  # Existing bounded PCM16 RIFF/RF64 reader, no decoder.
        if (pcm.rate != options.sample_rate or pcm.channels != options.channels
                or not 0 < pcm.frames <= options.max_seconds * pcm.rate):
            raise ValueError('The recorded PCM does not match the selected format or duration allowance.')
        return {'sha256': _digest(path, cancelled), 'size_bytes': info.st_size,
                'sample_rate': pcm.rate, 'channels': pcm.channels, 'frames': pcm.frames,
                'sample_width': 2, 'duration_seconds': pcm.frames / pcm.rate}

    def _receipt(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        return validate_recording_receipt(
            _read_receipt(self.root / (identity + '.json')), recording_id=identity)

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.assets / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        return {**value, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        paths = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries, 1):
                if cancelled():
                    raise AudioCancelled('Recording list cancelled.')
                if count > 10000:
                    raise ValueError('The recording list exceeds its item allowance.')
                path = Path(entry.path)
                if path.suffix != '.json' or _ID.fullmatch(path.stem) is None:
                    raise ValueError('Unexpected file in the recording receipt directory.')
                regular(path)
                paths.append(path)
        records = []
        for path in sorted(paths):
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing receipts does not rehash hours of audio. Opening or
            # attaching a selected recording uses get() and verifies its bytes.
            records.append({**value, 'path': str(self.assets / value['filename'])})
        return tuple(records)


def _read_receipt(path):
    if regular(path).st_size > 65536:
        raise ValueError('The recording receipt exceeds its bounded metadata allowance.')
    return read_json(path)


def recording_receipts(project_root, *, cancelled=lambda: False, file_budget=10000):
    """Verify typed receipts against retained inputs, without granting device authority.

    Called by the existing audio snapshot before publication and after restore.
    Orphan final WAV inputs remain included by input_assets() as unverified media.
    """
    integer(file_budget, 'Recording receipt file budget', 1, 1000000)
    project = local_path(project_root, exists=True)
    root = local_path(project / 'recordings', root=project)
    if not root.exists():
        return {}
    regular(root, directory=True)
    files = {}
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, 1):
            if cancelled():
                raise AudioCancelled('Recording receipt verification cancelled.')
            if count > min(file_budget, 10000):
                raise ValueError('The recording receipts exceed their file allowance.')
            path = Path(entry.path)
            if entry.name != path.name or path.suffix != '.json' or _ID.fullmatch(path.stem) is None:
                raise ValueError('Unexpected file in the recording receipt directory.')
            value = validate_recording_receipt(_read_receipt(path), recording_id=path.stem)
            selected = local_path(project / 'assets' / value['filename'], root=project, exists=True)
            if RecordingService._audio(selected, RecordingOptions(**value['limits']), cancelled) != value['audio']:
                raise ValueError('The retained recording differs from its verified receipt.')
            files['audio/recordings/' + path.name] = path
    return files
