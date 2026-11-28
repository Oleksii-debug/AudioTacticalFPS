"""Current-API regressions for reviewed canonical process and signal assertions.

Real local Python children exercise the existing owned launcher. Signal cases
require the selected local FFmpeg; no alternate process or DSP implementation
is introduced and no synthesized fixture stands in for an FFmpeg measurement.
"""
from array import array
from hashlib import sha256
import math
import os
from pathlib import Path
import sys
import threading
import time
import wave

import pytest

from carr_library.audio import recording_process as process
from carr_library.audio.ffmpeg import measure_loudness, measure_peak, process_voice, silence, wav_info
from carr_library.audio.models import Effect
from carr_library.audio.runtime import resolve_ffmpeg


def command(code, *arguments):
    return [sys.executable, '-I', '-B', '-c', code, *(str(item) for item in arguments)]


def observed_children(monkeypatch, *, finished=False):
    children = []
    original = process._start_process
    def start(args, **options):
        child = original(args, **options)
        children.append(child)
        if finished:
            # Each diagnostic fixture is below the native pipe capacity, so
            # actual exit precedes the runner's first poll without a deadlock.
            try:
                assert child.wait(timeout=10) == 0
            except BaseException:
                process._stop_owned(child)
                raise
        return child
    monkeypatch.setattr(process, '_start_process', start)
    return children


def assert_settled(children):
    assert len(children) == 1
    child = children[0]
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    if child.stdin is not None:
        assert child.stdin.closed
    if os.name == 'nt':
        assert child._carr_job.handle is None


@pytest.mark.parametrize('descriptor', [1, 2])
def test_fast_owned_diagnostic_overflow_is_rejected_after_actual_exit(monkeypatch, descriptor):
    children = observed_children(monkeypatch, finished=True)
    with pytest.raises(process.AudioError, match='diagnostic output budget'):
        process.run_process(command('import os;os.write(' + str(descriptor) + ',b"x"*3072)'),
            budget=process.CaptureBudget(timeout=10, max_log_bytes=1024))
    assert children[0].returncode == 0
    assert_settled(children)


def test_owned_process_timeout_terminates_actual_child_promptly(monkeypatch):
    children = observed_children(monkeypatch)
    started = time.monotonic()
    with pytest.raises(process.AudioError, match='time allowance'):
        process.run_process(command('import time;time.sleep(30)'),
                            budget=process.CaptureBudget(timeout=.1))
    assert time.monotonic() - started < 5
    assert_settled(children)


def test_prestart_cancel_prevents_child_side_effects(tmp_path, monkeypatch):
    marker = tmp_path / 'must-not-exist'
    monkeypatch.setattr(process, '_start_process',
                        lambda *args, **kwargs: pytest.fail('Pre-start cancellation cannot launch.'))
    with pytest.raises(process.AudioCancelled):
        process.run_process(command('import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(b"bad")', marker),
                            cancelled=lambda: True)
    assert not marker.exists()


@pytest.mark.parametrize('shape', ['one-file', 'combined-files', 'entry-count'])
def test_completed_worker_staging_is_checked_after_actual_exit(tmp_path, monkeypatch, shape):
    stage = tmp_path / 'stage'
    stage.mkdir()
    children = observed_children(monkeypatch, finished=True)
    if shape == 'one-file':
        code, count, size = '[(root/"large").write_bytes(b"x"*200000)]', 1, 200000
    elif shape == 'combined-files':
        code, count, size = '[(root/str(i)).write_bytes(b"x"*3000) for i in range(2)]', 2, 6000
    else:
        code, count, size = '[(root/str(i)).write_bytes(b"x") for i in range(5)]', 5, 5
    expected = 'staging file budget' if shape == 'entry-count' else 'output disk budget'
    with pytest.raises(process.AudioError, match=expected):
        process.run_process(command('import pathlib,sys;root=pathlib.Path(sys.argv[1]);' + code, stage),
            watch=(stage,), budget=process.CaptureBudget(timeout=10, max_output_bytes=4096,
                max_staging_files=2 if shape == 'entry-count' else 10, reserve_disk_bytes=0))
    assert len(tuple(stage.iterdir())) == count
    assert sum(path.stat().st_size for path in stage.iterdir()) == size
    assert_settled(children)


def test_nonzero_owned_child_is_not_reported_as_success(monkeypatch):
    children = observed_children(monkeypatch)
    with pytest.raises(process.AudioError, match='exit 7'):
        process.run_process(command('raise SystemExit(7)'), budget=process.CaptureBudget(timeout=10))
    assert children[0].returncode == 7
    assert_settled(children)


