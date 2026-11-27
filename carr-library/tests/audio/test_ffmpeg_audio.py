"""Actual local FFmpeg audio tests; missing runtimes fail this required suite."""
from array import array
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import struct
import subprocess
import sys
import wave

import pytest
import carr_library

from carr_library.audio import AudioCancelled, Effect, FFmpegAudio, Track
from carr_library.audio.ffmpeg import _local_path, _pcm_header


RATE = 24000


def test_windows_absolute_binary_lookup_uses_the_exe_suffix_on_python_311(tmp_path, monkeypatch):
    import carr_library.audio.ffmpeg as module
    selected = tmp_path / 'approved-runtime'
    selected.mkdir()
    paths = []
    for name in ('ffmpeg', 'ffprobe'):
        path = selected / (name + '.exe')
        path.write_bytes(b'explicitly synthetic executable identity; never run')
        path.chmod(0o700)
        paths.append(path)
    monkeypatch.setattr(module, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(module, '_runtime_directories', lambda: (selected,))
    # The native drive/reparse checks have their own tests. This regression
    # isolates the real shutil.which behavior with an absolute file name.
    monkeypatch.setattr(module, '_local_path', Path)
    assert [Path(module.FFmpegAudio._binary(None, name)) for name in ('ffmpeg', 'ffprobe')] == paths


def tone(path, frequency=440, seconds=.4, gain=.3, rate=RATE):
    data = array('h', (round(32767 * gain * math.sin(2 * math.pi * frequency * i / rate))
                       for i in range(round(seconds * rate))))
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        out.writeframes(data.tobytes())
    return path


def channels(path):
    with wave.open(str(path), 'rb') as audio:
        count = audio.getnchannels()
        data = array('h')
        data.frombytes(audio.readframes(audio.getnframes()))
    if sys.byteorder != 'little':
        data.byteswap()
    return tuple(data[i::count] for i in range(count))


def amplitude(signal, frequency, start, end, rate=RATE):
    samples = signal[round(start * rate):round(end * rate)]
    c = math.fsum(x * math.cos(2 * math.pi * frequency * i / rate) for i, x in enumerate(samples))
    s = math.fsum(x * math.sin(2 * math.pi * frequency * i / rate) for i, x in enumerate(samples))
    return 2 * math.hypot(c, s) / len(samples) / 32767


@pytest.fixture
def engine(tmp_path):
    return FFmpegAudio(allowed_roots=[tmp_path])


def test_real_pan_timing_duck_and_source_integrity(tmp_path, engine):
    left = tone(tmp_path / 'лівий голос.wav', 440, 1, .45)
    right = tone(tmp_path / 'pravý hlas.wav', 660, 1, .45)
    center = tone(tmp_path / "center's voice.wav", 880, .5, .35)
    music = tone(tmp_path / 'musical bed.wav', 220, 4, .15)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (left, right, center, music)}
    report = engine.render_timeline([
        Track(left, 1, pan=-1), Track(right, 2, routing='right'),
        Track(center, 3, routing='center'), Track(music, role='music'),
    ], tmp_path / 'сцена.wav', duration=4)
    l, r = channels(report.path)
    assert report.frames == RATE * 4
    assert report.duration_seconds == 4
    assert len(report.source_sha256) == 4
    assert 0 < report.peak < .951
    assert amplitude(l, 440, 1.2, 1.8) > .35
    assert amplitude(r, 440, 1.2, 1.8) < .005
    assert amplitude(r, 660, 2.2, 2.8) > .35
    assert amplitude(l, 660, 2.2, 2.8) < .005
    assert abs(amplitude(l, 880, 3.1, 3.4) - amplitude(r, 880, 3.1, 3.4)) < .005
    assert amplitude(l, 440, .2, .8) < .005
    assert amplitude(l, 220, 1.3, 1.7) < amplitude(l, 220, .2, .8) * .5
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert engine.inspect(report.path) == type(report)(report.path, report.frames, report.sample_rate,
        report.channels, report.duration_seconds, report.peak, report.sha256)


def test_silence_scene_exact_duration(tmp_path, engine):
    report = engine.render_timeline((), tmp_path / 'pause.wav', duration=.375)
    assert report.frames == 9000
    assert report.peak == 0
    assert report.source_sha256 == ()


def test_actual_hour_offset_has_precise_cue_and_bounded_stream(tmp_path):
    source = tone(tmp_path / 'cue.wav', 400, .2, rate=8000)
    target = tmp_path / 'one-hour.wav'
    # Isolate lifetime child-RSS accounting from other suites' subprocesses.
    script = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from carr_library.audio import FFmpegAudio, Track
