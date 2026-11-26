"""Real FFmpeg signal and file-integrity regressions, not mocked audio."""
import array
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import threading
import time
import wave

import pytest

from carr_library.audio.effects import Effect
from carr_library.audio.render import (Chapter, RenderCancelled, Track, analyze_audio,
    concatenate_audio, convert_audio, estimate_pcm_bytes, file_sha256, mix_tracks,
    probe_audio, run_process, runtime_identity)


RATE = 24000


def tone(path, frequency, seconds, gain=.4, rate=RATE):
    samples = array.array('h', (round(32767 * gain * math.sin(2 * math.pi * frequency * i / rate))
                                for i in range(round(seconds * rate))))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples.tobytes())
    return path


def read_pcm(path):
    with wave.open(str(path), 'rb') as audio:
        channels, rate, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
        values = array.array('h')
        values.frombytes(audio.readframes(frames))
    if sys.byteorder != 'little':
        values.byteswap()
    return [values[i::channels] for i in range(channels)], rate


def amplitude(values, frequency, start, end, rate=RATE):
    values = values[round(start*rate):round(end*rate)]
    real = math.fsum(x*math.cos(2*math.pi*frequency*i/rate) for i, x in enumerate(values))
    imaginary = math.fsum(x*math.sin(2*math.pi*frequency*i/rate) for i, x in enumerate(values))
    return 2 * math.hypot(real, imaginary) / len(values) / 32767


@pytest.fixture(autouse=True)
def real_runtime_required():
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'This suite requires a real reviewed FFmpeg runtime.'


def test_actual_pan_silence_ducking_and_source_invariance(tmp_path):
    left = tone(tmp_path/'left.wav', 440, 1, .45)
    right = tone(tmp_path/'right.wav', 660, 1, .45)
    center = tone(tmp_path/'center.wav', 880, .5, .35)
    music = tone(tmp_path/'music.wav', 220, 4, .15)
    before = {path: file_sha256(path) for path in (left, right, center, music)}
    result = mix_tracks([Track(left, 1, pan=-1), Track(right, 2, pan=1),
                         Track(center, 3), Track(music, kind='music')],
                        tmp_path/'production.wav', duration_seconds=4)
    (l, r), rate = read_pcm(result.path)
    assert result.frames == 4 * RATE and rate == RATE and result.channels == 2
    assert amplitude(l, 440, 1.2, 1.8) > .35
    assert amplitude(r, 440, 1.2, 1.8) < .005
    assert amplitude(r, 660, 2.2, 2.8) > .35
    assert amplitude(l, 660, 2.2, 2.8) < .005
    assert abs(amplitude(l, 880, 3.1, 3.4) - amplitude(r, 880, 3.1, 3.4)) < .005
    assert amplitude(l, 440, .2, .8) < .005
    before_voice = amplitude(l, 220, .2, .8)
    under_voice = amplitude(l, 220, 1.3, 1.7)
    assert before_voice > .08 and under_voice < before_voice * .5
    assert max(max(map(abs, l)), max(map(abs, r))) < 32767
    assert result.peak_dbfs < 0
    assert before == {path: file_sha256(path) for path in before}
    assert result.sha256 == file_sha256(result.path)


def test_stereo_voice_lane_remains_left_and_right(tmp_path):
    source = tone(tmp_path/'voice.wav', 440, 1)
    lane = mix_tracks([Track(source, pan=-1)], tmp_path/'lane.wav')
    result = mix_tracks([Track(lane.path, preserve_stereo=True)], tmp_path/'master.wav')
    (left, right), _ = read_pcm(result.path)
    assert amplitude(left, 440, .1, .9) > .35
    assert amplitude(right, 440, .1, .9) < .001
    with pytest.raises(ValueError, match='Stereo'):
        mix_tracks([Track(lane.path, preserve_stereo='yes')], tmp_path/'bad.wav')


def test_empty_track_stem_is_exact_silence(tmp_path):
    result = mix_tracks([], tmp_path/'silence.wav', duration_seconds=3.125)
    channels, rate = read_pcm(result.path)
    assert result.frames == round(3.125 * rate)
    assert all(not any(channel) for channel in channels)


