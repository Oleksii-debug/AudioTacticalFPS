"""Private listening derivatives using the existing typed FFmpeg converter.

This is neither a renderer nor another player. A reader worker prepares one
verified derivative; the existing WavePlayer adopts it without starting audio.
Original rendered PCM and synthesis settings are never changed.
"""
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
import wave
from uuid import uuid4

from ._io import local_file
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, AudioError, Effect, finite
from ..audio.cleanup import pending_cleanup
from ..durable_state import local_path, regular
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
    digest, read_bytes = sha256(), 0
    with Path(path).open('rb') as source:
        while True:
            if cancelled():
                raise AudioCancelled('Listening preparation cancelled.')
            # A changed file must not consume bytes beyond the captured
            # reservation while waiting for the final descriptor identity check.
            block = source.read(min(1024 * 1024, expected[2] - read_bytes + 1))
            if not block:
                break
            read_bytes += len(block)
            if read_bytes > expected[2]:
                raise ValueError('Listening audio grew during its bounded hash.')
            digest.update(block)
        if read_bytes != expected[2] or _info_stamp(os.fstat(source.fileno())) != expected:
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


def _prepare_listening(request, directory, *, state_root=None, cancelled=lambda: False,
                      runtime_factory=None, budget=LISTENING_BUDGET):
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
    temporary = TemporaryDirectory(prefix='listening-', dir=directory)
    try:
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
            runtime_budget = replace(budget, max_output_bytes=needed)
            if runtime_factory is None:
                if state_root is None:
                    from ..audio import AudioRuntime
                    from ..audio_runtime import bundled_audio_tools
                    tools = bundled_audio_tools() or {}
                    runtime = AudioRuntime(root, ffmpeg=tools.get('ffmpeg'), ffprobe=tools.get('ffprobe'), budget=runtime_budget)
                else:
                    from ..runtime_settings import audio_runtime
                    runtime = audio_runtime(root, state_root=state_root, budget=runtime_budget)
            else:
                runtime = runtime_factory(root, budget=runtime_budget)
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
        return PreparedListening(request, temporary, output, source_hash, output_hash, mapping, output_identity)
    except BaseException as error:
        def release_temporary():
            try:
                temporary.cleanup()
            except Exception:
                # Keep a strong owner even if ordinary filesystem cleanup fails.
                pending_cleanup(temporary.cleanup,
                    message='Private listening-copy cleanup is still pending.')
        if isinstance(error, AudioCleanupPending):
            # The native child must release source/output handles before staging
            # can disappear. ResourceBroker retains its lease for this owner.
            error.cleanup_owner.add_quiescent_callback(release_temporary)
        else:
            release_temporary()
        raise

def prepare_listening(request, directory, *, state_root=None, cancelled=lambda: False,
                      runtime_factory=None, budget=LISTENING_BUDGET):
    """Admit one private listening conversion in the selected library policy.

    Source and directory identities are captured before waiting and checked
    again under admission. Native cleanup retains the shared reservation and
    its private files until the current process owner confirms quiescence.
    """
    if (not isinstance(request, ListeningRequest) or not isinstance(request.settings, ListeningSettings)
            or not isinstance(budget, AudioBudget) or not callable(cancelled)):
        raise ValueError('A captured listening request and cancellation owner are required.')
    if state_root is None:
        raise ValueError('Select the current library before preparing listening controls.')
    selected_state = Path(state_root).absolute()
    if selected_state.name != 'state':
        raise ValueError('Listening controls require the selected library state directory.')
    library_root = local_path(selected_state.parent, exists=True)
    library_info = regular(library_root, directory=True)
    library_identity = library_info.st_dev, library_info.st_ino
    selected_state = local_path(selected_state, root=library_root)
    directory = local_path(directory, exists=True)
    directory_info = regular(directory, directory=True)
    directory_identity = directory_info.st_dev, directory_info.st_ino
    deadline = time.monotonic() + budget.timeout

    def check():
        if cancelled():
            raise AudioCancelled('Listening preparation cancelled.')
        if time.monotonic() >= deadline:
            raise AudioError('Listening preparation exceeded its total time allowance.')
        current_root = regular(local_path(library_root, exists=True), directory=True)
        current_directory = regular(local_path(directory, exists=True), directory=True)
        if ((current_root.st_dev, current_root.st_ino) != library_identity
                or (current_directory.st_dev, current_directory.st_ino) != directory_identity):
            raise ValueError('The selected library or private listening directory changed.')
        local_path(selected_state, root=library_root)
        if stamp(request.source) != request.identity:
            raise ValueError('The original recording changed before listening preparation.')
        return False

    check()
    size = request.identity[2]
    if not 0 < size <= budget.max_output_bytes:
        raise AudioError('The recording exceeds the private listening-copy byte allowance.')
    with wave.open(str(request.source), 'rb') as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        channels, width = audio.getnchannels(), audio.getsampwidth()
        if (audio.getcomptype() != 'NONE' or frames <= 0 or width not in (2, 3, 4)
                or channels not in (1, 2) or not 8000 <= rate <= 192000):
            raise ValueError('Listening preparation requires supported nonempty mono or stereo PCM WAV.')
    planned_frames = round(frames / request.settings.speed)
    output_bytes = planned_frames * channels * width + 65536
    if planned_frames <= 0 or output_bytes > budget.max_output_bytes:
        raise AudioError('The requested listening copy exceeds its configured byte allowance.')
    check()
    broker = ResourceBroker.for_app(library_root, cancelled=check)
    reservation = ResourceRequest(cpu_slots=budget.threads,
        memory_bytes=max(512 * 1024**2, 2 * budget.max_alloc_bytes),
        disk_bytes=size + output_bytes + 2 * budget.max_log_bytes + budget.reserve_disk_bytes,
        disk_path=directory)
    try:
        with broker.reserve(reservation, operation_id='listening-prepare:' + uuid4().hex,
                            cancelled=check, timeout=min(60, budget.timeout),
                            run_timeout=budget.timeout) as lease:
            def admitted_check():
                lease.checkpoint()
                return check()
            admitted_check()
            return _prepare_listening(request, directory, state_root=selected_state,
                cancelled=admitted_check, runtime_factory=runtime_factory, budget=budget)
    except ResourceCancelled as error:
        raise AudioCancelled('Listening preparation cancelled while waiting for shared resources.') from error
