"""Opt-in actual 1/3/10-hour PCM assembly, not a neural generation benchmark.

Run CARR_LONG_AUDIO_TESTS=1 python -m pytest -q -s
tests/audio/test_long_streaming_integrity.py. Peak disk is about 1.3 GiB;
large intermediates and outputs are removed even when an assertion fails.
"""
import json
import math
import os
from pathlib import Path
import shutil
import struct
import tempfile
import time
import wave

import pytest

from carr_library.audio.ffmpeg import concatenate, process_voice, silence, wav_info
from carr_library.audio.jobs import sha256_file


FFMPEG = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')
pytestmark = pytest.mark.skipif(os.environ.get('CARR_LONG_AUDIO_TESTS') != '1' or not FFMPEG.is_file(),
    reason='Opt-in actual long PCM assembly needs FFmpeg and 1.3 GiB free disk.')


@pytest.fixture(scope='module')
def synthetic_hour():
    with tempfile.TemporaryDirectory(prefix='carr-long-stream-') as temporary:
        root = Path(temporary)
        assert shutil.disk_usage(root).free > 1400 * 1024 ** 2, 'Long assembly needs 1.3 GiB free disk.'
        raw, rate, tone_frames = root / 'synthetic-tone.wav', 8000, 800
        with wave.open(str(raw), 'wb') as stream:
            stream.setparams((1, 2, rate, tone_frames, 'NONE', 'not compressed'))
            stream.writeframes(b''.join(struct.pack('<h', round(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                                       for i in range(tone_frames)))
        cue = process_voice(raw, root / 'left-cue.wav', ffmpeg=FFMPEG, sample_rate=rate, pan=-1)
        quiet = silence(root / 'quiet.wav', 3600 * rate - tone_frames, sample_rate=rate)
        hour = concatenate((quiet, cue), root / 'one-hour-scene.wav')
        quiet.unlink()
        yield root, hour


@pytest.mark.parametrize('hours', [1, 3, 10])
def test_exact_multi_hour_streamed_pcm(synthetic_hour, hours, record_property):
    root, scene = synthetic_hour
    output = root / f'{hours}-hour-output.wav'
    started = time.monotonic()
    try:
        concatenate([scene] * hours, output)
        info = wav_info(output)
        expected = hours * 3600 * 8000
        assert (info.frames, info.rate, info.channels) == (expected, 8000, 2)
        assert output.stat().st_size == expected * 4 + 44
        assert output.stat().st_nlink == 1
        # Read the actual ending frames, rather than trusting only a duration tag.
        with wave.open(str(output), 'rb') as stream:
            stream.setpos(expected - 1600)
            samples = struct.unpack('<3200h', stream.readframes(1600))
        assert not any(samples[:1600])
        assert max(abs(value) for value in samples[1600::2]) > 7000
        assert not any(samples[1601::2])
        evidence = {'synthetic_hours': hours, 'frames': info.frames, 'bytes': output.stat().st_size,
                    'sha256': sha256_file(output), 'elapsed_seconds': round(time.monotonic() - started, 3),
                    'scope': 'Actual streamed PCM assembly of an explicitly repeated synthetic scene.'}
        try:
            import resource
            evidence['process_peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            pass
        record_property('long_pcm_evidence', json.dumps(evidence, sort_keys=True))
        print(json.dumps(evidence, sort_keys=True))
    finally:
        output.unlink(missing_ok=True)
