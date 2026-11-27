"""Required real POSIX ffplay and bytes-environment regressions.

The Linux source gate runs this file explicitly with FFmpeg installed. Windows
uses MCI playback and does not support the POSIX bytes-environment interface;
its strict process gate keeps all applicable tests in test_audio_process.py.
"""
from pathlib import Path
import os
import shutil
import wave

import pytest

from carr_library.audio.process import run_process
from carr_library.speech.player import WavePlayer


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX subprocess interfaces require POSIX.')


def test_real_ffmpeg_bytes_environment_cannot_overwrite_an_inherited_report_destination(tmp_path, monkeypatch):
    executable = shutil.which('ffmpeg')
    assert executable is not None, 'Required POSIX native FFmpeg regression needs the installed executable.'
    source, sentinel = tmp_path / 'input.wav', tmp_path / 'unrelated-private.txt'
    sentinel.write_bytes(b'Unchanged unrelated private file')
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 2400)
    report = 'file=unrelated-private.txt:level=48'
    monkeypatch.setenv('FFREPORT', report)
    environment = {os.fsencode(key): os.fsencode(value) for key, value in os.environ.items()}
    before = dict(environment)
    run_process([str(Path(executable).resolve()), '-v', 'error', '-nostdin',
                 '-i', str(source), '-f', 'null', '-'], cwd=tmp_path, env=environment)
    assert sentinel.read_bytes() == b'Unchanged unrelated private file'
    assert os.environ['FFREPORT'] == report
    assert environment == before


def test_real_ffplay_cannot_overwrite_an_inherited_report_destination(tmp_path, monkeypatch):
    assert shutil.which('ffplay') is not None, 'Required POSIX native playback regression needs installed ffplay.'
    source, sentinel = tmp_path / 'input.wav', tmp_path / 'unrelated-private.txt'
    sentinel.write_bytes(b'Unchanged unrelated private file')
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 2400)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FFREPORT', 'file=unrelated-private.txt:level=48')
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    player = WavePlayer()
    try:
        player.play(source)
        assert player._process.wait(timeout=5) == 0
        assert player.state == 'stopped'
        assert sentinel.read_bytes() == b'Unchanged unrelated private file'
    finally:
        player.close()
