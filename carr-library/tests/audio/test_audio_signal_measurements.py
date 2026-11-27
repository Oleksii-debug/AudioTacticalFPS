"""Measure the actual integrated FFmpeg signal path using synthetic PCM.

These tests measure routing, dynamics and levels, not speech or music quality.
"""
from array import array
import hashlib
import math
import sys
import time
import wave

import pytest

from carr_library.audio.ffmpeg import (measure_loudness, measure_peak, mix_scene,
                                       process_voice, run_ffmpeg, wav_info)
from carr_library.audio.jobs import RenderCancelled
from carr_library.audio.models import Effect
from carr_library.audio.runtime import resolve_ffmpeg


@pytest.fixture
def ffmpeg():
    selected = resolve_ffmpeg()
    if selected is None:
        pytest.skip("These measurements require an actual local FFmpeg executable.")
    return selected


def _write(path, signal, *, seconds=3, channels=1, rate=8000):
    samples = array("h")
    for frame in range(round(seconds * rate)):
        values = signal(frame / rate)
        if channels == 1:
            values = (values,)
        samples.extend(round(value) for value in values)
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, 2, rate, len(samples) // channels, "NONE", "PCM"))
        output.writeframes(samples.tobytes())
    return path


def _channel(path, channel=0):
    with wave.open(str(path), "rb") as source:
        values = array("h", source.readframes(source.getnframes()))
        channels, rate = source.getnchannels(), source.getframerate()
    if sys.byteorder != "little":
        values.byteswap()
    return values[channel::channels], rate


def _rms(path, first, last, channel=0):
    values, rate = _channel(path, channel)
    values = values[round(first * rate):round(last * rate)]
    return math.sqrt(sum(sample * sample for sample in values) / len(values))


def _component(path, frequency, first=.25, last=.75):
    values, rate = _channel(path)
    values = values[round(first * rate):round(last * rate)]
    real = sum(value * math.cos(2 * math.pi * frequency * i / rate) for i, value in enumerate(values))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / rate) for i, value in enumerate(values))
    return 2 * math.hypot(real, imag) / len(values)


def test_actual_music_is_ducked_under_voice_with_exact_frames_and_unchanged_inputs(tmp_path, ffmpeg):
    voice = _write(tmp_path / "Голос ліворуч.wav", lambda t:
                   (18000 * math.sin(2 * math.pi * 1000 * t) if 1 <= t < 2 else 0, 0), channels=2)
    music = _write(tmp_path / "Музика.wav", lambda t: 10000 * math.sin(2 * math.pi * 100 * t))
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (voice, music)}
    output = mix_scene(voice, music, tmp_path / "Разом.wav", ffmpeg=ffmpeg, music_gain=.4)
    # The right channel contains only the music; voice energy cannot fake a
    # ducking measurement. Sample well away from fades and attack/release.
    quiet = _rms(output, .4, .8, channel=1)
    speaking = _rms(output, 1.35, 1.75, channel=1)
    recovered = _rms(output, 2.5, 2.7, channel=1)
    assert speaking < quiet * .4
    assert recovered > speaking * 2
    assert wav_info(output).frames == 24000
    assert measure_peak(output)["peak"] <= .951
    assert hashes == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in hashes}


def test_actual_highpass_lowpass_frequency_response(tmp_path, ffmpeg):
    source = _write(tmp_path / "two-tones.wav", lambda t:
                    3000 * math.sin(2 * math.pi * 100 * t) + 3000 * math.sin(2 * math.pi * 2000 * t), seconds=1)
    outputs = {}
    variants = [("flat", ()), ("high", (Effect("highpass", (("frequency", 500),)),)),
                ("low", (Effect("lowpass", (("frequency", 500),)),))]
    for name, effects in variants:
        outputs[name] = process_voice(source, tmp_path / (name + ".wav"), ffmpeg=ffmpeg,
                                      sample_rate=8000, effects=effects)
    assert _component(outputs["high"], 100) < _component(outputs["flat"], 100) * .1
    assert _component(outputs["high"], 2000) > _component(outputs["flat"], 2000) * .85
    assert _component(outputs["low"], 2000) < _component(outputs["flat"], 2000) * .1
    assert _component(outputs["low"], 100) > _component(outputs["flat"], 100) * .85


def test_actual_gate_reduces_quiet_signal_and_compressor_reduces_loud_signal(tmp_path, ffmpeg):
    source = _write(tmp_path / "dynamics.wav", lambda t:
                    (600 if t < 1 else 22000) * math.sin(2 * math.pi * 440 * t))
    results = {}
    for name, effects in (("flat", ()),
        ("gate", (Effect("gate", (("threshold_db", -20), ("attack_ms", 1), ("release_ms", 100))),)),
        ("compressor", (Effect("compressor", (("threshold_db", -20), ("ratio", 6), ("attack_ms", 5))),))):
        results[name] = process_voice(source, tmp_path / (name + ".wav"), ffmpeg=ffmpeg,
                                      sample_rate=8000, effects=effects)
    assert _rms(results["gate"], .4, .8) < _rms(results["flat"], .4, .8) * .1
    assert _rms(results["gate"], 1.5, 2.5) > _rms(results["flat"], 1.5, 2.5) * .9
    assert _rms(results["compressor"], 1.5, 2.5) < _rms(results["flat"], 1.5, 2.5) * .5


def test_actual_loudness_measurement_reports_expected_gain_difference(tmp_path, ffmpeg):
    source = _write(tmp_path / "level.wav", lambda t: 10000 * math.sin(2 * math.pi * 1000 * t))
    loud = process_voice(source, tmp_path / "loud.wav", ffmpeg=ffmpeg, sample_rate=8000)
    quiet = process_voice(source, tmp_path / "quiet.wav", ffmpeg=ffmpeg, sample_rate=8000, gain=.5)
    loudness = measure_loudness(loud, ffmpeg=ffmpeg)["integrated_lufs"]
    reduced = measure_loudness(quiet, ffmpeg=ffmpeg)["integrated_lufs"]
    assert abs((loudness - reduced) - 20 * math.log10(2)) < .2


def test_actual_running_ffmpeg_process_is_terminated_by_cancellation(ffmpeg, monkeypatch):
    from carr_library.audio import ffmpeg as audio_ffmpeg
    processes = []
    start = audio_ffmpeg.start_owned_process

    def tracked_process(*args, **kwargs):
        process = start(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(audio_ffmpeg, "start_owned_process", tracked_process)
    started = time.monotonic()
    with pytest.raises(RenderCancelled):
        run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-f", "null", "-"],
                   cancelled=lambda: time.monotonic() - started > .2)
    assert time.monotonic() - started < 10
    assert processes and all(process.poll() is not None for process in processes)
