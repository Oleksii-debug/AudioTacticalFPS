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
from tempfile import TemporaryDirectory, mkdtemp
import threading
import time
import wave
from uuid import uuid4

from .models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, AudioUnavailable, integer
from .process import native_audio_environment, publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceRequest, ResourceCancelled, ResourceError

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')
MAX_RECORDING_SEGMENTS = 128
MAX_PAUSED_SECONDS = 3600
_FRAME_NOTICE = 'Retained PCM frames only; paused wall time is omitted. Review joins before use.'


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}.')
    return value


def _digest(path, cancelled=lambda: False):
    before = regular(path)
    digest, count = sha256(), 0
    with Path(path).open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
            raise ValueError('Recording input changed before verification.')
        while block := stream.read(min(1024 * 1024, before.st_size + 1 - count)):
            if cancelled():
                raise AudioCancelled('Recording verification cancelled.')
            count += len(block)
            if count > before.st_size:
                raise ValueError('Recording input grew while being verified.')
            digest.update(block)
        after = os.fstat(stream.fileno())
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if count != before.st_size or stamp(before) != stamp(after) or stamp(before) != stamp(regular(path)):
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


def _validate_v1_recording_receipt(value, *, recording_id=None):
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


@dataclass(frozen=True)
class RecordingState:
    state: str
    frames: int
    sample_rate: int
    segments: int
    monitoring: bool = False
    monitor_submitted_frames: int = 0
    monitor_dropped_frames: int = 0


def validate_recording_receipt(value, *, recording_id=None):
    """Validate either version passively; legacy bytes and receipt rules stay unchanged."""
    if type(value) is not dict or value.get('schema') != 'carr-recording/2':
        return _validate_v1_recording_receipt(value, recording_id=recording_id)
    extras = {'segments', 'monitoring', 'timeline'}
    legacy = {key: item for key, item in value.items() if key not in extras}
    legacy['schema'] = 'carr-recording/1'
    _validate_v1_recording_receipt(legacy, recording_id=recording_id)
    if set(value) != set(legacy) | extras:
        raise ValueError('Invalid recording receipt.')
    audio = value['audio']
    segments = value['segments']
    if (type(segments) is not list or not 1 <= len(segments) <= MAX_RECORDING_SEGMENTS
            or value['timeline'] != _FRAME_NOTICE):
        raise ValueError('Invalid recording segment receipt.')
    position = 0
    for index, segment in enumerate(segments):
        if (type(segment) is not dict or set(segment) != {'start_frame', 'frames', 'pcm_sha256', 'ended_by'}
                or type(segment['start_frame']) is not int or segment['start_frame'] != position
                or type(segment['frames']) is not int or not 0 < segment['frames'] <= audio['frames'] - position
                or type(segment['pcm_sha256']) is not str or _HASH.fullmatch(segment['pcm_sha256']) is None
                or segment['ended_by'] not in ('paused', 'stopped', 'duration-limit', 'device-ended')
                or index < len(segments) - 1 and segment['ended_by'] != 'paused'):
            raise ValueError('Invalid contiguous recording segment range.')
        position += segment['frames']
    if position != audio['frames']:
        raise ValueError('Recording segments do not cover the retained PCM frames.')
    monitoring = value['monitoring']
    if (type(monitoring) is not dict or set(monitoring) != {
            'enabled', 'output_identity', 'submitted_frames', 'dropped_frames'}
            or type(monitoring['enabled']) is not bool
            or any(type(monitoring[key]) is not int or not 0 <= monitoring[key] <= position
                   for key in ('submitted_frames', 'dropped_frames'))
            or monitoring['submitted_frames'] + monitoring['dropped_frames'] > position
            or monitoring['enabled'] and (type(monitoring['output_identity']) is not str
                or _HASH.fullmatch(monitoring['output_identity']) is None)
            or not monitoring['enabled'] and (monitoring['output_identity'] is not None
                or monitoring['submitted_frames'] or monitoring['dropped_frames'])):
        raise ValueError('Invalid recording monitoring receipt.')
    return value