def test_output_and_input_files_are_not_overwritten(tmp_path):
    source = tone(tmp_path/'source.wav', 220, 1)
    before = source.read_bytes()
    with pytest.raises(FileExistsError):
        mix_tracks([Track(source)], source)
    assert source.read_bytes() == before
    target = tmp_path/'existing.wav'
    target.write_bytes(b'private keep')
    with pytest.raises(FileExistsError):
        convert_audio(source, target)
    assert target.read_bytes() == b'private keep'


def test_generated_audio_publication_sync_accepts_windows_file_handles(tmp_path, monkeypatch):
    source = tone(tmp_path/'source.wav', 440, .25)
    original = source.read_bytes()
    sync = os.fsync
    synced_files = []

    def writable_file_sync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            # Windows fsync rejects read-only file descriptors. A zero-byte
            # write enforces that same contract on POSIX without changing data.
            os.write(descriptor, b'')
            synced_files.append(descriptor)
        return sync(descriptor)

    monkeypatch.setattr(os, 'fsync', writable_file_sync)
    result = convert_audio(source, tmp_path/'published.wav', channels=1)
    (samples,), rate = read_pcm(result.path)
    assert synced_files
    assert result.frames == round(.25 * RATE) and rate == RATE
    assert any(samples) and result.sha256 == file_sha256(result.path)
    assert source.read_bytes() == original


@pytest.mark.parametrize('option', [dict(pan=math.nan), dict(gain_db=math.inf), dict(start_seconds=-1),
                                    dict(kind='http'), dict(fade_in=-1)])
def test_invalid_track_has_no_output(tmp_path, option):
    source = tone(tmp_path/'source.wav', 220, 1)
    with pytest.raises(ValueError):
        mix_tracks([Track(source, **option)], tmp_path/'invalid.wav')
    assert not (tmp_path/'invalid.wav').exists()


def test_speech_cannot_be_silently_truncated(tmp_path):
    source = tone(tmp_path/'source.wav', 220, 1)
    with pytest.raises(ValueError, match='truncate'):
        mix_tracks([Track(source, start_seconds=.5)], tmp_path/'short.wav', duration_seconds=1)
    assert not (tmp_path/'short.wav').exists()


def test_plain_unicode_filename_is_data_not_shell(tmp_path):
    source = tone(tmp_path/'Оповідач; $(touch NO).wav', 220, .2)
    result = mix_tracks([Track(source)], tmp_path/'výsledok; & final.wav')
    assert result.path.is_file()
    assert not (tmp_path/'NO').exists()
    with pytest.raises(ValueError, match='local'):
        probe_audio('https://example.invalid/track.wav')
    with pytest.raises(ValueError, match='local'):
        probe_audio('\\\\server\\share\\audio.wav')


def test_output_budget_prevents_render(tmp_path):
    with pytest.raises(ValueError, match='budget'):
        mix_tracks([], tmp_path/'big.wav', duration_seconds=10, max_output_bytes=1)
    assert not (tmp_path/'big.wav').exists()
    assert estimate_pcm_bytes(36000, sample_rate=48000, channels=2) > 4 * 1024**3


