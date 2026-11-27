"""Real installed reader closures composed with the navigation controller.

Native transport/controls are explicit stand-ins here. Actual PCM/ffplay and
installed Windows keyboard execution have separate qualification gates.
"""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.ui import speech_dialog as ui
from carr_library.ui.playback_navigation import PlaybackNavigation


def reader_hooks(state):
    source = ast.parse(Path(ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    names = {'speak', 'pause_audio', 'stop_audio', 'seek_audio', 'open_project',
             'start_playback', 'playback_navigation'}
    hooks = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    template = ast.parse('''
def factory(state):
    alive, busy, source_loaded = True, False, True
    playback_generation, last_audio = 10, None
    player, progress, wx = state.player, state.progress, state.wx
    document = ReadingDocument('Retained original reading text')
    editor = SimpleNamespace(ChangeValue=lambda text: state.displayed.append(text), SetFocus=lambda: None)
    save_path, draft_store, recovery_notice, source_loader = None, None, '', None
    dialog, cancel = object(), Event()
    audio_files = SimpleNamespace(name=str(state.path.parent))
    registry = SimpleNamespace(synthesize=lambda *args, **kwargs: state.result)
    voices = [SimpleNamespace(id='fixture:reader')]
    voice_choice = SimpleNamespace(GetSelection=lambda: 0)
    rate, volume = SimpleNamespace(GetValue=lambda: 0), SimpleNamespace(GetValue=lambda: 100)
    def source_guard(): pass
    def sync_document(): pass
    def selected_text(from_cursor=False): return document.text
    def report(message, **kwargs): state.notices.append(str(message))
    def run(work, done):
        cancel.clear(); done(work())
    def save_project(): return True
    def placeholder(): pass
    def dispatch(action, *, alive, on_error):
        play_when_quiet((progress,), action, schedule=wx.CallLater, alive=alive, on_error=on_error)
    controller = PlaybackNavigation(player, generation=lambda: playback_generation,
        alive=lambda: alive, stop_current=stop_audio, dispatch=dispatch,
        clock=lambda: state.clock[0], report=report)
    navigation_host = SimpleNamespace(controller=controller, bind=controller.bind,
        invalidate=controller.invalidate, show=lambda: state.shown.append(True))
    state.navigation = navigation_host
    def snapshot(): return playback_generation, last_audio, document
    return SimpleNamespace(speak=speak, pause=pause_audio, stop=stop_audio, seek=seek_audio,
        open=open_project, show=playback_navigation, snapshot=snapshot)
''')
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index + 1] = hooks
    namespace = dict(ui.__dict__, Event=Event, SimpleNamespace=SimpleNamespace,
                     PlaybackNavigation=PlaybackNavigation)
    exec(compile(ast.fix_missing_locations(template), ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def reader(tmp_path):
    path = tmp_path / 'existing.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\0' * 80000)
    selected = tmp_path / 'new-reading.txt'; selected.write_text('Explicit new reading copy', encoding='utf-8')
    class Player:
        state, path, duration_seconds, position_seconds, position_is_approximate = 'stopped', None, 0, 0, True
        def __init__(self): self.calls = []
        def play(self, source):
            self.calls.append(('play', source))
            self.state, self.path, self.duration_seconds, self.position_seconds = 'playing', source, 10.0, 0
        def pause(self): self.calls.append(('pause',)); self.state = 'paused'
        def resume(self): self.calls.append(('resume',)); self.state = 'playing'
        def stop(self): self.calls.append(('stop',)); self.state, self.position_seconds = 'stopped', 0
        def seek(self, seconds): self.calls.append(('seek', seconds)); self.position_seconds = seconds
    state = SimpleNamespace(player=Player(), path=path, selected=selected, displayed=[], shown=[],
        notices=[], pending=[], clock=[100.0], ready=True, picker=lambda: None, entry='3.5')
    class Hold:
        @property
        def ready(self): return state.ready
        def close(self): pass
    class Progress:
        def hold_audio(self): return Hold()
        def refresh_audio_state(self): pass
    state.progress = Progress()
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self): state.picker(); return 1
        def GetValue(self): return state.entry
        def GetPath(self): return str(state.selected)
    state.wx = SimpleNamespace(CallLater=lambda delay, callback: state.pending.append(callback),
        TextEntryDialog=Picker, FileDialog=Picker, ID_OK=1, FD_OPEN=2, FD_FILE_MUST_EXIST=4)
    state.result = SimpleNamespace(path=path, duration_seconds=10, sample_rate=8000, channels=1)
    state.handlers = reader_hooks(state)
    def flush():
        for _ in range(10):
            if not state.pending: return
            callbacks, state.pending = state.pending, []
            for callback in callbacks: callback()
        raise AssertionError('Reader quiet callbacks did not finish within their bounded fixture schedule.')
    state.flush = flush
    return state


def test_reader_explicit_play_binds_once_and_pause_resume_preserve_navigation(reader):
    r = reader
    r.handlers.show()
    assert r.shown == [True] and not r.player.calls
    r.handlers.speak()
    assert r.player.calls == [('stop',), ('play', r.path)]
    controller = r.navigation.controller
    assert controller.snapshot()['active'] and r.handlers.snapshot()[0] == 11
    controller.set_range(1, 2); controller.enable_repeat(True); controller.set_sleep(30)
    deadline = controller.sleep_deadline
    r.handlers.pause(); r.handlers.pause()
    assert r.player.calls[-2:] == [('pause',), ('resume',)]
    assert controller.a == 1 and controller.b == 2 and controller.repeat and controller.sleep_deadline == deadline


def test_sleep_uses_real_reader_stop_to_cancel_its_queued_resume_and_seek(reader):
    r = reader; r.handlers.speak(); r.handlers.pause()
    controller = r.navigation.controller
    controller.set_sleep(1)
    r.ready = False
    r.handlers.pause(); controller.seek_percent(50)
    assert len(r.pending) == 2
    r.clock[0] += 1; controller.tick()
    assert r.handlers.snapshot()[0] == 12 and r.player.state == 'stopped'
    r.ready = True; r.flush()
    assert not any(call[0] in ('resume', 'seek') for call in r.player.calls)
    assert controller.audio is None and controller.sleep_deadline is None
    assert r.notices[-1] == 'Sleep timer stopped the current recording.'


def test_reader_stop_clears_repeat_and_sleep_and_time_seek_does_not_restart(reader):
    r = reader; r.handlers.speak()
    controller = r.navigation.controller
    controller.set_range(1, 2); controller.enable_repeat(True); controller.set_sleep(10)
    r.handlers.stop(); calls = list(r.player.calls)
    r.handlers.seek(); r.clock[0] += 20; controller.tick()
    assert r.player.calls == calls and r.handlers.snapshot()[0] == 12
    assert not controller.repeat and controller.sleep_deadline is None
    assert 'never starts stopped audio' in r.notices[-1]


def test_existing_time_seek_uses_same_bounded_controller_and_preserves_pause(reader):
    r = reader; r.handlers.speak(); r.handlers.pause(); r.handlers.seek()
    assert r.player.calls[-1] == ('seek', 3.5) and r.player.state == 'paused'
    r.entry = 'nan'; calls = list(r.player.calls); r.handlers.seek()
    assert r.player.calls == calls and 'finite' in r.notices[-1]


def test_stop_while_time_picker_is_open_revokes_its_seek(reader):
    r = reader; r.handlers.speak()
    r.picker = r.handlers.stop; r.handlers.seek()
    assert r.player.calls[-1] == ('stop',)
    assert not any(call[0] == 'seek' for call in r.player.calls)


def test_opening_another_copy_discards_pending_old_playback_and_navigation(reader):
    r = reader; r.ready = False; r.handlers.speak()
    assert r.pending and r.player.calls == [('stop',)]
    r.handlers.open(); r.ready = True; r.flush()
    generation, last_audio, document = r.handlers.snapshot()
    assert generation == 12 and last_audio is None and r.navigation.controller.audio is None
    assert document.text == 'Explicit new reading copy' and r.displayed == [document.text]
    assert not any(call[0] == 'play' for call in r.player.calls)


def test_new_reader_play_discards_old_repeat_and_sleep_even_for_same_pcm_path(reader):
    r = reader; r.handlers.speak()
    controller = r.navigation.controller
    controller.set_range(1, 2); controller.enable_repeat(True); controller.set_sleep(1)
    r.handlers.speak(); r.clock[0] += 2; controller.tick()
    assert r.handlers.snapshot()[0] == 12 and r.player.state == 'playing'
    assert controller.audio is not None and controller.a is None and controller.sleep_deadline is None
    assert r.player.calls == [('stop',), ('play', r.path), ('stop',), ('play', r.path)]
