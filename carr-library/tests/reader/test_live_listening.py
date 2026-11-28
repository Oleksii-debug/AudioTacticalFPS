"""Real listening conversion on the current live-reader worker and source model.

Only the PCM device/voice are explicit synthetic boundaries. The shared FFmpeg
runtime, source checks, output frames, cancellation and cleanup are real.
"""
from dataclasses import replace
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import AudioRuntime
from carr_library.reader.live_player import CanonicalPlayer
from carr_library.reader.live_reading import LiveReader, LiveBudget, SourceSequence
from carr_library.speech import listening, player as implementation

_spec = importlib.util.spec_from_file_location(
    '_carr_live_listening_fixtures', Path(__file__).with_name('test_live_reading.py'))
_base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _base
_spec.loader.exec_module(_base)
source = _base.source
VOICE = _base.VOICE


def until(predicate, *, timeout=30):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail('The current listening owner did not reach its expected boundary.')
        threading.Event().wait(.01)


class MCI:
    def __init__(self, *, automatic=True):
        self.commands, self.paths, self.threads = [], [], []
        self.mode, self.position, self.end = 'stopped', 0, 0
        self.automatic = automatic
        self.finish = threading.Event()
        self.played = threading.Event()

    def __call__(self, command):
        self.commands.append(command)
        self.threads.append(threading.get_ident())
        if command.startswith('open '):
            path = Path(command.split('"')[1])
            with wave.open(str(path)) as audio:
                self.end = round(audio.getnframes() / audio.getframerate() * 1000)
            self.paths.append((path, path.read_bytes()))
        elif command.startswith('play '):
            self.mode = 'playing'
            self.position = int(command.split(' from ')[1].split()[0])
            self.played.set()
        elif command.startswith('pause '):
            self.mode = 'paused'
        elif command.startswith('resume '):
            self.mode = 'playing'
        elif command.startswith('seek '):
            self.position = int(command.split(' to ')[1].split()[0])
        elif command.startswith(('stop ', 'close ')):
            self.mode = 'stopped'
        elif command.endswith(' mode'):
            if self.mode == 'playing' and (self.automatic or self.finish.is_set()):
                self.mode, self.position = 'stopped', self.end
            return self.mode
        elif command.endswith(' position'):
            return str(self.position)
        return ''


@pytest.fixture
def live(source, monkeypatch):
    app, workspace, book, desk, pages = source
    rows = []
    monkeypatch.setattr(implementation, 'os', SimpleNamespace(name='nt'))
    def make(*, automatic=True, player=None, renderer=None):
        driver = MCI(automatic=automatic)
        backend = implementation.WavePlayer()
        backend._mci = driver
        facade = player or CanonicalPlayer(backend)
        renderer = renderer or _base.SyntheticRender()
        core = LiveReader(SourceSequence(desk), discover=lambda **kw: (VOICE,),
            render=renderer, player_factory=lambda: facade,
            budget=LiveBudget(chunk_characters=64, minimum_free_bytes=1))
        rows.append((core, driver))
        core.discover_voices().result(10)
        return core, renderer, facade, driver
    yield make
    for core, driver in rows:
        driver.finish.set()
        future = core.close()
        if future is not None:
            future.result(30)


@pytest.mark.parametrize('speed,pitch', [(2, 7), (.5, -5)])
def test_live_worker_keeps_synthesis_and_exact_source_separate_from_playback(live, source, speed, pitch):
    core, render, player, driver = live()
    app, workspace, book, desk, pages = source
    original = tuple(desk.page(index).raw_text for index in (1, 2, 3))
    core.set_listening_settings(speed, pitch)
    future = core.start(1, VOICE.id, start=0, end=30, rate=.75, volume=.5)
    future.result(30)
    assert core.status().state == 'complete', core.status()
    assert [(row.rate, row.volume) for row in render.requests] == [(.75, .5)]
    assert render.requests[0].text == pages[0][:30]
    assert len(driver.paths) == 1 and driver.paths[0][0].name == 'listening.wav'
    import io
    with wave.open(io.BytesIO(driver.paths[0][1])) as audio:
        assert audio.getnframes() == pytest.approx(round(1600 / speed), abs=1)
        assert audio.getframerate() == 8000
    assert core.status().listening_speed == speed and core.status().listening_pitch == pitch
    assert len(set(driver.threads)) == 1 and driver.threads[0] != threading.get_ident()
    assert tuple(desk.page(index).raw_text for index in (1, 2, 3)) == original
    assert not list(app.root.glob('.live-reading-*'))
    assert core.quiescent and core._retained_resources is None
    saved = core.state.resume(workspace, book['edition'])
    assert saved.offset == 30 and saved.rate == .75 and saved.volume == .5


