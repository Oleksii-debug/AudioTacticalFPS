"""Strict installed-package Windows SAPI render. Not a manual NVDA test."""
from dataclasses import asdict
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from carr_library.audio.voices import VoiceRegistry


def main():
    if os.name != 'nt':
        raise RuntimeError('This required native speech probe must run on Windows.')
    registry = VoiceRegistry({'ffmpeg': False, 'espeak_ng': False})
    voices = registry.list_voices()
    if not voices or any(voice.engine != 'sapi5' for voice in voices):
        raise RuntimeError('No actual installed Windows SAPI5 voices were discovered.')
    with TemporaryDirectory(prefix='carr-native-speech-') as temp:
        result = registry.synthesize('A local voice reads the research library.',
                                     Path(temp) / 'voice.wav', voice_id=voices[0].id)
        if result.frames <= 0 or result.channels != 1 or result.sample_rate != 24000:
            raise RuntimeError('Native SAPI5 did not create the requested PCM waveform.')
        payload = asdict(result)
        payload.pop('path')
        print(json.dumps({'status': 'PASS', 'runtime': registry.runtime_identity(),
                          'render': payload, 'manual_nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
