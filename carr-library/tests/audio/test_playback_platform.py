"""Explicit platform/device availability probes; separate from portable state-machine gates."""
import sys
import pytest
from carr_library.audio.playback import (PCMPlayer, PlaybackUnavailable, playback_capabilities, native_playback_probe)

@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses its native device gate")
def test_non_windows_reports_unavailable_and_never_starts_fallback_process():
    assert not playback_capabilities()["available"]
    assert native_playback_probe()["status"] == "UNAVAILABLE"
    with pytest.raises(PlaybackUnavailable):
        PCMPlayer().play("not-used.wav")


@pytest.mark.skipif(sys.platform != "win32", reason="Actual WinMM requires Windows")
def test_actual_native_winmm_playback_controls_or_explicit_missing_device():
    result = native_playback_probe()
    if result["status"] == "UNAVAILABLE":
        pytest.skip(result["reason"])
    assert result["status"] == "PASS", result
    assert result["manual_listening"] == "NOT_RUN"


import shutil
import time
import wave
import carr_library.audio.player as player_module
from carr_library.audio.player import WavePlayer


def test_actual_ffplay_headless_decode_and_queue_completion(tmp_path, monkeypatch):
    if player_module.os.name == 'nt' or not shutil.which('ffplay'):
        pytest.skip('Actual ffplay dummy-device probe requires an installed POSIX ffplay.')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    paths = []
    for name in ('first.wav','second.wav'):
        path = tmp_path/name
        with wave.open(str(path), 'wb') as output:
            output.setparams((1,2,8000,0,'NONE','not compressed'))
            output.writeframes(b'\x10\x00' * 1600)
        paths.append(path)
    player = WavePlayer()
    try:
        player.set_playlist(paths); player.play_index(0)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            player.poll()
            assert player.state != 'error', 'The installed ffplay could not decode through SDL dummy audio.'
            if player.playlist_index == 1 and player.state == 'stopped':
                break
            time.sleep(.02)
        else:
            pytest.fail('Headless actual playback did not complete the two-item queue within its budget.')
    finally:
        player.close()
