"""Actual existing ffplay transport; required separately by the POSIX gate.

SDL's dummy sink makes no device or acoustic qualification claim. The shared
navigation suite contains no platform skips; Windows uses the existing MCI
player and its installed native reader probe instead of these subprocess tests.
"""
import hashlib
import os
import shutil
import wave

import pytest

from carr_library.speech.player import WavePlayer
from carr_library.ui.playback_navigation import PlaybackNavigation


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Actual ffplay subprocess lifecycle requires POSIX.')


@pytest.fixture
def pcm(tmp_path, monkeypatch):
    assert shutil.which('ffplay'), 'The required POSIX playback gate needs the installed ffplay executable.'
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    path = tmp_path / 'retained-pcm.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\0' * 16000)
    return path


def test_real_pcm_paused_seek_reaps_previous_child_and_sleep_uses_reader_stop(pcm):
    original = hashlib.sha256(pcm.read_bytes()).hexdigest()
    player, generation, now = WavePlayer(), [1], [100.0]
    children = []
    def stop():
        generation[0] += 1
        player.stop()
    controller = PlaybackNavigation(player, generation=lambda: generation[0],
        stop_current=stop, clock=lambda: now[0])
    try:
        player.play(pcm); children.append(player._process)
        controller.bind()
        assert controller.snapshot()['approximate'] is True
        assert player.pause()
        controller.seek_percent(25); children.append(player._process)
        assert children[0].poll() is not None and children[1] is not children[0]
        assert player.state == 'paused' and .5 <= player.position_seconds < .6
        controller.set_skip(1)
        controller.skip(1); children.append(player._process)
        assert children[1].poll() is not None
        assert player.state == 'paused' and 1.5 <= player.position_seconds < 1.6
        controller.set_sleep(1); now[0] += 1; controller.tick()
        assert generation == [2] and player.state == 'stopped'
        assert player._process is None and all(child.poll() is not None for child in children)
        assert controller.audio is None and controller.sleep_deadline is None
        assert hashlib.sha256(pcm.read_bytes()).hexdigest() == original
    finally:
        controller.close(); player.close()


def test_actual_eof_repeats_only_explicit_current_range_and_stop_never_restarts(pcm):
    player, generation = WavePlayer(), [1]
    controller = PlaybackNavigation(player, generation=lambda: generation[0])
    try:
        player.play(pcm, start_seconds=1.75)
        first = player._process
        controller.bind(); controller.set_range(.5, 2); controller.enable_repeat(True)
        assert first.wait(timeout=5) == 0
        assert player.state == 'stopped' and player.position_seconds == 2
        controller.tick()
        replacement = player._process
        assert replacement is not first and replacement.poll() is None
        assert player.state == 'playing' and .5 <= player.position_seconds < 1.0
        player.stop(); controller.tick(); controller.tick()
        assert replacement.poll() is not None and player._process is None
        assert controller.audio is None and not controller.repeat
    finally:
        controller.close(); player.close()
