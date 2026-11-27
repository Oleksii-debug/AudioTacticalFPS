"""Actual Studio handlers and shared progress tasks under observer faults.

This finite harness uses real ProgressHub/NativeProgressPanel methods;
native controls, devices and NVDA have separate Windows acceptance.
"""
import ast
from importlib.util import find_spec
from concurrent.futures import Future
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio.models import AudioCancelled
from carr_library.progress import ProgressError, ProgressHub, ProgressState, ProgressTask, bind_progress, operation_outcome
from carr_library.ui.progress_panel import NativeProgressPanel


@pytest.fixture
def studio():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = ('transfer_progress_owner', 'release_progress_owner', 'run')
    handlers = [next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == name)
                for name in names]
    factory = ast.parse('''
def make(progress, executor, wx, report, service):
    busy, alive = False, True
    current_job = 'saved-production'
    dialog = SimpleNamespace(working=False)
    cancel = threading.Event()
    controls = lambda: None
    frame = SimpleNamespace(report=report)
    owned_progress_voices = set()
    progress_ownership_lock = threading.RLock()
    def close():
        nonlocal alive
        alive = False
    return SimpleNamespace(run=run, cancel=cancel, close=close, dialog=dialog, is_busy=lambda: busy)
''')
    factory.body[0].body[-1:-1] = handlers
    environment = dict(threading=threading, SimpleNamespace=SimpleNamespace, operation_outcome=operation_outcome,
                       ProgressError=ProgressError, ProgressState=ProgressState, bind_progress=bind_progress)
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    queued, reports, history = [], [], []
    progress = object.__new__(NativeProgressPanel)
    progress.hub, progress.active_task, progress.feedback = ProgressHub(), None, Mock()
    progress.notice = ''
    progress.refresh = lambda: history.append(progress.active_task.snapshot()) if progress.active_task else None
    executor, service = Mock(), Mock()
    def report(message, focus=False):
        reports.append(str(message))
    def after(callback, *args):
        queued.append(lambda: callback(*args))
    handler = environment['make'](progress, executor, SimpleNamespace(CallAfter=after), report, service)
    return SimpleNamespace(handler=handler, progress=progress, executor=executor, service=service,
                           queued=queued, reports=reports, report=report, environment=environment, history=history)


def drain(studio):
    while studio.queued:
        studio.queued.pop(0)()


@pytest.mark.parametrize('fault', ['begin', 'update', 'finish', 'reporting_unavailable', 'classifier'])
def test_feedback_failure_cannot_reverse_publication_or_hide_callback_output(studio, tmp_path, monkeypatch, fault):
    future = Future()
    studio.executor.submit.return_value = future
    output = tmp_path / 'accepted-output.txt'
    message = 'A synthetic render has been durably accepted.'
    def broken(*args, **kwargs):
        raise RuntimeError('Synthetic unavailable feedback')
    if fault in ('classifier', 'reporting_unavailable'):
        monkeypatch.setitem(studio.environment, 'operation_outcome', broken)
        if fault == 'reporting_unavailable':
            monkeypatch.setattr(studio.progress, 'reporting_unavailable', broken)
    elif fault == 'update':
        monkeypatch.setattr(ProgressTask, 'update', broken)
    else:
        monkeypatch.setattr(studio.progress, fault, broken)
    callbacks = []
    def operation(report):
        output.write_text(message, encoding='utf-8')
        report('Output bytes saved')
        return {'status': 'complete', 'destination': str(output)}
    def complete(result):
        assert output.read_text(encoding='utf-8') == message
        callbacks.append(result)
        assert not studio.progress.active_task or studio.progress.active_task.snapshot().percent != 100
        studio.report('Saved verified output to ' + result['destination'])
    studio.handler.run(operation, complete, with_progress=True)
    future.set_result(studio.executor.submit.call_args.args[0]())
    drain(studio)
    assert len(callbacks) == 1 and not studio.handler.is_busy() and not studio.handler.dialog.working
    assert output.read_text(encoding='utf-8') == message
    assert studio.reports[-1] == 'Saved verified output to ' + str(output)
    assert not any(event.state == ProgressState.FAILED for event in studio.history)
    if fault != 'reporting_unavailable':
        assert 'Synthetic unavailable feedback' in studio.progress.notice
    if fault in ('update', 'finish', 'classifier'):
        assert studio.progress.active_task.snapshot().state == ProgressState.UNAVAILABLE
        assert not any(event.percent == 100 for event in studio.history)


