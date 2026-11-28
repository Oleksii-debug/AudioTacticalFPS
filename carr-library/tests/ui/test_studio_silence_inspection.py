"""Completed-audio inspection through the actual Studio callbacks.

The controls are modeled here; the separate existing Windows probe drives native
widgets. Actual PCM/FFmpeg acceptance is explicit below, never a voice-quality claim.
"""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import runpy
import shutil
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner, retained_context
from carr_library.ui import studio_dialog
from carr_library.ui import studio_silence_inspection as inspection
from carr_library.ui.operation_progress import operation_outcome


def report_fixture():
    return dict(schema='carr-silence-inspection/1', status='complete', job_id='completed-job',
        source=dict(artifact='master.wav', sha256='a' * 64, size=16044),
        audio=dict(sample_rate=8000, channels=1, frames=8000, source_codec='pcm_s16le'),
        criteria=dict(threshold_dbfs=-50.0, minimum_seconds=.25, minimum_frames=2000,
            amplitude_limit_s32=6790939, channel_policy='every-channel',
            comparison='absolute-sample-at-or-below'),
        intervals=[dict(start_frame=2000, end_frame=6000, start_seconds=.25,
            end_seconds=.75, duration_seconds=.5)],
        interval_count=1, omitted_count=0, pcm_sha256='b' * 64,
        notice='Synthetic signal measurement; no perceptual quality verdict.')


class Control:
    def __init__(self, value='', *, owner=None):
        self.value, self.enabled, self.owner = value, True, owner

    def check_thread(self):
        if self.owner is not None:
            assert threading.get_ident() == self.owner, 'A native control was touched on the worker.'

    def Enable(self, enabled=True):
        self.check_thread()
        self.enabled = bool(enabled)

    def SetLabel(self, label):
        self.check_thread()
        self.label = label

    def SetName(self, name):
        self.check_thread()
        self.name = name

    def GetValue(self):
        self.check_thread()
        return self.value

    def GetSelection(self):
        self.check_thread()
        return -1

    def ChangeValue(self, value):
        self.check_thread()
        self.value = value


class DeferredExecutor:
    """A real worker executes only when the test admits its captured callback."""
    def __init__(self):
        self.requests, self.shutdowns, self.failure = [], [], None

    def submit(self, operation):
        if self.failure is not None:
            raise self.failure
        future = Future()
        self.requests.append((future, operation))
        return future

    def execute(self):
        future, operation = self.requests[-1]
        if not future.set_running_or_notify_cancel():
            return
        def work():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='inspection-test-worker') as executor:
            executor.submit(work).result(timeout=30)

    def shutdown(self, *, wait, cancel_futures):
        self.shutdowns.append((wait, cancel_futures))
        if cancel_futures:
            for future, _ in self.requests:
                future.cancel()


