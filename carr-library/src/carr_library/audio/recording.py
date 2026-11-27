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
import time
import wave
from uuid import uuid4

from .capture_types import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, AudioUnavailable, integer
from .capture_process import publish_file, run_process
from ..operations._state import atomic_json, local_path, regular
from .jobs import read_json, sha256_file, file_identity, RenderCancelled, verified_reader
from ..operations import ResourceBroker, ResourceRequest
from ..operations.models import ResourceUnavailable, ResourceCancelled
from .runtime import resolve_ffmpeg
from ..backup_files import optional_present

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')
_SEGMENT = re.compile(r'segment-(?:00[0-9]|0[1-9][0-9]|1[01][0-9]|12[0-7])\.wav\Z')
MAX_RECORDING_SEGMENTS = 128
MAX_PAUSED_SECONDS = 3600
_FRAME_NOTICE = 'Retained PCM frames only; paused wall time is omitted. Review joins before use.'


def is_recording_stage_name(name):
    return name == 'capture.wav' or type(name) is str and _SEGMENT.fullmatch(name) is not None


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}.')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError(f'Invalid Unicode in {label}.') from exc
    return value


def _digest(path, cancelled=lambda: False):
    before = regular(path)
    if before.st_size > 2 * 1024**3:
        raise ValueError('Recording input exceeds its bounded byte allowance.')
    try:
        return sha256_file(path, cancelled=cancelled, expected_identity=file_identity(before))
    except RenderCancelled as exc:
        raise AudioCancelled('Recording verification cancelled.') from exc


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
    """Validate a saved recording declaration without opening files or devices."""
    identity = value.get('id') if type(value) is dict else None
    if (type(identity) is not str or _ID.fullmatch(identity) is None
            or (recording_id is not None and recording_id != identity)):
        raise ValueError('Invalid recording receipt identity.')
    version = value.get('schema')
    expected = {'schema', 'id', 'filename', 'title', 'created',
                'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
    if version == 'carr-recording/2':
        expected |= {'segments', 'monitoring', 'timeline'}
    if (set(value) != expected
            or version not in ('carr-recording/1', 'carr-recording/2')
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
    if version == 'carr-recording/2':
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
    """Validate exact retained PCM ranges without activating a runtime/device."""
    from .ffmpeg import wav_info
    validate_recording_receipt(value)
    if value['schema'] == 'carr-recording/1':
        return
    with verified_reader(path) as stream:
        audio, info = value['audio'], wav_info(path)
        if (info.rate, info.channels, info.frames) != (
                audio['sample_rate'], audio['channels'], audio['frames']):
            raise ValueError('Recording segment format differs from its retained PCM.')
        stream.seek(info.data_offset)
        width = info.channels * 2
        for segment in value['segments']:
            digest, remaining = sha256(), segment['frames']
            while remaining:
                if cancelled():
                    raise AudioCancelled('Recording segment verification cancelled.')
                count = min(remaining, 32768)
                data = stream.read(count * width)
                if len(data) != count * width:
                    raise ValueError('A retained recording segment is truncated.')
                digest.update(data)
                remaining -= count
            if digest.hexdigest() != segment['pcm_sha256']:
                raise ValueError('A retained recording segment differs from its frame hash.')


REFERENCE_KIND = 'microphone-recording'


def recording_reference(value):
    receipt = {key: item for key, item in value.items() if key != 'path'}
    validate_recording_receipt(receipt)
    return {'kind': REFERENCE_KIND, 'asset': 'recording-' + receipt['id'] + '.wav',
            'receipt': receipt}


def validate_recording_reference(value, *, original_text=None, source_id=None):
    if (type(value) is not dict or set(value) != {'kind', 'asset', 'receipt'}
            or value['kind'] != REFERENCE_KIND):
        raise ValueError('Invalid retained microphone source reference.')
    receipt = validate_recording_receipt(value['receipt'])
    expected = 'recording-' + receipt['id'] + '.wav'
    if value['asset'] != expected:
        raise ValueError('The recording source must use its owned relative WAV asset.')
    if original_text is not None and original_text != '[sound ' + expected + ']\n':
        raise ValueError('The Studio original differs from its retained recording source.')
    expected_source = 'recording:' + receipt['id'] + ':' + receipt['audio']['sha256']
    if source_id is not None and source_id != expected_source:
        raise ValueError('The Studio source identity differs from its recording receipt.')
    return value


def unverified_recording_files(library_root, *, cancelled=lambda: False):
    """List inert final/crash originals without opening or activating audio."""
    library = local_path(library_root, exists=True)
    root = local_path(library / 'state' / 'studio' / 'recordings', root=library)
    if not optional_present(root):
        return ()
    regular(root, directory=True)
    result, count = [], 0
    with os.scandir(root) as entries:
        for entry in entries:
            if cancelled():
                raise AudioCancelled('Recording recovery inventory cancelled.')
            count += 1
            if count > 40000:
                raise ValueError('The recording inventory exceeds its item allowance.')
            if re.fullmatch(r'\.pending-recording-[a-z0-9_]{8}', entry.name):
                directory = local_path(entry.path, root=root, exists=True)
                regular(directory, directory=True)
                with os.scandir(directory) as children:
                    for index, child in enumerate(children, 1):
                        if cancelled():
                            raise AudioCancelled('Recording recovery inventory cancelled.')
                        if index > MAX_RECORDING_SEGMENTS + 1 or not is_recording_stage_name(child.name):
                            raise ValueError('An unfinished recording contains an unsupported artifact.')
                        path = local_path(child.path, root=root, exists=True)
                        regular(path)
                        result.append(path)
                continue
            path = Path(entry.path)
            if path.suffix != '.wav' or _ID.fullmatch(path.stem) is None:
                continue
            if os.path.lexists(root / (path.stem + '.json')):
                continue
            path = local_path(path, root=root, exists=True)
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
    def __init__(self, root):
        from ..operations._locks import NativeLock
        self.root = local_path(root, exists=True)
        root_info = regular(self.root, directory=True)
        self.root_identity = (root_info.st_dev, root_info.st_ino)
        self.directory = None
        self.directory_identity = None
        self.owner = None
        self.deferred = False
        self.closed = False
        self.cleanup_error = None
        self.release_retry_required = False
        self._guard = threading.Lock()
        self.lock = None
        self.resource_context = self.resource_lease = None
        self.runtime_context = None
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

    def admit_resources(self, library, identity, device, options, cancelled, stopped, broker=None,
                        *, controlled=False, monitor_output=None):
        """The lease follows this reservation, including deferred native cleanup."""
        selected = broker or ResourceBroker.for_app(library, cancelled=cancelled)
        if (selected.root != library / 'runtime' / 'resources'
                or selected.policy_path != library / 'state' / 'operations' / 'resources.json'):
            raise ValueError('The recording resource coordinator belongs to another library.')
        limits = selected.limits
        if limits.memory_bytes <= 0:
            raise ResourceUnavailable('Configure a positive declared memory allowance before recording.')
        # RAM is an unmeasured cooperative allowance, not a physical peak claim.
        devices = ('dshow:' + device.id,)
        if monitor_output is not None:
            from .recording_monitor import output_identity
            devices += ('waveout:' + output_identity(monitor_output),)
        # Paused takes keep source parts until an independent joined original is
        # finalized. This is an explicit disk allowance, not measured usage.
        working_bytes = (2 * options.max_bytes + MAX_RECORDING_SEGMENTS * 4096
                         if controlled else options.max_bytes)
        request = ResourceRequest(cpu_slots=1, memory_bytes=limits.memory_bytes,
            disk_bytes=working_bytes + 32 * 1024**2, disk_path=self.root, devices=devices)
        waiting = [True]
        stopped_while_waiting = lambda: cancelled() or (waiting[0] and stopped())
        context = selected.reserve(request, operation_id='recording:' + identity,
            cancelled=stopped_while_waiting, timeout=30,
            run_timeout=options.max_seconds + 600 + (MAX_PAUSED_SECONDS if controlled else 0))
        try:
            lease = context.__enter__()
        except ResourceCancelled as exc:
            raise AudioCancelled('Recording stopped before opening the microphone.') from exc
        # Preserve the broker's public cancellation callback. The graceful stop
        # bit ceases to be a cancellation condition after admission.
        waiting[0] = False
        self.resource_context, self.resource_lease = context, lease
        return lease

    def bind_runtime(self, path):
        if self.runtime_context is not None:
            raise RuntimeError('The capture runtime is already retained.')
        context = verified_reader(path)
        context.__enter__()
        self.runtime_context = context

    def release_runtime(self):
        context, self.runtime_context = self.runtime_context, None
        if context is not None:
            context.__exit__(None, None, None)

    def staging(self):
        if self.directory is None:
            self.directory = Path(mkdtemp(prefix='.pending-recording-', dir=self.root))
            info = regular(self.directory, directory=True)
            self.directory_identity = (info.st_dev, info.st_ino)
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
                self.release_runtime()
            except (OSError, RuntimeError, ValueError) as exc:
                # No publication follows this deferred/failed cleanup path.
                # The actual child is quiet; keep releasing its owned resources.
                self.cleanup_error = str(exc)
            try:
                if self.directory is not None and os.path.lexists(self.directory):
                    root_info = regular(local_path(self.root, exists=True), directory=True)
                    local_path(self.directory, root=self.root, exists=True)
                    info = regular(self.directory, directory=True)
                    if ((root_info.st_dev, root_info.st_ino) != self.root_identity
                            or (info.st_dev, info.st_ino) != self.directory_identity):
                        raise ValueError('The private recording stage changed identity; its current path was retained.')
                    children = []
                    with os.scandir(self.directory) as entries:
                        for entry in entries:
                            if not is_recording_stage_name(entry.name) or len(children) >= MAX_RECORDING_SEGMENTS + 1:
                                raise ValueError('The private capture stage contains an unknown file; it was retained.')
                            regular(entry.path)
                            children.append(Path(entry.path))
                    for path in children:
                        path.unlink()
                    self.directory.rmdir()
            except (OSError, ValueError) as exc:
                # Native silence is already established. Leave failed cleanup
                # exact and inert; it must not erase a committed original.
                self.cleanup_error = str(exc)
            try:
                if self.resource_context is not None:
                    self.resource_lease.release()
                    self.resource_context.__exit__(None, None, None)
                    self.resource_context = self.resource_lease = None
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
    def __init__(self, library_root, *, ffmpeg=None, broker=None):
        self.library_root = local_path(library_root, exists=True)
        library_info = regular(self.library_root, directory=True)
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_info = regular(self.root, directory=True)
        self._roots = ((library_info.st_dev, library_info.st_ino),
                       (directory_info.st_dev, directory_info.st_ino))
        self.selected_ffmpeg, self.ffmpeg, self.broker = ffmpeg, None, broker
        self._devices, self._engine_hash = {}, None

    def _validate_root(self):
        library = regular(local_path(self.library_root, exists=True), directory=True)
        directory = regular(local_path(self.root, root=self.library_root, exists=True), directory=True)
        if ((library.st_dev, library.st_ino), (directory.st_dev, directory.st_ino)) != self._roots:
            raise ValueError('The recording library changed. Reopen the recording window.')

    def _runtime(self):
        self._validate_root()
        selected = resolve_ffmpeg(self.selected_ffmpeg)
        if selected is None:
            raise AudioUnavailable('Choose an installed local FFmpeg build before refreshing microphones.')
        regular(selected)
        self.ffmpeg = str(selected)
        return self.ffmpeg

    def devices(self, *, cancelled=lambda: False):
        self._devices, self._engine_hash = {}, None
        if os.name != 'nt':
            raise AudioUnavailable('Microphone recording currently requires Windows DirectShow and a configured local FFmpeg build.')
        self._runtime()
        identity = _digest(self.ffmpeg, cancelled)
        result = run_process([self.ffmpeg, '-hide_banner', '-nostdin', '-list_devices', 'true',
                              '-f', 'dshow', '-i', 'dummy'],
            budget=AudioBudget(timeout=15, max_log_bytes=512 * 1024),
            cancelled=cancelled, allow_failure=True, owned_tree=True)
        if result.returncode not in (0, 1):
            raise AudioUnavailable('The local FFmpeg device inspection failed. Check the configured Windows runtime.')
        devices = parse_devices(result.stderr)
        if not devices:
            raise AudioUnavailable('No identifiable microphone was reported. Check Windows microphone permission and the FFmpeg DirectShow build, then refresh devices.')
        if _digest(self.ffmpeg, cancelled) != identity:
            raise ValueError('The FFmpeg runtime changed during device discovery. Refresh devices.')
        self._devices, self._engine_hash = {item.id: item for item in devices}, identity
        return devices

    @staticmethod
    def _input_arguments(device):
        return ['-thread_queue_size', '128', '-rtbufsize', '16M', '-f', 'dshow',
                '-i', 'audio=' + device.alternative]

    @staticmethod
    def monitoring_outputs(*, cancelled=lambda: False):
        from .playback_devices import output_devices
        return output_devices(cancelled=cancelled)

    def capture(self, device_id, *, title='Microphone recording', options=None,
                stop_requested=lambda: False, cancelled=lambda: False, progress=None,
                pause_requested=None, state_changed=None, monitor_output=None):
        options = options or RecordingOptions()
        if not isinstance(options, RecordingOptions):
            raise ValueError('Validated recording options are required.')
        _text(title, 'recording title')
        if not callable(stop_requested) or not callable(cancelled):
            raise ValueError('Recording requires explicit stop and cancel callbacks.')
        controlled = pause_requested is not None or state_changed is not None or monitor_output is not None
        if pause_requested is not None and not callable(pause_requested):
            raise ValueError('Pause requires an explicit control callback.')
        if state_changed is not None and not callable(state_changed):
            raise ValueError('Recording state feedback requires a callable.')
        if monitor_output is not None:
            from .recording_monitor import output_identity
            output_identity(monitor_output)  # Typed explicit identity before admission.
        device = self._devices.get(device_id)
        if device is None or self._engine_hash is None:
            raise ValueError('Refresh microphones and explicitly select a current device before Start.')
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        self._validate_root()
        identity = uuid4().hex
        if os.path.lexists(self.root / (identity + '.wav')) or os.path.lexists(self.root / (identity + '.json')):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
        reservation = _CaptureReservation(self.root)
        try:
            lease = reservation.admit_resources(self.library_root, identity, device, options,
                cancelled, stop_requested, self.broker,
                controlled=controlled, monitor_output=monitor_output)
            self._validate_root()
            if cancelled() or stop_requested():
                raise AudioCancelled('Recording stopped before opening the microphone.')
            return self._capture(identity, device, title, options, stop_requested,
                                 lease.cancelled, progress, reservation,
                                 pause_requested=pause_requested, state_changed=state_changed,
                                 monitor_output=monitor_output)
        except AudioCleanupPending as exc:
            reservation.retain(exc.cleanup_owner)
            exc.recording_reservation = reservation
            raise
        finally:
            if not reservation.deferred:
                reservation.close()

    def _capture(self, identity, device, title, options, stop_requested, cancelled, progress, reservation,
                 *, pause_requested=None, state_changed=None, monitor_output=None):
        output, receipt = self.root / (identity + '.wav'), self.root / (identity + '.json')
        if os.path.lexists(output) or os.path.lexists(receipt):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
        reservation.bind_runtime(self.ffmpeg)
        if _digest(self.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed. Refresh microphones before recording.')
        budget = AudioBudget(timeout=options.max_seconds + 20, max_output_bytes=options.max_bytes,
                             max_log_bytes=512 * 1024, max_staging_files=4, threads=1)
        controlled = pause_requested is not None or state_changed is not None or monitor_output is not None
        predicted_pcm = options.max_seconds * options.sample_rate * options.channels * 2
        working_pcm = 2 * predicted_pcm + MAX_RECORDING_SEGMENTS * 4096 if controlled else predicted_pcm
        if shutil.disk_usage(self.root).free < working_pcm + budget.reserve_disk_bytes:
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
        controlled = pause_requested is not None or state_changed is not None or monitor_output is not None
        extra = {}
        if controlled:
            completion, extra = self._capture_segments(device, staged, options, stop_requested,
                cancelled, report, pause_requested or (lambda: False), state_changed, monitor_output)
        else:
            command = self._command(device, staged, options, budget, options.max_seconds)
            result = run_process(command, budget=budget, cancelled=cancelled,
                stop_requested=stop_requested, watch=(staged,), progress=report,
                cwd=temporary, owned_tree=True)
            completion = 'stopped' if result.stopped else None
        self._validate_root()
        audio = self._audio(staged, options, cancelled)
        if _digest(self.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
        reservation.release_runtime()  # Final immutable check before publication.
        value = {'schema': 'carr-recording/2' if controlled else 'carr-recording/1', 'id': identity, 'filename': output.name,
            'title': title, 'created': datetime.now(timezone.utc).isoformat(),
            'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
            'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
            'completion': completion or ('duration-limit' if audio['duration_seconds'] >= options.max_seconds - .1
                                         else 'device-ended'), **extra}
        # This is the commit boundary. A late Stop/Cancel cannot erase a
        # validated published original. A receipt failure leaves that WAV
        # available for explicit recovery, never claims successful capture.
        validate_recording_receipt(value, recording_id=identity)
        verify_recording_segments(staged, value, cancelled=cancelled)
        self._validate_root()
        publish_file(staged, output, cancelled=cancelled)
        staged.unlink(missing_ok=True)
        try:
            # Publication moves the owned file. Recheck its actual retained
            # bytes before acknowledging a receipt, even if an outside writer
            # touched the stage around that boundary. A late user cancellation
            # cannot erase this already-published original.
            if self._audio(output, options, lambda: False) != audio:
                raise ValueError('The recorded original changed during publication.')
            verify_recording_segments(output, value)
            self._validate_root()
            atomic_json(receipt, value)
        except (OSError, ValueError, RuntimeError) as exc:
            raise AudioError('The recorded WAV was retained, but its receipt could not be saved. The saved-input recovery list and complete backup preserve its original bytes.') from exc
        return {**value, 'path': str(output)}

    def _command(self, device, staged, options, budget, seconds, *, streaming=False):
        return [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
            '-max_alloc', str(budget.max_alloc_bytes), '-threads', '1', '-stats_period', '0.1',
            '-progress', 'pipe:1', *self._input_arguments(device), '-map', '0:a:0', '-vn',
            '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
            '-t', str(seconds), *(['-flush_packets', '1'] if streaming else []),
            '-f', 'wav', '-rf64', 'never', str(staged)]

    def _capture_segments(self, device, staged, options, stop_requested, cancelled,
                          progress, pause_requested, state_changed, monitor_output):
        """Acknowledge Pause only after the exact input tree and output are quiet."""
        from .cleanup import pending_cleanup
        from .recording_monitor import DryMonitor, RecordingMonitorTap, output_identity

        parts, frames, paused_seconds, attempts = [], 0, 0.0, 0
        maximum = options.max_seconds * options.sample_rate
        monitoring = {'enabled': monitor_output is not None,
            'output_identity': output_identity(monitor_output) if monitor_output is not None else None,
            'submitted_frames': 0, 'dropped_frames': 0}
        completion, last_reported, last_notice = 'device-ended', 0.0, None

        def check():
            self._validate_root()
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
                    if paused_seconds + time.monotonic() - started >= MAX_PAUSED_SECONDS:
                        raise AudioError('The take exceeded its one-hour total pause allowance; no recording was published.')
                    time.sleep(.025)
                paused_seconds += time.monotonic() - started
                check()
                if paused_seconds >= MAX_PAUSED_SECONDS:
                    raise AudioError('The take exceeded its one-hour total pause allowance; no recording was published.')
                if stop_requested():
                    completion = 'stopped'
                    break
            if attempts >= MAX_RECORDING_SEGMENTS:
                raise AudioError('The take exceeded its 128-segment allowance; stop and review a shorter take.')
            # The original runtime reader remains held across every segment and
            # paused interval. A resume never chooses a replacement by display name.
            if _digest(self.ffmpeg, cancelled) != self._engine_hash:
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
            monitor = tap = pending_child = None
            notify('opening')

            def finish_segment():
                nonlocal pause_seen, live_state
                if stop_requested():
                    return True
                if pause_requested():
                    pause_seen = True
                    if live_state != 'pausing':
                        live_state = 'pausing'
                        notify('pausing', live_frames=min(maximum, max(frames,
                               int(last_reported * options.sample_rate))))
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

            def close_segment():
                """Compose actual existing owners, never infer native shutdown."""
                errors, quiet = [], True
                for owner in (tap, monitor):
                    if owner is not None:
                        try:
                            owner.close()
                        except (Exception, KeyboardInterrupt, SystemExit) as exc:
                            errors.append(exc)
                if pending_child is not None:
                    try:
                        pending_child.close()
                        quiet = pending_child.quiescent_event.is_set()
                    except (Exception, KeyboardInterrupt, SystemExit) as exc:
                        errors.append(exc)
                        quiet = False
                if errors:
                    raise AudioError('An owned recording resource has not confirmed cleanup.') from errors[0]
                return quiet

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
                    continue  # Do not open input for an already requested pause.
                run_process(self._command(device, segment_path, options, budget,
                    remaining / options.sample_rate, streaming=monitor is not None),
                    budget=budget, cancelled=cancelled, stop_requested=finish_segment,
                    watch=(staged.parent,), progress=measured, cwd=staged.parent, owned_tree=True)
                if tap is not None:
                    tap.poll(cancelled=cancelled)
            except AudioCleanupPending as exc:
                pending_child = exc.cleanup_owner
                raise
            finally:
                try:
                    if close_segment() is False:
                        raise AudioError('A recording segment still owns its native child.')
                except (Exception, KeyboardInterrupt, SystemExit) as exc:
                    # The reservation retains its stage, runtime reader, device
                    # and resource lease until this compound owner is quiescent.
                    raise pending_cleanup(close_segment,
                        message='Microphone or monitoring cleanup is still pending; its admission remains reserved.') from exc
                if tap is not None:
                    monitoring['submitted_frames'] += tap.submitted_frames
                    monitoring['dropped_frames'] += tap.dropped_frames
            check()
            # Count zero-frame pauses without claiming an original. Nonempty
            # segments additionally pass the strict complete RIFF/RF64 validator.
            with verified_reader(segment_path) as stream, wave.open(stream, 'rb') as reader:
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
                if regular(segment_path).st_size > 4096:
                    raise ValueError('An empty recording segment exceeds its header allowance.')
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
        """Copy unchanged PCM under native immutable readers, hash each final range."""
        from .ffmpeg import wav_info
        ranges, position = [], 0
        width = options.channels * 2
        with Path(staged).open('xb') as output:
            with wave.open(output, 'wb') as writer:
                writer.setparams((options.channels, 2, options.sample_rate, 0, 'NONE', 'PCM'))
                for path, audio, ended_by in parts:
                    with verified_reader(path) as source:
                        if _digest(path, cancelled) != audio['sha256']:
                            raise ValueError('A completed recording segment changed before assembly.')
                        info = wav_info(path)
                        if (info.rate, info.channels, info.frames) != (
                                options.sample_rate, options.channels, audio['frames']):
                            raise ValueError('A recording segment changed its PCM format before assembly.')
                        source.seek(info.data_offset)
                        digest, remaining = sha256(), info.frames
                        while remaining:
                            if cancelled():
                                raise AudioCancelled('Recording assembly cancelled; no take was published.')
                            if shutil.disk_usage(staged.parent).free < 32 * 1024**2:
                                raise AudioError('Recording assembly stopped to preserve its disk reserve.')
                            count = min(remaining, 32768)
                            data = source.read(count * width)
                            if len(data) != count * width:
                                raise ValueError('A completed recording segment is truncated.')
                            digest.update(data)
                            writer.writeframesraw(data)
                            remaining -= count
                    ranges.append({'start_frame': position, 'frames': audio['frames'],
                                   'pcm_sha256': digest.hexdigest(), 'ended_by': ended_by})
                    position += audio['frames']
            output.flush()
            os.fsync(output.fileno())
        return ranges

    @staticmethod
    def _audio(path, options, cancelled):
        from .ffmpeg import wav_info
        if cancelled():
            raise AudioCancelled('Recording PCM verification cancelled.')
        info = regular(path)
        if info.st_size > options.max_bytes:
            raise ValueError('The recording exceeds its byte allowance.')
        with verified_reader(path, expected_identity=file_identity(info)):
            pcm = wav_info(path)
            rate, channels, frames, width = pcm.rate, pcm.channels, pcm.frames, 2
            if (rate != options.sample_rate or channels != options.channels or width != 2
                    or not 0 < frames <= options.max_seconds * rate):
                raise ValueError('The recorded PCM does not match the selected format or duration allowance.')
            digest = _digest(path, cancelled)
        return {'sha256': digest, 'size_bytes': info.st_size,
                'sample_rate': rate, 'channels': channels, 'frames': frames,
                'sample_width': width, 'duration_seconds': frames / rate}

    def _receipt(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        return validate_recording_receipt(
            read_json(self.root / (identity + '.json'), limit=65536), recording_id=identity)

    def get(self, identity, *, cancelled=lambda: False):
        self._validate_root()
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        verify_recording_segments(output, value, cancelled=cancelled)
        return {**value, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        self._validate_root()
        paths = []
        with os.scandir(self.root) as entries:
            for count, entry in enumerate(entries):
                if cancelled():
                    raise AudioCancelled('Recording list cancelled.')
                if count >= 30000:
                    raise ValueError('The recording list exceeds its item allowance.')
                if entry.name.endswith('.json'):
                    if _ID.fullmatch(entry.name[:-5]) is None:
                        raise ValueError('An unrecognized recording receipt needs review.')
                    paths.append(Path(entry.path))
                    if len(paths) > 10000:
                        raise ValueError('The recording list exceeds its item allowance.')
        records = []
        for path in sorted(paths):
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing never reads or activates the saved PCM or a runtime.
            records.append({**value, 'path': str(self.root / value['filename'])})
        return tuple(records)
