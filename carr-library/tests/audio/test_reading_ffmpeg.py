"""Actual FFmpeg pipeline; speech is a declared synthetic PCM contract fixture."""
from pathlib import Path
import shutil
import wave

from carr_library.audio.reading import render_reading
from carr_library.audio.ffmpeg import wav_info
from carr_library.operations import ResourceBroker


class SyntheticSpeech:
    def __init__(self):
        self.requests = []
    def cache_identity(self, voice):
        return {'adapter': 'test-pcm-contract-only', 'voice': voice}
    def synthesize(self, text, output, **kwargs):
        self.requests.append((text, kwargs))
        with wave.open(str(output), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b'\x01\0' * 2400)
        return Path(output)


def test_actual_ffmpeg_preserves_literal_reading_and_exports_pcm(tmp_path):
    binary = shutil.which('ffmpeg')
    assert binary, 'This required qualification needs the actual installed FFmpeg runtime.'
    registry = SyntheticSpeech()
    text = '[Music: calm]\nThe exact reading copy.'
    result = render_reading(tmp_path, text, registry=registry, voice='synthetic:one',
                            language='en', rate=2, volume=61, ffmpeg=binary)
    assert ''.join(row[0] for row in registry.requests) == text
    assert all(row[1]['rate'] == 2 and row[1]['volume'] == .61 for row in registry.requests)
    assert wav_info(result.path).frames > 0
    destination = tmp_path / 'saved-reading.wav'
    result.save_wav(destination)
    assert wav_info(destination) == wav_info(result.path)
    assert not ResourceBroker.for_app(tmp_path).snapshot()['leases']
    result.close()
    assert destination.exists()