@pytest.fixture
def studio(tmp_path, monkeypatch):
    source_path = Path(studio_dialog.__file__)
    source = ast.parse(source_path.read_text(encoding='utf-8'))
    outer = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'show_studio')
    wanted = {'run', 'controls', 'close', 'inspect_long_silences', 'inspection_owner_destroyed'}
    callbacks = [n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {n.name for n in callbacks} == wanted
    owner = threading.get_ident()
    project, library = object(), SimpleNamespace(root=tmp_path / 'library')
    library.root.mkdir()
    frame = SimpleNamespace(library=library, _closing=False)
    controller = SimpleNamespace(project=project)
    editor, title = Control('Unchanged source script', owner=owner), Control('Review', owner=owner)
    buttons = {name: Control(owner=owner) for name in
        ('render', 'open', 'roles', 'jobs', 'pause', 'preview', 'progress_voice', 'inspect_silence')}
    service = Mock()
    service.inspect_silence.return_value = report_fixture()
    executor, queued, reports, shown, criteria_calls = DeferredExecutor(), [], [], [], []
    progress_module = runpy.run_path(str(Path(__file__).with_name('test_studio_progress_isolation.py')))
    class ProgressView(progress_module['Progress']):
        def pause(self, phase, *, token=None):
            super().pause(phase, token=self.tracker.event.token if token is None else token)
        def cancel(self, phase, *, token=None):
            super().cancel(phase, token=self.tracker.event.token if token is None else token)
        def close(self):
            self.closed = True
    progress = ProgressView()
    dialog = SimpleNamespace(_audio_player=None, EndModal=Mock())
    timer = SimpleNamespace(Stop=Mock())
    wx = SimpleNamespace(ID_CLOSE=321, CallAfter=lambda callback, *args: queued.append(lambda: callback(*args)))
    factory = ast.parse('''
def make():
    busy, alive, active_production = False, True, False
    inspection_generation, inspection_owner_id = 0, 7301
    current_job = 'completed-job'
    cancel = threading.Event()
    save_library, save_root = frame.library, frame.library.root
    parent_progress = None
    voices = ()
    voice_choice = Control(owner=owner)
    format_choice = Control(owner=owner)
    stems = Control(owner=owner)
    autosave = lambda: None
    def editor_signature():
        return (editor.GetValue(), title.GetValue())
    def select_job(identity):
        nonlocal current_job
        current_job = identity
    def mutate(kind):
        nonlocal current_job, inspection_generation
        if kind == 'job':
            current_job = 'different-job'
        elif kind == 'project':
            controller.project = object()
        elif kind == 'library':
            frame.library = SimpleNamespace(root=save_root)
        elif kind == 'root':
            frame.library.root = save_root / 'changed'
        elif kind == 'closing':
            frame._closing = True
        elif kind == 'editor':
            editor.value = 'Changed draft'
        elif kind == 'generation':
            inspection_generation += 1
        elif kind == 'cancel':
            cancel.set()
        else:
            raise AssertionError(kind)
    return SimpleNamespace(inspect=inspect_long_silences, run=run, close=close,
        destroy=inspection_owner_destroyed, mutate=mutate, select_job=select_job, cancel=cancel,
        state=lambda: (busy, alive, active_production), controls=controls)
''')
    factory.body[0].body[-1:-1] = callbacks
    env = dict(threading=threading, SimpleNamespace=SimpleNamespace, Control=Control,
        owner=owner, frame=frame, controller=controller, editor=editor, title=title,
        buttons=buttons, service=service, executor=executor, progress=progress,
        dialog=dialog, timer=timer, wx=wx, operation_outcome=operation_outcome,
        report=lambda message, focus=False: reports.append(str(message)),
        __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(source_path), 'exec'), env)
    handler = env['make']()
    criteria = inspection.criteria_from_text('-50', '.25')
    def choose(parent, *, current):
        assert threading.get_ident() == owner and current()
        criteria_calls.append(parent)
        return criteria
    def show(parent, result, *, current):
        assert threading.get_ident() == owner and current()
        assert progress.tracker.event.percent != 100
        shown.append(deepcopy(result))
    monkeypatch.setattr(inspection, 'choose_criteria', choose)
    monkeypatch.setattr(inspection, 'show_result', show)
    value = SimpleNamespace(handler=handler, service=service, executor=executor, queued=queued,
        reports=reports, shown=shown, criteria_calls=criteria_calls, criteria=criteria,
        progress=progress, dialog=dialog, controller=controller, frame=frame, library=library,
        editor=editor, buttons=buttons, owner=owner)
    yield value
    handler.cancel.set()
    for future, _ in executor.requests:
        future.cancel()


def drain(studio):
    while studio.queued:
        studio.queued.pop(0)()


@pytest.mark.parametrize(('threshold', 'minimum'), [
    ('-192', '.000001'), ('0', '31536000'), ('-50', '2'), ('-6.5', '0.25'),
])
def test_criteria_uses_the_actual_shared_finite_bounds(threshold, minimum):
    result = inspection.criteria_from_text(threshold, minimum)
    from carr_library.audio.silence import SilenceCriteria
    assert result == SilenceCriteria(float(threshold), float(minimum))


