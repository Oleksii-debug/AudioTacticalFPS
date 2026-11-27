"""Explicit offline playback controls on the existing FFmpeg and player owners.

Preparation never opens an audio device. The caller keeps its source authority
and generation guard, then gives the pinned derivative to the current WavePlayer.
"""
from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import math
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from time import monotonic_ns
from uuid import uuid4

from ..native_file_metadata import stream_change_time
from ..operations import ResourceBroker, ResourceRequest
from . import ffmpeg as audio
from .capture_process import run_process, wait_for_cleanup
from .capture_types import AudioBudget, AudioCleanupPending
from .jobs import checked_directory, file_identity, regular_file, sha256_file, verified_reader
from .models import Effect, number
from .runtime import resolve_ffmpeg


@dataclass(frozen=True)
class PlaybackControls:
    speed: float = 1.0
    pitch_semitones: float = 0.0
    volume: float = 1.0
    pan: float = 0.0

    def __post_init__(self):
        number(self.speed, .25, 4, 'Playback speed')
        number(self.pitch_semitones, -12, 12, 'Playback pitch')
        number(self.volume, 0, 1, 'Playback volume')
        number(self.pan, -1, 1, 'Playback stereo balance')

    @property
    def original(self):
        return (self.speed, self.pitch_semitones, self.volume, self.pan) == (1, 0, 1, 0)