def test_default_live_reading_does_not_prepare_or_call_optional_converter(live, monkeypatch):
    monkeypatch.setattr(CanonicalPlayer, 'prepare_listening',
        lambda *a, **k: pytest.fail('Default playback started a listening conversion.'))
    core, render, player, driver = live()
    core.start(1, VOICE.id, start=0, end=20).result(10)
    assert core.status().state == 'complete'
    assert [path.name for path, _ in driver.paths] == ['chunk.wav']
    assert len(render.requests) == 1


def test_paused_seek_uses_original_chunk_time_after_real_speed_conversion(live):
    core, render, player, driver = live(automatic=False)
    core.set_listening_settings(.5, 0)
    future = core.start(1, VOICE.id, start=0, end=20)
    until(lambda: core.status().state == 'playing')
    core.toggle_pause()
    until(lambda: core.status().state == 'paused')
    plays = sum(command.startswith(('play ', 'resume ')) for command in driver.commands)
    view = core.status()
    assert view.duration_seconds == .2
    core.seek(.15, expected=view)
    until(lambda: abs(core.status().audio_seconds - .15) < .001)
    assert any(' to 300 wait' in command for command in driver.commands)
    assert sum(command.startswith(('play ', 'resume ')) for command in driver.commands) == plays
    with pytest.raises(ValueError, match='Stop reading'):
        core.set_listening_settings(3, 0)
    assert core.listening_settings == listening.ListeningSettings(.5, 0)
    core.toggle_pause()
    until(lambda: core.status().state == 'playing')
    driver.finish.set()
    future.result(10)
    assert core.status().state == 'complete' and len(render.requests) == 1


def test_live_level_changes_stay_on_current_worker_and_do_not_resume_paused_audio(live):
    class LevelPlayer(_base.SyntheticPlayer):
        def __init__(self):
            super().__init__(automatic=False)
            self.levels = []
        def set_levels(self, volume, *, muted=False, balance=0):
            self.levels.append((threading.get_ident(), volume, muted, balance))
    device = LevelPlayer()
    core, render, _, driver = live(player=device)
    future = core.start(1, VOICE.id, start=0, end=20, rate=1, volume=.7)
    until(lambda: core.status().state == 'playing')
    core.toggle_pause()
    until(lambda: core.status().state == 'paused')
    count = device.play_count
    core.set_playback_levels(35, muted=True, balance=-.5)
    until(lambda: core.status().playback_muted)
    assert device.levels[-1][1:] == (35, True, -.5)
    assert device.levels[-1][0] != threading.get_ident()
    assert device.state == 'paused' and device.play_count == count
    core.set_playback_levels(35, muted=False, balance=-.5)
    until(lambda: not core.status().playback_muted)
    assert device.play_count == count and render.requests[0].volume == .7
    core.stop()
    future.result(10)
    assert core.quiescent and core._retained_resources is None


def test_close_during_real_conversion_cannot_admit_a_late_listening_copy(live, monkeypatch):
    actual = CanonicalPlayer.prepare_listening
    reached, release = threading.Event(), threading.Event()
    def held(self, *args, **kwargs):
        result = actual(self, *args, **kwargs)
        reached.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(CanonicalPlayer, 'prepare_listening', held)
    core, render, player, driver = live()
    core.set_listening_settings(2, 0)
    future = core.start(1, VOICE.id, start=0, end=20)
    assert reached.wait(30)
    core.close()
    release.set()
    future.result(10)
    assert not driver.played.is_set()
    assert core.quiescent and core._retained_resources is None
    assert not list(core.root.glob('.live-reading-*'))


def test_facade_retains_source_and_prepared_pcm_on_unconfirmed_native_close(live):
    core, render, player, driver = live()
    backend = player.backend
    close = backend.close
    backend.close = lambda: False
    try:
        with pytest.raises(RuntimeError, match='not confirmed'):
            player.close()
    finally:
        backend.close = close
    assert backend.close() is True
