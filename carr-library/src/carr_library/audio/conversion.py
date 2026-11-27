"""Reviewed processed copies through the existing local AudioRuntime.

This module owns no codec, device, queue or saved job store. It binds one
explicit source/output review to the shared resource and publication services.
"""
from dataclasses import dataclass, replace
from hashlib import sha256
import math
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..durable_state import digest_file, encode, local_path, regular
from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
from ..research.export_file import ExportDestination, capture_export_destination
from ..runtime_settings import audio_runtime
from .cleanup import pending_cleanup
from .effect_presets import checked_chain
from .models import AudioBudget, AudioCancelled, AudioError, finite, integer
from .process import publish_file

FORMATS = frozenset(('.wav', '.flac', '.mp3', '.m4a', '.m4b', '.ogg', '.opus',
                     '.aac', '.aif', '.aiff', '.wma'))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _directory(path):
    value = regular(local_path(path, exists=True), directory=True)
    return value.st_dev, value.st_ino


def _stopped(cancelled, guard):
    guard()
    if cancelled():
        raise AudioCancelled('Audio processing cancelled before publication.')
    return False


@dataclass(frozen=True)
class ConversionSettings:
    start: float = 0
    duration: float | None = None
    sample_rate: int = 24000
    channels: int = 2
    bit_depth: int = 16
    bitrate: int = 192
    effects: tuple = ()

    def __post_init__(self):
        finite(self.start, 'Selection start', 0)
        if self.duration is not None:
            finite(self.duration, 'Selection duration', .000001)
        integer(self.sample_rate, 'Output sample rate', 8000, 192000)
        integer(self.channels, 'Output channels', 1, 2)
        if type(self.bit_depth) is not int or self.bit_depth not in (16, 24, 32):
            raise ValueError('PCM bit depth must be 16, 24 or 32.')
        integer(self.bitrate, 'Encoding bitrate', 32, 512)
        object.__setattr__(self, 'effects', checked_chain(self.effects, self.sample_rate))


@dataclass(frozen=True)
class ConversionReview:
    library_root: Path
    library_identity: tuple
    source: Path
    source_identity: tuple
    destination: ExportDestination
    settings: ConversionSettings
    budget: AudioBudget


@dataclass(frozen=True)
class ConversionResult:
    audio: object
    measurement: object
    source_sha256: str
    effects_sha256: str
    durability_confirmed: bool = True
    cleanup_warning: str | None = None
    cleanup_owner: object | None = None


def review_conversion(library_root, source, destination, *, settings=None, budget=None,
                      guard=lambda: None):
    """Capture only explicit local identities; large byte reads stay on the worker."""
    guard()
    root = local_path(library_root, exists=True)
    identity = _directory(root)
    selected = local_path(source, exists=True)
    source_info = regular(selected)
    settings = ConversionSettings() if settings is None else settings
    budget = AudioBudget() if budget is None else budget
    if not isinstance(settings, ConversionSettings) or not isinstance(budget, AudioBudget):
        raise ValueError('Choose typed conversion settings and an audio budget.')
    if not 0 < source_info.st_size <= budget.max_output_bytes:
        raise ValueError('The selected source exceeds the configured audio file allowance.')
    if Path(destination).suffix.casefold() not in FORMATS:
        raise ValueError('Choose a supported audio output extension.')
    target = capture_export_destination(destination, private_root=root)
    if not target.require_absent:
        raise FileExistsError('Choose a new output filename; processed audio never replaces an existing file.')
    guard()
    if _directory(root) != identity or _stamp(regular(selected)) != _stamp(source_info):
        raise ValueError('The selected library or source changed during review.')
    return ConversionReview(root, identity, selected, _stamp(source_info), target, settings, budget)


def _memory(runtime):
    # Declared cooperative allowance; FFmpeg's per-allocation limit is not RSS.
    return max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes)