@pytest.mark.parametrize('fault', ['begin', 'finish', 'reporting_unavailable'])
def test_observer_failure_does_not_hide_executor_failure_or_leave_busy(studio, monkeypatch, fault):
    def unavailable(*args, **kwargs):
        raise RuntimeError('Observer detached')
    monkeypatch.setattr(studio.progress, 'begin' if fault == 'reporting_unavailable' else fault, unavailable)
    if fault == 'reporting_unavailable':
        monkeypatch.setattr(studio.progress, 'reporting_unavailable', unavailable)
    studio.executor.submit.side_effect = OSError('Worker did not start')
    complete = Mock()
    studio.handler.run(lambda: None, complete)
    complete.assert_not_called()
    assert not studio.handler.is_busy() and not studio.handler.dialog.working
    assert studio.reports == ['Worker did not start']
    assert not any(event.percent == 100 for event in studio.history)


@pytest.mark.parametrize('saved_state', ['complete', 'paused', 'cancelled'])
def test_late_cancel_cannot_relabel_real_worker_failure_as_pause(studio, saved_state):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': saved_state}
    complete = Mock()
    studio.handler.run(lambda report: None, complete, with_progress=True)
    studio.handler.cancel.set()
    future.set_exception(OSError('Output commit failed'))
    drain(studio)
    complete.assert_not_called()
    assert studio.progress.active_task.snapshot().state == ProgressState.FAILED
    assert studio.reports == ['Output commit failed']
    assert not any(event.percent == 100 for event in studio.history)


def test_publication_callback_failure_is_not_hidden_by_complete_receipt_and_late_cancel(studio):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': 'complete'}
    def publication(result):
        studio.handler.cancel.set()
        raise OSError('Final publication failed')
    complete = Mock(side_effect=publication)
    studio.handler.run(lambda report: None, complete, with_progress=True)
    future.set_result({'status': 'complete'})
    drain(studio)
    complete.assert_called_once()
    assert studio.progress.active_task.snapshot().state == ProgressState.FAILED
    assert studio.reports == ['Final publication failed']
    assert not any(event.percent == 100 for event in studio.history)


@pytest.mark.parametrize('exception_type', [InterruptedError, AudioCancelled])
@pytest.mark.parametrize('saved_state', ['paused', 'cancelled'])
def test_explicit_speech_and_dsp_interruptions_keep_saved_pause_or_cancel(studio, exception_type, saved_state):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': saved_state}
    studio.handler.run(lambda report: None, Mock(), with_progress=True)
    future.set_exception(exception_type('Explicit runtime interruption'))
    drain(studio)
    assert studio.progress.active_task.snapshot().state == saved_state
    assert not any(event.percent == 100 for event in studio.history)


def test_queued_old_phase_cannot_overwrite_completed_output_report(studio):
    future = Future()
    studio.executor.submit.return_value = future
    def operation(report):
        report('An earlier phase')
        return {'status': 'complete'}
    studio.handler.run(operation, lambda result: studio.report('Saved final output'), with_progress=True)
    future.set_result(studio.executor.submit.call_args.args[0]())
    assert len(studio.queued) == 2
    studio.queued.pop()()  # Complete before a delayed native phase event.
    drain(studio)
    assert studio.reports == ['Saved final output']


def test_begin_failure_never_finalizes_callback_started_new_operation(studio, monkeypatch):
    original = studio.progress.begin
    monkeypatch.setattr(studio.progress, 'begin', Mock(side_effect=RuntimeError('No feedback')))
    future = Future()
    studio.executor.submit.return_value = future
    def complete(result):
        original('A separately started new operation')
    studio.handler.run(lambda: None, complete)
    future.set_result({'status': 'complete'})
    drain(studio)
    snapshot = studio.progress.active_task.snapshot()
    assert snapshot.state == ProgressState.RUNNING
    assert snapshot.title == 'A separately started new operation'
    assert not any(event.percent == 100 for event in studio.history)


