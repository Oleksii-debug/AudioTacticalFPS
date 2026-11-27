"""Private listening derivatives using the existing typed FFmpeg converter.

This is neither a renderer nor another player. A reader worker prepares one
verified derivative; the existing WavePlayer adopts it without starting audio.
Original rendered PCM and synthesis settings are never changed.
"""
from dataclasses import dataclass
from contextlib import ExitStack
from uuid import uuid4
from hashlib import sha256
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
import wave

from ._io import local_file
from ..audio.models import AudioBudget, AudioCancelled, AudioError, Effect, finite
from ..durable_state import local_path
from ..audio.cleanup import retained_context
from ..operations import ResourceBroker, ResourceRequest


LISTENING_BUDGET = AudioBudget(timeout=300, max_output_bytes=2 * 1024**3,
                              max_inputs=1, max_staging_files=8, threads=1)


def stamp(path):
    info = local_file(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _info_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class ListeningSettings:
    speed: float = 1
    pitch_semitones: float = 0

    def __post_init__(self):
        object.__setattr__(self, 'speed', finite(self.speed, 'Listening speed', .25, 4))
        object.__setattr__(self, 'pitch_semitones', finite(self.pitch_semitones, 'Listening pitch', -12, 12))


@dataclass(frozen=True)
class ListeningRequest:
    source: Path
    identity: tuple
    settings: ListeningSettings
    position_seconds: float
    token: object


@dataclass(frozen=True)
class FrameMapping:
    source_frames: int
    output_frames: int
    sample_rate: int

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (
                self.source_frames, self.output_frames, self.sample_rate)):
            raise ValueError('Listening timelines need actual positive PCM frame counts.')

    @property
    def source_duration(self): return self.source_frames / self.sample_rate

    @property
    def output_duration(self): return self.output_frames / self.sample_rate

    def to_output(self, seconds):
        seconds = finite(seconds, 'Original recording position', 0, self.source_duration)
        if seconds == self.source_duration:
            return self.output_duration
        return seconds * self.output_frames / self.source_frames

    def to_source(self, seconds):
        seconds = finite(seconds, 'Listening position', 0)
        if seconds >= self.output_duration:
            return self.source_duration
        return seconds * self.source_frames / self.output_frames


class PreparedListening:
    """Own the private PCM until its player confirms native device closure."""
    def __init__(self, request, temporary, path, source_sha256, output_sha256, mapping, identity):
        self.request, self._temporary, self.path = request, temporary, Path(path)
        self.source_sha256, self.output_sha256, self.mapping = source_sha256, output_sha256, mapping
        self.identity = identity
        self.closed = False
        self._owner = None
        self.check_live()

    def check_live(self):
        if self.closed:
            raise ValueError('The prepared listening copy has already been released.')
        if stamp(self.request.source) != self.request.identity or stamp(self.path) != self.identity:
            raise ValueError('Original or prepared listening audio changed; prepare it again explicitly.')

    def close(self):
        if self.closed:
            return
        if self._owner is not None:
            raise RuntimeError('The listening device still owns this prepared PCM.')
        self._temporary.cleanup()
        self.closed = True

    def claim(self, owner):
        self.check_live()
        if self._owner is not None:
            raise RuntimeError('A listening copy already has a playback owner.')
        self._owner = owner

    def release(self, owner):
        if self._owner is not owner:
            raise RuntimeError('Only the playback owner can release its listening PCM.')
        self._owner = None
        try:
            self.close()
        except BaseException:
            self._owner = owner
            raise


def _hash(path, expected, cancelled):
    if stamp(path) != expected:
        raise ValueError('The original recording changed before listening preparation.')
    digest, count = sha256(), 0
    with Path(path).open('rb') as source:
        if _info_stamp(os.fstat(source.fileno())) != expected or stamp(path) != expected:
            raise ValueError('The reviewed recording changed before its bytes were consumed.')
        while True:
            if cancelled():
                raise AudioCancelled('Listening preparation cancelled.')
            block = source.read(min(1024 * 1024, expected[2] - count + 1))
            if not block:
                break
            count += len(block)
            if count > expected[2]:
                raise ValueError('The reviewed recording changed beyond its captured byte count.')
            digest.update(block)
        if count != expected[2] or _info_stamp(os.fstat(source.fileno())) != expected:
            raise ValueError('The original recording changed while being checked.')
    if stamp(path) != expected:
        raise ValueError('The original recording changed while being checked.')
    return digest.hexdigest()


