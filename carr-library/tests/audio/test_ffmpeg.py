from pathlib import Path
from array import array
import os
import struct
import tempfile
import unittest
import wave
import math
import shutil

import pytest

from carr_library.audio.ffmpeg import (wav_info, silence, concatenate, process_voice,
    mix_scene, measure_peak, effect_filters, _write_header)
from carr_library.audio.jobs import RenderCancelled, publish_new
from carr_library.audio.models import Effect


FFMPEG = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')


def tone(path, frequency=440, seconds=0.2, amplitude=10000):
    rate, frames = 8000, round(8000 * seconds)
    with wave.open(str(path), 'wb') as out:
        out.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
        out.writeframes(b''.join(struct.pack('<h', round(amplitude*math.sin(i*2*math.pi*frequency/rate))) for i in range(frames)))


class StreamingTests(unittest.TestCase):
    def test_streaming_concat_exact_frames_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            one = silence(root/'one.wav', 8000, sample_rate=8000)
            two = silence(root/'two.wav', 4000, sample_rate=8000)
            final = concatenate([one, two, one], root/'master.wav')
            self.assertEqual(wav_info(final).frames, 20000)
            with self.assertRaises(FileExistsError):
                concatenate([one], final)

    def test_cancel_does_not_publish_partial(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/'cancel.wav'
            with self.assertRaises(RenderCancelled):
                silence(target, 8000, cancelled=lambda: True)
            self.assertFalse(target.exists())

    def test_rf64_header_has_exact_64bit_size(self):
        import io
        buffer = io.BytesIO()
        frames = 2 ** 31
        _write_header(buffer, frames, 48000, 2)
        data = buffer.getvalue()
        self.assertEqual(len(data), 80)
        self.assertEqual(data[:4], b'RF64')
        self.assertEqual(struct.unpack_from('<Q', data, 28)[0], frames * 4)

    def test_publication_loses_race_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, destination = root/'source', root/'output'
            source.write_bytes(b'new')
            destination.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                publish_new(source, destination)
            self.assertEqual(destination.read_bytes(), b'keep')

    def test_truncated_wave_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'bad.wav'
            silence(path, 8000)
            with path.open('r+b') as stream:
                stream.truncate(80)
            with self.assertRaises(ValueError):
                wav_info(path)


@unittest.skipUnless(FFMPEG.is_file(), 'Requires installed FFmpeg')
class SignalTests(unittest.TestCase):
    def test_actual_left_right_pan_and_limiter(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            tone(root/'source.wav')
            for pan, silent_channel in ((-1,1),(1,0)):
                output = process_voice(root/'source.wav',root/f'pan{pan}.wav',ffmpeg=FFMPEG,sample_rate=8000,pan=pan)
                with wave.open(str(output),'rb') as stream:
                    values = array('h',stream.readframes(stream.getnframes()))
                self.assertEqual(max(abs(x) for x in values[silent_channel::2]),0)
                self.assertGreater(max(abs(x) for x in values[(1-silent_channel)::2]),5000)
                self.assertLessEqual(measure_peak(output)['peak'],0.951)

    def test_actual_equalizer_changes_energy_at_center(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            tone(root/'source.wav',frequency=1000,seconds=0.5,amplitude=2000)
            flat=process_voice(root/'source.wav',root/'flat.wav',ffmpeg=FFMPEG,sample_rate=8000)
            boosted=process_voice(root/'source.wav',root/'boost.wav',ffmpeg=FFMPEG,sample_rate=8000,
                                  effects=(Effect('equalizer',(('frequency',1000),('gain_db',6),('q',1))),))
            self.assertGreater(measure_peak(boosted)['peak'],measure_peak(flat)['peak']*1.8)


@pytest.mark.parametrize('operation', ['voice', 'silence', 'concatenate', 'mix', 'transcode'])
def test_cancellation_at_actual_publication_preserves_input_and_absent_output(tmp_path, monkeypatch, operation):
    from carr_library.audio import ffmpeg as audio
    assert FFMPEG.is_file(), 'Publication acceptance requires actual FFmpeg.'
    source = audio.silence(tmp_path / 'source.wav', 8000, sample_rate=8000)
    original = source.read_bytes()
    output = tmp_path / ('output.flac' if operation == 'transcode' else 'output.wav')
    publish, stopped = audio.publish_new, [False]

    def stop_after_render_before_copy(source_path, destination, **options):
        stopped[0] = True
        return publish(source_path, destination, **options)

    monkeypatch.setattr(audio, 'publish_new', stop_after_render_before_copy)
    cancelled = lambda: stopped[0]
    operations = {
        'voice': lambda: audio.process_voice(source, output, ffmpeg=FFMPEG,
                                             sample_rate=8000, cancelled=cancelled),
        'silence': lambda: audio.silence(output, 8000, sample_rate=8000, cancelled=cancelled),
        'concatenate': lambda: audio.concatenate([source], output, cancelled=cancelled),
        'mix': lambda: audio.mix_scene(source, None, output, ffmpeg=FFMPEG, cancelled=cancelled),
        'transcode': lambda: audio.transcode(source, output, ffmpeg=FFMPEG, format='flac',
                                            cancelled=cancelled),
    }
    with pytest.raises(RenderCancelled):
        operations[operation]()
    assert stopped[0], 'Cancellation must begin at real publication after rendering.'
    assert source.read_bytes() == original
    assert not output.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ['source.wav']


if __name__=='__main__':
    unittest.main()