def verify_recording_segments(path, value, *, cancelled=lambda: False):
    """Verify retained PCM ranges without opening an engine or native device."""
    validate_recording_receipt(value, recording_id=value['id'])
    if value['schema'] == 'carr-recording/1':
        return
    before = regular(path)
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with Path(path).open('rb') as stream, wave.open(stream, 'rb') as reader:
        opened = os.fstat(stream.fileno())
        if stamp(before) != stamp(opened) or opened.st_nlink != 1:
            raise ValueError('Recording changed before segment verification.')
        audio = value['audio']
        if (reader.getcomptype() != 'NONE' or reader.getnframes() != audio['frames']
                or reader.getframerate() != audio['sample_rate'] or reader.getnchannels() != audio['channels']
                or reader.getsampwidth() != 2):
            raise ValueError('Recording segment format differs from its retained PCM.')
        width = audio['channels'] * 2
        for segment in value['segments']:
            digest, remaining = sha256(), segment['frames']
            while remaining:
                if cancelled():
                    raise AudioCancelled('Recording segment verification cancelled.')
                count = min(remaining, 32768)
                data = reader.readframes(count)
                if len(data) != count * width:
                    raise ValueError('A retained recording segment is truncated.')
                digest.update(data)
                remaining -= count
            if digest.hexdigest() != segment['pcm_sha256']:
                raise ValueError('A retained recording segment differs from its frame hash.')
        after = os.fstat(stream.fileno())
    if stamp(before) != stamp(after) or stamp(before) != stamp(regular(path)):
        raise ValueError('The recording changed during segment verification.')


def verify_retained_recordings(state_root, entries, *, cancelled=lambda: False):
    """Validate recognized recording originals only within an already copied state.

    No device/runtime is constructed and no historical source/output path is
    followed. The caller retains canonical snapshot/manifest authority.
    """
    from ..backup_sidecars import _json, read_regular
    root = local_path(state_root, exists=True)
    by_name = {entry['filename']: entry for entry in entries}
    selected = [name for name in by_name if name.startswith('studio/recordings/')]
    if len(selected) > 10000:
        raise ValueError('The retained recording inventory exceeds its allowance.')
    for name in selected:
        relative = name[len('studio/recordings/'):]
        if re.fullmatch(r'[0-9a-f]{32}\.(?:json|wav)', relative) is None:
            raise ValueError('Unknown retained recording payload.')
    for stem in sorted({Path(name).stem for name in selected}):
        if cancelled():
            raise AudioCancelled('Recording recovery validation cancelled.')
        receipt_name = 'studio/recordings/' + stem + '.json'
        media_name = 'studio/recordings/' + stem + '.wav'
        receipt_entry, media_entry = by_name.get(receipt_name), by_name.get(media_name)
        if receipt_entry is None or media_entry is None:
            raise ValueError('A retained recording needs both its original WAV and verified receipt.')
        raw = read_regular(local_path(root / receipt_name, root=root, exists=True), 32 * 1024**2)
        if len(raw) != receipt_entry['size_bytes'] or sha256(raw).hexdigest() != receipt_entry['sha256']:
            raise ValueError('A recording receipt differs from the copied inventory.')
        receipt = validate_recording_receipt(_json(raw), recording_id=stem)
        media = local_path(root / media_name, root=root, exists=True)
        if (media_entry['size_bytes'] != receipt['audio']['size_bytes']
                or media_entry['sha256'] != receipt['audio']['sha256']
                or RecordingService._audio(media, RecordingOptions(**receipt['limits']), cancelled) != receipt['audio']):
            raise ValueError('The retained recording differs from its verified receipt.')
        verify_recording_segments(media, receipt, cancelled=cancelled)


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


def _close_segment_resources(tap, monitor, child_pending=None):
    """Keep every actual native owner before returning an uncertain cleanup."""
    owners, retry = [], []
    if child_pending is not None:
        owners.append(child_pending.cleanup_owner)
    failure = child_pending
    for resource in (tap, monitor):
        if resource is None:
            continue
        try:
            resource.close()
        except AudioCleanupPending as exc:
            if exc.cleanup_owner not in owners:
                owners.append(exc.cleanup_owner)
            failure = failure or exc
        except BaseException as exc:
            retry.append(resource.close)
            failure = failure or exc
    if not owners and not retry:
        return
    if len(owners) == 1 and not retry:
        raise AudioCleanupPending('Recording native cleanup is unconfirmed; capture resources remain reserved.',
                                  cleanup_owner=owners[0]) from failure

    def settle():
        ready = True
        for owner in owners:
            if not owner.close():
                ready = False
        for close in tuple(retry):
            try:
                close()
            except AudioCleanupPending as exc:
                if exc.cleanup_owner not in owners:
                    owners.append(exc.cleanup_owner)
                retry.remove(close)
                ready = False
            except Exception:
                ready = False
            else:
                retry.remove(close)
        return ready and not retry and all(owner.quiescent for owner in owners)

    from .cleanup import pending_cleanup
    raise pending_cleanup(settle,
        message='Recording producer and monitor cleanup are unconfirmed; all capture resources remain reserved.') from failure


