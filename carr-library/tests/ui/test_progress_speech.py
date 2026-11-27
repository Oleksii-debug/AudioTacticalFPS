"""Bounded progress speech shares the canonical voice boundary and mute policy."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.ui.operation_progress import (OperationProgress, ProgressEvent,
    ProgressPreferences, load_preferences, save_preferences)
from carr_library.ui.progress_speech import LocalProgressSpeech, spoken_progress


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Progress worker failed to finish.'
        threading.Event().wait(.01)


def test_speech_is_off_by_default_and_all_modes_roundtrip(tmp_path):
    assert load_preferences(tmp_path).mode == 'off'
    for mode in ('off', 'beep', 'speech', 'beep-speech'):
        value = ProgressPreferences(mode=mode, interval_seconds=7, volume=.3)
        save_preferences(tmp_path, value)
        assert load_preferences(tmp_path) == value


def test_measured_announcements_keep_incomplete_work_below_100():
    text = spoken_progress(ProgressEvent(state='running', percent=99, phase='Verifying files'))
    assert '99 percent' in text and 'Completed' not in text
    assert 'indeterminate' in spoken_progress(ProgressEvent(state='running', phase='Loading model'))
    assert spoken_progress(ProgressEvent(state='running', percent=50), resumed=True).startswith('Resumed.')
    assert 'Completed. 100 percent.' in spoken_progress(ProgressEvent(state='succeeded', percent=100))


def test_one_speech_worker_coalesces_pending_text_cancels_stale_and_removes_private_pcm():
    entered, release = threading.Event(), threading.Event()
    renders, played, threads, paths = [], [], [], []
    ui_thread = threading.get_ident()

    class Registry:
        def voices(self):
            threads.append(threading.get_ident())
            return (SimpleNamespace(id='fixture:local'),)

        def synthesize(self, text, voice_id, path, *, settings, cancelled):
            renders.append(text)
            threads.append(threading.get_ident())
            assert voice_id == 'fixture:local' and settings.volume == 25
            if len(renders) == 1:
                entered.set()
                assert release.wait(2)
            if cancelled():
                raise InterruptedError('Superseded announcement.')
            with wave.open(str(path), 'wb') as output:
                output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                output.writeframes(b'\1\0' * 100)

    class Player:
        state = 'stopped'
        def play(self, path):
            assert Path(path).is_file()
            with wave.open(str(path), 'rb') as audio:
                assert audio.getnframes() == 100
            paths.append(Path(path))
            played.append(renders[-1])
            threads.append(threading.get_ident())
        def close(self):
            pass

    speech = LocalProgressSpeech(registry_factory=Registry, player_factory=Player)
    assert speech.speak('First', volume=.25)
    assert entered.wait(1)
    assert speech.speak('Outdated pending progress', volume=.25)
    assert speech.speak('Completed. 100 percent.', volume=.25)
    release.set()
    wait_for(lambda: len(played) == 1)
    speech.close()
    speech._thread.join(2)
    assert not speech._thread.is_alive()
    assert renders == ['First', 'Completed. 100 percent.']
    assert played == ['Completed. 100 percent.']
    assert len(set(threads)) == 1 and ui_thread not in threads
    assert all(not path.exists() for path in paths)
    assert not speech.speak('After close', volume=.25)


def test_quiet_mode_cancels_active_and_pending_speech_without_replay():
    entered, release = threading.Event(), threading.Event()
    heard, attempts = [], []

    class Registry:
        def voices(self): return (SimpleNamespace(id='fixture'),)
        def synthesize(self, text, voice_id, path, *, settings, cancelled):
            attempts.append(text)
            entered.set()
            assert release.wait(2)
            assert cancelled()
            raise InterruptedError('Quiet requested')

    speech = LocalProgressSpeech(registry_factory=Registry,
        player_factory=lambda: SimpleNamespace(play=heard.append, close=lambda: None))
    speech.speak('In progress', volume=.2)
    assert entered.wait(1)
    speech.speak('Pending result', volume=.2)
    speech.muted = True
    assert not speech.speak('Muted result', volume=.2)
    release.set()
    speech.close()
    speech._thread.join(2)
    assert not speech._thread.is_alive()
    assert attempts == ['In progress'] and heard == [] and speech.last_error is None


def test_provider_failure_is_redacted_and_does_not_block_caller():
    def failing_registry():
        raise RuntimeError('Private path and transcript must not enter the global status')
    speech = LocalProgressSpeech(registry_factory=failing_registry)
    before = time.monotonic()
    assert speech.speak('In progress', volume=.2)
    assert time.monotonic() - before < .2
    wait_for(lambda: speech.last_error is not None)
    speech.close()
    speech._thread.join(2)
    assert speech.last_error == 'Local progress speech is unavailable (RuntimeError).'
    assert not speech.available


def test_thread_start_failure_is_nonblocking_and_cannot_break_a_product_job(monkeypatch):
    def fail_start(thread):
        raise RuntimeError('Synthetic worker startup failure with private diagnostic.')
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    speech = LocalProgressSpeech(registry_factory=lambda: None)
    assert not speech.speak('Progress must remain usable.', volume=.2)
    assert not speech.available and speech._thread is None and speech._pending is None
    assert speech.last_error == 'Local progress speech is unavailable (worker could not start).'
    speech.close()


@pytest.mark.parametrize('mode,beep_count,speech_count', [
    ('off', 0, 0), ('beep', 1, 0), ('speech', 0, 1), ('beep-speech', 1, 1)])
def test_native_controller_routes_modes_and_mutes_recording(tmp_path, mode, beep_count, speech_count):
    sounds, words = [], []
    class Timer:
        def __init__(self, frame): self.running = False
        def Start(self, interval): self.running = True
        def Stop(self): self.running = False
        def IsRunning(self): return self.running
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path),
        Bind=lambda *args: None, IsActive=lambda: True,
        status=SimpleNamespace(SetValue=lambda text: None))
    gauge = SimpleNamespace(SetName=lambda value: None, SetValue=lambda value: None,
        Pulse=lambda: None, SetRange=lambda value: None)
    label = SimpleNamespace(SetName=lambda value: None, SetLabel=lambda value: None)
    sound = SimpleNamespace(available=True, muted=False,
        play=lambda *args, **kwargs: sounds.append((args, kwargs)) or True, close=lambda: None)
    speech = SimpleNamespace(available=True, muted=False, last_error=None,
        speak=lambda *args, **kwargs: words.append((args, kwargs)) or True, close=lambda: None)
    wx = SimpleNamespace(Timer=Timer, EVT_TIMER='timer', CallAfter=lambda callback: callback())
    progress = OperationProgress(frame, gauge, label, wx_module=wx, sound=sound, speech=speech)
    progress.configure(ProgressPreferences(mode=mode))
    progress.start('Converting', 10)
    assert (len(sounds), len(words)) == (beep_count, speech_count)
    progress.audio_active = True
    progress.refresh_audio_state()
    progress.succeed()
    assert (len(sounds), len(words)) == (beep_count, speech_count)
    assert sound.muted and speech.muted
    progress.close()
