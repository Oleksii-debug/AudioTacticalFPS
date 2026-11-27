"""Explicit local microphone recording, adapted from PR123 at 28a4e542.

Capture reuses the existing bounded audio runner, runtime selection, atomic
publication and project asset namespace. DirectShow discovery never starts a
recording. A caller must select an enumerated device and separately request
Start. Originals and retakes retain their exact PCM receipt in private state.

Upstream device contract: https://ffmpeg.org/ffmpeg-devices.html#dshow
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import ctypes
import os
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp
import threading
from uuid import uuid4
import wave

from .models import AudioBudget, AudioCancelled, AudioError, AudioUnavailable, integer
from .process import publish_file, run_process
from .cleanup import AudioCleanupPending
from ..ingest.writer_guard import exclusive_writer
from ..durable_state import atomic_json, local_path, read_json, regular, sync_directory

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')


def _check(cancelled):
    if cancelled():
        raise AudioCancelled('Recording operation cancelled before publication.')


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_verified_input(path):
    """Keep Windows input bytes stable, independently of timestamp precision.

    Unlike replaceable worker status, a verified audio input must not accept a
    concurrent writer. Other readers remain allowed. The native handle becomes
    the Python stream's sole owned descriptor and is closed on every failure.
    """
    if os.name != 'nt':
        return Path(path).open('rb')
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, normal/no-reparse-follow.
    # Omit write/delete sharing rather than assuming an mtime tick occurred.
    handle = create(str(path), 0x80000000, 0x1, None, 3, 0x80 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise



@contextmanager
def _verified_reader(path):
    """Keep one regular input bound while validating actual PCM and its hash."""
    path = local_path(path, exists=True)
    before = _file_identity(regular(path))
    with _open_verified_input(path) as stream:
        if _file_identity(os.fstat(stream.fileno())) != before:
            raise ValueError('A recording input changed before verification.')
        yield stream
        local_path(path, exists=True)
        if (_file_identity(os.fstat(stream.fileno())) != before
                or _file_identity(regular(path)) != before):
            raise ValueError('A recording input changed during verification.')


def _digest(path, cancelled=lambda: False):
    digest = sha256()
    with _verified_reader(path) as stream:
        remaining = os.fstat(stream.fileno()).st_size
        while remaining:
            _check(cancelled)
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError('A recording input was truncated during verification.')
            digest.update(block)
            remaining -= len(block)
        _check(cancelled)
        if stream.read(1):
            raise ValueError('A recording input grew during verification.')
    return digest.hexdigest()


def _text(value, label, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'Invalid {label}.')
    return value


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
    def __init__(self, root):
        from filelock import FileLock, Timeout
        self.root = local_path(root, exists=True)
        self.directory = None
        self.owner = None
        self.identity = uuid4().hex
        self.deferred = False
        self.closed = False
        self.cleanup_error = None
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
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.add(self)

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
            try:
                self.lock.release()
            except Exception as exc:
                self.cleanup_error = str(exc)
                return False
            self.closed = True
            _CAPTURE.release()
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


class RecordingService:
    def __init__(self, library_root, *, runtime=None, ffmpeg=None, budget=None):
        from .render import _require_local_volume
        self.library_root = local_path(library_root, exists=True)
        _require_local_volume(self.library_root)
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        with exclusive_writer(self.library_root):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if runtime is not None and ffmpeg is not None:
            raise ValueError('Select one configured FFmpeg runtime.')
        if budget is not None and not isinstance(budget, AudioBudget):
            raise ValueError('Recording requires a validated AudioBudget.')
        self.runtime, self.ffmpeg = runtime, ffmpeg
        self.budget = budget or getattr(runtime, 'budget', None)
        if self.budget is not None and not isinstance(self.budget, AudioBudget):
            raise ValueError('Recording requires a validated AudioBudget.')
        self._library_identity = self._directory_identity(self.library_root)
        self._root_identity = self._directory_identity(self.root)
        self._devices, self._engine_hash = {}, None
        self._settings_snapshot = None

    @staticmethod
    def _directory_identity(path):
        info = regular(local_path(path, exists=True), directory=True)
        return info.st_dev, info.st_ino

    def _validate_root(self):
        if (self._directory_identity(self.library_root) != self._library_identity
                or self._directory_identity(self.root) != self._root_identity):
            raise ValueError('The recording library changed. Reopen recording before continuing.')

    def _runtime(self, cancelled=lambda: False):
        from .render import tool_path
        from .dsp import AudioRuntime
        self._validate_root()
        _check(cancelled)
        if self.runtime is None:
            if self.ffmpeg is None:
                from ..runtime_settings import _enabled_settings
                value = _enabled_settings(self.library_root, cancelled=cancelled)
                self._settings_snapshot = value
                ffmpeg, ffprobe = value['ffmpeg'], value['ffprobe']
            else:
                ffmpeg, ffprobe = self.ffmpeg, None
            self.runtime = AudioRuntime(self.root, ffmpeg=tool_path('ffmpeg', ffmpeg),
                                        ffprobe=tool_path('ffprobe', ffprobe))
        elif self._settings_snapshot is not None:
            from ..runtime_settings import _enabled_settings
            if _enabled_settings(self.library_root, cancelled=cancelled) != self._settings_snapshot:
                raise ValueError('Local audio settings changed. Refresh microphones before recording.')
        binary = tool_path('ffmpeg', self.runtime.ffmpeg)
        if binary != str(self.runtime.ffmpeg):
            raise ValueError('The selected FFmpeg identity changed. Refresh microphones.')
        _check(cancelled)
        return self.runtime

    def devices(self, *, cancelled=lambda: False):
        self._devices, self._engine_hash = {}, None
        if self._settings_snapshot is not None:
            # Explicit Refresh rebinds configuration; Start cannot do so silently.
            self.runtime, self._settings_snapshot = None, None
        if os.name != 'nt':
            raise AudioUnavailable('Microphone recording requires Windows DirectShow and a selected local FFmpeg build.')
        self._runtime(cancelled)
        identity = _digest(self.runtime.ffmpeg, cancelled)
        result = run_process([self.runtime.ffmpeg, '-hide_banner', '-nostdin', '-list_devices', 'true',
                              '-f', 'dshow', '-i', 'dummy'],
            budget=AudioBudget(timeout=15, max_log_bytes=512 * 1024),
            cancelled=cancelled, allow_failure=True, owned_tree=True)
        if result.returncode not in (0, 1):
            raise AudioUnavailable('FFmpeg device discovery failed. Check the selected Windows runtime.')
        devices = parse_devices(result.stderr)
        if not devices:
            raise AudioUnavailable('No identifiable microphone was reported. Check Windows microphone permission and the DirectShow runtime, then refresh.')
        self._validate_root()
        if _digest(self.runtime.ffmpeg, cancelled) != identity:
            raise ValueError('The FFmpeg runtime changed during device discovery. Refresh microphones.')
        self._devices, self._engine_hash = {item.id: item for item in devices}, identity
        return devices

    @staticmethod
    def _input_arguments(device):
        # Realtime frames are bounded at the input. Current FFmpeg treats
        # thread_queue_size as output-only; keep that limit in _capture after -i.
        return ['-rtbufsize', '16M', '-f', 'dshow',
                '-i', 'audio=' + device.alternative]

    def capture(self, device_id, *, title='Microphone recording', options=None,
                stop_requested=lambda: False, cancelled=lambda: False, progress=None, retake_of=None):
        options = options or RecordingOptions()
        if not isinstance(options, RecordingOptions):
            raise ValueError('Validated recording options are required.')
        _text(title, 'recording title')
        if not callable(stop_requested) or not callable(cancelled):
            raise ValueError('Recording requires explicit Stop and Cancel callbacks.')
        if progress is not None and not callable(progress):
            raise ValueError('Recording progress requires a callback.')
        device = self._devices.get(device_id)
        if device is None or self._engine_hash is None:
            raise ValueError('Refresh microphones and explicitly select a current device before Start.')
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        if retake_of is not None and (type(retake_of) is not str or _ID.fullmatch(retake_of) is None):
            raise ValueError('Choose an existing saved recording for another take.')
        self._validate_root()
        reservation = _CaptureReservation(self.root)
        try:
            previous = self.get(retake_of, cancelled=cancelled) if retake_of is not None else None
            return self._capture(device, title, options, stop_requested, cancelled,
                                 progress, reservation, previous)
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
        self._runtime(cancelled)
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed. Refresh microphones before recording.')
        budget = self.budget or AudioBudget(timeout=options.max_seconds + 20,
                    max_output_bytes=options.max_bytes, max_log_bytes=512 * 1024,
                    max_staging_files=4, threads=1, reserve_disk_bytes=64 * 1024**2)
        budget = replace(budget, timeout=min(budget.timeout, options.max_seconds + 20),
                         max_output_bytes=min(budget.max_output_bytes, options.max_bytes))
        maximum_pcm = options.max_seconds * options.sample_rate * options.channels * 2 + 4096
        if maximum_pcm > budget.max_output_bytes:
            raise ValueError('The recording duration exceeds the caller audio byte allowance.')
        if shutil.disk_usage(self.root).free < maximum_pcm + budget.reserve_disk_bytes:
            raise AudioError('There is not enough free space for the selected recording allowance.')

        def report(seconds):
            nonlocal progress
            if progress is not None:
                try:
                    progress(seconds)
                except Exception:
                    progress = None  # An observer cannot destroy captured audio.

        temporary = reservation.staging()
        staged = temporary / 'capture.wav'
        command = [self.runtime.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
            '-max_alloc', str(budget.max_alloc_bytes), '-threads', str(budget.threads),
            '-stats_period', '0.1', '-progress', 'pipe:1',
            *self._input_arguments(device), '-thread_queue_size', '128', '-map', '0:a:0', '-vn',
            '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
            '-t', str(options.max_seconds), '-f', 'wav', '-rf64', 'never', str(staged)]
        self._validate_root()
        if cancelled() or stop_requested():
            raise AudioCancelled('Recording stopped before opening the microphone.')
        result = run_process(command, budget=budget, cancelled=cancelled,
            stop_requested=stop_requested, watch=(staged,), progress=report, cwd=temporary, owned_tree=True)
        audio = self._audio(staged, options, cancelled)
        self._validate_root()
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
        value = {'schema': 'carr-recording/1', 'id': identity, 'filename': output.name,
            'title': title, 'created': datetime.now(timezone.utc).isoformat(),
            'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
            'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
            'completion': 'stopped' if result.stopped else 'duration-limit'
                if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended'}
        if previous is not None:
            value['schema'] = 'carr-recording/2'
            value['retake'] = {
                'origin_id': previous.get('retake', {}).get('origin_id', previous['id']),
                'previous_id': previous['id'], 'previous_sha256': previous['audio']['sha256']}
        validate_recording_receipt(value, recording_id=identity)
        self._validate_root()
        publish_file(staged, output, cancelled=cancelled)
        staged.unlink()
        # Publication is committed. A late Cancel must not erase or relabel
        # actual durable PCM. Reverify the retained path before its receipt.
        if self._audio(output, options, lambda: False) != audio:
            raise AudioError('The retained WAV changed after publication; preserve it for explicit recovery.')
        self._validate_root()
        try:
            atomic_json(receipt, value)
        except (OSError, ValueError) as exc:
            raise AudioError(f'The original WAV was retained at {output}, but its receipt could not be saved. Preserve it for recovery.') from exc
        return {**value, 'path': str(output)}

    @staticmethod
    def _audio(path, options, cancelled):
        _check(cancelled)
        with _verified_reader(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            if not 44 <= size <= options.max_bytes:
                raise ValueError('The recording exceeds its byte allowance or has no PCM header.')
            try:
                with wave.open(stream, 'rb') as audio:
                    rate, channels, frames = audio.getframerate(), audio.getnchannels(), audio.getnframes()
                    if (rate != options.sample_rate or channels != options.channels
                            or audio.getsampwidth() != 2 or audio.getcomptype() != 'NONE'
                            or not 0 < frames <= options.max_seconds * rate):
                        raise ValueError('Recorded PCM does not match the selected format or duration.')
                    remaining = frames * channels * 2
                    if remaining + 44 > size:
                        raise ValueError('The recorded WAV has truncated PCM.')
                    while remaining:
                        _check(cancelled)
                        block = audio.readframes(min(65536, remaining // (channels * 2)))
                        if not block or len(block) > remaining:
                            raise ValueError('The recorded WAV has truncated PCM.')
                        remaining -= len(block)
            except (wave.Error, EOFError) as exc:
                raise ValueError('The recording is not supported PCM16 WAV.') from exc
            stream.seek(0)
            remaining, digest = size, sha256()
            while remaining:
                _check(cancelled)
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError('The recorded WAV changed during verification.')
                digest.update(block)
                remaining -= len(block)
            _check(cancelled)
            if stream.read(1):
                raise ValueError('The recorded WAV grew during verification.')
            return {'sha256': digest.hexdigest(), 'size_bytes': size,
                    'sample_rate': rate, 'channels': channels, 'frames': frames,
                    'sample_width': 2, 'duration_seconds': frames / rate}


    def _receipt(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        self._validate_root()
        path = local_path(self.root / (identity + '.json'), root=self.root, exists=True)
        if regular(path).st_size > 64 * 1024:
            raise ValueError('The recording receipt exceeds its metadata allowance.')
        return validate_recording_receipt(read_json(path), recording_id=identity)

    def _lineage(self, value, cancelled=lambda: False):
        seen = {value['id']}
        current = value
        while current.get('schema') == 'carr-recording/2':
            _check(cancelled)
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
        self._lineage(value, cancelled)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        return {**value, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        self._validate_root()
        paths = []
        for count, path in enumerate(self.root.iterdir()):
            _check(cancelled)
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
        """Retain exact recorded PCM in the existing content-addressed assets."""
        from .projects import ProjectStore
        value = self.get(identity, cancelled=cancelled)
        store = ProjectStore(self.library_root)
        project = store.load(project_id)
        source = Path(value['path'])
        folder = store.path(project.id) / 'assets'
        target = folder / (value['audio']['sha256'] + '.wav')
        # Existing Studio writer lock also coordinates full backup snapshots.
        with store.transaction():
            self._validate_root()
            if os.path.lexists(target):
                if self._audio(target, RecordingOptions(**value['limits']), cancelled) != value['audio']:
                    raise ValueError('The project recording asset differs from its receipt.')
            else:
                staged = folder / ('.recording-' + uuid4().hex)
                try:
                    with _verified_reader(source) as incoming, staged.open('xb') as outgoing:
                        remaining = value['audio']['size_bytes']
                        while remaining:
                            _check(cancelled)
                            block = incoming.read(min(remaining, 1024 * 1024))
                            if not block:
                                raise ValueError('The original recording changed during retention.')
                            outgoing.write(block)
                            remaining -= len(block)
                        if incoming.read(1):
                            raise ValueError('The original recording grew during retention.')
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if self._audio(staged, RecordingOptions(**value['limits']), cancelled) != value['audio']:
                        raise ValueError('The selected recording changed before retention.')
                    self._validate_root()
                    publish_file(staged, target, cancelled=cancelled)
                finally:
                    staged.unlink(missing_ok=True)
            # Successful publication is immutable even if Cancel arrives now.
            if self._audio(target, RecordingOptions(**value['limits']), lambda: False) != value['audio']:
                raise ValueError('The retained project asset differs from its receipt.')
        return {'asset': value['audio']['sha256'], 'recording_id': identity,
                'project_id': project.id, 'duration_seconds': value['audio']['duration_seconds']}

    def attach(self, identity, project_id, scene_id, *, expected_fingerprint, cancelled=lambda: False):
        """Append one existing asset cue; source script and quotations stay exact."""
        from .projects import ProjectStore, Cue
        store = ProjectStore(self.library_root)
        project = store.load(project_id)
        if project.fingerprint != expected_fingerprint:
            raise ValueError('The selected project changed. Reopen it before adding a recording.')
        if scene_id not in {scene.id for scene in project.scenes}:
            raise ValueError('Select a scene in the saved project.')
        retained = self.retain(identity, project_id, cancelled=cancelled)
        cue = Cue('recording-' + identity, 'asset', seconds=retained['duration_seconds'],
                  asset=retained['asset'])
        matches = [(scene.id, item) for scene in project.scenes for item in scene.cues if item.id == cue.id]
        if matches:
            if matches == [(scene_id, cue)]:
                return project
            raise ValueError('This recording is already attached to a different cue or scene.')
        _check(cancelled)
        updated = replace(project, revision=project.revision + 1,
            scenes=tuple(replace(scene, cues=(*scene.cues, cue)) if scene.id == scene_id else scene
                         for scene in project.scenes))
        self._validate_root()
        return store.save(updated, expected_fingerprint=project.fingerprint)