@pytest.mark.parametrize(('threshold', 'minimum'), [
    ('nan', '2'), ('inf', '2'), ('-inf', '2'), ('-193', '2'), ('0.01', '2'),
    ('word', '2'), ('-50', 'nan'), ('-50', 'inf'), ('-50', '0'), ('-50', '-1'),
    ('-50', '31536001'), ('-50', ''), ('', '2'),
])
def test_invalid_criteria_never_become_a_worker_request(threshold, minimum):
    with pytest.raises(ValueError):
        inspection.criteria_from_text(threshold, minimum)


def test_results_use_seconds_and_explicit_bounded_total_without_changing_exact_frames():
    value = report_fixture()
    value['interval_count'], value['omitted_count'] = 7, 6
    original = deepcopy(value)
    text = inspection.result_text(value)
    assert '0.250000 to 0.750000 seconds; duration 0.500000 seconds' in text
    assert 'Matching intervals: 7; shown: 1; omitted: 6.' in text
    assert 'Additional matching intervals are omitted' in text
    assert 'Every channel must be at or below the threshold.' in text
    assert 'Sample rate: 8000 Hz; total frames: 8000.' in text
    assert 'frames [2000, 6000)' in text and ('a' * 64) in text
    assert 'not a quality verdict' in text
    assert value == original


def test_no_matching_interval_never_reports_quality_acceptance():
    value = report_fixture()
    value.update(intervals=[], interval_count=0, omitted_count=0)
    text = inspection.result_text(value)
    assert 'No interval met the selected threshold and minimum duration.' in text
    assert 'No matching interval means only that none met these criteria.' in text
    assert 'PASS' not in text and 'quality verdict' in text


@pytest.mark.parametrize('change', ['cap', 'negative', 'wrong_total', 'bool', 'wrong_schema'])
def test_report_contract_refuses_unbounded_or_inconsistent_results(change):
    value = report_fixture()
    if change == 'cap':
        value['intervals'] *= inspection.MAX_SILENCE_INTERVALS + 1
        value['interval_count'] = len(value['intervals'])
    elif change == 'negative':
        value['omitted_count'] = -1
    elif change == 'wrong_total':
        value['interval_count'] = 2
    elif change == 'bool':
        value['interval_count'] = True
    else:
        value['schema'] = 'foreign'
    with pytest.raises(ValueError):
        inspection.result_text(value)


def test_actual_callbacks_use_the_existing_worker_and_complete_only_after_review(studio):
    s = studio
    workers = []
    def inspect(job, **kwargs):
        workers.append(threading.get_ident())
        assert job == 'completed-job'
        assert kwargs['threshold_dbfs'] == -50 and kwargs['minimum_seconds'] == .25
        assert not kwargs['cancelled']()
        kwargs['progress']('Inspecting PCM without modifying it')
        return report_fixture()
    s.service.inspect_silence.side_effect = inspect
    s.handler.inspect()
    assert s.handler.state() == (True, True, False)
    assert s.buttons['pause'].enabled and not s.buttons['inspect_silence'].enabled
    assert s.buttons['pause'].label == s.buttons['pause'].name == 'Cancel inspection (F8)'
    assert s.service.inspect_silence.call_count == 0 and not s.shown
    s.executor.execute()
    assert workers and workers[0] != s.owner
    assert not s.shown and s.progress.tracker.event.percent != 100
    drain(s)
    assert s.shown == [report_fixture()]
    assert s.handler.state() == (False, True, False)
    assert s.progress.tracker.event.state == 'succeeded'
    assert s.progress.tracker.event.percent == 100
    assert s.buttons['pause'].label == '&Pause (F8)' and s.buttons['pause'].name == 'Pause (F8)'
    assert s.service.request_pause.call_count == s.service.close.call_count == 0
    assert s.dialog._audio_player is None