_PENDING_CAPTURES = set()
_PENDING_CAPTURE_LOCK = threading.Lock()


class _CaptureReservation:
    """Own one existing capture lock and staging until native cleanup is known.

    The cross-thread context is intentional: native cleanup is owned by the
    process runner, and its daemon may release this reservation after the
    submitting executor worker has returned. This is the same recording lock
    path and global admission lock used for ordinary captures.
    """
    def __init__(self, root):
        from ..operations._locks import NativeLock
        self.root = local_path(root, exists=True)
        info = regular(self.root, directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.directory_identity = None
        self.directory = None
        self.owner = None
        self.deferred = False
        self.closed = False
        self.cleanup_error = None
        self.release_retry_required = False
        self._guard = threading.Lock()
        self.lock = None
        # Retry only a failed admission release, and only for a new explicit
        # capture request. An active capture without a native owner must never
        # be mistaken for an abandoned reservation.
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
            raise RuntimeError('Another microphone recording or cleanup is active. Confirm it has stopped first.')
        try:
            path = self.root / '.recording.lock'
            if os.path.lexists(path):
                regular(path)
            self.lock = NativeLock(path)
            if not self.lock.acquire():
                raise RuntimeError('Another microphone recording or cleanup owns this library.')
        except BaseException:
            _CAPTURE.release()
            raise
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.add(self)

    def staging(self):
        current = regular(local_path(self.root, exists=True), directory=True)
        if (current.st_dev, current.st_ino) != self.root_identity:
            raise ValueError('The recording directory changed before capture.')
        if self.directory is None:
            self.directory = Path(mkdtemp(prefix='.pending-recording-', dir=self.root))
            info = regular(self.directory, directory=True)
            self.directory_identity = info.st_dev, info.st_ino
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
                    parent = regular(local_path(self.root, exists=True), directory=True)
                    child = regular(local_path(self.directory, root=self.root, exists=True), directory=True)
                    if ((parent.st_dev, parent.st_ino) != self.root_identity
                            or (child.st_dev, child.st_ino) != self.directory_identity):
                        raise ValueError('The captured recording directory changed; replacement files were not removed.')
                    shutil.rmtree(self.directory)
            except (OSError, ValueError) as exc:
                # Native silence is already established. Leave failed cleanup
                # exact and inert; it must not erase a committed original.
                self.cleanup_error = str(exc)
            try:
                self.lock.release()
            except Exception as exc:
                self.cleanup_error = str(exc)
                self.release_retry_required = True
                return False
            self.release_retry_required = False
            self.closed = True
            _CAPTURE.release()
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


class RecordingService:
    def __init__(self, library_root, *, runtime=None, broker=None):
        self.library_root = local_path(library_root, exists=True)
        library_info = regular(self.library_root, directory=True)
        self._library_identity = library_info.st_dev, library_info.st_ino
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = regular(self.root, directory=True)
        self._root_identity = info.st_dev, info.st_ino
        if broker is not None and (not isinstance(broker, ResourceBroker)
                or broker.root != self.library_root / 'runtime' / 'resources'
                or broker.policy_path != self.library_root / 'state' / 'operations' / 'resources.json'):
            raise ValueError('Recording admission must use this library resource coordinator.')
        self._broker_instance = broker
        self.runtime = runtime
        self._devices, self._engine_hash = {}, None

    def _scope(self):
        library = regular(local_path(self.library_root, exists=True), directory=True)
        if (library.st_dev, library.st_ino) != self._library_identity:
            raise ValueError('The selected recording library changed; reopen its recording tools.')
        root = regular(local_path(self.root, root=self.library_root, exists=True), directory=True)
        if (root.st_dev, root.st_ino) != self._root_identity:
            raise ValueError('The selected recording library changed; reopen its recording tools.')

    def _stopped(self, cancelled):
        self._scope()
        return bool(cancelled())

    def _broker(self, cancelled):
        self._scope()
        if self._broker_instance is None:
            self._broker_instance = ResourceBroker.for_app(self.library_root, cancelled=cancelled)
        return self._broker_instance

    @staticmethod
    def monitoring_outputs(*, cancelled=lambda: False):
        from ..speech.playback_devices import output_devices
        return output_devices(cancelled=cancelled)

    def _runtime(self):
        if self.runtime is None:
            from ..runtime_settings import load_settings
            from .dsp import AudioRuntime
            settings = load_settings(self.library_root / 'state')
            self.runtime = AudioRuntime(self.root, ffmpeg=settings['ffmpeg'], ffprobe=settings['ffprobe'])
        return self.runtime

    def devices(self, *, cancelled=lambda: False):
        self._devices, self._engine_hash = {}, None
        self._scope()
        if os.name != 'nt':
            raise AudioUnavailable('Microphone recording currently requires Windows DirectShow and a configured local FFmpeg build.')
        self._runtime()
        check = lambda: self._stopped(cancelled)
        try:
            with self._broker(check).reserve(ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2),
                    operation_id='recording-discovery:' + uuid4().hex, cancelled=check,
                    timeout=30, run_timeout=30) as lease:
                lease.checkpoint()
                identity = _digest(self.runtime.ffmpeg, lease.cancelled)
                result = run_process([self.runtime.ffmpeg, '-hide_banner', '-nostdin', '-list_devices', 'true',
                                      '-f', 'dshow', '-i', 'dummy'],
                    budget=AudioBudget(timeout=15, max_log_bytes=512 * 1024),
                    cancelled=lease.cancelled, allow_failure=True, owned_windows_job=True,
                    env=native_audio_environment())
                if result.returncode not in (0, 1):
                    raise AudioUnavailable('The local FFmpeg device inspection failed. Check the configured Windows runtime.')
                devices = parse_devices(result.stderr)
                if not devices:
                    raise AudioUnavailable('No identifiable microphone was reported. Check Windows microphone permission and the FFmpeg DirectShow build, then refresh devices.')
                if _digest(self.runtime.ffmpeg, lease.cancelled) != identity:
                    raise ValueError('The FFmpeg runtime changed during device discovery. Refresh devices.')
                lease.checkpoint()
                self._scope()
                self._devices, self._engine_hash = {item.id: item for item in devices}, identity
                return devices
        except ResourceCancelled as exc:
            raise AudioCancelled('Microphone discovery cancelled before completion.') from exc
        except ResourceError as exc:
            raise AudioError('The shared resource policy refused microphone discovery.') from exc

    @staticmethod
    def _input_arguments(device):
        return ['-thread_queue_size', '128', '-rtbufsize', '16M', '-f', 'dshow',
                '-i', 'audio=' + device.alternative]

    def capture(self, device_id, *, title='Microphone recording', options=None,
                stop_requested=lambda: False, cancelled=lambda: False, progress=None,
                pause_requested=None, state_changed=None, monitor_output=None):
        options = options or RecordingOptions()
        if not isinstance(options, RecordingOptions):
            raise ValueError('Validated recording options are required.')
        _text(title, 'recording title')
        if not callable(stop_requested) or not callable(cancelled):
            raise ValueError('Recording requires explicit stop and cancel callbacks.')
        if pause_requested is not None and not callable(pause_requested):
            raise ValueError('Recording pause requires an explicit request callback.')
        if state_changed is not None and not callable(state_changed):
            raise ValueError('Recording state feedback must be a callback.')
        devices = []
        if monitor_output is not None:
            from .recording_monitor import output_identity
            devices.append('audio-output:' + output_identity(monitor_output))
        device = self._devices.get(device_id)
        if device is None or self._engine_hash is None:
            raise ValueError('Refresh microphones and explicitly select a current device before Start.')
        self._scope()
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        controlled = pause_requested is not None or monitor_output is not None
        peak = options.max_seconds * options.sample_rate * options.channels * 2 * (2 if controlled else 1)
        peak += (MAX_RECORDING_SEGMENTS + 1 if controlled else 1) * 4096 + 65536
        request = ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2,
            disk_bytes=peak, disk_path=self.root, devices=tuple(devices + ['microphone:' + device.id]))
        reservation = _CaptureReservation(self.root)
        admitted = False

        def check():
            self._scope()
            # Stop while waiting never opens a microphone. Once admitted, Stop
            # goes to the producer's graceful WAV close, not cancellation.
            return bool(cancelled()) or (not admitted and bool(stop_requested()))

        try:
            with self._broker(check).reserve(request, operation_id='recording:' + uuid4().hex,
                    cancelled=check, run_timeout=options.max_seconds + MAX_PAUSED_SECONDS + 60) as lease:
                admitted = True
                lease.checkpoint()
                if stop_requested():
                    raise AudioCancelled('Recording stopped before opening the microphone.')
                try:
                    return self._capture(device, title, options, stop_requested, lease.cancelled,
                        progress, reservation, pause_requested, state_changed, monitor_output)
                except AudioCleanupPending as exc:
                    # The broker settles this SAME owner before releasing its
                    # lease; the retained staging/native lock stays owned too.
                    reservation.retain(exc.cleanup_owner)
                    exc.recording_reservation = reservation
                    raise
        except ResourceCancelled as exc:
            raise AudioCancelled('Recording cancelled before capture completion.') from exc
        except ResourceError as exc:
            raise AudioError('The shared resource policy refused this recording allowance.') from exc
        finally:
            if not reservation.deferred:
                reservation.close()

    def _capture(self, device, title, options, stop_requested, cancelled, progress, reservation,
                 pause_requested=None, state_changed=None, monitor_output=None):
        self._scope()
        identity = uuid4().hex
        output, receipt = self.root / (identity + '.wav'), self.root / (identity + '.json')
        if os.path.lexists(output) or os.path.lexists(receipt):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
        self._runtime()
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed. Refresh microphones before recording.')
        budget = AudioBudget(timeout=options.max_seconds + 20, max_output_bytes=options.max_bytes,
                             max_log_bytes=512 * 1024, max_staging_files=4, threads=1)
        controlled = pause_requested is not None or monitor_output is not None
        needed = options.max_seconds * options.sample_rate * options.channels * 2 * (2 if controlled else 1)
        if shutil.disk_usage(self.root).free < needed + budget.reserve_disk_bytes + (MAX_RECORDING_SEGMENTS * 4096 if controlled else 0):
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
        extra = {}
        if controlled:
            completion, extra = self._capture_segments(device, staged, options, stop_requested,
                cancelled, report, pause_requested or (lambda: False), state_changed, monitor_output)
        else:
            result = run_process(self._command(device, staged, options, budget, options.max_seconds),
                budget=budget, cancelled=cancelled, stop_requested=stop_requested,
                watch=(staged,), progress=report, cwd=temporary,
                owned_windows_job=os.name == 'nt', env=native_audio_environment())
        audio = self._audio(staged, options, cancelled)
        if not controlled:
            completion = 'stopped' if result.stopped else 'duration-limit' if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended'
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
        value = {'schema': 'carr-recording/2' if controlled else 'carr-recording/1',
            'id': identity, 'filename': output.name, 'title': title,
            'created': datetime.now(timezone.utc).isoformat(),
            'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
            'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
            'completion': completion, **extra}
        validate_recording_receipt(value, recording_id=identity)
        verify_recording_segments(staged, value, cancelled=cancelled)
        self._scope()
        # Same commit boundary as v1. A late cancellation cannot erase a
        # validated published original; an unreceipted WAV stays recoverable.
        publish_file(staged, output, cancelled=cancelled)
        staged.unlink()
        try:
            self._scope()
            atomic_json(receipt, value)
        except (OSError, ValueError) as exc:
            raise AudioError(f'The recorded WAV was retained at {output}, but its receipt could not be saved. Preserve the file for recovery.') from exc
        return {**value, 'path': str(output)}

    def _command(self, device, staged, options, budget, seconds, *, streaming=False):
        return [self.runtime.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
            '-max_alloc', str(budget.max_alloc_bytes), '-threads', '1', '-stats_period', '0.1',
            '-progress', 'pipe:1', *self._input_arguments(device), '-map', '0:a:0', '-vn',
            '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
            '-t', str(seconds), *(['-flush_packets', '1'] if streaming else []),
            '-f', 'wav', '-rf64', 'never', str(staged)]

    def _capture_segments(self, device, staged, options, stop_requested, cancelled,
                          progress, pause_requested, state_changed, monitor_output):
        """Close each owned capture before acknowledging Pause or opening another."""
        from .recording_monitor import DryMonitor, RecordingMonitorTap, output_identity

        parts, frames, paused_seconds, attempts = [], 0, 0.0, 0
        maximum = options.max_seconds * options.sample_rate
        monitoring = {'enabled': monitor_output is not None,
            'output_identity': output_identity(monitor_output) if monitor_output is not None else None,
            'submitted_frames': 0, 'dropped_frames': 0}
        completion, last_reported, last_notice = 'device-ended', 0.0, None

        def check():
            self._scope()
            if cancelled():
                raise AudioCancelled('Recording cancelled; unfinished segments discarded.')

        def notify(state, *, live_frames=None):
            nonlocal state_changed, last_notice
            if state_changed is not None:
                notice = RecordingState(state, frames if live_frames is None else live_frames,
                    options.sample_rate, len(parts), monitor_output is not None,
                    monitoring['submitted_frames'], monitoring['dropped_frames'])
                if notice == last_notice:
                    return
                last_notice = notice
                try:
                    state_changed(notice)
                except Exception:
                    state_changed = None  # Feedback cannot destroy captured samples.

        while frames < maximum:
            check()
            if stop_requested():
                completion = 'stopped'
                break
            if pause_requested():
                notify('paused')
                started = time.monotonic()
                while pause_requested() and not stop_requested():
                    check()
                    if paused_seconds + time.monotonic() - started > MAX_PAUSED_SECONDS:
                        raise AudioError('The take exceeded its one-hour total pause allowance; no recording was published.')
                    time.sleep(.025)
                paused_seconds += time.monotonic() - started
                check()
                if stop_requested():
                    completion = 'stopped'
                    break
            if attempts >= MAX_RECORDING_SEGMENTS:
                raise AudioError('The take exceeded its 128-segment allowance; stop and review a shorter take.')
            # Device alternative identity is never replaced by a display name.
            # FFmpeg must reopen that exact input; unplugging cannot select another.
            if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
                raise ValueError('The FFmpeg runtime changed before a recording segment.')
            check()
            if stop_requested():
                completion = 'stopped'
                break
            segment_path = staged.with_name(f'segment-{attempts:03d}.wav')
            attempts += 1
            remaining = maximum - frames
            budget = AudioBudget(timeout=remaining / options.sample_rate + 20,
                max_output_bytes=options.max_bytes + MAX_RECORDING_SEGMENTS * 4096,
                max_log_bytes=512 * 1024, max_staging_files=MAX_RECORDING_SEGMENTS + 1, threads=1)
            pause_seen, live_state = False, 'opening'
            monitor = tap = None
            child_pending = None
            notify('opening')

            def finish_segment():
                nonlocal pause_seen, live_state
                if stop_requested():
                    return True
                if pause_requested():
                    pause_seen = True
                    if live_state != 'pausing':
                        live_state = 'pausing'
                        notify('pausing', live_frames=max(frames, int(last_reported * options.sample_rate)))
                return pause_seen

            def measured(seconds):
                nonlocal last_reported, live_state
                check()
                if tap is not None:
                    tap.poll(cancelled=cancelled)
                actual = min(maximum, frames + max(0, int(seconds * options.sample_rate)))
                if live_state == 'opening':
                    live_state = 'recording'
                    notify('recording', live_frames=actual)
                last_reported = max(last_reported, actual / options.sample_rate)
                progress(last_reported)

            try:
                if monitor_output is not None:
                    monitor = DryMonitor()
                    monitor.open(options.sample_rate, options.channels, monitor_output, cancelled=cancelled)
                    tap = RecordingMonitorTap(segment_path, options.sample_rate, options.channels,
                                              remaining, monitor)
                check()
                if stop_requested():
                    completion = 'stopped'
                    break
                if pause_requested():
                    continue  # No microphone is opened for an already requested pause.
                result = run_process(self._command(device, segment_path, options, budget,
                    remaining / options.sample_rate, streaming=monitor is not None),
                    budget=budget, cancelled=cancelled, stop_requested=finish_segment,
                    watch=(staged.parent,), progress=measured, cwd=staged.parent,
                    owned_windows_job=os.name == 'nt', env=native_audio_environment())
            except AudioCleanupPending as exc:
                child_pending = exc
                raise
            finally:
                if tap is not None:
                    monitoring['submitted_frames'] += tap.submitted_frames
                    monitoring['dropped_frames'] += tap.dropped_frames
                _close_segment_resources(tap, monitor, child_pending)
            # run_process has now acknowledged the whole owned tree's exit,
            # and monitoring has confirmed buffer/handle release.
            check()
            with wave.open(str(segment_path), 'rb') as reader:
                count = reader.getnframes()
                if (reader.getcomptype() != 'NONE' or reader.getsampwidth() != 2
                        or reader.getframerate() != options.sample_rate
                        or reader.getnchannels() != options.channels or count > remaining):
                    raise ValueError('A recording segment differs from the selected PCM frame allowance.')
            ended_by = ('stopped' if stop_requested() else 'duration-limit' if frames + count >= maximum
                        else 'paused' if pause_seen else 'device-ended')
            if count:
                audio = self._audio(segment_path, options, cancelled)
                parts.append((segment_path, audio, ended_by))
                frames += count
            else:
                segment_path.unlink()
            if monitoring['submitted_frames'] + monitoring['dropped_frames'] > frames:
                raise ValueError('Monitoring observed frames outside the finalized recording.')
            progress(max(last_reported, frames / options.sample_rate))
            if ended_by != 'paused':
                completion = ended_by
                break
            notify('paused')

        if not parts:
            raise AudioCancelled('No microphone frames were retained; no recording was saved.')
        notify('finalizing')
        segments = self._join_segments(parts, staged, options, cancelled)
        return completion, {'segments': segments, 'monitoring': monitoring, 'timeline': _FRAME_NOTICE}

    @staticmethod
    def _join_segments(parts, staged, options, cancelled):
        """Stream unchanged sample frames, with a hash for every final range."""
        ranges, position = [], 0
        width = options.channels * 2
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        with Path(staged).open('xb') as output:
            with wave.open(output, 'wb') as writer:
                writer.setparams((options.channels, 2, options.sample_rate, 0, 'NONE', 'PCM'))
                for path, audio, ended_by in parts:
                    # The identity interval must cover both verification and
                    # copying; a post-hash replacement cannot acquire a fresh
                    # baseline and mint a new retained-frame receipt.
                    before = regular(path)
                    if _digest(path, cancelled) != audio['sha256']:
                        raise ValueError('A completed recording segment changed before assembly.')
                    digest, remaining = sha256(), audio['frames']
                    with Path(path).open('rb') as source, wave.open(source, 'rb') as reader:
                        if stamp(before) != stamp(os.fstat(source.fileno())):
                            raise ValueError('A recording segment changed at the assembly open.')
                        while remaining:
                            if cancelled():
                                raise AudioCancelled('Recording assembly cancelled; no take was published.')
                            if shutil.disk_usage(staged.parent).free < 32 * 1024 * 1024:
                                raise AudioError('Recording assembly stopped to preserve its disk reserve.')
                            count = min(remaining, 32768)
                            data = reader.readframes(count)
                            if len(data) != count * width:
                                raise ValueError('A completed recording segment is truncated.')
                            digest.update(data)
                            writer.writeframesraw(data)
                            remaining -= count
                        after = os.fstat(source.fileno())
                    if stamp(before) != stamp(after) or stamp(before) != stamp(regular(path)):
                        raise ValueError('A recording segment changed during assembly.')
                    ranges.append({'start_frame': position, 'frames': audio['frames'],
                                   'pcm_sha256': digest.hexdigest(), 'ended_by': ended_by})
                    position += audio['frames']
            output.flush()
            os.fsync(output.fileno())
        return ranges

    @staticmethod
    def _audio(path, options, cancelled):
        from ..speech.runtime import _wave_info
        info = regular(path)
        if info.st_size > options.max_bytes:
            raise ValueError('The recording exceeds its byte allowance.')
        try:
            rate, channels, frames, width = _wave_info(path, cancelled=cancelled, max_output_bytes=options.max_bytes)
        except InterruptedError as exc:
            raise AudioCancelled('Recording PCM verification cancelled.') from exc
        if (rate != options.sample_rate or channels != options.channels or width != 2
                or frames > options.max_seconds * rate):
            raise ValueError('The recorded PCM does not match the selected format or duration allowance.')
        return {'sha256': _digest(path, cancelled), 'size_bytes': info.st_size,
                'sample_rate': rate, 'channels': channels, 'frames': frames,
                'sample_width': width, 'duration_seconds': frames / rate}

    def _receipt(self, identity):
        self._scope()
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        return validate_recording_receipt(
            read_json(self.root / (identity + '.json')), recording_id=identity)

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        verify_recording_segments(output, value, cancelled=cancelled)
        self._scope()
        return {**value, 'path': str(output)}

    def export(self, identity, destination, *, cancelled=lambda: False):
        """Stream one verified original to a new WAV outside this library.

        Export needs no microphone or FFmpeg runtime. Existing destinations are
        never replaced. The same retained receipt and source bytes must still
        match after copying and before atomic publication.
        """
        from ..research.export_file import capture_export_destination

        if not callable(cancelled):
            raise ValueError('Recording export requires a cancellation callback.')

        def check():
            self._scope()
            if cancelled():
                raise AudioCancelled('Recording export cancelled; no new WAV was published.')
            return False

        check()
        if Path(destination).suffix.casefold() != '.wav':
            raise ValueError('Choose a new WAV filename for the recording export.')
        selected = capture_export_destination(destination, private_root=self.library_root, cancelled=check)
        if not selected.require_absent:
            raise FileExistsError('The recording export already exists. Choose a new WAV filename.')
        value = self.get(identity, cancelled=check)
        receipt = {key: item for key, item in value.items() if key != 'path'}
        source = local_path(value['path'], root=self.root, exists=True)
        expected = value['audio']
        if shutil.disk_usage(selected.target.parent).free < expected['size_bytes'] + AudioBudget().reserve_disk_bytes:
            raise AudioError('Not enough free space for the selected recording export.')
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                              info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        before = stamp(regular(source))
        with TemporaryDirectory(prefix='.recording-export-', dir=selected.target.parent) as temporary:
            staged = Path(temporary) / 'recording.wav'
            digest, total = sha256(), 0
            with source.open('rb') as incoming, staged.open('xb') as output:
                if stamp(os.fstat(incoming.fileno())) != before:
                    raise ValueError('The retained recording changed before export.')
                while True:
                    check()
                    block = incoming.read(min(1024 * 1024, expected['size_bytes'] + 1 - total))
                    if not block:
                        break
                    total += len(block)
                    if total > expected['size_bytes']:
                        raise ValueError('The retained recording grew during export.')
                    digest.update(block)
                    output.write(block)
                if stamp(os.fstat(incoming.fileno())) != before or stamp(regular(source)) != before:
                    raise ValueError('The retained recording changed during export.')
                output.flush()
                os.fsync(output.fileno())
            if total != expected['size_bytes'] or digest.hexdigest() != expected['sha256']:
                raise ValueError('The retained recording differs from its verified receipt.')
            # Rehash after copying: equal-size writes can preserve timestamps
            # on Windows, including a change to an already-copied block.
            if (self._receipt(identity) != receipt or stamp(regular(source)) != before
                    or _digest(source, check) != expected['sha256']
                    or stamp(regular(source)) != before):
                raise ValueError('The retained recording changed during export.')
            if capture_export_destination(selected.requested, private_root=self.library_root,
                                          cancelled=check) != selected:
                raise ValueError('The export destination changed. Choose a new WAV filename.')
            publish_file(staged, selected.target, cancelled=check)
        return {'recording_id': identity, 'path': str(selected.target),
                'sha256': expected['sha256'], 'size_bytes': total}

    def list(self, *, cancelled=lambda: False):
        self._scope()
        paths = []
        # Propagate an unreadable directory rather than report an empty list.
        # Bound enumeration before collecting receipt names in memory.
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries):
                if cancelled():
                    raise AudioCancelled('Recording list cancelled.')
                if count >= 10000:
                    raise ValueError('The recording list exceeds its item allowance.')
                if Path(entry.name).suffix == '.json':
                    paths.append(Path(entry.path))
        records = []
        for path in sorted(paths):
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing receipts does not rehash hours of audio. Opening or
            # attaching a selected recording uses get() and verifies its bytes.
            records.append({**value, 'path': str(self.root / value['filename'])})
        return tuple(records)