def discover_effects(library_root, *, cancelled=lambda: False, guard=lambda: None):
    """Probe current local capabilities on the caller's existing worker."""
    _stopped(cancelled, guard)
    root = local_path(library_root, exists=True)
    identity = _directory(root)
    def check():
        _stopped(cancelled, guard)
        if _directory(root) != identity:
            raise ValueError('The selected library changed during effect discovery.')
        return False
    broker = ResourceBroker.for_app(root, cancelled=check)
    temporary = TemporaryDirectory(prefix='carr-effect-capabilities-')
    try:
        runtime = audio_runtime(temporary.name, state_root=root / 'state')
        request = ResourceRequest(cpu_slots=runtime.budget.threads, memory_bytes=_memory(runtime),
            disk_bytes=2 * runtime.budget.max_log_bytes, disk_path=Path(temporary.name))
        with broker.reserve(request, operation_id='audio-effects:' + uuid4().hex,
                            cancelled=check, timeout=60):
            check()
            result = runtime.capabilities(cancelled=check)
            check()
            return tuple(result['effects'])
    except ResourceCancelled as error:
        raise AudioCancelled('Effect discovery cancelled while waiting for resources.') from error
    finally:
        # reserve settles existing native CleanupOwner before reaching this.
        temporary.cleanup()


def _copy_source(review, staged, check):
    expected = review.source_identity
    checksum, count = sha256(), 0
    with review.source.open('rb') as source, staged.open('xb') as target:
        if _stamp(os.fstat(source.fileno())) != expected:
            raise ValueError('The selected audio source changed before copying.')
        while True:
            check()
            block = source.read(min(1024 * 1024, expected[4] + 1 - count))
            if not block:
                break
            count += len(block)
            if count > expected[4]:
                raise ValueError('The selected audio source grew during copying.')
            checksum.update(block)
            target.write(block)
        if count != expected[4] or _stamp(os.fstat(source.fileno())) != expected:
            raise ValueError('The selected audio source changed during copying.')
        check()
        target.flush()
        os.fsync(target.fileno())
    return checksum.hexdigest()


def _published(staged, target):
    """Distinguish our visible hard-link commit from an earlier I/O failure."""
    try:
        first, second = staged.lstat(), target.lstat()
    except OSError:
        return False
    if any(not stat.S_ISREG(value.st_mode) or getattr(value, 'st_file_attributes', 0)
           & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0) for value in (first, second)):
        return False
    return (first.st_dev, first.st_ino, first.st_size) == (second.st_dev, second.st_ino, second.st_size)


