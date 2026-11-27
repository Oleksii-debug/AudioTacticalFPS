"""Studio progress, output acceptance and media handoff through real handlers.

Native controls and the audible player are explicit doubles. The shared audio
worker's child-process quiescence is exercised separately in test_progress.
"""
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.ui.audio_studio import AudioStudioDialog
from test_audio_studio import controller
from test_operation_progress import panel
from test_portable_sync import DeferredExecutor


class Control:
    def __init__(self):
        self.alive, self.enabled, self.value, self.focus = True, True, '', 0
    def Enable(self, value=True):
        assert self.alive, 'A late Studio callback touched a deleted control'
        self.enabled = value
    def ChangeValue(self, value):
        assert self.alive, 'A late Studio status touched a deleted control'
        self.value = value
    def SetFocus(self):
        assert self.alive, 'A late Studio callback took deleted-window focus'
        self.focus += 1


@pytest.fixture
def view(controller, panel, tmp_path):
    value = object.__new__(AudioStudioDialog)
    value.parent = SimpleNamespace(library=SimpleNamespace(root=tmp_path),
        progress_actions=SimpleNamespace(panel=panel), _closing=False)
    value.controller = controller
    value.busy = value.closed = value.close_pending = value._disposed = False
    value._window_alive, value._job_epoch = True, 0
    value._owner_scope = value._scope_identity()
    value._media_panel, value._operation_progress = panel, None
    value.editing_dirty = False
    value.cancelled = Event()
    value.executor = DeferredExecutor()
    value.queued = []
    value.wx = SimpleNamespace(CallAfter=lambda callback, *args: value.queued.append(
        lambda: callback(*args)), ID_CANCEL=2)
    value.dialog = SimpleNamespace(IsModal=lambda: False, Hide=Mock(), Destroy=Mock())
    value.timer = SimpleNamespace(Stop=Mock())
    value.playback = SimpleNamespace(play=Mock(), stop=Mock())
    value.status, value.cancel_button = Control(), Control()
    value.controls = [Control(), Control()]
    value._unsaved = lambda: True
    yield value
    value._dispose()


def pump(view):
    while view.queued:
        view.queued.pop(0)()


def test_render_status_is_bounded_and_completion_waits_for_actual_controller_acceptance(view, panel, tmp_path):
    view.controller.new('Exact synthetic narration.')
    view.controller.configure(voice='test:local', output_dir=tmp_path)
    callbacks, accepted = [], []
    output = tmp_path / 'render.wav'
    def renderer(plan, folder, **options):
        callbacks.append(options['on_status'])
        for count in range(100):
            options['on_status'](f'Rendering chunk {count}')
        with wave.open(str(output), 'wb') as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(8000)
            writer.writeframes(b'\0\0' * 800)
        return {'status': 'complete', 'output': str(output), 'duration_seconds': 0.1}
    view.controller.renderer = renderer
    view._run('Render narration', lambda status: view.controller.render(on_status=status),
              accepted.append, reports_status=True)
    view.executor.run()
    assert len(view.queued) == 2  # One latest status and one completion, not 100 callbacks.
    event, = panel.hub.snapshot()
    assert event.total is None and event.percent is None and event.phase == 'Rendering chunk 99'
    callbacks[0]('Stale producer message after return')
    assert len(view.queued) == 2 and panel.hub.snapshot() == (event,)
    pump(view)
    assert accepted and Path(accepted[0]['output']).read_bytes()[:4] == b'RIFF'
    assert panel.hub.snapshot()[-1].percent == 100
    assert not view.busy and all(control.enabled for control in view.controls)


def test_missing_render_file_fails_before_progress_can_be_completed(view, panel, tmp_path):
    view.controller.new('Narration.')
    view.controller.configure(output_dir=tmp_path)
    view.controller.renderer = lambda *args, **kwargs: {'status': 'complete', 'output': str(tmp_path / 'missing.wav')}
    accepted = Mock()
    view._run('Render', lambda: view.controller.render(), accepted)
    view.executor.run()
    pump(view)
    assert not accepted.called and 'expected file' in view.status.value
    assert panel.hub.snapshot()[-1].state == 'failed'


