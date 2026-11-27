"""Real local processed-copy boundaries; synthetic tones are not quality evidence."""
from array import array
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioBudget, AudioRuntime, Effect
from carr_library.audio import conversion
from carr_library.audio.conversion import ConversionSettings, convert_audio, review_conversion
from carr_library.audio.models import AudioCancelled, AudioCleanupPending
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceTimeout, ResourceUnavailable


@pytest.fixture
def selected(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Actual local FFmpeg/ffprobe required.'
    library = tmp_path / 'library'; library.mkdir()
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    source = tmp_path / 'Original — Україна.wav'
    values = array('h', (round(6000 * math.sin(2 * math.pi * 440 * index / 24000))
                         for index in range(48000)))
    if sys.byteorder != 'little':
        values.byteswap()
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(values.tobytes())
    return SimpleNamespace(root=tmp_path, library=library, source=source, broker=broker)


def review(e, name='processed.wav', *, effects=(), **settings):
    return review_conversion(e.library, e.source, e.root / name,
        settings=ConversionSettings(effects=effects, channels=1, **settings),
        budget=AudioBudget(max_output_bytes=4 * 1024**2))


def samples(path):
    with wave.open(str(path), 'rb') as stream:
        values = array('h')
        values.frombytes(stream.readframes(stream.getnframes()))
        result = stream.getframerate(), stream.getnchannels(), stream.getnframes()
    if sys.byteorder != 'little':
        values.byteswap()
    return result, values


def frequency(values, rate):
    # Count positive crossings away from encoder startup/tail. This measures
    # the actual generated PCM, rather than asserting a filter argument.
    middle = values[len(values)//4:3*len(values)//4]
    crossings = sum(first <= 0 < second for first, second in zip(middle, middle[1:]))
    return crossings * rate / (len(middle) - 1)


def test_processed_copy_tempo_and_pitch_have_separate_measured_meanings(selected):
    e = selected
    original = e.source.read_bytes()
    tempo = convert_audio(review(e, 'faster.wav', effects=(Effect('tempo', factor=2),)))
    pitched = convert_audio(review(e, 'higher.wav', effects=(Effect('pitch', semitones=12),)))
    (rate, channels, frames), values = samples(tempo.audio.path)
    assert (rate, channels, frames) == (24000, 1, 24000)
    assert 425 < frequency(values, rate) < 455, 'Tempo changed the measured source pitch.'
    (rate, channels, frames), values = samples(pitched.audio.path)
    assert (rate, channels, frames) == (24000, 1, 48000)
    assert 850 < frequency(values, rate) < 910, 'Pitch did not change by one measured octave.'
    for result in (tempo, pitched):
        assert result.source_sha256 == sha256(original).hexdigest()
        assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
        assert result.cleanup_warning is None and result.durability_confirmed
    assert e.source.read_bytes() == original and e.broker.snapshot()['leases'] == []
    assert not list(e.root.glob('.audio-convert-*'))


@pytest.mark.parametrize('destination', ['existing', 'private', 'original'])
def test_unapproved_destination_is_refused_before_codec_work(selected, monkeypatch, destination):
    e = selected
    path = e.root / 'already.wav'
    path.write_bytes(b'Existing independently owned bytes.')
    if destination == 'private':
        path = e.library / 'state' / 'unapproved.wav'
    elif destination == 'original':
        path = e.source
    before = e.source.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('Refused export admitted a codec.')
    monkeypatch.setattr(conversion, 'audio_runtime', forbidden)
    with pytest.raises((ValueError, FileExistsError)):
        review_conversion(e.library, e.source, path)
    assert e.source.read_bytes() == before
    assert (e.root / 'already.wav').read_bytes() == b'Existing independently owned bytes.'
    assert not (e.library / 'state/unapproved.wav').exists()


def test_exact_review_refuses_source_replacement_before_queued_work(selected, monkeypatch):
    e = selected
    request = review(e)
    prior = e.source.with_suffix('.prior')
    e.source.rename(prior)
    e.source.write_bytes(prior.read_bytes())
    monkeypatch.setattr(conversion, 'audio_runtime',
                        lambda *args, **kwargs: pytest.fail('Queued replacement admitted a codec.'))
    with pytest.raises(ValueError, match='source or library changed'):
        convert_audio(request)
    assert not request.destination.target.exists()


def test_source_byte_mutation_is_detected_even_when_observed_metadata_is_unchanged(selected, monkeypatch):
    e = selected
    request = review(e)
    original = AudioRuntime.convert
    # Model the same-size rapid-overwrite metadata ambiguity explicitly.
    monkeypatch.setattr(conversion, '_stamp', lambda info: request.source_identity)
    def changed(runtime, source, destination, **options):
        result = original(runtime, source, destination, **options)
        with e.source.open('r+b') as stream:
            stream.seek(80)
            stream.write(b'changed-sample')
        return result
    monkeypatch.setattr(AudioRuntime, 'convert', changed)
    with pytest.raises(ValueError, match='source changed'):
        convert_audio(request)
    assert not request.destination.target.exists()
    assert e.broker.snapshot()['leases'] == []
    assert not list(e.root.glob('.audio-convert-*'))


def test_same_byte_competing_create_is_never_reconciled_as_our_success(selected, monkeypatch):
    from carr_library.audio import process
    e = selected
    request = review(e)
    real_link = process.os.link
    competed = []
    def race(source, destination, *args, **kwargs):
        if Path(destination) == request.destination.target:
            raw = Path(source).read_bytes()
            Path(destination).write_bytes(raw)
            competed.append(raw)
        return real_link(source, destination, *args, **kwargs)
    monkeypatch.setattr(process.os, 'link', race)
    with pytest.raises(FileExistsError):
        convert_audio(request)
    assert competed and request.destination.target.read_bytes() == competed[0]
    assert e.broker.snapshot()['leases'] == []


def test_committed_copy_remains_completed_after_late_cancellation(selected, monkeypatch):
    from carr_library.audio import process
    e = selected
    request = review(e)
    cancelled = Event()
    real_link = process.os.link
    def committed(source, destination, *args, **kwargs):
        result = real_link(source, destination, *args, **kwargs)
        if Path(destination) == request.destination.target:
            cancelled.set()
        return result
    monkeypatch.setattr(process.os, 'link', committed)
    result = convert_audio(request, cancelled=cancelled.is_set)
    assert cancelled.is_set() and result.audio.path == request.destination.target
    assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
    assert e.broker.snapshot()['leases'] == []


def test_postcommit_directory_flush_failure_reports_saved_but_unconfirmed_durability(selected, monkeypatch):
    from carr_library.audio import process
    e = selected
    request = review(e)
    original = process.fsync_directory
    def failed(path):
        if Path(path) == request.destination.target.parent:
            raise OSError('Synthetic directory flush refusal after the real link.')
        return original(path)
    monkeypatch.setattr(process, 'fsync_directory', failed)
    result = convert_audio(request)
    assert result.audio.path.is_file() and not result.durability_confirmed
    assert 'saved' in result.cleanup_warning and 'durability' in result.cleanup_warning
    assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
    assert result.audio.path.stat().st_nlink == 1


def test_postcommit_staging_cleanup_retains_owner_without_reclassifying_output(selected, monkeypatch):
    e = selected
    request = review(e)
    real_temporary = conversion.TemporaryDirectory
    release = Event()
    held = []
    class PendingTemporary:
        def __init__(self, *args, **kwargs):
            self.owner = real_temporary(*args, **kwargs)
            self.name = self.owner.name
            held.append(Path(self.name))
        def cleanup(self):
            if not release.is_set():
                raise OSError('Synthetic retained staging handle')
            return self.owner.cleanup()
    monkeypatch.setattr(conversion, 'TemporaryDirectory', PendingTemporary)
    result = None
    try:
        result = convert_audio(request)
        assert result.audio.path.is_file() and result.cleanup_owner is not None
        assert 'saved' in result.cleanup_warning and 'cleanup' in result.cleanup_warning
        assert held[0].is_dir() and not result.cleanup_owner.quiescent
        assert result.audio.sha256 == sha256(result.audio.path.read_bytes()).hexdigest()
    finally:
        release.set()
        if result is not None and result.cleanup_owner is not None:
            result.cleanup_owner.close()
            assert result.cleanup_owner.quiescent_event.wait(10)
    assert not held[0].exists()
    assert result.audio.path.stat().st_nlink == 1


def test_explicit_lower_cpu_policy_does_not_silently_retune_the_selected_runtime(selected):
    e = selected
    e.broker.configure(ResourceLimits(cpu_slots=1, min_free_disk_bytes=0))
    request = review(e)
    with pytest.raises(ResourceUnavailable):
        convert_audio(request)
    assert e.broker.limits.cpu_slots == 1 and e.broker.snapshot()['leases'] == []
    assert not request.destination.target.exists()


def test_cancellation_during_real_shared_admission_starts_no_copy_or_decoder(selected, monkeypatch):
    e = selected
    request = review(e)
    entered, cancelled = Event(), Event()
    errors = []
    original = ResourceBroker.reserve
    @contextmanager
    def observed(self, value, **options):
        if options['operation_id'].startswith('audio-convert:'):
            entered.set()
        with original(self, value, **options) as lease:
            yield lease
    monkeypatch.setattr(ResourceBroker, 'reserve', observed)
    monkeypatch.setattr(conversion, '_copy_source',
                        lambda *args: pytest.fail('Queued conversion copied source bytes before admission.'))
    def work():
        try:
            convert_audio(request, cancelled=cancelled.is_set)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    try:
        with e.broker.reserve(ResourceRequest(cpu_slots=2), operation_id='conversion-blocker', timeout=0):
            worker.start()
            assert entered.wait(10)
            cancelled.set()
            worker.join(10)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], AudioCancelled)
        assert e.broker.snapshot()['leases'] == []
        assert not request.destination.target.exists()
        assert not list(e.root.glob('.audio-convert-*'))
    finally:
        cancelled.set()
        if worker.ident is not None:
            worker.join(10)


