"""Explicit local microphone capture through the configured FFmpeg runtime.

DirectShow discovery does not record. A capture needs a device from the current
inventory and a separate caller action. Completed PCM and its relative receipt
live in ordinary private state, so existing backups retain them without another
archive format. No microphone, model, driver or network fallback is selected.

Upstream device contract: https://ffmpeg.org/ffmpeg-devices.html#dshow
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import wave
from uuid import uuid4

from .models import AudioBudget, AudioCancelled, AudioError, AudioUnavailable, integer
from .process import publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular, state_lock, sync_directory

_CAPTURE = threading.Lock()
_ID = re.compile(r'[0-9a-f]{32}')
_HASH = re.compile(r'[0-9a-f]{64}')
_MAX_RECORDINGS = 10000
_MAX_RECORDING_ENTRIES = 30000




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


@contextmanager
def _recording_stage(root, identity, completed):
    directory = Path(root) / ('.pending-recording-' + identity)
    directory.mkdir(mode=0o700)
    owner = regular(directory, directory=True)

    def cleanup():
        observed = regular(local_path(directory, exists=True), directory=True)
        if (observed.st_dev, observed.st_ino) != (owner.st_dev, owner.st_ino):
            raise ValueError('The capture staging directory changed; all entries were preserved.')
        entries = _stage_entries(directory)
        if entries == ['capture.wav']:
            if completed.get('status') == 'complete':
                # A failed post-publication unlink is retried only by explicit
                # receipt recovery, with the retained output identity checked.
                raise OSError('The owned capture staging link still needs receipt recovery.')
            regular(directory / 'capture.wav')
            (directory / 'capture.wav').unlink()
        elif entries:
            raise ValueError('Unexpected capture staging entries were preserved.')
        directory.rmdir()
        sync_directory(root)

    try:
        yield directory
    finally:
        try:
            cleanup()
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            if completed.get('status') != 'complete':
                raise
            completed['cleanup_pending'] = True
            _notice(completed, 'The recorded WAV is saved and verified, but staging cleanup '
                    'needs attention: ' + str(exc)[:1000])


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


@contextmanager
def _capture_resources(service, device, options, cancelled, completed):
    from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
    budget = AudioBudget(timeout=options.max_seconds + 20, max_output_bytes=options.max_bytes,
                         max_log_bytes=512 * 1024, max_staging_files=4, threads=1)
    request = ResourceRequest(cpu_slots=1,
        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
        disk_path=service.root,
        disk_bytes=options.max_seconds * options.sample_rate * options.channels * 2
                   + budget.reserve_disk_bytes + 65536,
        devices=('microphone:' + device.id,))
    lease = None
    try:
        broker = ResourceBroker.for_app(service.library_root, cancelled=cancelled)
        with broker.reserve(request, operation_id='recording:' + uuid4().hex,
                            cancelled=cancelled, timeout=60, run_timeout=budget.timeout) as lease:
            try:
                yield lease
            except (Exception, KeyboardInterrupt) as exc:
                if completed.get('status') != 'complete':
                    raise
                _notice(completed, 'The recorded WAV is saved and verified, but final '
                        'notification or cleanup was interrupted: ' + str(exc)[:1000])
    except ResourceCancelled as exc:
        if completed.get('status') != 'complete':
            raise AudioCancelled('Recording cancelled before shared resource admission.') from exc
        completed['resource_release_confirmed'] = False
        _notice(completed, 'The recorded WAV is saved and verified. Resource release '
                'reported an interruption: ' + str(exc)[:1000])
    except (Exception, KeyboardInterrupt) as exc:
        if completed.get('status') != 'complete':
            raise
        completed['resource_release_confirmed'] = False
        _notice(completed, 'The recorded WAV is saved and verified. Shared resource release '
                'was not confirmed: ' + type(exc).__name__ + ': ' + str(exc)[:1000])
    finally:
        if lease is not None and lease.cleanup_pending and completed.get('status') == 'complete':
            completed['resource_cleanup_pending'] = True
            _notice(completed, 'The microphone resource lock was released; stale coordination '
                    'metadata awaits normal cleanup.')


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
        completed = {}
        try:
            try:
                with state_lock(self.library_root / 'state', 'studio-recording'):
                    with _capture_resources(self, device, options, cancelled, completed) as lease:
                        self._capture(device, title, options, stop_requested, lease.cancelled, progress, completed)
            except (Exception, KeyboardInterrupt) as exc:
                if completed.get('status') != 'complete':
                    raise
                completed['state_release_confirmed'] = False
                _notice(completed, 'The recorded WAV is saved and verified. Private state lock '
                        'release reported an interruption: ' + str(exc)[:1000])
            return completed
        finally:
            _CAPTURE.release()

    def _capture(self, device, title, options, stop_requested, cancelled, progress, completed):
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

        parent_owner = regular(self.root, directory=True)
        with _recording_stage(self.root, identity, completed) as temporary:
            staged = temporary / 'capture.wav'
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
                'completion': 'stopped' if result.stopped else 'duration-limit' if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended',
                'status': 'prepared'}
            # Prepared metadata precedes the create-only publication. Process
            # death can leave it prepared, but never invents a completed WAV.
            atomic_json(receipt, value)
            owner = regular(staged)
            observed_parent = regular(local_path(self.root, exists=True), directory=True)
            if (observed_parent.st_dev, observed_parent.st_ino) != (parent_owner.st_dev, parent_owner.st_ino):
                raise ValueError('The recording directory changed before publication.')
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
        fields = {'schema', 'id', 'filename', 'title', 'created',
                  'source', 'ffmpeg_sha256', 'audio', 'limits', 'completion'}
        if (not isinstance(value, dict) or set(value) not in (fields, fields | {'status'})
                or value.get('status', 'complete') not in ('prepared', 'complete')
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