def test_cancellation_and_timeout_stop_real_worker(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(RenderCancelled):
        mix_tracks([], tmp_path/'cancel.wav', duration_seconds=1, cancelled=cancelled)
    assert not (tmp_path/'cancel.wav').exists()
    start = time.monotonic()
    with pytest.raises(RuntimeError, match='time budget'):
        run_process([sys.executable, '-I', '-c', 'import time; time.sleep(20)'], timeout_seconds=.1)
    assert time.monotonic() - start < 5
    cancelled.clear()
    timer = threading.Timer(.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(RenderCancelled):
            run_process([sys.executable, '-I', '-c', 'import time; time.sleep(20)'], cancelled=cancelled)
    finally:
        timer.cancel()


def test_worker_output_is_bounded(tmp_path):
    with pytest.raises(RuntimeError, match='diagnostic'):
        run_process([sys.executable, '-I', '-c', 'print("x"*100000)'], maximum_log_bytes=1024)


def test_trim_convert_and_lossless_concatenation(tmp_path):
    source = tone(tmp_path/'source.wav', 440, 2)
    trimmed = convert_audio(source, tmp_path/'trim.flac', channels=1,
                            start_seconds=.5, duration_seconds=.75)
    assert trimmed.frames == round(.75 * RATE)
    assembled = concatenate_audio([trimmed.path, trimmed.path], tmp_path/'assembled.wav')
    assert assembled.frames == round(1.5 * RATE)
    assert assembled.metadata['repeat'] == 1
    assert assembled.metadata['original_neural_generation'] is False
    (samples,), _ = read_pcm(assembled.path)
    assert amplitude(samples, 440, .1, .6) > .25
    assert amplitude(samples, 440, .85, 1.35) > .25


def test_concat_rejects_mismatched_chunks(tmp_path):
    first = tone(tmp_path/'a.wav', 440, .1, rate=24000)
    second = tone(tmp_path/'b.wav', 440, .1, rate=16000)
    with pytest.raises(ValueError, match='geometry'):
        concatenate_audio([first, second], tmp_path/'bad.wav')


def test_actual_eq_gate_compression_and_delay(tmp_path):
    source = tone(tmp_path/'source.wav', 440, 1, .1)
    filtered = convert_audio(source, tmp_path/'filtered.wav', channels=1,
                             effects=(Effect('equalizer', {'frequency_hz': 440, 'gain_db': -12, 'q': 1}),))
    (values,), _ = read_pcm(filtered.path)
    assert amplitude(values, 440, .2, .8) < .03
    gated = convert_audio(source, tmp_path/'gate.wav', channels=1,
                          effects=(Effect('gate', {'threshold_db': -12, 'range_db': -60, 'ratio': 10}),))
    (values,), _ = read_pcm(gated.path)
    assert amplitude(values, 440, .2, .8) < .01
    loud = tone(tmp_path/'loud.wav', 220, 1, .8)
    compressed = convert_audio(loud, tmp_path/'compressed.wav', channels=1,
        effects=(Effect('compressor', {'threshold_db': -20, 'ratio': 10}),))
    assert compressed.peak_dbfs < -8
    impulse = tmp_path/'impulse.wav'
    data = array.array('h', [0] * RATE)
    data[2400] = 12000
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(impulse), 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(data.tobytes())
    delayed = convert_audio(impulse, tmp_path/'delay.wav', channels=1,
        effects=(Effect('delay', {'delay_ms': 200, 'wet': .4, 'feedback': .5}),))
    (values,), _ = read_pcm(delayed.path)
    assert values[2400] > 10000
    assert 4000 < values[7200] < 6000
    assert len(values) > RATE


def test_measured_loudness_and_chaptered_export(tmp_path):
    source = tone(tmp_path/'source.wav', 440, 4, .2)
    measured = analyze_audio(source, measure_loudness=True)
    assert measured['integrated_lufs'] is not None
    assert measured['true_peak_dbfs'] < 0
    assert measured['peak_dbfs'] < -10
    output = convert_audio(source, tmp_path/'chaptered.m4b', chapters=(
        Chapter('Перший; = розділ', 0, 2), Chapter('Druhý', 2, 4)))
    assert output.path.is_file() and output.metadata['chapter_count'] == 2
    import json
    out, _ = run_process([shutil.which('ffprobe'), '-v', 'error', '-show_chapters', '-of', 'json', str(output.path)])
    chapters = json.loads(out)['chapters']
    assert len(chapters) == 2
    assert chapters[0]['tags']['title'] == 'Перший; = розділ'


def test_runtime_identity_is_real_and_json_safe():
    import json
    identity = runtime_identity()
    assert len(identity['ffmpeg']['executable_sha256']) == 64
    assert 'ffmpeg version' in identity['ffmpeg']['version_and_build']
    assert len(identity['render_sha256']) == len(identity['effects_sha256']) == 64
    assert json.loads(json.dumps(identity)) == identity
