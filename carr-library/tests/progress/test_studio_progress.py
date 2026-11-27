"""Actual Studio persistence/FFmpeg with labelled synthetic PCM voice fixtures.

The native controls are explicit doubles here. Windows layout and event-loop
acceptance belong to probe_studio_native; no listening or NVDA claim is made.
"""
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.progress import ProgressHub
from carr_library.progress_feedback import ProgressFeedback
from carr_library.progress_preferences import ProgressPreferences
from carr_library.studio import Budget, Line, Project, ProjectStore, Role, Scene
from carr_library.studio import director as director_module
from carr_library.studio import progress as render_progress
from carr_library.studio.store import digest_file
from carr_library.ui.progress_panel import NativeProgressPanel
from carr_library.ui.studio_actions import StudioDialog


@pytest.fixture
def studio(tmp_path, monkeypatch):
    controller = object.__new__(StudioDialog)
    controller.store = ProjectStore(tmp_path / 'project')
    project = Project('Measured render', (Role('n', 'fixture:voice'),),
        (Scene('one', 'One', (Line('a', 'n', 'Exact first text.'),
                              Line('b', 'n', 'Exact second text.'))),),
        budget=Budget(chunk_characters=32))
    controller.store.save(project)
    calls = []

    def speech(request, target, cancelled=None):
        if cancelled:
            cancelled()
        calls.append(request.text)
        with wave.open(str(target), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
            output.writeframes(b'\x00\x00' * 2400)
        return dict(path=str(target), frames=2400, sample_rate=24000, channels=1,
            duration_seconds=.1, sha256=digest_file(target), engine_fingerprint='explicit-pcm-fixture')

    actual_director = director_module.Director
    configured = []
    def engine(store, *, configuration_root):
        configured.append(configuration_root)
        return actual_director(store, speech_renderer=speech,
                               engine_identity=lambda _: 'explicit-pcm-fixture')
    monkeypatch.setattr(director_module, 'Director', engine)
    controller.configuration_root = tmp_path
    controller.dialog = SimpleNamespace(working=False)
    controller.cancel, controller.pause = Event(), Event()
    controller.closed = controller.busy = False
    controller.result = controller._progress_revision = None
    controller.save = controller.store.load
    controller.say, controller._editing = Mock(), Mock()
    controller.executor = ThreadPoolExecutor(max_workers=1)
    callbacks = Queue()
    controller.wx = SimpleNamespace(CallAfter=callbacks.put)
    panel = object.__new__(NativeProgressPanel)
    panel.hub = ProgressHub()
    panel.active_task = None
    panel.refresh, panel.set_busy = Mock(), Mock()
    controller.progress_panel = panel
    try:
        yield controller, callbacks, calls, configured
    finally:
        controller.cancel.set()
        controller.executor.shutdown(wait=True, cancel_futures=True)


def completion(callbacks):
    return callbacks.get(timeout=30)


def test_studio_100_follows_actual_committed_result_and_known_speech_units(studio):
    controller, callbacks, calls, configured = studio
    controller.render()
    complete = completion(callbacks)
    snapshot = controller.progress_panel.active_task.snapshot()
    assert (snapshot.completed, snapshot.total, snapshot.unit) == (2, 2, 'speech chunks')
    assert snapshot.phase == 'Complete' and snapshot.percent == 99
    assert controller.busy and controller.result is None
    job = controller.store.jobs()[0]
    assert job['state'] == 'complete'
    assert Path(job['result']['master']['path']).is_file()
    assert Path(job['result']['manifest_path']).is_file()
    assert Path(job['result']['captions_path']).is_file()
    complete()
    assert controller.progress_panel.active_task.snapshot().percent == 100
    assert controller.progress_panel.active_task.snapshot().state == 'succeeded'
    assert json.loads(json.dumps(controller.result, default=str)) == job['result'] and not controller.busy
    assert calls == ['Exact first text.', 'Exact second text.']
    assert configured == [controller.configuration_root]


def test_pause_acknowledgement_and_resume_remeasures_verified_cached_units(studio, monkeypatch):
    controller, callbacks, calls, _ = studio
    actual = render_progress.report_render_progress
    observed, stop_once = [], [True]
    def observe(state):
        actual(state)
        observed.append(controller.progress_panel.active_task.snapshot())
        if state['speech_chunks_done'] == 1 and stop_once[0]:
            stop_once[0] = False
            controller.pause.set()
    monkeypatch.setattr(render_progress, 'report_render_progress', observe)
    controller.render()
    apply_pause = completion(callbacks)
    task = controller.progress_panel.active_task
    assert task.snapshot().state == 'running'
    assert controller.store.jobs()[0]['state'] == 'paused'
    apply_pause()
    assert task.snapshot().state == 'paused' and task.snapshot().percent == 50
    assert task.snapshot().eta_seconds is None and controller.result is None
    controller.render()
    completion(callbacks)()
    resumed = controller.progress_panel.active_task
    assert resumed is not task and task.snapshot().state == 'paused'
    assert resumed.snapshot().state == 'succeeded' and resumed.snapshot().percent == 100
    assert calls == ['Exact first text.', 'Exact second text.']
    for attempt in (task, resumed):
        counts = [value.completed for value in observed if value.operation_id == attempt.operation_id]
        assert counts == sorted(counts)
    assert any(value.phase == 'Finalizing audio' and value.percent == 99 for value in observed)


def test_cancellation_is_acknowledged_after_actual_retained_checkpoint(studio, monkeypatch):
    controller, callbacks, calls, _ = studio
    actual = render_progress.report_render_progress
    def observe(state):
        actual(state)
        if state['speech_chunks_done'] == 1:
            controller.cancel.set()
            controller.progress_panel.active_task.request_cancel()
    monkeypatch.setattr(render_progress, 'report_render_progress', observe)
    controller.render()
    acknowledge = completion(callbacks)
    task = controller.progress_panel.active_task
    assert task.snapshot().state == 'cancelling'
    assert controller.store.jobs()[0]['state'] == 'cancelled'
    acknowledge()
    assert task.snapshot().state == 'cancelled' and task.snapshot().percent == 50
    assert controller.result is None and calls == ['Exact first text.']
    assert len(tuple(controller.store.cache.glob('*.wav'))) == 1


def test_final_publication_error_never_turns_complete_units_into_success(studio, monkeypatch):
    controller, callbacks, _, _ = studio
    actual = director_module._write_once
    def write(path, text):
        if path.name == 'captions.srt':
            raise OSError('Synthetic captions publication failure')
        return actual(path, text)
    monkeypatch.setattr(director_module, '_write_once', write)
    controller.render()
    completion(callbacks)()
    snapshot = controller.progress_panel.active_task.snapshot()
    assert snapshot.state == 'failed' and snapshot.percent == 99
    assert 'captions publication failure' in snapshot.message
    assert controller.result is None and controller.store.jobs()[0]['state'] == 'failed'
    assert not controller.busy


def test_broken_observer_does_not_reverse_durable_studio_success(studio, monkeypatch):
    controller, callbacks, _, _ = studio
    actual_begin = controller.progress_panel.begin
    def begin(*args, **kwargs):
        task = actual_begin(*args, **kwargs)
        monkeypatch.setattr(task, 'update', Mock(side_effect=OSError('Synthetic observer failure')))
        return task
    monkeypatch.setattr(controller.progress_panel, 'begin', begin)
    controller.render()
    completion(callbacks)()
    assert controller.progress_panel.active_task.snapshot().state == 'unavailable'
    assert controller.store.jobs()[0]['state'] == 'complete'
    assert Path(controller.result['master']['path']).is_file()
    assert 'Render complete.' in controller.say.call_args.args[0]


def test_executor_rejection_keeps_saved_project_and_releases_controls(studio, monkeypatch):
    controller, _, calls, _ = studio
    original = controller.store.load()
    monkeypatch.setattr(controller.executor, 'submit', Mock(side_effect=RuntimeError('Synthetic closed executor')))
    controller.render()
    assert not controller.busy and not controller.dialog.working and not calls
    assert controller.store.load() == original
    assert controller.progress_panel.active_task.snapshot().state == 'failed'
    assert not controller.store.jobs()


@pytest.mark.parametrize('cancel_before_play', (False, True))
def test_playback_waits_for_real_feedback_worker_without_blocking_ui(cancel_before_play):
    entered, release, finished = Event(), Event(), Event()
    class Sink:
        def play(self, tone, *, cancelled):
            entered.set()
            assert release.wait(3)
            assert cancelled()
            finished.set()
    feedback = ProgressFeedback(ProgressPreferences(mode='beep'), tone_sink=Sink())
    controller = object.__new__(StudioDialog)
    controller.closed = controller._playback_pending = False
    controller._playback_generation = 0
    controller.playback_timer = None
    controller.say = Mock()
    controller.progress_panel = SimpleNamespace(feedback=feedback, preferences=feedback.preferences)
    scheduled = []
    def later(milliseconds, callback):
        scheduled.append(callback)
        return SimpleNamespace(Stop=lambda: None)
    controller.wx = SimpleNamespace(CallLater=later)
    action = Mock()
    try:
        feedback.update(ProgressHub().start('Notification fixture', total=2).snapshot())
        assert entered.wait(1)
        controller._start_playback(action)
        assert scheduled and controller._playback_pending
        action.assert_not_called()
        if cancel_before_play:
            controller._stop_pending_playback()
        release.set()
        assert finished.wait(2)
        # The production callback yields again if the worker is still returning.
        for _ in range(10):
            if feedback.quiescent:
                break
            Event().wait(.01)
        scheduled[-1]()
        assert action.call_count == (0 if cancel_before_play else 1)
        assert not controller._playback_pending
    finally:
        release.set()
        feedback.close()


@pytest.mark.parametrize('changed_library', (False, True))
def test_discovery_completion_queues_modal_after_host_task_finishes(tmp_path, changed_library):
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_actions.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == 'open_studio')
    queue, phases = [], []
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), cancel_event=Event(),
                            phase='running', report=Mock())
    def run(title, operation, done):
        voices = operation()
        done(voices)
        assert not phases
        frame.phase = 'succeeded'
    frame.run_job = run
    voice = SimpleNamespace(id='fixture:voice')
    discover = Mock(return_value=(voice,))
    def dialog(parent, voices, **kwargs):
        phases.append(parent.phase)
        assert voices == (voice,) and kwargs['configuration_root'] == tmp_path
        return SimpleNamespace(show=Mock())
    namespace = {'frame': frame, 'discover_voices': discover, 'StudioDialog': dialog,
                 'wx': SimpleNamespace(CallAfter=lambda function, *args: queue.append((function, args)))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    namespace['open_studio']()
    assert frame.phase == 'succeeded' and not phases
    if changed_library:
        frame.library = SimpleNamespace(root=tmp_path / 'different-library')
    function, args = queue.pop()
    function(*args)
    assert phases == ([] if changed_library else ['succeeded'])
    assert frame.report.call_count == int(changed_library)
    discover.assert_called_once_with(configuration_root=tmp_path, cancelled=frame.cancel_event.is_set)