@pytest.mark.parametrize('boundary', ['modal', 'queued', 'completed'])
@pytest.mark.parametrize('change', ['job', 'project', 'library', 'root', 'closing', 'generation'])
def test_fresh_request_and_late_result_never_cross_current_owner(studio, monkeypatch, boundary, change):
    s = studio
    if boundary == 'modal':
        def choose(parent, *, current):
            assert current()
            s.handler.mutate(change)
            return s.criteria
        monkeypatch.setattr(inspection, 'choose_criteria', choose)
    s.handler.inspect()
    if boundary == 'modal':
        assert not s.executor.requests
    elif boundary == 'queued':
        s.handler.mutate(change)
        s.executor.execute()
        drain(s)
        s.service.inspect_silence.assert_not_called()
    else:
        s.executor.execute()
        s.handler.mutate(change)
        drain(s)
        s.service.inspect_silence.assert_called_once()
    assert not s.shown and s.dialog._audio_player is None
    s.service.request_pause.assert_not_called()
    s.service.close.assert_not_called()
    assert s.progress.tracker.event.percent != 100


@pytest.mark.parametrize('boundary', ['modal', 'completed'])
def test_editor_signature_is_checked_only_on_owner_thread(studio, monkeypatch, boundary):
    s = studio
    if boundary == 'modal':
        def choose(parent, *, current):
            s.handler.mutate('editor')
            return s.criteria
        monkeypatch.setattr(inspection, 'choose_criteria', choose)
    s.handler.inspect()
    if boundary == 'completed':
        s.executor.execute()
        s.handler.mutate('editor')
        drain(s)
    assert not s.shown
    assert s.progress.tracker.event.percent != 100


def test_cancelled_criteria_is_passive(studio, monkeypatch):
    monkeypatch.setattr(inspection, 'choose_criteria', lambda parent, **kwargs: None)
    studio.handler.inspect()
    assert not studio.executor.requests and not studio.shown
    studio.service.inspect_silence.assert_not_called()
    studio.service.request_pause.assert_not_called()


@pytest.mark.parametrize('when', ['queued', 'completed'])
def test_f8_cancellation_is_inspection_cancelled_not_saved_production_pause(studio, when):
    s = studio
    s.handler.inspect()
    if when == 'completed':
        s.executor.execute()
    s.handler.cancel.set()
    if when == 'queued':
        s.executor.execute()
    drain(s)
    assert not s.shown
    assert s.progress.tracker.event.state == 'cancelled'
    s.service.request_pause.assert_not_called()
    s.service.get.assert_not_called()
    s.service.close.assert_not_called()
    assert not any('completed chunks retained' in report for report in s.reports)


def test_close_cancels_queued_inspection_without_writing_job_control(studio):
    s = studio
    s.handler.inspect()
    s.handler.close()
    s.executor.execute()
    drain(s)
    assert s.handler.cancel.is_set() and not s.shown
    s.service.inspect_silence.assert_not_called()
    s.service.request_pause.assert_not_called()
    s.service.close.assert_not_called()
    assert s.progress.tracker.event.state == 'cancelled'
    assert s.executor.shutdowns == [(False, True)]


@pytest.mark.parametrize('child', [False, True])
def test_captured_window_id_controls_queued_delivery_before_widget_access(studio, child):
    s = studio
    s.handler.inspect()
    s.executor.execute()
    event = SimpleNamespace(GetId=lambda: 9999 if child else 7301, Skip=Mock(),
                            GetEventObject=lambda: object())
    s.handler.destroy(event)
    event.Skip.assert_called_once()
    if not child:
        for control in s.buttons.values():
            control.Enable = Mock(side_effect=AssertionError('Deleted control accessed'))
    drain(s)
    assert bool(s.shown) is child
    assert s.handler.state() == (False, True, False)
    assert s.service.request_pause.call_count == 0