def convert_audio(review, *, cancelled=lambda: False, guard=lambda: None,
                  before_publish=None, before_runtime=None):
    """Create one immutable processed copy, with final source/destination checks.

    An optional runtime guard runs inside inspection admission before any codec.
    An optional checkpoint may durably record a candidate before publication. It
    grants no commit authority: final checks and the create-only publisher still
    run afterward. No checkpoint is called after an external file is committed.
    """
    if not isinstance(review, ConversionReview):
        raise ValueError('Review the exact source, settings and new output before processing.')
    if (not callable(cancelled) or not callable(guard)
            or before_publish is not None and not callable(before_publish)
            or before_runtime is not None and not callable(before_runtime)):
        raise ValueError('Audio processing requires callable cancellation, scope and publication guards.')

    def check():
        _stopped(cancelled, guard)
        if (_directory(review.library_root) != review.library_identity
                or local_path(review.source, exists=True) != review.source
                or _stamp(regular(review.source)) != review.source_identity):
            raise ValueError('The reviewed audio source or library changed.')
        if capture_export_destination(review.destination.requested,
                private_root=review.library_root) != review.destination:
            raise ValueError('The reviewed output destination changed. Choose it again.')
        return False

    check()
    broker = ResourceBroker.for_app(review.library_root, cancelled=check)
    operation = 'audio-convert:' + uuid4().hex
    temporary = TemporaryDirectory(prefix='.audio-convert-', dir=review.destination.target.parent)
    result, failure = None, None
    try:
        root = local_path(temporary.name, exists=True)
        runtime = audio_runtime(root, state_root=review.library_root / 'state', budget=review.budget)
        copied, staged = root / 'source.audio', root / ('processed' + review.destination.target.suffix)
        log_bytes = 2 * runtime.budget.max_log_bytes + runtime.budget.reserve_disk_bytes
        inspection = ResourceRequest(cpu_slots=runtime.budget.threads, memory_bytes=_memory(runtime),
            disk_bytes=review.source_identity[4] + log_bytes, disk_path=root)
        with broker.reserve(inspection, operation_id=operation + ':inspect', cancelled=check, timeout=60):
            check()
            if before_runtime is not None:
                before_runtime(runtime)
                check()
            source_digest = _copy_source(review, copied, check)
            capabilities = runtime.capabilities(cancelled=check)
            checked_chain(review.settings.effects, review.settings.sample_rate,
                          available=capabilities['effects'])
            info = runtime.inspect(copied, cancelled=check)
            settings = review.settings
            selected = info.duration - settings.start if settings.duration is None else settings.duration
            if settings.start >= info.duration or selected > info.duration - settings.start + 1 / info.sample_rate:
                raise ValueError('The reviewed interval exceeds the measured selected source.')
            tempo = math.prod(float(effect.parameters.get('factor', 1)) for effect in settings.effects
                              if effect.kind == 'tempo' and effect.enabled)
            seconds = selected / tempo
            per_second = settings.sample_rate * settings.channels * (settings.bit_depth // 8)
            if not 0 < seconds <= runtime.budget.max_output_bytes / per_second:
                raise AudioError('The processed interval exceeds the configured output allowance.')
            pcm_bytes = math.ceil(seconds * per_second)
            if staged.suffix.casefold() in ('.wav', '.aif', '.aiff'):
                output_bytes = pcm_bytes + 65536
            else:
                # A requested compressed bitrate can exceed low-rate PCM.
                # Reserve both that rate and PCM, with a bounded VBR/container
                # allowance; enforce this same declared cap in the codec owner.
                encoded_bytes = math.ceil(seconds * settings.bitrate * 1000 / 8)
                output_bytes = 2 * max(pcm_bytes, encoded_bytes) + 65536
            if output_bytes > runtime.budget.max_output_bytes:
                raise AudioError('The processed interval exceeds the configured output allowance.')
            runtime.budget = replace(runtime.budget, max_output_bytes=output_bytes)
            check()
        # Do not hold a partial CPU reservation while waiting for the complete
        # output allowance. The private verified copy is passive between jobs.
        request = ResourceRequest(cpu_slots=runtime.budget.threads, memory_bytes=_memory(runtime),
            disk_bytes=review.source_identity[4] + output_bytes + log_bytes, disk_path=root)
        with broker.reserve(request, operation_id=operation + ':render', cancelled=check, timeout=60):
            check()
            if digest_file(copied, cancelled=check) != source_digest:
                raise ValueError('The private selected-audio snapshot changed.')
            rendered = runtime.convert(copied, staged, start=settings.start, duration=settings.duration,
                sample_rate=settings.sample_rate, channels=settings.channels, bit_depth=settings.bit_depth,
                bitrate=settings.bitrate, effects=settings.effects, cancelled=check)
            measurement = runtime.measure(staged, cancelled=check)
            check()

            def publication_check():
                check()
                # Same-size overwrites can retain observed timestamps. Verify
                # exact original bytes at both sides of the pre-commit flush.
                if digest_file(review.source, cancelled=check) != source_digest:
                    raise ValueError('The selected source changed; no processed copy was published.')
                check()
                return False

            completed = ConversionResult(replace(rendered, path=review.destination.target), measurement,
                source_digest, sha256(encode([effect.to_dict() for effect in settings.effects])).hexdigest())
            if before_publish is not None:
                publication_check()
                before_publish(completed, staged, runtime)
            try:
                publish_file(staged, review.destination.target, cancelled=publication_check)
            except FileExistsError:
                # A competitor's same-byte file is still a no-clobber conflict.
                raise
            except BaseException:
                if not _published(staged, review.destination.target):
                    raise
                result = replace(completed, durability_confirmed=False,
                    cleanup_warning='The audio file is saved; directory durability could not be confirmed.')
            else:
                result = completed
            # No cancellation or scope callback follows the visible commit.
    except ResourceCancelled as error:
        failure = error
        if result is None:
            raise AudioCancelled('Audio processing cancelled while waiting for resources.') from error
        warning = 'The audio file is saved; resource-release review is pending.'
        result = replace(result, cleanup_warning=((result.cleanup_warning + ' ') if result.cleanup_warning else '') + warning)
    except BaseException as error:
        failure = error
        if result is None:
            raise
        warning = 'The audio file is saved; resource-release review is pending.'
        result = replace(result, cleanup_warning=((result.cleanup_warning + ' ') if result.cleanup_warning else '') + warning)
    finally:
        try:
            temporary.cleanup()
        except Exception as error:
            pending = pending_cleanup(temporary.cleanup,
                message='Audio processing staging cleanup remains owned.')
            if result is not None:
                warning = 'The audio file is saved; temporary-file cleanup is still pending.'
                if result.cleanup_warning:
                    warning = result.cleanup_warning + ' ' + warning
                result = replace(result, cleanup_warning=warning, cleanup_owner=pending.cleanup_owner)
            elif failure is not None:
                failure.add_note('Temporary audio cleanup remains owned and pending.')
            else:
                raise pending from error
    return result
