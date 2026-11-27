"""Actual local codec/container/decode gates, using short synthetic PCM only."""
from array import array
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

import pytest

from carr_library.audio import ffmpeg as audio
from carr_library.audio.jobs import RenderCancelled


@pytest.fixture
def binaries():
    ffmpeg = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    ffprobe = os.environ.get('CARR_FFPROBE') or shutil.which('ffprobe')
    assert ffmpeg and Path(ffmpeg).is_file(), 'Actual FFmpeg is required for this gate.'
    assert ffprobe and Path(ffprobe).is_file(), 'Actual ffprobe is required for this gate.'
    return str(Path(ffmpeg).absolute()), str(Path(ffprobe).absolute())


def tone(path, *, rate=16000, frames=4321, channels=2):
    samples = array('h', (int(5000 * math.sin(index * 2 * math.pi * 440 / rate))
                         for index in range(frames) for _ in range(channels)))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, 2, rate, frames, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())
    return path


@pytest.mark.parametrize('format,codec', [
    ('flac', 'flac'), ('mp3', 'mp3'), ('opus', 'opus'),
    ('m4a', 'aac'), ('m4b', 'aac'), ('aiff', 'pcm_s16be'),
])
def test_actual_export_verifies_container_codec_and_all_decoded_frames(tmp_path, binaries, format, codec):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav')
    destination = tmp_path / ('result.' + format)
    metadata = tmp_path / 'chapters.ffmeta'
    metadata.write_text(';FFMETADATA1\ntitle=Synthetic test\n[CHAPTER]\nTIMEBASE=1/16000\nSTART=0\nEND=4321\ntitle=One\n')
    audio.transcode(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe, format=format,
                    metadata=metadata if format in ('m4a', 'm4b') else None)
    checked = audio.verify_encoded(destination, audio.wav_info(source), ffmpeg=ffmpeg,
                                   ffprobe=ffprobe, format=format)
    assert checked['codec'] == codec and checked['channels'] == 2
    expected = 12963 if format == 'opus' else 8642 if format == 'mp3' else 4321
    assert checked['sample_rate'] == (48000 if format == 'opus' else 32000 if format == 'mp3' else 16000)
    assert expected <= checked['decoded_frames'] <= expected + (1023 if codec == 'aac' else 0)
    # Independently observe the real inspector's identified codec, not a test stub.
    report = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-select_streams', 'a',
        '-show_entries', 'stream=codec_name', '-of', 'json', str(destination)]))
    assert [stream['codec_name'] for stream in report['streams']] == [codec]


