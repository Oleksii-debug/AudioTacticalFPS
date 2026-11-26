"""Actual installed SAPI5 enumeration and offline WAV generation, no mock voice."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import wave

from carr_library.audio.voices import list_voices, render_speech


def main():
    if sys.platform != 'win32':
        raise RuntimeError('This acceptance check requires native Windows.')
    voices = list_voices()
    if not voices:
        raise RuntimeError('No enabled SAPI5 voice is installed; actual speech acceptance is NOT_RUN.')
    selected = next((v for v in voices if v.language.startswith('en')), voices[0])
    text = 'This is a synthetic local research library narration test. No document is uploaded.'
    with tempfile.TemporaryDirectory(prefix='Research Voice Ž ') as temp:
        target = Path(temp)/'Голос.wav'
        rendered = render_speech(text, target, voice_id=selected.id)
        with wave.open(str(target), 'rb') as stream:
            frames = stream.readframes(stream.getnframes())
            peak = max(abs(x[0]) for x in struct.iter_unpack('<h', frames))
        assert rendered.frames > 2400 and peak > 100
        assert rendered.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
        try:
            render_speech(text, target, voice_id=selected.id)
        except FileExistsError:
            pass
        else:
            raise AssertionError('Existing speech output was overwritten.')
        print(json.dumps({'status':'PASS','engine':'sapi5','voices':len(voices),
            'selected_language':selected.language,'frames':rendered.frames,
            'sample_rate':rendered.sample_rate,'peak_pcm16':peak,
            'plain_text_sha256':rendered.text_sha256,'audio_sha256':rendered.sha256,
            'manual_listening':'NOT_RUN','nvda':'NOT_RUN'},ensure_ascii=False))


if __name__=='__main__':
    main()
