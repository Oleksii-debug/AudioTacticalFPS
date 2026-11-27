"""Private listening derivatives using the existing typed FFmpeg converter.

This is neither a renderer nor another player. A reader worker prepares one
verified derivative; the existing WavePlayer adopts it without starting audio.
Original rendered PCM and synthesis settings are never changed.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import time
import wave
from uuid import uuid4

from ._io import local_file
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, Effect, finite
from ..durable_state import _open_json_file, local_path, regular
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest


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
            return True
        if self._owner is not None:
            raise RuntimeError('The listening device still owns this prepared PCM.')
        self._temporary.cleanup()
        self.closed = True
        return True

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


def _open_stream(path):
    """Open without following a substituted link or blocking on a FIFO."""
    if os.name == 'nt':
        return _open_json_file(path)
    else:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
        try:
            return os.fdopen(descriptor, 'rb')
        except BaseException:
            os.close(descriptor)
            raise

@contextmanager
def _opened(path, expected):
    """Capture a regular descriptor before any WAV parsing or bounded reads."""
    path = local_file(path)
    before = regular(path)
    if _info_stamp(before) != expected:
        raise ValueError('The original recording changed before listening preparation.')
    stream = _open_stream(path)
    def checked():
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, 'st_file_attributes', 0) & 0x400
                or _info_stamp(info) != expected or stamp(path) != expected):
            raise ValueError('The original or prepared listening recording changed.')
    with stream:
        checked()
        yield stream
        checked()


def _hash(path, expected, cancelled):
    digest, count = sha256(), 0
    with _opened(path, expected) as source:
        while True:
            if cancelled():
                raise AudioCancelled('Listening preparation cancelled.')
            block = source.read(min(1024 * 1024, expected[2] - count + 1))
            if not block:
                break
            count += len(block)
            if count > expected[2]:
                raise ValueError('The listening recording grew during verification.')
            digest.update(block)
        if count != expected[2]:
            raise ValueError('The listening recording was truncated during verification.')
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
    def cancelled():
        if original_cancelled():
            return True
        if time.monotonic() >= deadline:
            raise AudioError('Listening preparation exceeded its total time allowance.')
        return False
    if cancelled():
        raise AudioCancelled('Listening preparation cancelled before reading audio.')
    source, directory = local_file(request.source), local_path(directory, exists=True)
    if not directory.is_dir() or stamp(source) != request.identity:
        raise ValueError('The original recording or listening directory changed.')
    size = request.identity[2]
    if size > budget.max_output_bytes:
        raise AudioError('The recording exceeds the private listening-copy byte allowance.')
    with _opened(source, request.identity) as stream:
        with wave.open(stream, 'rb') as audio:
            frames, rate, channels, width = audio.getnframes(), audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
            if audio.getcomptype() != 'NONE' or frames <= 0 or width not in (2, 3, 4) or channels not in (1, 2):
                raise ValueError('Listening preparation requires nonempty mono or stereo PCM WAV.')
    if not 8000 <= rate <= 192000:
        raise ValueError('The original PCM rate is outside the listening conversion allowance.')
    expected_frames = round(frames / request.settings.speed)
    if expected_frames <= 0:
        raise ValueError('The selected speed leaves no complete listening frame.')
    needed = expected_frames * channels * width + 65536
    if needed > budget.max_output_bytes or shutil.disk_usage(directory).free < size + 2 * needed + budget.reserve_disk_bytes:
        raise AudioError('There is not enough bounded space for the original snapshot and listening copy.')
    app_root = local_path(Path(state_root).parent if state_root is not None else directory.parent,
                          exists=True)
    if state_root is not None and Path(state_root).name != 'state':
        raise ValueError('Listening preparation needs the selected library state directory.')
    try:
        broker = broker if broker is not None else ResourceBroker.for_app(app_root, cancelled=cancelled)
    except ResourceCancelled as exc:
        raise AudioCancelled('Listening resource selection cancelled.') from exc
    if (broker.root != app_root / 'runtime' / 'resources'
            or broker.policy_path != app_root / 'state' / 'operations' / 'resources.json'):
        raise ValueError('Listening resources belong to a different library.')
    reservation = ResourceRequest(cpu_slots=budget.threads,
        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
        disk_path=directory, disk_bytes=size + 2 * needed + budget.reserve_disk_bytes)
    temporary, prepared = None, None
    try:
        with ExitStack() as resources:
            lease = resources.enter_context(broker.reserve(reservation,
                operation_id='listening:' + uuid4().hex, cancelled=cancelled,
                timeout=min(60, budget.timeout), run_timeout=budget.timeout))
            try:
                before_lease = cancelled
                def cancelled():
                    return before_lease() or lease.cancelled()
                if cancelled():
                    raise AudioCancelled('Listening preparation cancelled before its private snapshot.')
                temporary = TemporaryDirectory(prefix='listening-', dir=directory)
                root = Path(temporary.name)
                snapshot = root / 'source.wav'
                digest = sha256()
                with _opened(source, request.identity) as original, snapshot.open('xb') as output:
                    if _info_stamp(os.fstat(original.fileno())) != request.identity:
                        raise ValueError('The original recording changed before its private snapshot.')
                    copied = 0
                    while True:
                        block = original.read(min(1024 * 1024, size - copied + 1))
                        if not block:
                            break
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
                with _opened(snapshot, snapshot_identity) as stream:
                    with wave.open(stream, 'rb') as audio:
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
                            from ..audio_runtime import bundled_audio_tools
                            tools = bundled_audio_tools() or {}
                            runtime = AudioRuntime(root, ffmpeg=tools.get('ffmpeg'), ffprobe=tools.get('ffprobe'), budget=budget)
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
                output_identity = stamp(output)
                with _opened(output, output_identity) as stream:
                    with wave.open(stream, 'rb') as audio:
                        if (audio.getcomptype() != 'NONE' or audio.getframerate() != rate
                                or audio.getnchannels() != channels or audio.getsampwidth() != width
                                or abs(audio.getnframes() - expected_frames) > 1):
                            raise AudioError('Listening conversion did not produce the measured frame plan.')
                        # Map actual integer output frames, including one-frame
                        # converter rounding; never infer from nominal speed.
                        mapping = FrameMapping(frames, audio.getnframes(), rate)
                        _verify_pcm_payload(audio, mapping.output_frames, channels, width, cancelled)
                if _hash(source, request.identity, cancelled) != source_hash:
                    raise ValueError('The original recording changed during listening conversion.')
                output_hash = _hash(output, output_identity, cancelled)
                if output_hash != expected_output_hash:
                    raise ValueError('The listening copy changed after conversion.')
                if cancelled():
                    raise AudioCancelled('Listening preparation cancelled before adoption.')
                if snapshot.exists():
                    snapshot.unlink()
                prepared = PreparedListening(request, temporary, output, source_hash, output_hash, mapping, output_identity)
            except AudioCleanupPending as exc:
                held = resources.pop_all()
                exc.cleanup_owner.add_quiescent_callback(held.close)
                raise
        return prepared
    except AudioCleanupPending as exc:
        # The same native owner retains its private files. Resource release is
        # attached inside the reservation scope below, before its stack exits.
        if temporary is not None:
            exc.cleanup_owner.add_quiescent_callback(temporary.cleanup)
            temporary = None
        raise
    except ResourceCancelled as exc:
        if temporary is not None:
            temporary.cleanup()
        raise AudioCancelled('Listening preparation cancelled while waiting for shared resources.') from exc
    except BaseException:
        if temporary is not None:
            temporary.cleanup()
        raise
