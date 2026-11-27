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

from .models import AudioBudget, AudioCancelled, AudioError, AudioUnavailable, integer
from .process import CaptureCleanupError, publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular, state_lock

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
            cancelled=cancelled, allow_failure=True)
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
        if not _CAPTURE.acquire(blocking=False):
            raise RuntimeError('Another microphone recording is active. Stop it before starting another.')
        release_capture = True
        try:
            with state_lock(self.root, 'recording'):
                return self._capture(device, title, options, stop_requested, cancelled, progress)
        except CaptureCleanupError:
            # An unconfirmed stop must not allow a second microphone session
            # in this host. Restarting the host resets this conservative latch.
            release_capture = False
            raise
        finally:
            if release_capture:
                _CAPTURE.release()

    def _capture(self, device, title, options, stop_requested, cancelled, progress):
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

        temporary = mkdtemp(prefix='.pending-recording-', dir=self.root)
        cleanup = True
        try:
            staged = Path(temporary) / 'capture.wav'
            command = [self.runtime.ffmpeg, '-hide_banner', '-loglevel', 'error', '-n', '-nostats',
                '-max_alloc', str(budget.max_alloc_bytes), '-threads', '1', '-stats_period', '0.1',
                '-progress', 'pipe:1', *self._input_arguments(device), '-map', '0:a:0', '-vn',
                '-c:a', 'pcm_s16le', '-ar', str(options.sample_rate), '-ac', str(options.channels),
                '-t', str(options.max_seconds), '-f', 'wav', '-rf64', 'never', str(staged)]
            result = run_process(command, budget=budget, cancelled=cancelled,
                stop_requested=stop_requested, watch=(staged,), progress=report, cwd=temporary)
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
            publish_file(staged, output, cancelled=cancelled)
            staged.unlink()
            try:
                atomic_json(receipt, value)
            except (OSError, ValueError) as exc:
                raise AudioError(f'The recorded WAV was retained at {output}, but its receipt could not be saved. Preserve the file for recovery.') from exc
        except CaptureCleanupError as exc:
            cleanup = False
            raise CaptureCleanupError(
                f'Microphone cleanup is unconfirmed. Incomplete recording retained at {temporary}; '
                'close the application before starting another recording.') from exc
        finally:
            if cleanup:
                shutil.rmtree(temporary)
        return {**value, 'path': str(output)}

    @staticmethod
    def _audio(path, options, cancelled):
        from ..speech.runtime import _wave_info
        info = regular(path)
        if info.st_size > options.max_bytes:
            raise ValueError('The recording exceeds its byte allowance.')
        rate, channels, frames, width = _wave_info(path)
        if (rate != options.sample_rate or channels != options.channels or width != 2
                or frames > options.max_seconds * rate):
            raise ValueError('The recorded PCM does not match the selected format or duration allowance.')
        return {'sha256': _digest(path, cancelled), 'size_bytes': info.st_size,
                'sample_rate': rate, 'channels': channels, 'frames': frames,
                'sample_width': width, 'duration_seconds': frames / rate}

    def _receipt(self, identity):
        if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
            raise ValueError('Invalid recording identity.')
        value = read_json(self.root / (identity + '.json'))
        if (not isinstance(value, dict) or set(value) != {'schema', 'id', 'filename', 'title', 'created',
                'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
                or value['schema'] != 'carr-recording/1' or value['id'] != identity
                or value['filename'] != identity + '.wav' or not isinstance(value['limits'], dict)
                or not isinstance(value['ffmpeg_sha256'], str) or _HASH.fullmatch(value['ffmpeg_sha256']) is None
                or value['completion'] not in ('stopped', 'duration-limit', 'device-ended')):
            raise ValueError('Invalid recording receipt.')
        _text(value['title'], 'recording title')
        source = value['source']
        if (not isinstance(source, dict) or set(source) != {'kind', 'backend', 'device_id', 'name'}
                or source['kind'] != 'microphone' or source['backend'] != 'dshow'
                or not isinstance(source['device_id'], str) or _HASH.fullmatch(source['device_id']) is None):
            raise ValueError('Invalid recording source receipt.')
        _text(source['name'], 'recording device name')
        options = RecordingOptions(**value['limits'])
        _text(value['created'], 'recording timestamp', 100)
        audio = value['audio']
        if (not isinstance(audio, dict) or set(audio) != {'sha256', 'size_bytes', 'sample_rate',
                'channels', 'frames', 'sample_width', 'duration_seconds'}
                or not isinstance(audio['sha256'], str) or _HASH.fullmatch(audio['sha256']) is None
                or any(type(audio[key]) is not int for key in ('size_bytes', 'sample_rate', 'channels', 'frames', 'sample_width'))
                or not 44 <= audio['size_bytes'] <= options.max_bytes
                or audio['sample_rate'] != options.sample_rate or audio['channels'] != options.channels
                or audio['sample_width'] != 2 or not 0 < audio['frames'] <= options.max_seconds * options.sample_rate
                or type(audio['duration_seconds']) not in (int, float)
                or audio['duration_seconds'] != audio['frames'] / options.sample_rate):
            raise ValueError('Invalid recording audio receipt.')
        return value

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        return {**value, 'path': str(output)}

    def list(self, *, cancelled=lambda: False):
        paths = sorted(self.root.glob('*.json'))
        if len(paths) > 10000:
            raise ValueError('The recording list exceeds its item allowance.')
        records = []
        for path in paths:
            if cancelled():
                raise AudioCancelled('Recording list cancelled.')
            value = self._receipt(path.stem)
            # Listing receipts does not rehash hours of audio. Opening or
            # attaching a selected recording uses get() and verifies its bytes.
            records.append({**value, 'path': str(self.root / value['filename'])})
        return tuple(records)
