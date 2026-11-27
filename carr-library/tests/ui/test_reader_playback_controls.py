"""Real controller/filesystem routes with explicit native-control doubles."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.transport import PlaybackTransport
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.ui.audio_studio import AudioStudioDialog, StudioController, LocalPlayback
from carr_library.audio.player import WavePlayer
from carr_library.ui.live_reader import LiveReaderDialog
from carr_library.ui.playback_controls import PlaybackControls, _MediaOnUI
from carr_library.ui.reading_desk import ReaderController, ReadingDialog
from test_portable_sync import DeferredExecutor


class Control:
    def __init__(self, value=''):
        self.value, self.alive, self.enabled, self.focus, self.items = value, True, True, 0, []
    def _touch(self):
        assert self.alive, 'A delayed playback callback touched a destroyed native control'
    def Enable(self, value=True):
        self._touch(); self.enabled = value
    def ChangeValue(self, value):
        self._touch(); self.value = value
    def GetValue(self):
        self._touch(); return self.value
    def SetValue(self, value):
        self.ChangeValue(value)
    def GetSelection(self):
        self._touch(); return self.value
    def SetSelection(self, value):
        self.ChangeValue(value)
    def SetFocus(self):
        self._touch(); self.focus += 1
    def SetInsertionPoint(self, value):
        self._touch()
    def SetItems(self, values):
        self._touch(); self.items = values


def pcm(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x01\x00' * 800)
    return path


def pump(view):
    while view.queued:
        view.queued.pop(0)()


@pytest.fixture
def controls(tmp_path):
    value = object.__new__(PlaybackControls)
    value.alive = value.window_alive = True
    value.busy = value._stop_pending = False
    value._stop_done = None
    value.epoch = 0
    value._last_text = None
    value.cancelled = Event()
    value.executor = DeferredExecutor()
    value.queued = []
    value.wx = SimpleNamespace(CallAfter=lambda callback: value.queued.append(callback))
    value._guard = lambda: None
    value.status, value.resume, value.device = Control(), Control(True), Control(0)
    value.device_rows = (None,)
    value.segments = Control()
    value.play_button, value.stop_button = Control(), Control()
    value.buttons, value.native_controls = [value.play_button], []
    value.window = object()
    value.timer = SimpleNamespace(Stop=Mock())
    value.controller = PlaybackTransport(tmp_path, guard=value.check)
    yield value
    if value.alive:
        value.dispose()
    while value.executor.pending:
        value.executor.run()
    pump(value)


def test_real_recording_preparation_is_worker_bound_and_does_not_steal_focus(controls, tmp_path):
    path = pcm(tmp_path / 'recording.wav')
    assert controls.set_source(path)
    assert controls.busy and controls.controller.prepared is None
    controls.executor.run()
    assert controls.busy and controls.controller.prepared.audio.sha256 == sha256(path.read_bytes()).hexdigest()
    pump(controls)
    assert not controls.busy and 'Recording prepared' in controls.status.value
    assert controls.status.focus == 0
    controls.query()
    assert controls.status.focus == 1


def test_cancelled_preparation_never_exposes_or_plays_partial_result(controls, tmp_path):
    assert controls.set_source(pcm(tmp_path / 'recording.wav'), autoplay=True)
    controls.stop()
    controls.stop()
    assert len(controls.executor.pending) == 1
    controls.executor.run()
    pump(controls)
    assert controls.controller.prepared is None and len(controls.executor.pending) == 1
    controls.executor.run()
    pump(controls)
    assert not controls.busy and not controls._stop_pending
    assert controls.controller.player.state == 'stopped'


def test_close_callback_waits_for_confirmed_stop_and_does_not_fire_after_destroy(controls, tmp_path):
    done = Mock()
    controls.set_source(pcm(tmp_path / 'recording.wav'))
    controls.stop(on_stopped=done)
    assert not done.called
    controls.executor.run()
    pump(controls)
    assert not done.called
    controls.executor.run()
    pump(controls)
    done.assert_called_once()
    controls.stop(on_stopped=done)
    controls.dispose()
    controls.executor.run()
    pump(controls)
    done.assert_called_once()


def test_forced_destroy_discards_pending_native_callbacks_and_closes_player(controls, tmp_path):
    controls.set_source(pcm(tmp_path / 'recording.wav'))
    controls.executor.run()
    controls._destroyed(SimpleNamespace(GetEventObject=lambda: controls.window, Skip=Mock()))
    for item in (controls.status, controls.resume, controls.device, controls.segments, *controls.buttons):
        item.alive = False
    pump(controls)
    while controls.executor.pending:
        controls.executor.run()
    assert controls.controller.closed and not controls.window_alive


def test_scope_change_before_ui_completion_discards_result_and_stops(controls, tmp_path):
    controls.set_source(pcm(tmp_path / 'recording.wav'))
    controls.executor.run()
    def changed():
        raise ValueError('New library')
    controls._guard = changed
    pump(controls)
    assert 'changed' in controls.status.value and len(controls.executor.pending) == 1
    controls.executor.run()
    pump(controls)
    assert controls.controller.player.state == 'stopped' and not controls.current()


def test_rejected_real_executor_restores_ui_and_disposal_still_closes(controls):
    controls.executor = ThreadPoolExecutor(max_workers=1)
    controls.executor.shutdown(wait=True)
    assert not controls.submit('Rejected operation', lambda: 'incorrect success')
    assert not controls.busy and controls.play_button.enabled
    controls.dispose()
    assert controls.controller.closed
    controls.executor = DeferredExecutor()  # Fixture cleanup owns no remaining thread.


def test_media_ownership_is_dispatched_on_ui_and_late_request_cannot_acquire():
    ui = get_ident()
    queue, valid, calls = Queue(), [True], []
    progress = SimpleNamespace(prepare_media=lambda **kw: calls.append(get_ident()) or True,
                               set_media_state=lambda **kw: calls.append(get_ident()))
    wx = SimpleNamespace(IsMainThread=lambda: get_ident() == ui, CallAfter=queue.put)
    bridge = _MediaOnUI(wx, progress, lambda: valid[0])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bridge.prepare_media, playback=True, owner='first')
        queue.get(timeout=1)()
        assert future.result(timeout=1) and calls == [ui]
        future = executor.submit(bridge.prepare_media, playback=True, owner='stale')
        callback = queue.get(timeout=1)
        valid[0] = False
        callback()
        assert not future.result(timeout=1) and calls == [ui]


def test_reordered_old_media_release_cannot_unmute_a_new_playback_generation():
    queue, owned = Queue(), {}
    progress = SimpleNamespace(prepare_media=lambda **kw: owned.update({kw['owner']: True}) or True,
                               set_media_state=lambda **kw: owned.pop(kw['owner'], None))
    wx = SimpleNamespace(IsMainThread=lambda: False, CallAfter=queue.put)
    bridge = _MediaOnUI(wx, progress, lambda: True)
    bridge.set_media_state(owner='player')
    old_release = queue.get(timeout=1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(bridge.prepare_media, playback=True, owner='player')
        queue.get(timeout=1)()
        assert future.result(timeout=1)
    old_release()
    assert owned == {'player': True}


@pytest.mark.parametrize('managed', [False, True])
def test_studio_routes_exact_master_to_shared_player_with_owned_resume_only(tmp_path, managed):
    app = LibraryApp(tmp_path / 'library')
    controller = StudioController(private_root=app.root)
    controller.new('Literal source')
    saved = controller.save_in_library() if managed else controller.save(tmp_path / 'external.json')
    output = pcm(tmp_path / 'master.wav')
    controller.result = {'output': str(output), 'sample_rate': 8000,
                         'chapters': [{'start_frame': 0, 'title': 'Synthetic chapter'}]}
    dialog = object.__new__(AudioStudioDialog)
    dialog.busy = dialog.editing_dirty = False
    dialog.controller, dialog._current = controller, lambda: True
    dialog._playback_source = None
    received = []
    dialog.playback_controls = SimpleNamespace(set_source=lambda path, **kw: received.append((path, kw)),
                                               play_button=Control())
    dialog.notebook = SimpleNamespace(SetSelection=Mock())
    dialog._error = lambda error: pytest.fail(str(error))
    dialog.play()
    path, values = received[0]
    assert path == output and values['autoplay'] and values['segments'] == ((0, 'Synthetic chapter'),)
    values['authorize']()
    assert (values['owner'] is not None) == managed
    assert LocalPlayback is WavePlayer
    if managed:
        assert values['owner'].project == controller.project_id
        saved.write_bytes(saved.read_bytes() + b' ')
        with pytest.raises(ValueError, match='changed'):
            values['authorize']()


def test_reader_entry_uses_real_fresh_source_and_native_prefix_text(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reader')
    raw = 'First 😀\r\nSelected text.\r\nEnd.'
    book = app.add_text(workspace, 'Source', raw)
    reader = ReaderController(ReadingDesk(app, workspace, book['edition']), ReadingState(app.root))
    reader.start()
    dialog = object.__new__(ReadingDialog)
    dialog.closed = dialog.busy = False
    dialog._window_alive = True
    dialog.controller, dialog.dialog, dialog.parent = reader, object(), object()
    dialog.dialogs = SimpleNamespace(choose=lambda *args: 1)
    # These are deliberately not Python offsets; GetRange provides actual text.
    dialog.text = SimpleNamespace(GetSelection=lambda: (100, 200), GetInsertionPoint=lambda: 200,
        GetRange=lambda first, last: 'First 😀\n' if last == 100 else 'First 😀\nSelected text.',
        GetValue=lambda: raw.replace('\r\n', '\n'), SetFocus=Mock())
    selections = []
    def opened(parent, frame, controller, selection):
        selections.append(selection)
        return SimpleNamespace(show=lambda: None)
    monkeypatch.setattr('carr_library.ui.live_reader.LiveReaderDialog', opened)
    dialog._run = lambda title, work, done: done(work())
    dialog._error = lambda error: pytest.fail(str(error))
    dialog.read_aloud()
    assert selections[0].text == 'Selected text.'
    assert selections[0].owner.source == reader.current.location
    assert app.assets.resolve(book['asset']).read_bytes().decode() == raw


@pytest.fixture
def live():
    value = object.__new__(LiveReaderDialog)
    value.alive = value.window_alive = True
    value.busy = value.close_pending = False
    value._epoch = 0
    value.cancelled, value._playback_stopped = Event(), Event()
    value.executor, value.queued = DeferredExecutor(), []
    value.wx = SimpleNamespace(CallAfter=lambda callback: value.queued.append(callback), ID_CANCEL=2)
    value.frame, value.reader = SimpleNamespace(), SimpleNamespace(check=lambda: None)
    value.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=Mock())
    value.status, value.cancel = Control(), Control()
    value.controls = [Control(), Control()]
    value.voice_changed = lambda: None
    value.playback = SimpleNamespace(quiesce=lambda: None, busy=False,
        dispose=lambda on_stopped: on_stopped(), set_source=Mock())
    value.narrator, value.measured = SimpleNamespace(close=Mock()), None
    yield value
    if value.alive:
        value._dispose()
    while value.executor.pending:
        value.executor.run()
    pump(value)


def test_live_reader_success_and_old_status_cannot_overwrite_next_job(live):
    done = Mock()
    live._run('First', lambda report: report('Old status') or 'first result', done)
    live.executor.run()
    # Deliberately reorder dispatcher delivery to expose stale producer notices.
    completed = live.queued.pop()
    completed()
    done.assert_called_once_with('first result')
    live._run('Second', lambda report: 'second result', done)
    pump(live)
    assert live.status.value == 'Second'
    live.executor.run()
    pump(live)
    assert done.call_args.args == ('second result',)


def test_live_reader_forced_destroy_discards_all_late_controls_and_autoplay(live):
    done = Mock()
    live._run('Rendering', lambda report: report('Late') or {'status': 'complete'}, done)
    live.executor.run()
    live._destroyed(SimpleNamespace(GetEventObject=lambda: live.dialog, Skip=Mock()))
    for control in (*live.controls, live.status, live.cancel):
        control.alive = False
    pump(live)
    assert not done.called and live.executor.stopped and not live.alive
    live.executor.run()
    live.narrator.close.assert_called_once()


def test_live_reader_close_cancels_then_waits_for_worker_before_ending_modal(live):
    done = Mock()
    live._run('Rendering', lambda report: {'status': 'complete'}, done)
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    live.close(event)
    assert live.cancelled.is_set() and live.close_pending and not live.dialog.EndModal.called
    live.executor.run()
    pump(live)
    assert not done.called and not live.alive
    live.dialog.EndModal.assert_called_once()
