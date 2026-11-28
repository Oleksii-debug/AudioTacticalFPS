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
from tempfile import TemporaryFile, mkdtemp
import threading
import time
from uuid import uuid4
import wave

from .models import AudioBudget, AudioCancelled, AudioError, AudioUnavailable, integer
from .process import CaptureCleanupError, publish_file, run_process
from ..durable_state import atomic_json, local_path, read_json, regular, state_lock

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


def validate_recording_receipt(value, identity):
    if not isinstance(identity, str) or _ID.fullmatch(identity) is None:
        raise ValueError('Invalid recording identity.')
    base_fields = {'schema', 'id', 'filename', 'title', 'created', 'source',
                   'ffmpeg_sha256', 'audio', 'limits', 'completion'}
    version = value.get('schema') if isinstance(value, dict) else None
    expected_fields = base_fields | ({'segments', 'monitoring', 'timeline'} if version == 'carr-recording/2' else set())
    if (not isinstance(value, dict) or set(value) != expected_fields
            or version not in ('carr-recording/1', 'carr-recording/2') or value['id'] != identity
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
    """Verify retained PCM ranges without opening an engine or native device."""
    validate_recording_receipt(value, value['id'])
    if value['schema'] == 'carr-recording/1':
        return
    before = regular(path)
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with Path(path).open('rb') as stream, wave.open(stream, 'rb') as reader:
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

    @staticmethod
    def monitoring_outputs(*, cancelled=lambda: False):
        from ..speech.playback_devices import output_devices
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
        if pause_requested is not None and not callable(pause_requested):
            raise ValueError('Recording pause requires an explicit request callback.')
        if state_changed is not None and not callable(state_changed):
            raise ValueError('Recording state feedback must be a callback.')
        if monitor_output is not None:
            from ..speech.playback_devices import PlaybackDevice
            if not isinstance(monitor_output, PlaybackDevice) or not monitor_output.selectable:
                raise ValueError('Monitoring requires an explicitly selected identifiable output.')
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
                return self._capture(device, title, options, stop_requested, cancelled, progress,
                                     pause_requested, state_changed, monitor_output)
        except CaptureCleanupError:
            # An unconfirmed stop must not allow a second microphone session
            # in this host. Restarting the host resets this conservative latch.
            release_capture = False
            raise
        finally:
            if release_capture:
                _CAPTURE.release()

    def _capture(self, device, title, options, stop_requested, cancelled, progress,
                 pause_requested=None, state_changed=None, monitor_output=None):
        identity = uuid4().hex
        output, receipt = self.root / (identity + '.wav'), self.root / (identity + '.json')
        if os.path.lexists(output) or os.path.lexists(receipt):
            raise FileExistsError('The recording identity already exists; no microphone was opened.')
        if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
            raise ValueError('The FFmpeg runtime changed. Refresh microphones before recording.')
        budget = AudioBudget(timeout=options.max_seconds + 20, max_output_bytes=options.max_bytes,
                             max_log_bytes=512 * 1024, max_staging_files=4, threads=1)
        controlled = pause_requested is not None or monitor_output is not None
        # Controlled assembly keeps one verified segment snapshot in addition
        # to the captured segments and their new joined output.
        needed = options.max_seconds * options.sample_rate * options.channels * 2 * (3 if controlled else 1)
        if shutil.disk_usage(self.root).free < needed + budget.reserve_disk_bytes + (MAX_RECORDING_SEGMENTS * 4096 if controlled else 0):
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
            extra = {}
            if controlled:
                completion, extra = self._capture_segments(device, staged, options, stop_requested,
                    cancelled, report, pause_requested or (lambda: False), state_changed, monitor_output)
            else:
                result = run_process(self._command(device, staged, options, budget, options.max_seconds),
                    budget=budget, cancelled=cancelled, stop_requested=stop_requested,
                    watch=(staged,), progress=report, cwd=temporary)
            audio = self._audio(staged, options, cancelled)
            if not controlled:
                completion = 'stopped' if result.stopped else 'duration-limit' if audio['duration_seconds'] >= options.max_seconds - .1 else 'device-ended'
            if _digest(self.runtime.ffmpeg, cancelled) != self._engine_hash:
                raise ValueError('The FFmpeg runtime changed during recording; no result was published.')
            value = {'schema': 'carr-recording/2' if controlled else 'carr-recording/1', 'id': identity, 'filename': output.name,
                'title': title, 'created': datetime.now(timezone.utc).isoformat(),
                'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': device.id, 'name': device.name},
                'ffmpeg_sha256': self._engine_hash, 'audio': audio, 'limits': asdict(options),
                'completion': completion, **extra}
            validate_recording_receipt(value, identity)
            verify_recording_segments(staged, value, cancelled=cancelled)
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
                    watch=(staged.parent,), progress=measured, cwd=staged.parent)
            finally:
                try:
                    if tap is not None:
                        tap.close()
                        monitoring['submitted_frames'] += tap.submitted_frames
                        monitoring['dropped_frames'] += tap.dropped_frames
                finally:
                    if monitor is not None:
                        monitor.close()
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
        """Stream verified private sample frames, with a hash for each range."""
        ranges, position = [], 0
        width = options.channels * 2
        stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        with Path(staged).open('xb') as output:
            with wave.open(output, 'wb') as writer:
                writer.setparams((options.channels, 2, options.sample_rate, 0, 'NONE', 'PCM'))
                for path, audio, ended_by in parts:
                    before = regular(path)
                    size = audio['size_bytes']
                    if not 44 <= size <= options.max_bytes or before.st_size != size:
                        raise ValueError('A completed recording segment changed before assembly.')
                    if _digest(path, cancelled) != audio['sha256']:
                        raise ValueError('A completed recording segment changed before assembly.')
                    # Windows timestamps can remain equal after a same-size
                    # write. Bind the PCM reader to the exact captured bytes,
                    # not a reopened source that merely has the same stamp.
                    with TemporaryFile(dir=staged.parent, prefix='.segment-snapshot-') as snapshot:
                        copied, remaining_bytes = sha256(), size
                        with Path(path).open('rb') as source:
                            while remaining_bytes:
                                if cancelled():
                                    raise AudioCancelled('Recording assembly cancelled; no take was published.')
                                block = source.read(min(remaining_bytes, 1024 * 1024))
                                if not block:
                                    raise ValueError('A recording segment changed during assembly.')
                                if shutil.disk_usage(staged.parent).free < 32 * 1024 * 1024 + len(block):
                                    raise AudioError('Recording assembly stopped to preserve its disk reserve.')
                                copied.update(block)
                                snapshot.write(block)
                                remaining_bytes -= len(block)
                            if source.read(1):
                                raise ValueError('A recording segment changed during assembly.')
                            after = os.fstat(source.fileno())
                        if (copied.hexdigest() != audio['sha256'] or stamp(before) != stamp(after)
                                or stamp(before) != stamp(regular(path))):
                            raise ValueError('A recording segment changed during assembly.')
                        snapshot.seek(0)
                        digest, remaining = sha256(), audio['frames']
                        with wave.open(snapshot, 'rb') as reader:
                            if (reader.getcomptype() != 'NONE' or reader.getsampwidth() != 2
                                    or reader.getframerate() != options.sample_rate
                                    or reader.getnchannels() != options.channels
                                    or reader.getnframes() != audio['frames']):
                                raise ValueError('A completed recording segment has changed PCM parameters.')
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
                    if stamp(before) != stamp(regular(path)):
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
        return validate_recording_receipt(read_json(self.root / (identity + '.json')), identity)

    def get(self, identity, *, cancelled=lambda: False):
        value = self._receipt(identity)
        options = RecordingOptions(**value['limits'])
        output = self.root / value['filename']
        if self._audio(output, options, cancelled) != value['audio']:
            raise ValueError('The retained recording differs from its verified receipt.')
        verify_recording_segments(output, value, cancelled=cancelled)
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