@dataclass
class PreparedPlayback:
    source: Path
    source_identity: tuple
    source_sha256: str
    source_frames: int
    sample_rate: int
    controls: PlaybackControls
    audio: Path
    audio_identity: tuple
    audio_sha256: str
    audio_frames: int
    processor: dict
    _temporary: object
    _reader: object = None
    _stream: object = None
    _native_change: object = None
    invalid: bool = False

    @property
    def source_duration(self):
        return self.source_frames / self.sample_rate

    @property
    def audio_duration(self):
        return self.audio_frames / self.sample_rate

    @property
    def source_time_accuracy(self):
        return ('estimated_after_time_stretch'
                if self.controls.speed != 1 or self.controls.pitch_semitones != 0
                else 'sample_frames')

    def source_to_audio(self, seconds):
        """Inclusive original-time to prepared-time mapping in whole samples."""
        seconds = number(seconds, 0, self.source_duration, 'Original recording position')
        if seconds == self.source_duration:
            return self.audio_duration
        source = min(self.source_frames, max(0, round(seconds * self.sample_rate)))
        frame = min(self.audio_frames, round(Fraction(source * self.audio_frames, self.source_frames)))
        return frame / self.sample_rate

    def audio_to_source(self, seconds):
        """Observed prepared samples mapped conservatively to original samples.

        Time stretching is not a word-alignment oracle. The accuracy label must
        accompany this source-position estimate in reading/highlight interfaces.
        """
        seconds = number(seconds, 0, self.audio_duration, 'Prepared playback position')
        if seconds == self.audio_duration:
            return self.source_duration
        frame = min(self.audio_frames, max(0, round(seconds * self.sample_rate)))
        source = min(self.source_frames, frame * self.source_frames // self.audio_frames)
        return source / self.sample_rate

    def pin(self, *, cancelled=lambda: False):
        """Call on an operation worker before a possibly delayed native Play."""
        if self.invalid or self._temporary is None:
            raise ValueError('Prepare the selected playback controls again.')
        if self._reader is not None:
            self.ready()
            return self
        owner = verified_reader(self.audio, expected_identity=self.audio_identity)
        stream = owner.__enter__()
        try:
            hashed, remaining = sha256(), regular_file(self.audio).st_size
            while remaining:
                if cancelled():
                    raise InterruptedError('Playback preparation cancelled.')
                block = stream.read(min(1024**2, remaining))
                if not block:
                    raise ValueError('Prepared playback PCM was truncated.')
                hashed.update(block)
                remaining -= len(block)
            if stream.read(1) or hashed.hexdigest() != self.audio_sha256:
                raise ValueError('Prepared playback PCM changed; prepare it again.')
            if cancelled():
                raise InterruptedError('Playback preparation cancelled.')
            native = stream_change_time(stream)
        except BaseException:
            owner.__exit__(None, None, None)
            raise
        self._reader, self._stream, self._native_change = owner, stream, native
        return self

    def ready(self):
        """Cheap delayed-action check; caller also checks source scope/generation."""
        if (self.invalid or self._reader is None or self._temporary is None
                or file_identity(regular_file(self.audio)) != self.audio_identity
                or file_identity(os.fstat(self._stream.fileno())) != self.audio_identity
                or stream_change_time(self._stream) != self._native_change):
            raise ValueError('Prepared playback PCM changed; prepare it again.')
        return self.audio

    def release(self):
        """Only after the caller's current player has acknowledged Stop/Close."""
        owner, self._reader = self._reader, None
        self._stream, self._native_change = None, None
        if owner is not None:
            try:
                owner.__exit__(None, None, None)
            except BaseException:
                self.invalid = True
                raise

    def close(self):
        """Discard private PCM after release; failed cleanup remains retryable."""
        if self._reader is not None:
            raise RuntimeError('Stop the owned player and release prepared playback before discarding it.')
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def _frames(info, controls):
    ratio = Fraction(*float(controls.speed).as_integer_ratio())
    return max(1, round(Fraction(info.frames, 1) / ratio))


def _filters(info, controls, frames):
    effects = []
    if controls.pitch_semitones:
        effects.append(Effect('pitch', (('semitones', controls.pitch_semitones),)))
    if controls.speed != 1:
        effects.append(Effect('tempo', (('factor', controls.speed),)))
    graph = ['aformat=sample_fmts=fltp']
    if effects:
        # Supply enough trailing silence for the existing atempo analysis
        # windows, including a very short selected recording; trim the output
        # back to the exact declared frame count below.
        graph.append(f'apad=pad_len={info.rate // 2}')
    graph.extend(audio.effect_filters(effects, sample_rate=info.rate))
    if info.channels == 1:
        angle = (controls.pan + 1) * math.pi / 4
        graph.append(f'pan=stereo|c0={math.cos(angle):.12f}*c0|c1={math.sin(angle):.12f}*c0')
    else:
        left = math.cos(max(0, controls.pan) * math.pi / 2)
        right = math.cos(max(0, -controls.pan) * math.pi / 2)
        graph.append(f'pan=stereo|c0={left:.12f}*c0|c1={right:.12f}*c1')
    graph.extend((f'volume={controls.volume:.12f}', 'alimiter=limit=0.95:level=0:latency=1',
                  f'apad=whole_len={frames}', f'atrim=end_sample={frames}', 'asetpts=PTS-STARTPTS'))
    return ','.join(graph)


def prepare_playback(source, controls, *, library_root, budget=None, ffmpeg=None,
                     broker=None, cancelled=lambda: False, progress=lambda message: None,
                     temporary_parent=None):
    """Prepare and pin a bounded local derivative; never start playback.

    Existing source owners remain authoritative. A result does not approve a
    library selection, project revision, quotation or a future native action.
    """
    if type(controls) is not PlaybackControls:
        raise TypeError('Choose typed independent playback controls.')
    budget = AudioBudget() if budget is None else budget
    if type(budget) is not AudioBudget or not callable(cancelled) or not callable(progress):
        raise TypeError('Choose the existing typed audio budget and callbacks.')
    if cancelled():
        raise InterruptedError('Playback preparation cancelled.')
    numerator, denominator = float(budget.timeout).as_integer_ratio()
    deadline = monotonic_ns() + numerator * 1000000000 // denominator
    root = checked_directory(library_root)
    root_stat = root.stat()
    root_identity = root_stat.st_dev, root_stat.st_ino
    source = Path(source).absolute()
    before = file_identity(regular_file(source))
    with verified_reader(source, expected_identity=before):
        info = audio.wav_info(source)
    if info.frames <= 0 or info.channels not in (1, 2) or not 8000 <= info.rate <= 192000:
        raise ValueError('Choose nonempty mono or stereo PCM from 8,000 to 192,000 Hz.')
    frames = _frames(info, controls)
    required = frames * 4 + 65536
    if required > budget.max_output_bytes:
        raise ValueError('The selected playback controls exceed the temporary PCM byte allowance.')
    binary = resolve_ffmpeg(ffmpeg)
    if binary is None:
        raise ValueError('Configure the reviewed local FFmpeg runtime before preparing playback controls.')
    binary = Path(audio.trusted_ffmpeg(binary))
    binary_identity = file_identity(regular_file(binary))
    coordinator = broker or ResourceBroker.for_app(root, cancelled=cancelled)
    if not isinstance(coordinator, ResourceBroker):
        raise TypeError('Use the current typed resource coordinator.')
    if coordinator.limits.memory_bytes <= 0:
        raise ValueError('Configure a positive declared memory allowance.')
    if temporary_parent is not None:
        temporary_parent = checked_directory(temporary_parent)
    temporary = TemporaryDirectory(prefix='.carr-playback-', dir=temporary_parent)
    stage = Path(temporary.name)
    output = stage / 'playback.wav'
    leases = []
    result = None

    def external_stop():
        return cancelled() or monotonic_ns() >= deadline

    def check():
        if cancelled():
            raise InterruptedError('Playback preparation cancelled.')
        if monotonic_ns() >= deadline:
            raise TimeoutError('Playback preparation reached its selected time allowance.')
        if any(lease.cancelled() for lease in leases):
            raise InterruptedError('Playback preparation resource admission stopped.')
        current = checked_directory(root).stat()
        if (current.st_dev, current.st_ino) != root_identity:
            raise ValueError('The selected library changed during playback preparation.')
        total = 0
        with os.scandir(stage) as entries:
            for count, entry in enumerate(entries, 1):
                if count > 1 or entry.name != output.name:
                    raise ValueError('Unexpected playback staging content.')
                total += regular_file(stage / entry.name).st_size
        if total > budget.max_output_bytes:
            raise ValueError('Playback preparation exceeded its temporary PCM byte allowance.')
        if shutil.disk_usage(stage).free < budget.reserve_disk_bytes:
            raise OSError('Playback preparation reached its selected free disk reserve.')
        return False

    def report(message):
        check()
        try:
            progress(message)
        except Exception:
            pass
        check()

    try:
        check()
        request = ResourceRequest(cpu_slots=budget.threads,
            memory_bytes=coordinator.limits.memory_bytes, disk_bytes=required, disk_path=stage)
        with coordinator.reserve(request, operation_id='playback-prepare:' + uuid4().hex,
                cancelled=external_stop, timeout=60, run_timeout=budget.timeout) as lease:
            leases.append(lease)
            try:
                report('Verifying the selected recording and local playback processor')
                with ExitStack() as readers:
                    readers.enter_context(verified_reader(source, expected_identity=before))
                    readers.enter_context(verified_reader(binary, expected_identity=binary_identity))
                    source_hash = sha256_file(source, cancelled=check, expected_identity=before)
                    processor = audio.ffmpeg_identity(binary, cancelled=check)
                    # Re-read the header under the full native-input interval.
                    if audio.wav_info(source) != info:
                        raise ValueError('The selected recording changed while waiting for resources.')
                    report('Preparing independent playback speed, pitch, volume and balance')
                    command = [str(binary), '-hide_banner', '-nostdin', '-loglevel', 'error',
                        '-max_alloc', str(budget.max_alloc_bytes), '-threads', str(budget.threads),
                        '-filter_threads', str(budget.threads), '-protocol_whitelist', 'file',
                        '-f', 'wav', '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
                        '-map_metadata', '-1', '-map_chapters', '-1', '-af', _filters(info, controls, frames),
                        '-ar', str(info.rate), '-ac', '2', '-c:a', 'pcm_s16le', '-rf64', 'auto',
                        '-fs', str(required), '-progress', 'pipe:1', '-f', 'wav', str(output)]
                    try:
                        run_process(command, budget=budget, cancelled=check, watch=(output,),
                            owned_tree=True, env=audio._ffmpeg_environment(),
                            progress=lambda seconds: report(f'Prepared {seconds:.1f} seconds of playback PCM'))
                    except AudioCleanupPending as exc:
                        wait_for_cleanup(exc)
                        raise
                    actual = audio.wav_info(output)
                    if (actual.frames, actual.rate, actual.channels) != (frames, info.rate, 2):
                        raise ValueError('Playback processing did not preserve its declared frame mapping.')
                    output_identity = file_identity(regular_file(output))
                    output_hash = sha256_file(output, cancelled=check, expected_identity=output_identity)
                    check()
                # The original/runtime owners finish before a result can be
                # adopted. Pinning is still worker work, never a GUI hash pass.
                result = PreparedPlayback(source, before, source_hash, info.frames, info.rate,
                    controls, output, output_identity, output_hash, frames, processor, temporary)
                result.pin(cancelled=check)
                report('Playback controls prepared. Native playback starts only after explicit approval.')
            finally:
                leases.clear()
        check()
        return result
    except BaseException:
        if result is not None:
            try:
                result.release()
            finally:
                result.close()
        else:
            temporary.cleanup()
        raise