@pytest.mark.parametrize('outcome', ['success', 'failure', 'submit_failure'])
def test_inspection_pause_policy_clears_and_does_not_leak_into_later_render(studio, outcome):
    s = studio
    if outcome == 'submit_failure':
        s.executor.failure = OSError('Worker unavailable')
    elif outcome == 'failure':
        s.service.inspect_silence.side_effect = ValueError('Completed receipt changed')
    s.handler.inspect()
    if outcome != 'submit_failure':
        s.executor.execute()
        drain(s)
    assert s.handler.state() == (False, True, False)
    s.executor.failure = None
    s.handler.run(lambda: None, lambda value: None)
    assert s.handler.state() == (True, True, True)
    assert s.buttons['pause'].label == '&Pause (F8)' and s.buttons['pause'].name == 'Pause (F8)'
    s.handler.close()
    s.service.request_pause.assert_called_once_with('completed-job')


def test_wrong_job_report_is_not_presented(studio):
    studio.service.inspect_silence.return_value = dict(report_fixture(), job_id='foreign-job')
    studio.handler.inspect()
    studio.executor.execute()
    drain(studio)
    assert not studio.shown
    assert 'different production' in studio.reports[-1]
    assert studio.progress.tracker.event.state == 'failed'


def test_pending_cleanup_owner_outlives_failed_future_and_close_without_job_mutation(studio, tmp_path):
    """File-backed cleanup ownership is real; native-family proof is tested by the service suite."""
    s = studio
    stage = tmp_path / 'retained-inspection'
    stage.mkdir()
    captured = stage / 'captured-pcm.bin'
    captured.write_bytes(b'\0' * 8000)
    release, cleaned = threading.Event(), threading.Event()
    owner = CleanupOwner(lambda: release.is_set())
    @contextmanager
    def keep_stage():
        try:
            yield
        finally:
            captured.unlink()
            stage.rmdir()
            cleaned.set()
    def pending(job, **kwargs):
        with retained_context(keep_stage()):
            raise AudioCleanupPending('Synthetic file owner has not finished', cleanup_owner=owner)
    s.service.inspect_silence.side_effect = pending
    try:
        s.handler.inspect()
        s.executor.execute()
        drain(s)
        assert stage.is_dir() and captured.stat().st_size == 8000 and not owner.quiescent
        assert s.handler.state() == (False, True, False) and not s.shown
        s.handler.close()
        assert stage.is_dir() and not cleaned.is_set()
        s.service.request_pause.assert_not_called()
        s.service.close.assert_not_called()
        assert owner.close() is False
        release.set()
        assert owner.close() is True and cleaned.wait(2)
        assert not stage.exists()
    finally:
        release.set()
        owner.close()


@pytest.fixture(scope='module')
def completed_pcm(tmp_path_factory):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Reviewed FFmpeg/ffprobe are required.'
    from carr_library.application import LibraryApp
    from carr_library.studio.render import ProductionService
    from carr_library.audio import AudioBudget, AudioRuntime
    tone = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'studio/test_book_audio.py'))['ToneVoices']()
    root = tmp_path_factory.mktemp('studio-silence-ui')
    library = LibraryApp(root / 'library')
    service = ProductionService(library.root / 'state' / 'studio', voices=tone,
        runtime_factory=lambda directory, **kwargs: AudioRuntime(directory, budget=AudioBudget(threads=1), **kwargs))
    project = service.create('Narrator: Synthetic tone.\n[Pause: 1]\nNarrator: Preserve the completed output.',
        voice_ids=('synthetic-tone',), title='Inspection fixture')
    job = service.submit(project, root)
    complete = service.run(job)
    assert complete['status'] == 'complete'
    master = Path(complete['destination']) / 'master.wav'
    before = master.read_bytes()
    yield SimpleNamespace(library=library, service=service, job=job, master=master, before=before)
    service.close()
    assert tone.closed == 0


def test_actual_completed_pcm_is_inspected_off_thread_and_left_exactly_unchanged(studio, completed_pcm):
    s, actual = studio, completed_pcm
    threads = []
    def inspect(job, **kwargs):
        threads.append(threading.get_ident())
        assert job == actual.job
        return actual.service.inspect_silence(job, **kwargs)
    s.service.inspect_silence.side_effect = inspect
    durable = actual.service.get(actual.job)
    s.handler.select_job(actual.job)
    s.handler.inspect()
    s.executor.execute()
    drain(s)
    assert threads and threads[0] != s.owner
    assert len(s.shown) == 1
    result = s.shown[0]
    assert result['source']['sha256'] == sha256(actual.before).hexdigest()
    assert result['interval_count'] >= 1 and result['omitted_count'] == 0
    assert any(row['duration_seconds'] >= 1 for row in result['intervals'])
    assert actual.master.read_bytes() == actual.before
    assert actual.service.get(actual.job) == durable
    assert s.dialog._audio_player is None
    s.service.request_pause.assert_not_called()


