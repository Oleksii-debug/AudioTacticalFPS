"""Optional actual Windows MCI gate on an audio-capable machine.

This is separate from the controlled-player CI probe. Missing native audio is a
failure of this gate, never a substituted device or a silent skip. A successful
device probe still does not establish human listening quality or NVDA usability.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice-id', required=True, help='Explicit installed SAPI voice ID from the reader inventory.')
    parser.add_argument('--library-root', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This measured-device acceptance gate requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    from carr_library.reader.live_player import CanonicalPlayer
    from carr_library.speech.live_adapter import SpeechRequest, discover_voices, render_speech
    voices = discover_voices(configuration_root=options.library_root)
    voice = next((item for item in voices if item.id == options.voice_id), None)
    if voice is None or not voice.id.startswith('sapi5-'):
        raise ValueError('Choose an explicitly installed SAPI voice for this actual-device probe.')
    deadline = time.monotonic() + 120

    def cancelled():
        return time.monotonic() >= deadline

    def wait_for(predicate):
        end = min(deadline, time.monotonic() + 5)
        while not predicate():
            if time.monotonic() >= end:
                raise RuntimeError('Actual Windows playback did not reach the measured state within its deadline.')
            time.sleep(.02)

    with TemporaryDirectory(prefix='resonance-actual-player-') as temporary:
        rendered = render_speech(SpeechRequest(
            'This is a synthetic acceptance recording for the actual Windows audio player. '
            'The measured playback position must pause, resume, seek, and stop on request.',
            voice.id, volume=.5), Path(temporary) / 'native.wav', cancelled=cancelled,
            timeout_seconds=60, configuration_root=options.library_root)
        if rendered.duration_seconds < 1:
            raise RuntimeError('The actual synthesized acceptance recording is too short for pause timing.')
        player = CanonicalPlayer()
        try:
            player.open(rendered.path)
            assert player.status()['backend'] == 'Windows MCI'
            assert player.status()['position_is_approximate'] is False
            player.play()
            wait_for(lambda: player.status()['position_seconds'] >= .08)
            player.pause()
            before = player.status()['position_seconds']
            time.sleep(.3)
            paused = player.status()
            assert paused['state'] == 'paused' and abs(paused['position_seconds'] - before) <= .03
            player.seek(.15)
            assert player.status()['state'] == 'paused'
            assert abs(player.status()['position_seconds'] - .15) <= .05
            player.resume()
            wait_for(lambda: player.status()['position_seconds'] >= .23)
            player.stop()
            assert player.status()['state'] == 'stopped'
        finally:
            player.close()
        assert player.status()['state'] == 'closed'
        report = dict(status='PASS', actual_sapi='PASS', actual_windows_mci='PASS',
                      measured_pause_resume_seek_stop='PASS', voice_id=voice.id,
                      duration_seconds=rendered.duration_seconds, sha256=rendered.sha256,
                      audible_listening='NOT_RUN', nvda='NOT_RUN')
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)
    del native_handle


if __name__ == '__main__':
    main()