source, target = map(Path, sys.argv[2:])
report = FFmpegAudio(allowed_roots=[target.parent]).render_timeline(
    [Track(source, start_seconds=3599.5)], target,
    duration=3600, sample_rate=8000, channels=1)
rss = None
if sys.platform.startswith('linux'):
    import resource
    rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
print(json.dumps({'frames': report.frames, 'rss': rss}))
'''
    package_root = Path(carr_library.__file__).resolve().parent.parent
    run = subprocess.run([sys.executable, '-I', '-c', script, str(package_root), str(source), str(target)],
                         capture_output=True, text=True, timeout=60, check=True)
    measured = json.loads(run.stdout)
    assert measured['frames'] == 28_800_000
    with wave.open(str(target), 'rb') as audio:
        audio.setpos(3599 * 8000)
        assert not any(audio.readframes(4000))
        assert any(audio.readframes(1600))
    if sys.platform.startswith('linux'):
        # A former resampler prefix allocated 229 MB for this same fixture.
        assert measured['rss'] < 100_000


def test_silence_requires_duration(tmp_path, engine):
    with pytest.raises(ValueError, match='duration'):
        engine.render_timeline((), tmp_path / 'invalid.wav')


def test_actual_join_normalizes_and_preserves_timing(tmp_path, engine):
    a = tone(tmp_path / 'a.wav', 300, .3, rate=16000)
    b = tone(tmp_path / 'b.wav', 600, .2, rate=48000)
    report = engine.concatenate_segments([a, b, a], tmp_path / 'joined.wav')
    assert report.frames == round(.8 * RATE)
    assert report.duration_seconds == .8
    left, _ = channels(report.path)
    assert amplitude(left, 300, .05, .25) > .2
    assert amplitude(left, 600, .33, .48) > .2
    assert amplitude(left, 300, .55, .75) > .2
    assert report.source_sha256[0] == report.source_sha256[2]


def test_no_overwrite_even_when_processing_would_succeed(tmp_path, engine):
    source = tone(tmp_path / 'source.wav')
    target = tmp_path / 'output.wav'
    target.write_bytes(b'keep existing user data')
    with pytest.raises(FileExistsError):
        engine.render_timeline([Track(source)], target)
    assert target.read_bytes() == b'keep existing user data'


def test_cancellation_before_and_during_render_does_not_publish(tmp_path, engine):
    with pytest.raises(AudioCancelled):
        engine.render_timeline((), tmp_path / 'cancel-before.wav', duration=2, cancelled=lambda: True)
    counter = [0]
    def cancelled():
        counter[0] += 1
        return counter[0] > 3
    with pytest.raises(AudioCancelled):
        engine.render_timeline((), tmp_path / 'cancel-during.wav', duration=60, cancelled=cancelled)
    assert not list(tmp_path.glob('*.wav'))
    assert not list(tmp_path.glob('.audio-*'))


def test_progress_is_measured(tmp_path, engine):
    values = []
    report = engine.render_timeline((), tmp_path / 'progress.wav', duration=.5, progress=values.append)
    assert values
    assert max(values) == pytest.approx(report.duration_seconds, abs=.05)


def test_scope_rejects_paths_outside_approved_roots(tmp_path, engine):
    outside = tmp_path.parent / ('outside-' + tmp_path.name + '.wav')
    tone(outside)
    try:
        with pytest.raises(ValueError, match='approved'):
            engine.render_timeline([Track(outside)], tmp_path / 'out.wav')
        with pytest.raises(ValueError, match='approved'):
            engine.render_timeline((), tmp_path.parent / 'outside-out.wav', duration=1)
    finally:
        outside.unlink()


def test_symlink_input_rejected(tmp_path, engine):
    original = tone(tmp_path / 'original.wav')
    link = tmp_path / 'link.wav'
    try:
        link.symlink_to(original)
    except OSError as exc:
        # Windows may lack Developer Mode. A failing symlink creation is itself
        # evidence that this account cannot create the malicious link fixture.
        assert sys.platform == 'win32'
        assert exc.winerror == 1314
        return
    with pytest.raises(ValueError, match='symbolic'):
        engine.render_timeline([Track(link)], tmp_path / 'out.wav')


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_invalid_timeline_values(value):
    with pytest.raises(ValueError):
        Track('unused.wav', start_seconds=value)


def test_arbitrary_filter_arguments_rejected():
    with pytest.raises(ValueError, match='Unsupported'):
        Effect('amovie=/etc/passwd')
    with pytest.raises(ValueError):
        Effect('highpass', {'frequency_hz': "1;amovie='https://example.com'"})
    with pytest.raises(ValueError, match='Unsupported'):
        Effect('highpass', {'unrecognized': 1})


@pytest.mark.parametrize('path', [r'\\server\share\secret.wav', '//server/share/secret.wav',
                                 r'/\server\share\secret.wav', r'\/server/share/secret.wav',
                                 r'\\?\UNC\server\share\secret.wav', r'\\.\PhysicalDrive0',
                                 'https://example.invalid/audio.wav', 'file:///private.wav', 'C:relative.wav'])
def test_network_device_and_url_paths_rejected_before_io(path, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail('The rejected path must not reach filesystem lookup.')
    monkeypatch.setattr(Path, 'lstat', no_io)
    monkeypatch.setattr(Path, 'resolve', no_io)
    with pytest.raises(ValueError):
        _local_path(path)


def test_implicit_current_directory_runtime_not_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', str(tmp_path))
    executable = tmp_path / ('ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg')
    executable.write_bytes(b'not a trusted runtime')
    executable.chmod(0o755)
    with pytest.raises(RuntimeError, match='not installed'):
        FFmpegAudio()


def test_format_whitelist_rejects_playlist_indirection(tmp_path, engine):
    path = tmp_path / 'playlist.wav'
    path.write_text('#EXTM3U\nhttps://example.invalid/private.mp3\n')
    with pytest.raises(ValueError, match='supported local'):
        engine.render_timeline([Track(path)], tmp_path / 'out.wav')
    assert not (tmp_path / 'out.wav').exists()


def test_configured_budget_is_checked_without_arbitrary_duration_ceiling(tmp_path):
    engine = FFmpegAudio(allowed_roots=[tmp_path], max_output_bytes=100000)
    with pytest.raises(ValueError, match='byte budget'):
        engine.render_timeline((), tmp_path / 'large.wav', duration=36000)


def test_actual_effect_equalizer_response(tmp_path, engine):
    source = tone(tmp_path / 'tone.wav', 1000, 1, .15)
    plain = engine.render_timeline([Track(source)], tmp_path / 'plain.wav')
    boosted = engine.render_timeline([Track(source, effects=(Effect('equalizer',
        {'frequency_hz': 1000, 'gain_db': 6, 'q': 1}),))], tmp_path / 'boost.wav')
    p = amplitude(channels(plain.path)[0], 1000, .2, .8)
    b = amplitude(channels(boosted.path)[0], 1000, .2, .8)
    assert b / p == pytest.approx(10 ** (6 / 20), rel=.03)


def test_crossfade_preserves_explicit_voice_channels(tmp_path, engine):
    first = tone(tmp_path / 'left.wav', 400, 1, .3)
    second = tone(tmp_path / 'right.wav', 600, 1, .3)
    result = engine.render_timeline([
        Track(first, routing='left', effects=(Effect('fade_out', {'start_seconds': .75, 'seconds': .25}),)),
        Track(second, start_seconds=.75, routing='right', effects=(Effect('fade_in', {'seconds': .25}),)),
    ], tmp_path / 'crossfade.wav')
    left, right = channels(result.path)
    isolated_left = engine.render_timeline([
        Track(first, routing='left', effects=(Effect('fade_out', {'start_seconds': .75, 'seconds': .25}),)),
    ], tmp_path / 'only-left.wav', duration=1.75)
    isolated_right = engine.render_timeline([
        Track(second, start_seconds=.75, routing='right', effects=(Effect('fade_in', {'seconds': .25}),)),
    ], tmp_path / 'only-right.wav', duration=1.75)
    control_left, zero_right = channels(isolated_left.path)
    zero_left, control_right = channels(isolated_right.path)
    assert result.duration_seconds == 1.75
    assert amplitude(left, 400, .8, .95) > .05
    assert amplitude(right, 600, .8, .95) > .05
    assert left == control_left
    assert right == control_right
    assert not any(zero_left)
    assert not any(zero_right)
    assert amplitude(left, 400, 1.2, 1.6) < .001
    assert amplitude(right, 600, 1.2, 1.6) > .25


def test_actual_gate_suppresses_quiet_signal(tmp_path, engine):
    source = tone(tmp_path / 'quiet.wav', 440, 1, .001)
    report = engine.render_timeline([Track(source, effects=(Effect('gate',
        {'threshold_db': -30, 'ratio': 10}),))], tmp_path / 'gated.wav')
    assert amplitude(channels(report.path)[0], 440, .2, .8) < .00005


def test_actual_echo_extends_timeline(tmp_path, engine):
    source = tone(tmp_path / 'dry.wav', 440, .2, .2)
    report = engine.render_timeline([Track(source, effects=(Effect('echo',
        {'delay_ms': 300, 'decay': .5}),))], tmp_path / 'echo.wav')
    assert report.duration_seconds == .5
    assert amplitude(channels(report.path)[0], 440, .35, .45) > .03


def test_actual_master_echo_tail_is_not_silently_cropped(tmp_path, engine):
    source = tone(tmp_path / 'dry.wav', 440, .2, .2)
    report = engine.render_timeline([Track(source)], tmp_path / 'master-echo.wav',
                                   master_effects=(Effect('echo', {'delay_ms': 300, 'decay': .5}),))
    assert report.duration_seconds == .5
    assert amplitude(channels(report.path)[0], 440, .35, .45) > .03


def test_actual_master_tempo_infers_duration_and_explicit_target_preserves_audio(tmp_path, engine):
    source = tone(tmp_path / 'tempo.wav', 440, 1, .2)
    inferred = engine.render_timeline([Track(source)], tmp_path / 'fast.wav',
                                     master_effects=(Effect('tempo', {'factor': 2}),))
    assert inferred.duration_seconds == .5
    explicit = engine.render_timeline([Track(source)], tmp_path / 'explicit-fast.wav', duration=.5,
                                     master_effects=(Effect('tempo', {'factor': 2}),))
    assert explicit.duration_seconds == .5
    assert amplitude(channels(explicit.path)[0], 440, .35, .45) > .1


def test_stereo_routing_rejects_ambiguous_mono_request(tmp_path, engine):
    source = tone(tmp_path / 'source.wav')
    with pytest.raises(ValueError, match='stereo'):
        engine.render_timeline([Track(source, routing='left')], tmp_path / 'out.wav', channels=1)


def test_actual_flac_conversion(tmp_path, engine):
    source = tone(tmp_path / 'source.wav')
    report = engine.convert(source, tmp_path / 'converted.flac')
    assert report.format == 'flac'
    assert report.path.read_bytes().startswith(b'fLaC')
    assert report.duration_seconds == pytest.approx(.4)


def test_actual_opus_conversion_reports_decoded_rate_and_frames(tmp_path, engine):
    source = tone(tmp_path / 'source.wav')
    report = engine.convert(source, tmp_path / 'converted.opus')
    assert report.format == 'opus'
    assert report.sample_rate == 48000
    assert report.frames == 19200
    assert report.duration_seconds == .4
    assert .15 < report.peak < .4


def test_actual_runtime_capabilities(engine):
    capabilities = engine.capabilities()
    assert capabilities['formats']['wav']
    assert capabilities['effects']['equalizer']['available']
    assert capabilities['ducking']
    assert capabilities['lufs_metering']
    assert capabilities['playback'] is False


def test_actual_loudness_meter_and_normalization(tmp_path, engine):
    source = tone(tmp_path / 'quiet.wav', 1000, 3, .04)
    before = engine.measure_loudness(source)
    assert before.integrated_lufs < -20
    assert before.true_peak_dbfs < -20
    report = engine.render_timeline([Track(source, effects=(Effect('loudness',
        {'integrated_lufs': -18, 'true_peak_dbfs': -1.5, 'range_lu': 11}),))],
        tmp_path / 'normalized.wav', channels=1)
    after = engine.measure_loudness(report.path)
    assert after.integrated_lufs == pytest.approx(-18, abs=.5)
    assert after.true_peak_dbfs <= -1.4


def test_silence_meter_does_not_invent_finite_peak(tmp_path, engine):
    report = engine.render_timeline((), tmp_path / 'silent.wav', duration=1)
    metered = engine.measure_loudness(report.path)
    assert metered.true_peak_dbfs is None


def test_rf64_header_reader_verifies_sizes(tmp_path):
    # Small RF64 verifies the same 64-bit path as large PCM masters without
    # pretending a synthetic header establishes a ten-hour rendering result.
    data = b'\x01\x00\x02\x00' * 10
    fmt = struct.pack('<HHIIHH', 1, 2, RATE, RATE * 4, 4, 16)
    chunks = b'fmt ' + struct.pack('<I', 16) + fmt + b'data' + struct.pack('<I', 0xffffffff) + data
    fullsize = 12 + 36 + len(chunks)
    descriptor = struct.pack('<QQQI', fullsize - 8, len(data), 10, 0)
    path = tmp_path / 'rf64.wav'
    path.write_bytes(b'RF64\xff\xff\xff\xffWAVEds64' + struct.pack('<I', 28) + descriptor + chunks)
    assert _pcm_header(path)[:3] == (RATE, 2, 10)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ValueError, match='truncated'):
        _pcm_header(path)
