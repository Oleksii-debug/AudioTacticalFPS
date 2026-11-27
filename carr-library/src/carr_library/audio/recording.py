"""Explicit local microphone capture through the configured FFmpeg runtime.

DirectShow discovery does not record. A capture needs a device from the current
inventory and a separate caller action. Completed PCM and its relative receipt
live in ordinary private state, so existing backups retain them without another
archive format. No microphone, model, driver or network fallback is selected.

Upstream device contract: https://ffmpeg.org/ffmpeg-devices.html#dshow
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
from uuid import uuid4
from types import SimpleNamespace

from .recording_process import (CaptureBudget as AudioBudget, AudioCancelled,
                                AudioError, AudioUnavailable, integer, run_process)
from .cleanup import AudioCleanupPending
from .jobs import (publish_new as publish_file, publish_noreplace, sha256_file, verified_reader,
                   file_identity, checked_directory, project_lock)
from .runtime import resolve_ffmpeg
from .managed import ManagedProjects
from ..ingest.writer_guard import exclusive_writer
from ..durable_state import atomic_json, local_path, read_json, regular

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def _digest(path, cancelled=lambda: False):
    # The same source reader used by production rejects replacement and denies
    # Windows writes/deletes while the exact selected bytes are being hashed.
    return sha256_file(path, cancelled=cancelled,
                       expected_identity=file_identity(regular(path)))


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
    fields = {'schema', 'id', 'filename', 'title', 'created',
              'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
    schema = value.get('schema')
    if schema == 'carr-recording/2':
        fields.add('retake')
    if (set(value) != fields
            or schema not in ('carr-recording/1', 'carr-recording/2')
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
    if schema == 'carr-recording/2':
        retake = value['retake']
        if (type(retake) is not dict or set(retake) != {'origin_id', 'previous_id', 'previous_sha256'}
                or any(type(retake[key]) is not str or _ID.fullmatch(retake[key]) is None
                       or retake[key] == identity for key in ('origin_id', 'previous_id'))
                or type(retake['previous_sha256']) is not str
                or _HASH.fullmatch(retake['previous_sha256']) is None):
            raise ValueError('Invalid recording retake receipt.')
    return value


def unverified_recording_files(library_root):
    """List owned final WAVs without receipts; never inspect or activate audio."""
    library = local_path(library_root, exists=True)
    root = local_path(library / 'state' / 'studio' / 'recordings', root=library)
    if not root.exists():
        return ()
    regular(root, directory=True)
    result = []
    for count, entry in enumerate(root.iterdir()):
        if count >= 10000:
            raise ValueError('The recording inventory exceeds its item allowance.')
        if entry.suffix != '.wav' or _ID.fullmatch(entry.stem) is None:
            continue
        if os.path.lexists(root / (entry.stem + '.json')):
            continue
        path = local_path(entry, root=root, exists=True)
        regular(path)
        result.append(path)
    return tuple(sorted(result))


_PENDING_CAPTURES = set()
_PENDING_CAPTURE_LOCK = threading.Lock()


class _CaptureReservation:
    """Own one existing capture lock and staging until native cleanup is known.

    The cross-thread context is intentional: native cleanup is owned by the
    process runner, and its daemon may release this reservation after the
    submitting executor worker has returned. This is the same recording lock
    path and global admission lock used for ordinary captures.
    """
    def __init__(self, root, progress_hub=None):
        from filelock import FileLock, Timeout
        self.root = local_path(root, exists=True)
        self.directory = None
        self.owner = None
        self.identity = uuid4().hex
        self.admission = self.lease = None
        self.deferred = False
        self.closed = False
        self.cleanup_error = None
        self.activity = None
        self._guard = threading.Lock()
        self.lock = None
        if not _CAPTURE.acquire(blocking=False):
            raise RuntimeError('Another microphone recording or cleanup is active. Confirm it has stopped first.')
        try:
            path = self.root / '.recording.lock'
            if os.path.lexists(path):
                regular(path)
            self.lock = FileLock(path, timeout=0, mode=0o600, thread_local=False)
            try:
                self.lock.acquire()
            except Timeout:
                raise RuntimeError('Another microphone recording or cleanup owns this library.') from None
        except BaseException:
            _CAPTURE.release()
            raise
        try:
            if progress_hub is not None:
                self.activity = progress_hub.media_activity()
                self.activity.__enter__()
        except BaseException:
            self.lock.release()
            _CAPTURE.release()
            raise
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.add(self)

    def admit(self, context):
        lease = context.__enter__()
        self.admission, self.lease = context, lease
        return lease

    def staging(self):
        if self.directory is None:
            self.directory = Path(mkdtemp(prefix='.pending-recording-', dir=self.root))
        return self.directory

    def retain(self, owner):
        with self._guard:
            self.owner = owner
            self.deferred = True
        # Register only a plain callback. The process runner owns every native
        # termination attempt; neither wx nor the capture executor waits here.
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
                # Native silence is already established. Leave failed cleanup
                # exact and inert; it must not erase a committed original.
                self.cleanup_error = str(exc)
            if self.admission is not None:
                try:
                    self.admission.__exit__(None, None, None)
                except Exception as exc:
                    # Native shutdown is confirmed; a reporting/state cleanup
                    # error must not keep the microphone itself reserved.
                    self.cleanup_error = str(exc)
                self.admission = self.lease = None
            try:
                self.lock.release()
            except Exception as exc:
                self.cleanup_error = str(exc)
                return False
            self.closed = True
            _CAPTURE.release()
            if self.activity is not None:
                try:
                    self.activity.__exit__(None, None, None)
                except Exception as exc:
                    self.cleanup_error = str(exc)
                self.activity = None
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


class RecordingService:
    def __init__(self, library_root, *, runtime=None, ffmpeg=None, progress_hub=None, resource_broker=None):
        self.library_root = local_path(library_root, exists=True)
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        # Namespace creation uses the existing outer backup/publication guard.
        # An already-running backup cannot miss a newly created recording root.
        with exclusive_writer(self.library_root):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runtime is not None and ffmpeg is not None:
            raise ValueError('Select one configured FFmpeg runtime.')
        self.runtime, self.ffmpeg = runtime, ffmpeg
        self.progress_hub = progress_hub
        self.resource_broker = resource_broker
        self._devices, self._engine_hash = {}, None

    def _runtime(self):
        selected = getattr(self.runtime, 'ffmpeg', self.ffmpeg)
        binary = resolve_ffmpeg(selected)
        if binary is None:
            raise AudioUnavailable('Choose an installed local FFmpeg executable before microphone discovery.')
        self.runtime = SimpleNamespace(ffmpeg=str(binary))
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
                stop_requested=lambda: False, cancelled=lambda: False, progress=None, retake_of=None):
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
        if retake_of is not None and (type(retake_of) is not str or _ID.fullmatch(retake_of) is None):
            raise ValueError('Choose an existing saved recording for another take.')
        from ..operations.broker import ResourceBroker
        from ..operations.models import ResourceRequest, ResourceCancelled
        reservation = _CaptureReservation(self.root, self.progress_hub)
        admitted = False
        try:
            broker = self.resource_broker or ResourceBroker.for_app(self.library_root, cancelled=cancelled)
            maximum_pcm = options.max_seconds * options.sample_rate * options.channels * 2 + 4096
            request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
                disk_bytes=maximum_pcm + 64 * 1024**2, disk_path=self.root,
                devices=('microphone:' + device.id,))
            lease = reservation.admit(broker.reserve(request,
                operation_id='capture:' + reservation.identity,
                cancelled=lambda: cancelled() or (not admitted and stop_requested()),
                timeout=60, run_timeout=None))
            admitted = True
            if cancelled() or stop_requested():
                raise AudioCancelled('Recording stopped before opening the microphone.')
            checked_cancelled = lease.cancelled
            previous = self.get(retake_of, cancelled=checked_cancelled) if retake_of is not None else None
            return self._capture(device, title, options, stop_requested, checked_cancelled,
                                 progress, reservation, previous)
        except ResourceCancelled as exc:
            raise AudioCancelled('Recording resource admission was cancelled before opening the microphone.') from exc
        except AudioCleanupPending as exc:
            reservation.retain(exc.cleanup_owner)
            exc.recording_reservation = reservation
            raise
        finally:
            if not reservation.deferred:
                reservation.close()

    def _capture(self, device, title, options, stop_requested, cancelled, progress, reservation, previous=None):
        identity = reservation.identity
        output, receipt = self.root / (identity + '.wav'), self.root / (identity + '.json')
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
        if previous is not None:
            value['schema'] = 'carr-recording/2'
            value['retake'] = {
                'origin_id': previous.get('retake', {}).get('origin_id', previous['id']),
                'previous_id': previous['id'], 'previous_sha256': previous['audio']['sha256']}
        validate_recording_receipt(value, recording_id=identity)
        if cancelled():
            raise AudioCancelled('Recording cancelled before original publication.')
        publish_noreplace(staged, output)
        try:
            atomic_json(receipt, value)
        except (OSError, ValueError) as exc:
            raise AudioError(f'The recorded WAV was retained at {output}, but its receipt could not be saved. Preserve the file for recovery.') from exc
        return {**value, 'path': str(output)}

    @staticmethod
    def _audio(path, options, cancelled):
        from .ffmpeg import wav_info
        if cancelled():
            raise AudioCancelled('Recording PCM verification cancelled.')
        # Keep a shared verified handle through header validation and hashing.
        # In particular, Windows cannot substitute bytes between those reads.
        with verified_reader(path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_size > options.max_bytes:
                raise ValueError('The recording exceeds its byte allowance.')
            audio = wav_info(path)
            if (audio.rate != options.sample_rate or audio.channels != options.channels
                    or audio.frames > options.max_seconds * audio.rate):
                raise ValueError('The recorded PCM does not match the selected format or duration allowance.')
            return {'sha256': _digest(path, cancelled), 'size_bytes': info.st_size,
                    'sample_rate': audio.rate, 'channels': audio.channels, 'frames': audio.frames,
                    'sample_width': 2, 'duration_seconds': audio.frames / audio.rate}

    def _receipt(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        path = local_path(self.root / (identity + '.json'), root=self.root, exists=True)
        if regular(path).st_size > 64 * 1024:
            raise ValueError('The recording receipt exceeds its metadata allowance.')
        return validate_recording_receipt(read_json(path), recording_id=identity)

    def _lineage(self, value):
        seen = {value['id']}
        current = value
        while current.get('schema') == 'carr-recording/2':
            relation = current['retake']
            previous = self._receipt(relation['previous_id'])
            if previous['id'] in seen or len(seen) >= 10000:
                raise ValueError('Recording retakes contain a cycle or exceed their allowance.')
            seen.add(previous['id'])
            origin = previous.get('retake', {}).get('origin_id', previous['id'])
            if (relation['previous_sha256'] != previous['audio']['sha256']
                    or relation['origin_id'] != origin):
                raise ValueError('Recording retake changed its retained original identity.')
            current = previous

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        self._lineage(value)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        return {**value, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        paths = []
        for count, path in enumerate(self.root.iterdir()):
            if count >= 20000:
                raise ValueError('The recording list exceeds its item allowance.')
            if path.suffix == '.json':
                if len(paths) >= 10000:
                    raise ValueError('The recording list exceeds its item allowance.')
                paths.append(path)
        records = []
        for path in sorted(paths):
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing receipts does not rehash hours of audio. Opening or
            # attaching a selected recording uses get() and verifies its bytes.
            records.append({**value, 'path': str(self.root / value['filename'])})
        return tuple(records)

    def retain(self, identity, project_id, *, cancelled=lambda: False):
        """Attach verified bytes to the current managed Sound asset namespace."""
        value = self.get(identity, cancelled=cancelled)
        source = Path(value['path'])
        managed = ManagedProjects(self.library_root)
        root, document, audio = managed.paths(project_id)
        # Do not create an unrelated project merely from an opaque caller ID.
        regular(local_path(document, root=self.library_root, exists=True))
        name = value['audio']['sha256'] + '.wav'
        with managed.writer(project_id):
            with project_lock(audio):
                assets = checked_directory(audio / 'assets', create=True)
                target = assets / name
                if os.path.lexists(target):
                    if _digest(target, cancelled) != value['audio']['sha256']:
                        raise ValueError('The managed recording asset differs from its content identity.')
                else:
                    staged = assets / ('.recording-' + uuid4().hex)
                    try:
                        publish_file(source, staged, cancelled=cancelled,
                                     expected_identity=file_identity(regular(source)))
                        if _digest(staged, cancelled) != value['audio']['sha256']:
                            raise ValueError('The selected recording changed before retention.')
                        if cancelled():
                            raise AudioCancelled('Recording retention cancelled before publication.')
                        publish_noreplace(staged, target)
                    finally:
                        staged.unlink(missing_ok=True)
                if _digest(target, cancelled) != value['audio']['sha256']:
                    raise ValueError('The retained recording asset differs from its receipt.')
        return {'asset': name, 'sha256': value['audio']['sha256'],
                'recording_id': value['id'], 'project_id': project_id}
