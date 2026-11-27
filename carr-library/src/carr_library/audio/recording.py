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
from uuid import uuid4

from .models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, AudioUnavailable, integer
from .process import native_audio_environment, publish_file, run_process
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
    before = regular(path)
    digest, count = sha256(), 0
    with Path(path).open('rb') as stream:
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
                self.release_retry_required = True
                return False
            self.release_retry_required = False
            self.closed = True
            _CAPTURE.release()
        with _PENDING_CAPTURE_LOCK:
            _PENDING_CAPTURES.discard(self)
        return True


class RecordingService:
    def __init__(self, library_root, *, runtime=None):
        self.library_root = local_path(library_root, exists=True)
        self.root = local_path(self.library_root / 'state' / 'studio' / 'recordings')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime = runtime
        self._devices, self._engine_hash = {}, None

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
            cancelled=cancelled, allow_failure=True, owned_windows_job=True, env=native_audio_environment())
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
                stop_requested=lambda: False, cancelled=lambda: False, progress=None):
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
        reservation = _CaptureReservation(self.root)
        try:
            return self._capture(device, title, options, stop_requested, cancelled, progress, reservation)
        except AudioCleanupPending as exc:
            reservation.retain(exc.cleanup_owner)
            exc.recording_reservation = reservation
            raise
        finally:
            if not reservation.deferred:
                reservation.close()

    def _capture(self, device, title, options, stop_requested, cancelled, progress, reservation):
        identity = uuid4().hex
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
            stop_requested=stop_requested, watch=(staged,), progress=report, cwd=temporary, owned_windows_job=os.name == 'nt', env=native_audio_environment())
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
        staged.unlink()
        try:
            atomic_json(receipt, value)
        except (OSError, ValueError) as exc:
            raise AudioError(f'The recorded WAV was retained at {output}, but its receipt could not be saved. Preserve the file for recovery.') from exc
        return {**value, 'path': str(output)}

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

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
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
