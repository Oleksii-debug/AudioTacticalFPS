"""Explicit local microphone capture through the configured FFmpeg runtime.

DirectShow discovery does not record. A capture needs a device from the current
inventory and a separate caller action. Completed PCM and its relative receipt
live in ordinary private state, so existing backups retain them without another
archive format. No microphone, model, driver or network fallback is selected.

Upstream device contract: https://ffmpeg.org/ffmpeg-devices.html#dshow
"""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import wave
from uuid import uuid4

from .models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, AudioUnavailable, integer
from .process import publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular, state_lock, sync_directory

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')
_MAX_RECORDINGS = 10000
_MAX_RECORDING_ENTRIES = 30000
MAX_RECORDING_SEGMENTS = 128
MAX_PAUSED_SECONDS = 3600
_FRAME_NOTICE = 'Retained PCM frames only; paused wall time is omitted. Review joins before use.'


def _notice(result, message):
    result.setdefault('warnings', []).append(message)


def _stage_entries(directory):
    # A capture owns only one known basename. Never enumerate an unbounded
    # injected directory or recursively remove unrelated entries.
    with os.scandir(directory) as listing:
        names = []
        for item in listing:
            names.append(item.name)
            if len(names) > 1:
                break
    return names


def _cleanup_recording_stage(root, directory, owner, completed):
    observed = regular(local_path(directory, exists=True), directory=True)
    if (observed.st_dev, observed.st_ino) != (owner.st_dev, owner.st_ino):
        raise ValueError('The capture staging directory changed; all entries were preserved.')
    entries = _stage_entries(directory)
    if entries == ['capture.wav']:
        if completed.get('status') == 'complete':
            raise OSError('The owned capture staging link still needs receipt recovery.')
        regular(directory / 'capture.wav')
        (directory / 'capture.wav').unlink()
    elif entries:
        raise ValueError('Unexpected capture staging entries were preserved.')
    directory.rmdir()
    sync_directory(root)


def _audio_stamp(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink)


def _read_recording(path, expected, maximum, cancelled=lambda: False, *, owner=None):
    """Bounded receipt-specific PCM verification; ordinary source guards stay strict."""
    path = Path(path)
    parent = local_path(path.parent, exists=True)
    parent_owner = regular(parent, directory=True)

    def check():
        if cancelled():
            raise AudioCancelled('Recording verification cancelled.')

    def validate(info):
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_nlink not in (1, 2) or info.st_size != expected['size_bytes']
                or not 44 <= info.st_size <= maximum
                or (owner is not None and ((info.st_dev, info.st_ino) !=
                    (owner.st_dev, owner.st_ino) or info.st_nlink != 2))):
            raise ValueError('The retained recording differs from its verified receipt or publication identity.')
        return _audio_stamp(info)

    before = validate(path.lstat())

    def stable(stream):
        observed = regular(local_path(parent, exists=True), directory=True)
        if ((observed.st_dev, observed.st_ino) != (parent_owner.st_dev, parent_owner.st_ino)
                or validate(path.lstat()) != before or validate(os.fstat(stream.fileno())) != before):
            raise ValueError('The retained recording changed during verification.')

    def digest(stream):
        stream.seek(0)
        hasher, count = sha256(), 0
        while block := stream.read(1024 * 1024):
            check()
            count += len(block)
            if count > expected['size_bytes']:
                raise ValueError('The retained recording grew during verification.')
            hasher.update(block)
        if count != expected['size_bytes'] or hasher.hexdigest() != expected['sha256']:
            raise ValueError('The retained recording differs from its verified receipt.')
        stable(stream)

    check()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        stable(stream)
        digest(stream)
        stream.seek(0)
        with wave.open(stream, 'rb') as audio:
            if (audio.getcomptype() != 'NONE'
                    or (audio.getframerate(), audio.getnchannels(), audio.getnframes(), audio.getsampwidth()) !=
                       (expected['sample_rate'], expected['channels'], expected['frames'], expected['sample_width'])):
                raise ValueError('The retained PCM format differs from its verified receipt.')
            count = 0
            pcm_bytes = expected['frames'] * expected['channels'] * expected['sample_width']
            while block := audio.readframes(65536):
                check()
                count += len(block)
                if count > pcm_bytes:
                    raise ValueError('The retained PCM exceeds its verified frame count.')
            if count != pcm_bytes:
                raise ValueError('The retained recording contains truncated PCM.')
        digest(stream)
        check()
    return before