def test_rejected_worker_start_restores_controls_and_progress(view, panel):
    view.executor.submit = Mock(side_effect=RuntimeError('Executor has shut down'))
    view._run('Reload voices', lambda: (), Mock())
    assert not view.busy and not view.cancel_button.enabled
    assert all(control.enabled for control in view.controls)
    assert panel.hub.snapshot()[-1].state == 'failed'
    assert 'shut down' in view.status.value


def test_pending_close_cancels_then_closes_after_worker_stops(view, panel):
    accepted = Mock()
    view._run('Render', lambda: (), accepted)
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    view.close(event)
    assert view.busy and view.close_pending and view.cancelled.is_set()
    event.Veto.assert_called_once()
    assert not view.dialog.Hide.called
    view.executor.run()
    pump(view)
    assert view.closed and view.dialog.Hide.called and not accepted.called
    assert panel.hub.snapshot()[-1].state == 'cancelled'


def test_forced_destroy_suppresses_every_queued_status_and_completion(view, panel):
    accepted = Mock()
    view._run('Render', lambda status: status('A final queued phase') or (), accepted,
              reports_status=True)
    view.executor.run()
    event = SimpleNamespace(GetEventObject=lambda: view.dialog, Skip=Mock())
    view._destroyed(event)
    for control in (*view.controls, view.status, view.cancel_button):
        control.alive = False
    pump(view)
    assert view.closed and not view._window_alive and view.executor.stopped
    assert not accepted.called and panel.hub.snapshot()[-1].state == 'cancelled'


def test_root_replacement_discards_old_result_without_changing_new_scope(view, panel, tmp_path):
    accepted = Mock()
    view._run('Read project', lambda: 'old project', accepted)
    view.executor.run()
    new_root = tmp_path / 'new-library'
    new_root.mkdir()
    view.parent.library = SimpleNamespace(root=new_root)
    pump(view)
    assert not accepted.called and not view.busy
    assert 'data folder changed' in view.status.value
    assert panel.hub.snapshot()[-1].state == 'cancelled'


def test_playback_requires_quiet_acknowledgement_and_releases_only_its_own_owner(view, panel, tmp_path):
    view.controller.result = {'output': str(tmp_path / 'master.wav')}
    panel.feedback.wait_quiet = lambda timeout=1: False
    view.play()
    assert not view.playback.play.called and 'has not started' in view.status.value
    assert panel.feedback.media == (False, False)
    panel.feedback.wait_quiet = lambda timeout=1: True
    seen = []
    view.playback.play.side_effect = lambda path: seen.append(panel.feedback.media)
    view.play()
    assert seen == [(True, False)]
    another_owner = object()
    panel.set_media_state(recording=True, owner=another_owner)
    view.stop_playback()
    assert panel.feedback.media == (False, True)
    panel.set_media_state(owner=another_owner)


def test_failed_playback_releases_suppression_after_the_player_confirms_stop(view, panel, tmp_path):
    view.controller.result = {'output': str(tmp_path / 'missing.wav')}
    view.playback.play.side_effect = ValueError('Invalid audio file')
    view.play()
    assert 'Invalid audio' in view.status.value
    assert panel.feedback.media == (False, False)


def test_failed_stop_retains_suppression_and_forced_close_still_disposes_worker(view, panel):
    panel.set_media_state(playback=True, owner=view)
    view.playback.stop.side_effect = OSError('Output has not stopped')
    view.stop_playback()
    assert panel.feedback.media == (True, False)
    view._destroyed(SimpleNamespace(GetEventObject=lambda: view.dialog, Skip=Mock()))
    assert view.executor.stopped and view.closed
    assert panel.feedback.media == (True, False)