def test_studio_close_retains_parent_observer_when_device_and_local_feedback_both_fail():
    """A local view cannot hide uncertain native audio from the surviving parent."""
    source = Path(find_spec('carr_library.ui.studio_dialog').origin)
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = {'close', 'cleanup', 'dispose', 'transfer_progress_owner', 'release_progress_owner', 'release_service'}
    handlers = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in handlers} == names
    factory = ast.parse('''
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor, owned_progress_voices, unregister):
    alive, busy, cleanup_started = True, False, False
    progress_ownership_lock = threading.RLock()
    current_job = None
    frame = SimpleNamespace(report=report)
    return close
''')
    factory.body[0].body[-1:-1] = handlers
    environment = {'wx': SimpleNamespace(ID_CLOSE=321, CallAfter=lambda callback: callback()),
                   'SimpleNamespace': SimpleNamespace, 'threading': threading,
                   '__package__': 'carr_library.ui'}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    autosave, service, timer, dialog, progress, parent_progress, executor, owned_voice, unregister = [Mock() for _ in range(9)]
    dialog._playback_generation, dialog._playback_navigation = 0, None
    cancel, reports = threading.Event(), []
    dialog._audio_player.close.side_effect = RuntimeError('The native device did not confirm silence')
    progress.unregister_player.side_effect = RuntimeError('The local progress view is detached')
    callback = environment['make'](autosave, cancel, service, timer, dialog,
        progress, parent_progress, lambda message, focus=False: reports.append(str(message)), executor, {owned_voice}, unregister)
    callback()
    dialog._audio_player.close.assert_called_once_with()
    progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=False)
    parent_progress.unregister_player.assert_called_once_with(dialog._audio_player, silence_confirmed=False)
    assert cancel.is_set()
    diagnostic = ' '.join(reports)
    assert 'native device did not confirm silence' in diagnostic
    assert 'local progress view is detached' in diagnostic
    progress.close.assert_called_once_with()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=False)
    assert executor.submit.call_count == 1
    assert executor.submit.call_args.args[0].__name__ == 'release_service'
    unregister.assert_called_once_with()
    service.close.assert_not_called()
    owned_voice.close.assert_not_called()
    executor.submit.call_args.args[0]()
    service.close.assert_called_once_with()
    owned_voice.close.assert_called_once_with()
    dialog.EndModal.assert_called_once_with(321)