def test_close_during_actual_inspection_cancels_without_creating_a_pause_control(studio, completed_pcm):
    s, actual = studio, completed_pcm
    entered, release = threading.Event(), threading.Event()
    durable = actual.service.get(actual.job)
    control = actual.service.jobs_root / actual.job / 'control.json'
    before = control.read_bytes() if control.exists() else None
    def inspect(job, **kwargs):
        assert job == actual.job
        entered.set()
        assert release.wait(10)
        return actual.service.inspect_silence(job, **kwargs)
    s.service.inspect_silence.side_effect = inspect
    s.service.request_pause.side_effect = actual.service.request_pause
    s.handler.select_job(actual.job)
    s.handler.inspect()
    with ThreadPoolExecutor(max_workers=1) as supervisor:
        running = supervisor.submit(s.executor.execute)
        try:
            assert entered.wait(10)
            s.handler.close()
            assert s.handler.cancel.is_set()
        finally:
            release.set()
            running.result(timeout=30)
    drain(s)
    assert not s.shown and s.handler.state()[1] is False
    s.service.request_pause.assert_not_called()
    s.service.close.assert_not_called()
    assert (control.read_bytes() if control.exists() else None) == before
    assert actual.service.get(actual.job) == durable
    assert actual.master.read_bytes() == actual.before


@pytest.fixture
def native_controls(monkeypatch):
    """Model only wx dispatch; production dialog construction/handlers execute unchanged."""
    state = SimpleNamespace(windows=[], scenarios=[], focused=None, next_id=9000)
    owner = threading.get_ident()
    class Widget:
        def __init__(self, parent=None, identity=None, *, label='', value='', title='', style=0, **kwargs):
            state.next_id += 1
            self.parent, self.identity = parent, state.next_id if identity is None else identity
            self.label, self.value, self.title, self.style = label, value, title, style
            self.name, self.handlers, self.children = '', {}, []
            self.destroyed, self.destroy_calls, self.decision = False, 0, None
            self.maximum = None
            if parent is not None and hasattr(parent, 'children'):
                parent.children.append(self)
        def check(self):
            assert threading.get_ident() == owner
            assert not self.destroyed, 'Deleted native control accessed'
        def GetId(self):
            self.check()
            return self.identity
        def SetName(self, name):
            self.check()
            self.name = name
        def GetValue(self):
            self.check()
            return self.value
        def SetValue(self, value):
            self.check()
            self.value = value
        ChangeValue = SetValue
        def SetMaxLength(self, maximum):
            self.maximum = maximum
        def SetFocus(self):
            self.check()
            state.focused = self
        def Bind(self, event, handler):
            self.handlers[event] = handler
        def SetSizer(self, layout):
            self.layout = layout
        def EndModal(self, decision):
            self.check()
            self.decision = decision
        def press(self):
            if 'button' in self.handlers:
                self.handlers['button'](SimpleNamespace(GetId=lambda: self.identity))
            elif self.identity == wx.ID_CANCEL:
                self.parent.EndModal(wx.ID_CANCEL)
        def Destroy(self):
            self.check()
            self.destroy_calls += 1
            callback = self.handlers.get('destroy')
            if callback:
                callback(SimpleNamespace(GetId=lambda: self.identity, GetEventObject=lambda: object(), Skip=Mock()))
            self.destroyed = True
        def ShowModal(self):
            state.windows.append(self)
            state.scenarios.pop(0)(self)
            return self.decision
    class Sizer:
        def __init__(self, *args): self.items = []
        def Add(self, *args): self.items.append(args)
    wx = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, StaticText=Widget, Button=Widget,
        BoxSizer=Sizer, VERTICAL=1, HORIZONTAL=2, DEFAULT_DIALOG_STYLE=4, RESIZE_BORDER=8,
        TE_MULTILINE=16, TE_READONLY=32, EXPAND=64, ALL=128, LEFT=256, RIGHT=512,
        ALIGN_RIGHT=1024, ID_OK=1, ID_CANCEL=2, ID_CLOSE=3,
        EVT_BUTTON='button', EVT_WINDOW_DESTROY='destroy')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    state.wx = wx
    def named(window, fragment):
        values = [control for control in window.children if fragment in control.name]
        assert len(values) == 1, (fragment, [control.name for control in window.children])
        return values[0]
    state.named = named
    return state