def test_actual_blocked_capability_probe_is_cancelled_and_never_cached(tmp_path, monkeypatch):
    from carr_library.audio import dsp, process
    ready, release = tmp_path / 'probe-ready', tmp_path / 'probe-release'
    cancelled, errors, processes, calls = Event(), [], [], []
    original_run = process.run_process
    if os.name == 'nt':
        from carr_library.audio import windows_job
        factory = windows_job.JobProcess
        def capture(*args, **kwargs):
            value = factory(*args, **kwargs)
            processes.append(value)
            return value
        monkeypatch.setattr(windows_job, 'JobProcess', capture)
    else:
        factory = process.subprocess.Popen
        def capture(*args, **kwargs):
            value = factory(*args, **kwargs)
            processes.append(value)
            return value
        monkeypatch.setattr(process.subprocess, 'Popen', capture)
    code = ("import sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_bytes(b'ready'); release=Path(sys.argv[2]); "
            "exec('while not release.exists():\\n time.sleep(.01)')")
    def blocked(args, **options):
        calls.append(tuple(args))
        return original_run([sys.executable, '-I', '-B', '-c', code, str(ready), str(release)], **options)
    monkeypatch.setattr(dsp, 'run_process', blocked)
    runtime = AudioRuntime(tmp_path, ffmpeg=sys.executable, ffprobe=sys.executable)
    def work():
        try:
            runtime.capabilities(cancelled=cancelled.is_set)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.is_file() and len(processes) == 1
        cancelled.set()
        worker.join(10)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], AudioCancelled)
        assert processes[0].returncode is not None and len(calls) == 1
        assert runtime._capabilities is None
    finally:
        cancelled.set()
        release.write_bytes(b'release')
        worker.join(20)


