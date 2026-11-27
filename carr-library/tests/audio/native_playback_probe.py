"""Actual native preview when a Windows output device is present.

No device is a recorded NOT_RUN capability, never playback acceptance. This
probe cannot prove what a listener or NVDA hears.
"""
import json
import ctypes
from argparse import ArgumentParser
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from carr_library.audio.playback import LocalPlayer
from carr_library.audio import playback
from carr_library.audio.voices import VoiceRegistry


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-layout', type=Path, required=True,
                        help='JSON emitted by native_winmm_layout.c using the Windows SDK.')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('The native player probe requires Windows.')
    expected = json.loads(args.sdk_layout.read_text(encoding='utf-8-sig'))
    structures = (playback._Open, playback._Generic, playback._Set,
                  playback._Position, playback._Play, playback._Status)
    actual = {structure.__name__: {
        'size': ctypes.sizeof(structure), 'alignment': ctypes.alignment(structure),
        'fields': {name: getattr(structure, name).offset for name, _type in structure._fields_},
    } for structure in structures}
    assert actual == expected, {'ctypes': actual, 'windows_sdk': expected}
    with LocalPlayer() as player:
        if not player.output_device_count:
            print(json.dumps({'status': 'NOT_RUN', 'reason': 'DEVICE_UNAVAILABLE',
                              'manual_listening': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
            return 77
        registry = VoiceRegistry({'ffmpeg': False, 'espeak_ng': False})
        voices = registry.list_voices()
        if not voices:
            raise RuntimeError('A real installed voice is required for this playback probe.')
        with TemporaryDirectory(prefix='carr-native-preview-') as temp:
            audio = registry.synthesize('A quiet local preview. Pause, seek and resume this sentence.',
                Path(temp) / 'preview.wav', voice_id=voices[0].id, volume=0.15)
            state = player.open(audio.path)
            assert state.state == 'stopped' and state.duration_seconds > 0.5
            player.play()
            time.sleep(0.1)
            paused = player.pause()
            assert paused.state == 'paused'
            player.seek(min(0.25, state.duration_seconds / 2))
            player.resume()
            time.sleep(0.05)
            player.stop()
            assert player.status().state == 'stopped'
            player.close()
            print(json.dumps({'status': 'PASS', 'device_count': player.output_device_count,
                              'windows_sdk_layout': 'PASS',
                              'checks': ['open', 'play', 'pause', 'seek', 'resume', 'stop', 'close'],
                              'manual_listening': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