@pytest.mark.parametrize('rate', [8000, 11025, 12000, 16000, 22050, 24000])
def test_low_rate_mp3_delivery_profile_has_independent_complete_decode(tmp_path, binaries, rate):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav', rate=rate)
    original = source.read_bytes()
    encoded = audio.transcode(source, tmp_path / 'result.mp3', ffmpeg=ffmpeg,
                              ffprobe=ffprobe, format='mp3')
    inspected = json.loads(subprocess.check_output(
        [ffprobe, '-v', 'error', '-select_streams', 'a', '-show_entries',
         'stream=codec_name,sample_rate,channels', '-of', 'json', str(encoded)],
        timeout=20))
    stream, = inspected['streams']
    assert (stream['codec_name'], int(stream['sample_rate']), stream['channels']) == ('mp3', 32000, 2)
    # Count the actual decoded PCM independently of verify_encoded. A relaxed
    # duration tolerance in the production verifier cannot make this test pass.
    decoded = subprocess.check_output(
        [ffmpeg, '-nostdin', '-v', 'error', '-i', str(encoded), '-map', '0:a:0',
         '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'], timeout=20)
    assert len(decoded) % 4 == 0
    expected = (4321 * 32000 + rate // 2) // rate
    assert abs(len(decoded) // 4 - expected) <= 1
    assert source.read_bytes() == original
    assert audio.wav_info(source).rate == rate


def test_high_rate_mp3_explicitly_resamples_with_bounded_rounding(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav', rate=96000, frames=9601)
    encoded = audio.transcode(source, tmp_path / 'result.mp3', ffmpeg=ffmpeg, ffprobe=ffprobe, format='mp3')
    result = audio.verify_encoded(encoded, audio.wav_info(source), format='mp3', ffmpeg=ffmpeg, ffprobe=ffprobe)
    assert result['sample_rate'] == 48000
    assert abs(result['decoded_frames'] - 4801) <= 1


def test_nonempty_wrong_codec_file_is_rejected(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav')
    encoded = audio.transcode(source, tmp_path / 'valid.mp3', ffmpeg=ffmpeg, ffprobe=ffprobe, format='mp3')
    with pytest.raises(ValueError, match='format'):
        audio.verify_encoded(encoded, audio.wav_info(source), format='flac', ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_valid_shorter_audio_cannot_stand_in_for_full_source(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    long_source = tone(tmp_path / 'source.wav', frames=32000)
    shorter = tone(tmp_path / 'shorter.wav', frames=12000)
    encoded = audio.transcode(shorter, tmp_path / 'shorter.flac', ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac')
    with pytest.raises(ValueError, match='shorter'):
        audio.verify_encoded(encoded, audio.wav_info(long_source), format='flac', ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_valid_longer_audio_cannot_stand_in_for_source(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav')
    longer = tone(tmp_path / 'longer.wav', frames=16000)
    encoded = audio.transcode(longer, tmp_path / 'longer.flac', ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac')
    with pytest.raises(ValueError, match='longer'):
        audio.verify_encoded(encoded, audio.wav_info(source), format='flac', ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_actual_truncated_mp3_is_not_a_complete_export(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav', frames=16000)
    encoded = audio.transcode(source, tmp_path / 'result.mp3', ffmpeg=ffmpeg, ffprobe=ffprobe, format='mp3')
    with encoded.open('r+b') as stream:
        stream.truncate(encoded.stat().st_size // 2)
    assert encoded.stat().st_size > 0
    with pytest.raises(ValueError):
        audio.verify_encoded(encoded, audio.wav_info(source), format='mp3', ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_wrong_channels_are_rejected(tmp_path, binaries):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav')
    mono = tone(tmp_path / 'mono.wav', channels=1)
    encoded = audio.transcode(mono, tmp_path / 'mono.flac', ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac')
    with pytest.raises(ValueError, match='format'):
        audio.verify_encoded(encoded, audio.wav_info(source), format='flac', ffmpeg=ffmpeg, ffprobe=ffprobe)


def test_successful_encode_with_damaged_artifact_does_not_publish(tmp_path, binaries, monkeypatch):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav')
    original = audio.run_ffmpeg

    def damaged_after_actual_encoder(*args, **kwargs):
        result = original(*args, **kwargs)
        Path(args[1][-1]).write_bytes(b'nonempty damaged encoder artifact')
        return result

    monkeypatch.setattr(audio, 'run_ffmpeg', damaged_after_actual_encoder)
    destination = tmp_path / 'never-published.flac'
    with pytest.raises(ValueError):
        audio.transcode(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac')
    assert not destination.exists()
    assert not list(tmp_path.glob('.encode-*'))


def test_cancellation_stops_actual_decoder_and_never_publishes(tmp_path, binaries, monkeypatch):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav', frames=32000)
    original, decoders = audio.subprocess.Popen, []

    def capture_actual_decoder(command, **kwargs):
        process = original(command, **kwargs)
        if 'pipe:1' in command and 's16le' in command:
            decoders.append(process)
        return process

    monkeypatch.setattr(audio.subprocess, 'Popen', capture_actual_decoder)
    destination = tmp_path / 'cancelled.flac'
    with pytest.raises(RenderCancelled):
        audio.transcode(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac',
                        cancelled=lambda: bool(decoders))
    assert len(decoders) == 1 and decoders[0].poll() is not None
    assert not destination.exists()
    assert not list(tmp_path.glob('.encode-*'))


def test_missing_explicit_inspector_fails_before_encoding(tmp_path, binaries):
    ffmpeg, _ = binaries
    source = tone(tmp_path / 'source.wav')
    destination = tmp_path / 'never.flac'
    with pytest.raises(RuntimeError, match='ffprobe'):
        audio.transcode(source, destination, ffmpeg=ffmpeg, ffprobe=tmp_path / 'missing', format='flac')
    assert not destination.exists()


def test_reader_start_failure_stops_actual_decoder_and_closes_pipe(tmp_path, binaries, monkeypatch):
    ffmpeg, ffprobe = binaries
    source = tone(tmp_path / 'source.wav', frames=32000)
    original, decoders = audio.subprocess.Popen, []

    def capture_actual_decoder(command, **kwargs):
        process = original(command, **kwargs)
        if 'pipe:1' in command and 's16le' in command:
            decoders.append(process)
        return process

    def refuse_thread_start(_):
        assert decoders, 'The regression must cover a real already-launched decoder.'
        raise RuntimeError('Injected thread-resource exhaustion')

    monkeypatch.setattr(audio.subprocess, 'Popen', capture_actual_decoder)
    monkeypatch.setattr(audio.Thread, 'start', refuse_thread_start)
    destination = tmp_path / 'unpublished.flac'
    with pytest.raises(RuntimeError, match='thread-resource'):
        audio.transcode(source, destination, ffmpeg=ffmpeg, ffprobe=ffprobe, format='flac')
    assert len(decoders) == 1 and decoders[0].poll() is not None
    assert decoders[0].stdout.closed
    assert not destination.exists() and not list(tmp_path.glob('.encode-*'))

