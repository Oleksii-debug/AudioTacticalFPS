"""Native playback protocol/lifecycle checks; no claim about physical audio output."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech.player import WavePlayer
from carr_library.ui.voice_reader import _close_reader_resources


def audio_file(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 8000, 8000, 'NONE', 'not compressed'))
        audio.writeframes(b'\x01\x02' * 8000)
    return path


def native_protocol_player(monkeypatch):
    player = WavePlayer()
    player._windows = True
    calls = []
    def command(value):
        calls.append(value)
        if value.endswith(' mode'):
            return 'playing'
        if value.endswith(' position'):
            return '250'
        return ''
    monkeypatch.setattr(player, '_mci', command)
    return player, calls


def test_pause_resume_seek_preserves_paused_state_and_closes_device(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    player.play(audio_file(tmp_path / 'speech.wav'), start_seconds=0.1)
    assert player.state == 'playing' and player.position_seconds == 0.25
    assert player.pause() and player.state == 'paused'
    player.seek(0.6)
    assert player.state == 'paused'
    assert any(command.startswith('play ') and command.endswith(' from 600') for command in calls)
    assert player.resume() and player.state == 'playing'
    player.close()
    assert player.state == 'stopped' and not player._open
    assert calls[-1].startswith('close ')


def test_invalid_position_never_opens_device(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    source = audio_file(tmp_path / 'speech.wav')
    for position in (-1, 2, float('nan'), True):
        with pytest.raises(ValueError, match='position'):
            player.play(source, start_seconds=position)
    assert calls == []


def test_play_failure_releases_opened_device_before_file_cleanup(tmp_path, monkeypatch):
    player, calls = native_protocol_player(monkeypatch)
    command = player._mci
    def fail_play(value):
        if value.startswith('play '):
            raise RuntimeError('Synthetic audio-device failure')
        return command(value)
    monkeypatch.setattr(player, '_mci', fail_play)
    source = audio_file(tmp_path / 'speech.wav')
    with pytest.raises(RuntimeError, match='device failure'):
        player.play(source)
    assert not player._open and calls[-1].startswith('close ')
    source.unlink()


@pytest.mark.parametrize('failed_step', ['timer', 'keyboard', 'player', 'files'])
def test_reader_cleanup_releases_worker_files_and_dialog_after_close_error(monkeypatch, failed_step):
    cancel, started, shutting_down, finished = (threading.Event() for _ in range(4))
    calls = []
    temporary = TemporaryDirectory(prefix='reader-cleanup-test-')
    folder = Path(temporary.name)
    pool = ThreadPoolExecutor(max_workers=1)

    def work():
        started.set()
        assert cancel.wait(5), 'The worker was not cancelled.'
        assert shutting_down.wait(5), 'The worker was not joined.'
        (folder / 'last-worker-write.txt').write_text('Synthetic cancelled worker result')
        finished.set()

    future = pool.submit(work)
    assert started.wait(5)
    player, device_calls = native_protocol_player(monkeypatch)
    player.play(audio_file(folder / 'speech.wav'))
    mci = player._mci

    def device(command):
        if command.startswith('close '):
            calls.append('player')
            if failed_step == 'player':
                raise RuntimeError('Synthetic player cleanup failure')
        return mci(command)

    monkeypatch.setattr(player, '_mci', device)

    def step(name):
        calls.append(name)
        if name == failed_step:
            raise RuntimeError('Synthetic ' + name + ' cleanup failure')

    def shutdown(**kwargs):
        calls.append('worker')
        shutting_down.set()
        pool.shutdown(**kwargs)

    def cleanup_files():
        calls.append('files')
        assert finished.is_set(), 'Temporary files were deleted before the worker finished.'
        assert (folder / 'last-worker-write.txt').is_file()
        temporary.cleanup()
        if failed_step == 'files':
            raise RuntimeError('Synthetic files cleanup failure')

    try:
        with pytest.raises(RuntimeError, match='Synthetic ' + failed_step + ' cleanup failure'):
            _close_reader_resources(cancel=cancel,
                timer=SimpleNamespace(Stop=lambda: step('timer')),
                keyboard=SimpleNamespace(close=lambda: step('keyboard')), player=player,
                executor=SimpleNamespace(shutdown=shutdown),
                audio_files=SimpleNamespace(cleanup=cleanup_files),
                dialog=SimpleNamespace(Destroy=lambda: step('dialog')))
        assert calls == ['timer', 'keyboard', 'player', 'worker', 'files', 'dialog']
        assert future.done() and future.result() is None
        assert not folder.exists()
    finally:
        cancel.set()
        shutting_down.set()
        pool.shutdown(wait=True, cancel_futures=True)
        temporary.cleanup()