def test_decoder_cleanup_holds_staging_and_native_admission_until_actual_child_death(selected, monkeypatch):
    from carr_library.audio.cleanup import CleanupOwner
    e = selected
    request = review(e)
    ready, release = e.root / 'cleanup-ready', e.root / 'cleanup-release'
    code = ("import sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_bytes(b'ready'); release=Path(sys.argv[2]); "
            "exec('while not release.exists():\\n time.sleep(.01)')")
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(ready), str(release)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    owner = CleanupOwner(lambda: child.poll() is not None)
    pending = AudioCleanupPending('Controlled actual decoder cleanup', cleanup_owner=owner)
    entered, errors, roots = Event(), [], []
    def interrupted(runtime, source, destination, **options):
        assert ready.is_file() and child.poll() is None
        Path(destination).write_bytes(b'Controlled incomplete codec output.')
        roots.append(Path(destination).parent)
        entered.set()
        raise pending
    monkeypatch.setattr(AudioRuntime, 'convert', interrupted)
    def work():
        try:
            convert_audio(request)
        except BaseException as error:
            errors.append(error)
    worker = Thread(target=work)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.is_file() and child.poll() is None
        worker.start()
        assert entered.wait(15) and worker.is_alive()
        assert roots[0].is_dir() and not owner.close()
        with pytest.raises(ResourceTimeout):
            with e.broker.reserve(ResourceRequest(cpu_slots=2), operation_id='pending-codec-competitor', timeout=0):
                pytest.fail('Unreaped decoder released its shared admission.')
        release.write_bytes(b'release')
        worker.join(15)
        assert not worker.is_alive() and errors == [pending]
        assert owner.quiescent and child.poll() == 0
        assert not roots[0].exists() and not request.destination.target.exists()
        assert e.broker.snapshot()['leases'] == []
    finally:
        release.write_bytes(b'release')
        child.wait(timeout=10)
        owner.close()
        if worker.ident is not None:
            worker.join(15)


def test_low_rate_high_bitrate_conversion_must_admit_its_real_encoded_output_cap(selected, monkeypatch):
    e = selected
    request = review(e, 'encoded.m4a', sample_rate=8000, bitrate=512)
    # Actual FFprobe/capability inspection is allowed. The policy admits the
    # old raw-PCM-only declaration but cannot fit this reviewed encoded cap.
    logs = 2 * request.budget.max_log_bytes + request.budget.reserve_disk_bytes
    old_pcm_output = 2 * 8000 * 2 + 65536
    e.broker.configure(ResourceLimits(cpu_slots=2, min_free_disk_bytes=0,
        disk_bytes=e.source.stat().st_size + logs + old_pcm_output + 1024))
    monkeypatch.setattr(AudioRuntime, 'convert',
        lambda *args, **kwargs: pytest.fail('Encoded output exceeded its admitted disk allowance.'))
    with pytest.raises(ResourceUnavailable):
        convert_audio(request)
    assert not request.destination.target.exists()
    assert e.broker.snapshot()['leases'] == [] and not list(e.root.glob('.audio-convert-*'))