@pytest.mark.parametrize(('case', 'handler_name', 'modal_kind'), [
    ('open', 'open_project', 'file'),
    ('save-as', 'save_as', 'file'),
    ('save-approval', 'save_as', 'approval'),
    ('roles-choice', 'edit_roles', 'single'),
    ('roles-detail', 'edit_roles', 'detail'),
    ('new-output', 'render', 'directory'),
    ('restored-inputs', 'render', 'message'),
    ('restored-output', 'render', 'directory'),
    ('jobs', 'jobs', 'single'),
])
def test_studio_nested_modal_owner_destruction_stops_native_reads_and_admission(tmp_path, case, handler_name, modal_kind):
    """Extract actual bound handlers; every forbidden access is recorded, even if caught."""
    from dataclasses import replace
    from carr_library.durable_state import encode, read_json
    from carr_library.studio.project import Project, Role, Scene
    source = Path(find_spec('carr_library.ui.studio_dialog').origin)
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = {'dispose', 'destroyed', 'report', 'open_project', 'save_as', 'edit_roles', 'render', 'jobs'}
    handlers = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in handlers} == names
    factory = ast.parse('''
def make(wx, frame, dialog, service, controller, project, status, editor, title,
         format_choice, stems, run, controls, initial_job):
    alive, busy, current_job = True, False, initial_job
    native_id = 431
    cancel = threading.Event()
    formats = ['wav']
    voices = (SimpleNamespace(id='voice-1', name='Fixture', language='en', capabilities=()),)
    save = lambda: project
    return SimpleNamespace(open_project=open_project, save_as=save_as, edit_roles=edit_roles,
                           render=render, jobs=jobs, destroyed=destroyed,
                           is_alive=lambda: alive, cancel=cancel)
''')
    factory.body[0].body[-1:-1] = handlers
    forbidden, peers = [], []
    destroyed = False
    bound = None
    project = Project('project-1', 'Synthetic project', (Role('narrator', 'Narrator', 'voice-1'),),
                      (Scene('scene-1', 'Scene', (), minimum_duration=1),), original_script='Synthetic prose')
    selected_path = tmp_path / 'selected.txt'
    selected_path.write_text('Synthetic selected prose', encoding='utf-8')

    def touch(name, result=None):
        if destroyed:
            forbidden.append(name)
        return result

    class Control:
        def SetName(self, *args): touch('control SetName')
        def SetSelection(self, *args): touch('control SetSelection')
        def SetValue(self, *args): touch('control SetValue')
        def ChangeValue(self, *args): touch('control ChangeValue')
        def SetFocus(self, *args): touch('control SetFocus')
        def GetSelection(self): return touch('control GetSelection', 0)
        def GetValue(self): return touch('control GetValue', 0)

    def destroy_owner():
        nonlocal destroyed
        destroyed = True
        for peer in peers:
            peer.live = False
        event = Mock()
        event.GetId.return_value = 431
        event.GetEventObject.return_value = object()
        bound.destroyed(event)
        event.Skip.assert_called_once()

    class Peer:
        def __init__(self, kind):
            touch('construct peer ' + kind)
            self.kind, self.live = kind, True
            peers.append(self)
        def __bool__(self): return self.live
        def ShowModal(self):
            if self.kind == modal_kind:
                destroy_owner()
            return 3 if self.kind == 'message' else 1
        def GetPath(self): return touch('chooser GetPath', str(selected_path))
        def GetSelection(self): return touch('chooser GetSelection', 0)
        def CreateButtonSizer(self, *args): return touch('detail CreateButtonSizer', object())
        def SetSizerAndFit(self, *args): touch('detail SetSizerAndFit')
        def Destroy(self):
            touch('chooser Destroy')
            self.live = False

    def peer_factory(kind):
        return lambda *args, **kwargs: Peer(kind)

    wx = SimpleNamespace(
        ID_OK=1, ID_YES=3, NOT_FOUND=-1, FD_OPEN=4, FD_FILE_MUST_EXIST=8, FD_SAVE=16,
        YES_NO=32, NO_DEFAULT=64, ICON_QUESTION=128, VERTICAL=1, ALL=2, EXPAND=4,
        OK=8, CANCEL=16, FileDialog=peer_factory('file'), DirDialog=peer_factory('directory'),
        SingleChoiceDialog=peer_factory('single'), MessageDialog=peer_factory('message'),
        Dialog=peer_factory('detail'), BoxSizer=lambda *args: SimpleNamespace(Add=lambda *args: touch('sizer Add')),
        StaticText=lambda *args, **kwargs: Control(), Choice=lambda *args, **kwargs: Control(),
        SpinCtrlDouble=lambda *args, **kwargs: Control(), SpinCtrl=lambda *args, **kwargs: Control())
    row = {'id': 'saved-job', 'project': project.to_dict(), 'status': 'paused', 'progress': 'Ready', 'error': ''}
    service = SimpleNamespace(
        register_project_inputs=lambda *args, **kwargs: touch('register imported paths'),
        projects=SimpleNamespace(save=lambda *args, **kwargs: touch('save project'),
                                 load=lambda *args: touch('load project', project)),
        submit=lambda *args, **kwargs: touch('submit output', 'new-job'),
        restored_inputs_to_review=lambda *args: touch('review restored inputs',
            ('/explicit/legacy-input.wav',) if case == 'restored-inputs' else ()),
        get=lambda *args: touch('read saved job', {'recovery': {'requires_output_directory': case == 'restored-output'}}),
        relocate_restored_job=lambda *args: touch('relocate restored job'),
        approve_restored_inputs=lambda *args, **kwargs: touch('approve restored inputs'),
        run=lambda *args, **kwargs: touch('execute production'),
        jobs=lambda: touch('list jobs', [row]))
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path / 'library'))
    controller = SimpleNamespace(service=service, project=project,
                                 draft=frame.library.root / 'state' / 'studio' / 'draft.json')
    def approve(*args, **kwargs):
        if modal_kind == 'approval':
            destroy_owner()
        return object()
    def write(*args, **kwargs):
        touch('write exported project')
        return object()
    environment = dict(threading=threading, SimpleNamespace=SimpleNamespace, Path=Path,
                       Project=Project, replace=replace, encode=encode, read_json=read_json,
                       confirm_export_destination=approve, write_binary_export=write,
                       export_completion=lambda *args: 'Exported')
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    initial_job = 'saved-job' if case in ('restored-inputs', 'restored-output') else None
    bound = environment['make'](wx, frame, object(), service, controller, project,
                               Control(), Control(), Control(), Control(), Control(),
                               lambda *args, **kwargs: touch('queue production'),
                               lambda: touch('refresh controls'), initial_job)
    getattr(bound, handler_name)()
    assert destroyed and not bound.is_alive() and bound.cancel.is_set()
    assert peers and all(not peer for peer in peers)
    assert forbidden == []


def test_start_failure_never_finalizes_callback_started_new_operation(studio, monkeypatch):
    # Preserve the earlier handler regression name against the one canonical
    # begin contract, including its actual new-operation snapshot assertions.
    test_begin_failure_never_finalizes_callback_started_new_operation(studio, monkeypatch)