def test_progress_reaches_caller_while_actual_child_is_still_running(tmp_path, monkeypatch):
    children = observed_children(monkeypatch)
    marker, received, caller = tmp_path / 'progress-observed', [], threading.get_ident()
    code = (
        'import pathlib,sys,time\n'
        'marker=pathlib.Path(sys.argv[1])\n'
        'sys.stdout.write("out_time_us=500000\\n");sys.stdout.flush()\n'
        'deadline=time.monotonic()+5\n'
        'while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)\n'
        'raise SystemExit(0 if marker.exists() else 9)\n'
    )
    def progress(seconds):
        assert threading.get_ident() == caller
        assert children[0].poll() is None
        received.append(seconds)
        marker.write_bytes(b'Observed before child exit.')
    result = process.run_process(command(code, marker), progress=progress,
                                 budget=process.CaptureBudget(timeout=10))
    assert received == [.5] and result.returncode == 0
    assert result.stdout == b'' and marker.is_file()
    assert_settled(children)


@pytest.mark.parametrize('args', ['python -c pass', ['python', '-c', 'pass']])
def test_argument_admission_refuses_shell_text_and_unresolved_executable(monkeypatch, args):
    monkeypatch.setattr(process, '_start_process',
                        lambda *a, **k: pytest.fail('Unresolved arguments cannot launch.'))
    with pytest.raises(ValueError):
        process.run_process(args)


@pytest.fixture
def ffmpeg():
    selected = resolve_ffmpeg()
    assert selected is not None, 'These exact signal regressions require the selected local FFmpeg.'
    return selected


def write_pcm(path, values, rate=24000):
    data = array('h', (round(value * 32767) for value in values))
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, rate, len(data), 'NONE', 'PCM'))
        stream.writeframes(data.tobytes())
    return path


def samples(path):
    with wave.open(str(path), 'rb') as stream:
        data = array('h', stream.readframes(stream.getnframes()))
        channels, rate = stream.getnchannels(), stream.getframerate()
    if sys.byteorder != 'little':
        data.byteswap()
    return [value / 32768 for value in data[::channels]], rate


def amplitude(values, rate, frequency):
    real = sum(value * math.cos(2 * math.pi * frequency * i / rate)
               for i, value in enumerate(values))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / rate)
               for i, value in enumerate(values))
    return 2 * math.hypot(real, imag) / len(values)


def test_actual_equalizer_preserves_canonical_selective_gain_tolerances(tmp_path, ffmpeg):
    rate = 24000
    source = write_pcm(tmp_path / 'two-tones.wav',
        (.08 * math.sin(2 * math.pi * 1000 * i / rate)
         + .08 * math.sin(2 * math.pi * 4000 * i / rate) for i in range(2 * rate)))
    original = sha256(source.read_bytes()).hexdigest()
    dry = process_voice(source, tmp_path / 'dry.wav', ffmpeg=ffmpeg, sample_rate=rate)
    changed = process_voice(source, tmp_path / 'equalized.wav', ffmpeg=ffmpeg, sample_rate=rate,
        effects=(Effect('equalizer', (('frequency', 1000), ('gain_db', 6), ('q', 3))),))
    before, before_rate = samples(dry)
    after, after_rate = samples(changed)
    assert before_rate == after_rate == rate
    center = 20 * math.log10(amplitude(after[rate:], rate, 1000)
                            / amplitude(before[rate:], rate, 1000))
    other = 20 * math.log10(amplitude(after[rate:], rate, 4000)
                           / amplitude(before[rate:], rate, 4000))
    assert 5.8 < center < 6.2 and abs(other) < .2
    assert wav_info(dry).frames == wav_info(changed).frames == 2 * rate
    assert sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.parametrize('kind,parameters,offset', [
    ('delay', (('delay_ms', 200), ('mix', .5), ('decay', .5)), 4800),
    ('reverb', (('room_ms', 50), ('mix', .5), ('decay', .5)), 1200),
])
def test_actual_reflection_impulse_positions_keep_declared_millisecond_units(
        tmp_path, ffmpeg, kind, parameters, offset):
    rate = 24000
    values = [0.] * rate
    values[100] = .6
    source = write_pcm(tmp_path / 'impulse.wav', values)
    original = sha256(source.read_bytes()).hexdigest()
    output = process_voice(source, tmp_path / 'reflected.wav', ffmpeg=ffmpeg, sample_rate=rate,
                           effects=(Effect(kind, parameters),))
    after, actual_rate = samples(output)
    assert actual_rate == rate and abs(after[100]) > .1
    assert abs(after[100 + offset]) > .03
    quiet = after[200:offset]
    assert math.sqrt(sum(value * value for value in quiet) / len(quiet)) < .0001
    assert sha256(source.read_bytes()).hexdigest() == original


def test_actual_silence_has_no_fabricated_finite_loudness_or_peak(tmp_path, ffmpeg):
    source = silence(tmp_path / 'silence.wav', 24000, sample_rate=24000)
    original = sha256(source.read_bytes()).hexdigest()
    measured = measure_loudness(source, ffmpeg=ffmpeg)
    assert measured['integrated_lufs'] is None and measured['true_peak_dbfs'] is None
    assert measure_peak(source)['peak'] == 0 and wav_info(source).frames == 24000
    assert sha256(source.read_bytes()).hexdigest() == original