def test_actual_criteria_controls_are_named_passive_and_validate_before_accept(native_controls):
    n = native_controls
    def scenario(window):
        threshold = n.named(window, 'Silence threshold')
        minimum = n.named(window, 'Minimum silence')
        accept = n.named(window, 'Inspect completed audio with')
        feedback = n.named(window, 'validation status')
        assert threshold.GetValue() == '-50' and minimum.GetValue() == '2'
        assert threshold.maximum == minimum.maximum == 128
        assert window.decision is None
        threshold.SetValue('nan')
        accept.press()
        assert window.decision is None and n.focused is feedback
        assert 'finite' in feedback.GetValue()
        threshold.SetValue('-50')
        minimum.SetValue('0')
        accept.press()
        assert window.decision is None
        minimum.SetValue('.25')
        accept.press()
    n.scenarios.append(scenario)
    criteria = inspection.choose_criteria(None, current=lambda: True)
    assert criteria == inspection.criteria_from_text('-50', '.25')
    assert n.windows[0].destroy_calls == 1


@pytest.mark.parametrize('cancel_kind', ['cancel', 'changed_owner', 'destroyed'])
def test_actual_criteria_modal_never_accepts_cancelled_or_destroyed_owner(native_controls, cancel_kind):
    n, current = native_controls, [True]
    def scenario(window):
        if cancel_kind == 'cancel':
            next(control for control in window.children if control.identity == n.wx.ID_CANCEL).press()
        elif cancel_kind == 'changed_owner':
            current[0] = False
            window.EndModal(n.wx.ID_OK)
        else:
            window.Destroy()
    n.scenarios.append(scenario)
    assert inspection.choose_criteria(None, current=lambda: current[0]) is None
    assert n.windows[0].destroy_calls == 1


def test_child_destruction_does_not_revoke_actual_criteria_modal(native_controls):
    n = native_controls
    def scenario(window):
        skipped = Mock()
        window.handlers['destroy'](SimpleNamespace(GetId=lambda: window.identity + 999, Skip=skipped))
        skipped.assert_called_once()
        n.named(window, 'Inspect completed audio with').press()
    n.scenarios.append(scenario)
    assert inspection.choose_criteria(None, current=lambda: True) == inspection.criteria_from_text('-50', '2')


def test_actual_result_controls_keep_complete_structured_report_read_only(native_controls):
    n, value = native_controls, report_fixture()
    before = deepcopy(value)
    def scenario(window):
        text = n.named(window, 'results in seconds')
        assert text.style & n.wx.TE_READONLY
        assert '0.250000 to 0.750000 seconds' in text.GetValue()
        assert window.inspection_result is value
        assert window.inspection_result['intervals'][0]['start_frame'] == 2000
        assert window.inspection_result['source']['sha256'] == 'a' * 64
        n.named(window, 'Close long-silence inspection').press()
    n.scenarios.append(scenario)
    inspection.show_result(None, value, current=lambda: True)
    assert value == before and n.windows[0].destroy_calls == 1


def test_late_result_owner_refusal_creates_no_native_dialog(native_controls):
    with pytest.raises(InterruptedError):
        inspection.show_result(None, report_fixture(), current=lambda: False)
    assert not native_controls.windows