def _verify_pcm_payload(audio, frames, channels, width, cancelled):
    length = 0
    while block := audio.readframes(32768):
        if cancelled():
            raise AudioCancelled('Listening PCM verification cancelled.')
        length += len(block)
    if length != frames * channels * width:
        raise ValueError('The listening source or result contains truncated PCM frames.')


def prepare_listening(request, directory, *, state_root=None, cancelled=lambda: False,
                      runtime_factory=None, budget=LISTENING_BUDGET, broker=None):
    """Run on the reader's existing worker, not a native UI callback.

    The runtime reads a bounded private snapshot, then this operation rechecks
    the original source's full digest and identity before returning. A late
    source/tab/owner change must also pass the caller's adoption guard.
    """
    if (not isinstance(request, ListeningRequest) or not isinstance(request.settings, ListeningSettings)
            or not isinstance(budget, AudioBudget) or not callable(cancelled)):
        raise ValueError('A captured listening request and cancellation owner are required.')
    original_cancelled, deadline = cancelled, time.monotonic() + budget.timeout
    lease = None
    def admission_cancelled():
        if original_cancelled():
            return True
        if time.monotonic() >= deadline:
            raise AudioError('Listening preparation exceeded its total time allowance.')
        return False
    def cancelled():
        if admission_cancelled():
            return True
        if lease is not None:
            lease.checkpoint()
        return False
    if cancelled():
        raise AudioCancelled('Listening preparation cancelled before reading audio.')
    source, directory = local_file(request.source), local_path(directory, exists=True)
    if not directory.is_dir() or stamp(source) != request.identity:
        raise ValueError('The original recording or listening directory changed.')
    size = request.identity[2]
    if size > budget.max_output_bytes:
        raise AudioError('The recording exceeds the private listening-copy byte allowance.')
    with wave.open(str(source), 'rb') as audio:
        frames, rate, channels, width = audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        if audio.getcomptype() != 'NONE' or frames <= 0 or width not in (2, 3, 4) or channels not in (1, 2):
            raise ValueError('Listening preparation requires nonempty mono or stereo PCM WAV.')
    if not 8000 <= rate <= 192000:
        raise ValueError('The original PCM rate is outside the listening conversion allowance.')
    expected_frames = round(frames / request.settings.speed)
    if expected_frames <= 0:
        raise ValueError('The selected speed leaves no complete listening frame.')
    needed = expected_frames * channels * width + 65536
    if needed > budget.max_output_bytes or shutil.disk_usage(directory).free < size + needed + budget.reserve_disk_bytes:
        raise AudioError('There is not enough bounded space for the original snapshot and listening copy.')
    info = directory.stat()
    directory_identity = info.st_dev, info.st_ino
    if broker is None:
        if state_root is None:
            raise ValueError('Listening preparation requires the existing library resource owner.')
        broker = ResourceBroker.for_app(Path(state_root).parent)
    admission = ResourceRequest(cpu_slots=budget.threads,
        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
        disk_bytes=size + needed, disk_path=directory)
    with retained_context(broker.reserve(admission,
            operation_id='reader-listening:' + uuid4().hex,
            cancelled=admission_cancelled, timeout=min(60, budget.timeout))) as lease:
        if cancelled():
            raise AudioCancelled('Listening preparation cancelled after resource admission.')
        current = local_path(directory, exists=True).stat()
        if (current.st_dev, current.st_ino) != directory_identity or stamp(source) != request.identity:
            raise ValueError('The source or listening directory changed while waiting for resources.')
        lease.checkpoint()
        if shutil.disk_usage(directory).free < size + needed + budget.reserve_disk_bytes:
            raise AudioError('The bounded listening-copy disk allowance changed during admission.')
        with retained_context(ExitStack()) as staging:
            temporary = TemporaryDirectory(prefix='listening-', dir=directory)
            staging.callback(temporary.cleanup)
            root = Path(temporary.name)
            snapshot = root / 'source.wav'
            digest = sha256()
            with source.open('rb') as original, snapshot.open('xb') as output:
                if _info_stamp(os.fstat(original.fileno())) != request.identity:
                    raise ValueError('The original recording changed before its private snapshot.')
                copied = 0
                while block := original.read(1024 * 1024):
                    if cancelled():
                        raise AudioCancelled('Listening snapshot cancelled.')
                    copied += len(block)
                    if copied > size or copied > budget.max_output_bytes:
                        raise ValueError('The original recording grew during its private snapshot.')
                    if shutil.disk_usage(root).free < budget.reserve_disk_bytes + needed:
                        raise AudioError('Listening preparation stopped to preserve its disk reserve.')
                    digest.update(block)
                    output.write(block)
                if copied != size or _info_stamp(os.fstat(original.fileno())) != request.identity:
                    raise ValueError('The original recording changed during its private snapshot.')
                output.flush()
                os.fsync(output.fileno())
            if stamp(source) != request.identity:
                raise ValueError('The original recording changed during its private snapshot.')
            source_hash = digest.hexdigest()
            snapshot_identity = stamp(snapshot)
            with wave.open(str(snapshot), 'rb') as audio:
                if (audio.getframerate(), audio.getnchannels(), audio.getnframes(), audio.getsampwidth()) != (rate, channels, frames, width):
                    raise ValueError('The private snapshot differs from the selected original PCM.')
                _verify_pcm_payload(audio, frames, channels, width, cancelled)
            if request.settings == ListeningSettings():
                # Explicit reset keeps exact original bytes and needs no optional
                # engine. The private copy still has the same ownership lifetime.
                output = root / 'listening.wav'
                snapshot.rename(output)
                expected_output_hash = source_hash
            else:
                if runtime_factory is None:
                    if state_root is None:
                        from ..audio import AudioRuntime
                        from ..audio.runtime import resolve_active_audio_tool
                        runtime = AudioRuntime(root,
                            ffmpeg=resolve_active_audio_tool('ffmpeg'),
                            ffprobe=resolve_active_audio_tool('ffprobe'), budget=budget)
                    else:
                        from ..runtime_settings import audio_runtime
                        runtime = audio_runtime(root, state_root=state_root, budget=budget)
                else:
                    runtime = runtime_factory(root, budget=budget)
                # Cache existing capability checks with this cancellation token;
                # subsequent Convert validation reuses them without another worker.
                runtime.capabilities(cancelled=cancelled)
                effects = (Effect('tempo', factor=request.settings.speed),
                           Effect('pitch', semitones=request.settings.pitch_semitones))
                result = runtime.convert(snapshot, 'listening.wav', effects=effects,
                    sample_rate=rate, channels=channels, bit_depth=width * 8, cancelled=cancelled)
                output = local_file(result.path)
                if output != root / 'listening.wav':
                    raise ValueError('The listening copy escaped its private output directory.')
                if _hash(snapshot, snapshot_identity, cancelled) != source_hash:
                    raise ValueError('The private original snapshot changed during conversion.')
                expected_output_hash = result.sha256
            with wave.open(str(output), 'rb') as audio:
                if (audio.getcomptype() != 'NONE' or audio.getframerate() != rate
                        or audio.getnchannels() != channels or audio.getsampwidth() != width
                        or abs(audio.getnframes() - expected_frames) > 1):
                    raise AudioError('Listening conversion did not produce the measured frame plan.')
                # Converter duration arithmetic can round one frame differently
                # from the original integer count. All transport mapping uses the
                # actual measured output count, never the nominal speed alone.
                mapping = FrameMapping(frames, audio.getnframes(), rate)
                _verify_pcm_payload(audio, mapping.output_frames, channels, width, cancelled)
            if _hash(source, request.identity, cancelled) != source_hash:
                raise ValueError('The original recording changed during listening conversion.')
            output_identity = stamp(output)
            output_hash = _hash(output, output_identity, cancelled)
            if output_hash != expected_output_hash:
                raise ValueError('The listening copy changed after conversion.')
            if cancelled():
                raise AudioCancelled('Listening preparation cancelled before adoption.')
            if snapshot.exists():
                snapshot.unlink()
            prepared = PreparedListening(request, temporary, output, source_hash, output_hash, mapping, output_identity)
            staging.pop_all()  # Ownership is now held by PreparedListening.
            return prepared
