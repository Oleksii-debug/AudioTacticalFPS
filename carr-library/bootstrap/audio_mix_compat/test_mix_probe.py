import array
import math
from pathlib import Path
import shutil
import tempfile
import unittest
import wave

from mix_probe import RATE, Track, mix_prepared

FFMPEG = shutil.which('ffmpeg')


def tone(path, frequency, seconds, gain):
    data = array.array('h', (round(32767*gain*math.sin(2*math.pi*frequency*i/RATE))
                             for i in range(round(seconds*RATE))))
    if __import__('sys').byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        audio.writeframes(data.tobytes())


def read_stereo(path):
    with wave.open(str(path), 'rb') as audio:
        assert audio.getnchannels() == 2
        data = array.array('h')
        data.frombytes(audio.readframes(audio.getnframes()))
        if __import__('sys').byteorder != 'little':
            data.byteswap()
    return data[::2], data[1::2]


def amplitude(signal, frequency, start, end):
    # Fourier projection over complete cycles, avoiding cross-tone contamination.
    values = signal[round(start*RATE):round(end*RATE)]
    c = math.fsum(x*math.cos(2*math.pi*frequency*i/RATE) for i,x in enumerate(values))
    s = math.fsum(x*math.sin(2*math.pi*frequency*i/RATE) for i,x in enumerate(values))
    return 2*math.hypot(c,s)/len(values)/32767


class MixTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG:
            self.fail('FFmpeg required; do not mark missing runtime as pass')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tone(self.root/'left.wav', 440, 1, .45)
        tone(self.root/'right.wav', 660, 1, .45)
        tone(self.root/'center.wav', 880, .5, .35)
        tone(self.root/'music.wav', 220, 4, .15)
        self.voices = [Track('left.wav', 1000, -1),
                       Track('right.wav', 2000, 1), Track('center.wav', 3000, 0)]
        self.music = Track('music.wav')
    
    def render(self, voices=None, name='out.wav'):
        return mix_prepared(self.root, self.voices if voices is None else voices,
                            self.music, name, duration_ms=4000, ffmpeg=FFMPEG)

    def test_actual_render_pan_timing_ducking(self):
        left, right = read_stereo(self.render())
        self.assertEqual(len(left), 4*RATE)
        l = amplitude(left, 440, 1.2, 1.8)
        r = amplitude(right, 440, 1.2, 1.8)
        self.assertGreater(l, .35)
        self.assertLess(r, .005)
        self.assertGreater(amplitude(right, 660, 2.2, 2.8), .35)
        self.assertLess(amplitude(left, 660, 2.2, 2.8), .005)
        self.assertAlmostEqual(amplitude(left,880,3.1,3.4),
                               amplitude(right,880,3.1,3.4), delta=.005)
        self.assertGreater(amplitude(left,880,3.1,3.4), .2)
        self.assertLess(amplitude(left,440,.2,.8), .005)
        before = amplitude(left,220,.2,.8)
        during = amplitude(left,220,1.3,1.7)
        self.assertGreater(before, .08)
        self.assertLess(during, before*.5)
        self.assertLess(max(map(abs, left+right)), 32767)

    def test_no_overwrite(self):
        target = self.root/'out.wav'
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            self.render()
        self.assertEqual(target.read_bytes(), b'keep')

    def test_invalid_track_parameters_and_traversal(self):
        for track in [Track('../left.wav'), Track('left.wav', 4000),
                      Track('left.wav', 0, float('nan')), Track('left.wav', 0, 2),
                      Track('left.wav', 0, 0, -1), Track('left.wav', 3500)]:
            with self.subTest(track=track):
                with self.assertRaises(ValueError):
                    self.render([track])
                self.assertFalse((self.root/'out.wav').exists())

    def test_original_inputs_unchanged(self):
        before = {p.name:p.read_bytes() for p in self.root.glob('*.wav')}
        self.render()
        self.assertEqual(before, {name:(self.root/name).read_bytes() for name in before})

    def test_linked_input_rejected(self):
        import os
        os.link(self.root/'left.wav', self.root/'alias.wav')
        with self.assertRaises(ValueError):
            self.render([Track('alias.wav')])
        self.assertFalse((self.root/'out.wav').exists())


if __name__ == '__main__':
    unittest.main()