def _recording_result(value):
    return dict(value, output_verified=True,
                receipt_status=value.get('status', 'complete'),
                receipt_finalized=value.get('status', 'complete') == 'complete')


def _complete_recording(path, previous):
    complete_receipt = dict(previous, status='complete')
    completed = _recording_result(complete_receipt)
    try:
        saved = read_json(path)
        if saved not in (previous, complete_receipt):
            raise ValueError('The prepared recording receipt changed before finalization.')
        if saved != complete_receipt:
            atomic_json(path, complete_receipt, replace=True)
    except (Exception, KeyboardInterrupt) as exc:
        saved = None
        try:
            saved = read_json(path)
        except (ValueError, OSError):
            pass
        known = saved == previous or saved == complete_receipt
        completed['receipt_status'] = saved.get('status', 'complete') if known else 'unconfirmed'
        completed['receipt_finalized'] = saved == complete_receipt
        _notice(completed, 'The recorded WAV is saved and verified, but receipt finalization '
                'needs attention: ' + str(exc)[:1000] + '. Reopen this saved recording; do not record it again.')
    return completed



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
    fields = {'schema', 'id', 'filename', 'title', 'created',
              'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
    version = value.get('schema')
    if version == 'carr-recording/2':
        fields |= {'segments', 'monitoring', 'timeline'}
    if (set(value) - {'status', 'retake_of'} != fields
            or value.get('status', 'complete') not in ('prepared', 'complete')
            or version not in ('carr-recording/1', 'carr-recording/2')
            or value['filename'] != identity + '.wav'
            or type(value['limits']) is not dict
            or set(value['limits']) != {'max_seconds', 'sample_rate', 'channels', 'max_bytes'}
            or type(value['ffmpeg_sha256']) is not str
            or _HASH.fullmatch(value['ffmpeg_sha256']) is None
            or value['completion'] not in ('stopped', 'duration-limit', 'device-ended')):
        raise ValueError('Invalid recording receipt.')
    if 'retake_of' in value:
        previous = value['retake_of']
        if (type(previous) is not dict or set(previous) != {'id', 'audio_sha256'}
                or type(previous['id']) is not str or _ID.fullmatch(previous['id']) is None
                or previous['id'] == identity or type(previous['audio_sha256']) is not str
                or _HASH.fullmatch(previous['audio_sha256']) is None):
            raise ValueError('Invalid prior recording identity for this take.')
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


def unverified_recording_files(library_root, *, cancelled=lambda: False):
    """List owned final WAVs without receipts; never inspect or activate audio."""
    library = local_path(library_root, exists=True)
    root = local_path(library / 'state' / 'studio' / 'recordings', root=library)
    if not root.exists():
        return ()
    regular(root, directory=True)
    result = []
    with os.scandir(root) as listing:
        for count, entry in enumerate(listing, 1):
            if cancelled():
                raise AudioCancelled('Recording inventory cancelled.')
            if count > _MAX_RECORDING_ENTRIES:
                raise ValueError('The recording inventory exceeds its total entry allowance.')
            name = entry.name
            if not name.endswith('.wav') or _ID.fullmatch(name[:-4]) is None:
                continue
            if os.path.lexists(root / (name[:-4] + '.json')):
                continue
            path = local_path(root / name, root=root, exists=True)
            regular(path)
            result.append(path)
            if len(result) > _MAX_RECORDINGS:
                raise ValueError('The recording inventory exceeds its item allowance.')
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
    def __init__(self, root, completed):
        from filelock import FileLock, Timeout
        self.root = local_path(root, exists=True)
        self.directory = None
        self.directory_owner = None
        self.completed = completed
        self.owner = None
        self.deferred = False
        self.closed = False
        self.cleanup_error = None
        self._guard = threading.Lock()
        self.lock = self.state_lock = None
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
            state_path = self.root.parent.parent / '.studio-recording.lock'
            if os.path.lexists(state_path):
                regular(state_path)
            self.state_lock = FileLock(state_path, timeout=0, mode=0o600, thread_local=False)
            try:
                self.state_lock.acquire()
            except Timeout:
                raise RuntimeError('This project is already being changed by another operation.') from None
        except BaseException:
            if self.state_lock is not None:
                self.state_lock.release()
            if self.lock is not None:
                self.lock.release()
            _CAPTURE.release()
            raise
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.add(self)

    def staging(self, identity):
        if self.directory is None:
            if _ID.fullmatch(identity) is None:
                raise ValueError('Invalid capture staging identity.')
            directory = self.root / ('.pending-recording-' + identity)
            directory.mkdir(mode=0o700)
            self.directory, self.directory_owner = directory, regular(directory, directory=True)
        return self.directory

    def retain(self, owner):
        with self._guard:
            self.owner = owner
            self.deferred = True
        # Register only a plain callback. The process runner owns every native
        # termination attempt; neither wx nor the capture executor waits here.
        # Native closure is confirmed before this callback runs. The owner's
        # public event also covers the remaining callback drain, so waiting for
        # that event from inside the drain would retain this reservation forever.
        # Bind the exact owner: a superseded callback cannot release a newer one.
        owner.add_quiescent_callback(lambda: self._close(confirmed_owner=owner))

    def close(self):
        return self._close()

    def _close(self, *, confirmed_owner=None):
        with self._guard:
            if self.closed:
                return True
            if confirmed_owner is not None and confirmed_owner is not self.owner:
                return False
            if (self.owner is not None and not self.owner.quiescent
                    and confirmed_owner is not self.owner):
                return False
            try:
                if self.directory is not None and os.path.lexists(self.directory):
                    _cleanup_recording_stage(self.root, self.directory,
                                             self.directory_owner, self.completed)
            except (OSError, ValueError, KeyboardInterrupt) as exc:
                # Native silence is already established. Leave failed cleanup
                # exact and inert; it must not erase a committed original.
                self.cleanup_error = str(exc)
            try:
                self.lock.release()
                self.state_lock.release()
            except (Exception, KeyboardInterrupt) as exc:
                self.cleanup_error = str(exc)
                return False
            self.closed = True
            _CAPTURE.release()
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


def verify_recording_segments(path, value, *, cancelled=lambda: False):
    """Verify retained PCM ranges without opening an engine or native device."""
    validate_recording_receipt(value, recording_id=value['id'])
    if value['schema'] == 'carr-recording/1':
        return
    before = regular(path)
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream, wave.open(stream, 'rb') as reader:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('The recording changed before segment verification.')
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


class RecordingService:
    def __init__(self, library_root, *, runtime=None, broker=None):
        self.library_root = local_path(library_root, exists=True)
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime = runtime
        self._broker = broker
        self._devices, self._engine_hash = {}, None

    def _admission_broker(self, cancelled=lambda: False):
        from ..operations import ResourceBroker, ResourceStateError
        if self._broker is None:
            self._broker = ResourceBroker.for_app(self.library_root, cancelled=cancelled)
        if (self._broker.root != self.library_root / 'runtime' / 'resources'
                or self._broker.policy_path != self.library_root / 'state' / 'operations' / 'resources.json'):
            raise ResourceStateError('The recording resource policy belongs to another data folder.')
        return self._broker

    def _runtime(self):
        if self.runtime is None:
            from ..runtime_settings import load_settings
            from .dsp import AudioRuntime
            settings = load_settings(self.library_root / 'state')
            self.runtime = AudioRuntime(self.root, ffmpeg=settings['ffmpeg'], ffprobe=settings['ffprobe'])
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

    @staticmethod
    def monitoring_outputs(*, cancelled=lambda: False):
        from .output_devices import output_devices
        if cancelled():
            raise AudioCancelled('Monitoring output discovery cancelled.')
        outputs = output_devices()
        if cancelled():
            raise AudioCancelled('Monitoring output discovery cancelled.')
        return outputs

    def capture(self, device_id, *, title='Microphone recording', options=None,
                stop_requested=lambda: False, cancelled=lambda: False, progress=None,
                pause_requested=None, state_changed=None, monitor_output=None, retake_of=None):
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
        if monitor_output is not None:
            from .output_devices import OutputDevice
            if type(monitor_output) is not OutputDevice:
                raise ValueError('Monitoring requires an explicitly selected identifiable output.')
        controlled = pause_requested is not None or monitor_output is not None
        device = self._devices.get(device_id)
        if device is None or self._engine_hash is None:
            raise ValueError('Refresh microphones and explicitly select a current device before Start.')
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        # A retake is a new capture. Verify the explicitly selected completed
        # original before acquiring the capture lock; get() owns that same lock.
        previous_take = self._prepare_retake(retake_of, cancelled=cancelled)
        completed = {}
        reservation = _CaptureReservation(self.root, completed)
        from ..operations import ResourceCancelled, ResourceRequest
        waiting, lease = True, None
        def admission_cancelled():
            return bool(cancelled()) or (waiting and bool(stop_requested()))
        try:
            with ExitStack() as admission:
                try:
                    broker = self._admission_broker(admission_cancelled)
                    budget = AudioBudget(threads=1)
                    pcm = options.max_seconds * options.sample_rate * options.channels * 2 + 4096
                    request = ResourceRequest(cpu_slots=1,
                        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
                        disk_bytes=2 * pcm + budget.reserve_disk_bytes + (MAX_RECORDING_SEGMENTS * 4096 if controlled else 0), disk_path=self.root,
                        devices=('microphone:' + device.id,))
                    lease = admission.enter_context(broker.reserve(request,
                        operation_id='recording:' + uuid4().hex, cancelled=admission_cancelled,
                        timeout=60, run_timeout=options.max_seconds + (MAX_PAUSED_SECONDS if controlled else 0) + 20))
                    if admission_cancelled():
                        raise AudioCancelled('Recording stopped before opening the microphone.')
                    waiting = False  # Stop now finalizes PCM; cancellation still discards.
                    self._capture(device, title, options, stop_requested,
                                  lease.cancelled, progress, reservation, completed,
                                  pause_requested, state_changed, monitor_output, previous_take)
                except AudioCleanupPending as exc:
                    reservation.retain(exc.cleanup_owner)
                    exc.recording_reservation = reservation
                    retained = admission.pop_all()
                    exc.cleanup_owner.add_quiescent_callback(retained.close)
                    raise
                except ResourceCancelled as exc:
                    raise AudioCancelled('Recording cancelled while waiting for shared local resources.') from exc
                finally:
                    if not reservation.deferred:
                        released = reservation.close()
                        if completed.get('status') == 'complete':
                            if released is False:
                                completed['state_release_confirmed'] = False
                            if reservation.cleanup_error is not None:
                                completed['cleanup_pending'] = True
                                _notice(completed, 'The recorded WAV is saved and verified. Capture cleanup '
                                        'or state lock release needs attention: ' + reservation.cleanup_error[:1000])
        except (Exception, KeyboardInterrupt) as exc:
            if completed.get('status') != 'complete':
                raise
            completed['resource_release_confirmed'] = False
            _notice(completed, 'The recorded WAV is saved and verified. Shared resource release '
                    'or final notification was not confirmed: ' + type(exc).__name__ + ': ' + str(exc)[:1000])
        finally:
            if lease is not None and lease.cleanup_pending and completed.get('status') == 'complete':
                completed['resource_cleanup_pending'] = True
                _notice(completed, 'The microphone resource lock was released; stale coordination '
                        'metadata awaits normal cleanup.')
        return completed

    def _prepare_retake(self, identity, *, cancelled):
        if identity is None:
            return None
        value = self._receipt(identity)
        if value.get('status', 'complete') != 'complete':
            raise ValueError('Open and finish recovery of the prior recording before making another take.')
        receipt = self.root / (identity + '.json')
        output = self.root / value['filename']
        # Ordinary single-link originals only: taking another recording must not
        # finalize an old intent or clean any old staging alias as a side effect.
        receipt_stamp = _audio_stamp(regular(receipt))
        audio_stamp = _audio_stamp(regular(output))
        verified = self.get(identity, cancelled=cancelled)
        if (not verified.get('output_verified') or not verified.get('receipt_finalized')
                or self._receipt(identity) != value
                or _audio_stamp(regular(receipt)) != receipt_stamp
                or _audio_stamp(regular(output)) != audio_stamp):
            raise ValueError('The selected prior recording changed before the new take.')
        return value, receipt_stamp, audio_stamp

    def _check_retake(self, previous, *, cancelled):
        if previous is None:
            return None
        value, receipt_stamp, audio_stamp = previous
        identity = value['id']
        receipt, output = self.root / (identity + '.json'), self.root / value['filename']
        def unchanged():
            if cancelled():
                raise AudioCancelled('New take cancelled while checking the prior recording.')
            if (self._receipt(identity) != value
                    or _audio_stamp(regular(receipt)) != receipt_stamp
                    or _audio_stamp(regular(output)) != audio_stamp):
                raise ValueError('The selected prior recording changed; no new take was published.')
        unchanged()
        # The capture reservation already owns studio-recording. Reuse the
        # read-only PCM validators here, never recursively acquire get()'s lock.
        _read_recording(output, value['audio'], value['limits']['max_bytes'], cancelled)
        verify_recording_segments(output, value, cancelled=cancelled)
        unchanged()
        return {'id': identity, 'audio_sha256': value['audio']['sha256']}

    def _capture(self, device, title, options, stop_requested, cancelled, progress, reservation, completed,
                 pause_requested=None, state_changed=None, monitor_output=None, previous_take=None):
        identity = uuid4().hex
        output, receipt = self.root / (identity + '.wav'), self.root / (identity + '.json')
        if os.path.lexists(output) or os.path.lexists(receipt):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
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

        self._check_retake(previous_take, cancelled=cancelled)
        parent_owner = regular(self.root, directory=True)
        temporary = reservation.staging(identity)
        staged = temporary / 'capture.wav'
        extra = {}
        if controlled:
            completion, extra = self._capture_segments(device, staged, options, stop_requested,
                cancelled, report, pause_requested or (lambda: False), state_changed, monitor_output)
        else:
            result = run_process(self._command(device, staged, options, budget, options.max_seconds),
                budget=budget, cancelled=cancelled, stop_requested=stop_requested,
                watch=(staged,), progress=report, cwd=temporary, owned_tree=True)
        audio = self._audio(staged, options, cancelled)
        if not controlled:
            completion = ('stopped' if result.stopped else 'duration-limit'
                          if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended')
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
        value = {'schema': 'carr-recording/2' if controlled else 'carr-recording/1', 'id': identity, 'filename': output.name,
            'title': title, 'created': datetime.now(timezone.utc).isoformat(),
            'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
            'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
            'completion': completion, 'status': 'prepared', **extra}
        if previous_take is not None:
            previous = previous_take[0]
            value['retake_of'] = {'id': previous['id'], 'audio_sha256': previous['audio']['sha256']}
        # Prepared metadata precedes the create-only publication. Process
        # death can leave it prepared, but never invents a completed WAV.
        validate_recording_receipt(value, recording_id=identity)
        verify_recording_segments(staged, value, cancelled=cancelled)
        atomic_json(receipt, value)
        owner = regular(staged)
        observed_parent = regular(local_path(self.root, exists=True), directory=True)
        if (observed_parent.st_dev, observed_parent.st_ino) != (parent_owner.st_dev, parent_owner.st_ino):
            raise ValueError('The recording directory changed before publication.')
        self._check_retake(previous_take, cancelled=cancelled)
        publication_error = None
        try:
            publish_file(staged, output, cancelled=cancelled)
        except (AudioCancelled, FileExistsError):
            raise
        except (Exception, KeyboardInterrupt) as exc:
            publication_error = exc
        try:
            stamp = _read_recording(output, audio, options.max_bytes, owner=owner)
            current_parent = regular(local_path(self.root, exists=True), directory=True)
            if (current_parent.st_dev, current_parent.st_ino) != (parent_owner.st_dev, parent_owner.st_ino):
                raise ValueError('The recording directory changed during publication.')
            if _audio_stamp(staged.lstat()) != stamp:
                raise ValueError('The retained WAV is not this capture attempt\'s linked staging file.')
        except Exception:
            if publication_error is not None:
                raise publication_error
            raise
        completed.update(_complete_recording(receipt, value))
        completed['path'] = str(output)
        if publication_error is not None:
            _notice(completed, 'The recorded WAV was published and its exact PCM verified, '
                    'but the writer reported a later interruption: ' + str(publication_error)[:1000])
        try:
            staged.unlink()
        except (OSError, KeyboardInterrupt) as exc:
            completed['cleanup_pending'] = True
            _notice(completed, 'The recorded WAV is saved and verified. Its known temporary '
                    'link needs cleanup: ' + str(exc)[:1000])
        return completed

    def _command(self, device, staged, options, budget, seconds, *, streaming=False):
        return [self.runtime.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
            '-max_alloc', str(budget.max_alloc_bytes), '-threads', '1',
            '-filter_threads', '1', '-filter_complex_threads', '1', '-stats_period', '0.1',
            '-progress', 'pipe:1', *self._input_arguments(device), '-map', '0:a:0', '-vn',
            '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
            '-t', str(seconds), *(['-flush_packets', '1'] if streaming else []),
            '-f', 'wav', '-rf64', 'never', str(staged)]

    def _capture_segments(self, device, staged, options, stop_requested, cancelled,
                          progress, pause_requested, state_changed, monitor_output):
        from .cleanup import retained_temporary_directory
        with retained_temporary_directory(prefix='.segments-', dir=staged.parent) as segment_root:
            return self._capture_segments_owned(device, staged, options, stop_requested, cancelled,
                progress, pause_requested, state_changed, monitor_output, Path(segment_root))

    def _capture_segments_owned(self, device, staged, options, stop_requested, cancelled,
                                progress, pause_requested, state_changed, monitor_output, segment_root):
        """Close each owned capture before acknowledging Pause or opening another."""
        from .recording_monitor import DryMonitor, RecordingMonitorTap, output_identity

        parts, frames, paused_seconds, attempts = [], 0, 0.0, 0
        maximum = options.max_seconds * options.sample_rate
        monitoring = {'enabled': monitor_output is not None,
            'output_identity': output_identity(monitor_output) if monitor_output is not None else None,
            'submitted_frames': 0, 'dropped_frames': 0}
        completion, last_reported, last_notice = 'device-ended', 0.0, None

        def check():
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
            segment_path = segment_root / f'segment-{attempts:03d}.wav'
            attempts += 1
            remaining = maximum - frames
            budget = AudioBudget(timeout=remaining / options.sample_rate + 20,
                max_output_bytes=options.max_bytes + MAX_RECORDING_SEGMENTS * 4096,
                max_log_bytes=512 * 1024, max_staging_files=MAX_RECORDING_SEGMENTS + 1, threads=1)
            pause_seen, live_state = False, 'opening'
            monitor = tap = None
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

            pending_process = None
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
                    watch=(segment_root,), progress=measured, cwd=segment_root, owned_tree=True)
            except AudioCleanupPending as exc:
                pending_process = exc.cleanup_owner
                raise
            finally:
                close_errors = []
                for owned in (tap, monitor):
                    if owned is not None:
                        try:
                            owned.close()
                        except (Exception, KeyboardInterrupt) as exc:
                            close_errors.append(exc)
                if tap is not None:
                    monitoring['submitted_frames'] += tap.submitted_frames
                    monitoring['dropped_frames'] += tap.dropped_frames
                if close_errors:
                    from .cleanup import pending_cleanup
                    def release_capture_outputs(tap=tap, monitor=monitor, process_owner=pending_process):
                        stopped = process_owner.close() if process_owner is not None else True
                        retry_errors = []
                        for owned in (tap, monitor):
                            if owned is not None:
                                try:
                                    owned.close()
                                except (Exception, KeyboardInterrupt) as error:
                                    retry_errors.append(error)
                        if retry_errors:
                            raise retry_errors[0]
                        return stopped is not False
                    raise pending_cleanup(release_capture_outputs,
                        message='Monitoring cleanup is unconfirmed; capture files and resource ownership remain retained.') from close_errors[0]
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
                    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                    with os.fdopen(os.open(path, flags), 'rb') as source, wave.open(source, 'rb') as reader:
                        if stamp(os.fstat(source.fileno())) != stamp(before):
                            raise ValueError('A recording segment changed before assembly.')
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
                    # Windows timestamps alone can remain unchanged across a short
                    # same-size write. Recheck exact verified source bytes after
                    # copying, before a new PCM-range receipt can be admitted.
                    if (_digest(path, cancelled) != audio['sha256']
                            or stamp(before) != stamp(after) or stamp(before) != stamp(regular(path))):
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
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        return validate_recording_receipt(
            read_json(self.root / (identity + '.json')), recording_id=identity)

    def _recover_owned_stage(self, identity, output, stamp, *, cancelled=lambda: False):
        """Only a UUID-named, otherwise empty capture staging directory is owned.

        The receipt provides no cleanup path. A foreign alias, extra directory
        entry or equal-byte different inode cannot grant deletion authority.
        """
        if stamp[-1] == 1:
            return
        directory = self.root / ('.pending-recording-' + identity)
        if not directory.exists():
            raise ValueError('The recorded WAV bytes are verified, but its other link is not '
                             'the known capture staging file. All names were preserved.')
        parent = regular(local_path(directory, exists=True), directory=True)
        staged = directory / 'capture.wav'
        if _stage_entries(directory) != ['capture.wav']:
            raise ValueError('The recording staging directory has unrelated entries; all names were preserved.')
        info = staged.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or _audio_stamp(info) != stamp):
            raise ValueError('The retained recording and known staging file have different identities; '
                             'all names were preserved.')
        if cancelled():
            raise AudioCancelled('Recording recovery cancelled before staging cleanup.')
        observed = regular(local_path(directory, exists=True), directory=True)
        if ((observed.st_dev, observed.st_ino) != (parent.st_dev, parent.st_ino)
                or _audio_stamp(output.lstat()) != stamp or _audio_stamp(staged.lstat()) != stamp
                or _stage_entries(directory) != ['capture.wav']):
            raise ValueError('The recording staging identity changed; all names were preserved.')
        staged.unlink()
        directory.rmdir()
        sync_directory(self.root)
        info = regular(output)
        if (info.st_dev, info.st_ino, info.st_size) != stamp[:3]:
            raise ValueError('The retained recording changed during staging recovery.')

    def get(self, identity, *, cancelled=lambda: False):
        with state_lock(self.library_root / 'state', 'studio-recording'):
            value = self._receipt(identity)
            options = RecordingOptions(**value['limits'])
            output = self.root / value['filename']
            stamp = _read_recording(output, value['audio'], options.max_bytes, cancelled)
            result = _recording_result(value)
            try:
                self._recover_owned_stage(identity, output, stamp, cancelled=cancelled)
            except OSError as exc:
                result['cleanup_pending'] = True
                _notice(result, 'The recorded WAV is verified, but its owned temporary link '
                        'cleanup needs another attempt: ' + str(exc)[:1000])
            # Recovery only removes the known staging alias. Re-read the same
            # retained bytes after any cleanup attempt before finalizing metadata.
            _read_recording(output, value['audio'], options.max_bytes, cancelled)
            verify_recording_segments(output, value, cancelled=cancelled)
            # Source metadata was valid before publication; a prepared receipt
            # is finalized only after reading the actual exact PCM again.
            if value.get('status') == 'prepared':
                finalized = _complete_recording(self.root / (identity + '.json'), value)
                for key in ('cleanup_pending', 'warnings'):
                    if key in result:
                        if key == 'warnings':
                            finalized.setdefault(key, []).extend(result[key])
                        else:
                            finalized[key] = result[key]
                result = finalized
            return {**result, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        paths = []
        with os.scandir(self.root) as listing:
            for count, entry in enumerate(listing, 1):
                if cancelled():
                    raise AudioCancelled('Recording list cancelled.')
                if count > _MAX_RECORDING_ENTRIES:
                    raise ValueError('The recording directory exceeds its total entry allowance.')
                if entry.name.casefold().endswith('.json'):
                    paths.append(self.root / entry.name)
                    if len(paths) > _MAX_RECORDINGS:
                        raise ValueError('The recording list exceeds its item allowance.')
        paths.sort(key=lambda path: path.name)
        records = []
        for path in paths:
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing receipts does not rehash hours of audio. Opening or
            # attaching a selected recording uses get() and verifies its bytes.
            records.append({**value, 'path': str(self.root / value['filename'])})
        return tuple(records)
