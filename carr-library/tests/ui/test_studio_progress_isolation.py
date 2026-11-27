"""Actual nested Studio handlers with deterministic observer fault injection.

These are headless business/feedback-boundary tests, not native audio or NVDA
acceptance. Files represent only purpose-written synthetic publication data.
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
from carr_library.ui.operation_progress import ProgressTracker, operation_outcome


class Progress:
    def __init__(self):
        self.tracker = ProgressTracker()
        self.history, self.finalized = [], []

    def start(self, phase):
        event = self.tracker.start(phase)
        self.history.append(event)
        return event.token

    def _transition(self, state, phase, *, token):
        event = self.tracker.transition(state, phase, token=token)
        if event is not None:
            self.history.append(event)

    def succeed(self, phase, *, token): self._transition('succeeded', phase, token=token)
    def fail(self, phase, *, token): self._transition('failed', phase, token=token)
    def pause(self, phase, *, token): self._transition('paused', phase, token=token)
    def cancel(self, phase, *, token): self._transition('cancelled', phase, token=token)

    def update(self, completed, total, phase, *, token):
        event = self.tracker.update(completed, total, phase, token=token)
        if event is not None:
            self.history.append(event)

    def finalize(self, *, token):
        self.finalized.append(token)
        if self.tracker.event.token == token and self.tracker.event.state == 'running':
            self._transition('incomplete', 'Feedback completion unavailable', token=token)


@pytest.fixture
def studio():
    source = Path(find_spec('carr_library.ui.studio_dialog').origin)
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    run = next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
    factory = ast.parse('''
def make(progress, executor, wx, report, service):
    busy, alive = False, True
    current_job = 'saved-production'
    cancel = threading.Event()
    owned_progress_voices = set()
    retire_progress_voice = lambda provider: provider.close()
    controls = lambda: None
    def close():
        nonlocal alive
        alive = False
    return SimpleNamespace(run=run, cancel=cancel, close=close, is_busy=lambda: busy)
''')
    factory.body[0].body.insert(-1, run)
    environment = dict(threading=threading, SimpleNamespace=SimpleNamespace, operation_outcome=operation_outcome)
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    queued, reports = [], []
    progress, executor, service = Progress(), Mock(), Mock()
    def report(message, focus=False):
        reports.append(str(message))
    def after(callback, *args):
        queued.append(lambda: callback(*args))
    handler = environment['make'](progress, executor, SimpleNamespace(CallAfter=after), report, service)
    return SimpleNamespace(handler=handler, progress=progress, executor=executor, service=service,
                           queued=queued, reports=reports, report=report, environment=environment)


def drain(studio):
    while studio.queued:
        studio.queued.pop(0)()


@pytest.mark.parametrize('fault', ['start', 'update', 'succeed', 'finalize', 'classifier'])
def test_feedback_failure_cannot_reverse_publication_or_hide_callback_output(studio, tmp_path, monkeypatch, fault):
    future = Future()
    studio.executor.submit.return_value = future
    output = tmp_path / 'accepted-output.txt'
    message = 'A synthetic render has been durably accepted.'
    def broken(*args, **kwargs):
        raise RuntimeError('Synthetic unavailable feedback')
    if fault == 'classifier':
        monkeypatch.setitem(studio.environment, 'operation_outcome', broken)
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
        assert studio.progress.tracker.event.percent != 100
        studio.report('Saved verified output to ' + result['destination'])
    studio.handler.run(operation, complete, with_progress=True)
    future.set_result(studio.executor.submit.call_args.args[0]())
    drain(studio)
    assert len(callbacks) == 1 and not studio.handler.is_busy()
    assert output.read_text(encoding='utf-8') == message
    assert studio.reports[-1] == 'Saved verified output to ' + str(output)
    assert not any(event.state == 'failed' for event in studio.progress.history)
    assert 'Synthetic unavailable feedback' in studio.progress.reporting_error
    assert None not in studio.progress.finalized
    if fault in ('update', 'succeed', 'classifier'):
        assert studio.progress.tracker.event.state == 'incomplete'
        assert not any(event.percent == 100 for event in studio.progress.history)


@pytest.mark.parametrize('fault', ['start', 'fail', 'finalize'])
def test_observer_failure_does_not_hide_executor_failure_or_leave_busy(studio, monkeypatch, fault):
    def unavailable(*args, **kwargs):
        raise RuntimeError('Observer detached')
    monkeypatch.setattr(studio.progress, fault, unavailable)
    studio.executor.submit.side_effect = OSError('Worker did not start')
    complete = Mock()
    studio.handler.run(lambda: None, complete)
    complete.assert_not_called()
    assert not studio.handler.is_busy()
    assert studio.reports == ['Worker did not start']
    assert not any(event.percent == 100 for event in studio.progress.history)


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
    assert studio.progress.tracker.event.state == 'failed'
    assert studio.reports == ['Output commit failed']
    assert not any(event.percent == 100 for event in studio.progress.history)


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
    assert studio.progress.tracker.event.state == 'failed'
    assert studio.reports == ['Final publication failed']
    assert not any(event.percent == 100 for event in studio.progress.history)


@pytest.mark.parametrize('exception_type', [InterruptedError, AudioCancelled])
@pytest.mark.parametrize('saved_state', ['paused', 'cancelled'])
def test_explicit_speech_and_dsp_interruptions_keep_saved_pause_or_cancel(studio, exception_type, saved_state):
    future = Future()
    studio.executor.submit.return_value = future
    studio.service.get.return_value = {'status': saved_state}
    studio.handler.run(lambda report: None, Mock(), with_progress=True)
    future.set_exception(exception_type('Explicit runtime interruption'))
    drain(studio)
    assert studio.progress.tracker.event.state == saved_state
    assert not any(event.percent == 100 for event in studio.progress.history)


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


def test_start_failure_never_finalizes_callback_started_new_operation(studio, monkeypatch):
    original = studio.progress.start
    monkeypatch.setattr(studio.progress, 'start', Mock(side_effect=RuntimeError('No feedback')))
    future = Future()
    studio.executor.submit.return_value = future
    def complete(result):
        original('A separately started new operation')
    studio.handler.run(lambda: None, complete)
    future.set_result({'status': 'complete'})
    drain(studio)
    assert studio.progress.tracker.event.state == 'running'
    assert studio.progress.tracker.event.phase == 'A separately started new operation'
    assert not studio.progress.finalized


def test_studio_close_retains_parent_observer_when_device_and_local_feedback_both_fail():
    """A local view cannot hide uncertain native audio from the surviving parent."""
    source = Path(find_spec('carr_library.ui.studio_dialog').origin)
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    names = {'close', 'cleanup', 'dispose', 'retire_progress_voice', 'release_service'}
    handlers = [node for node in window.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in handlers} == names
    factory = ast.parse('''
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor):
    alive, busy, cleanup_started = True, False, False
    owned_progress_voices, cleanup_retry_registered = set(), False
    current_job = None
    frame = SimpleNamespace(report=report)
    return close
''')
    factory.body[0].body[-1:-1] = handlers
    environment = {'wx': SimpleNamespace(ID_CLOSE=321), 'SimpleNamespace': SimpleNamespace}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    autosave, service, timer, dialog, progress, parent_progress, executor = [Mock() for _ in range(7)]
    dialog._playback_generation, dialog._playback_navigation = 0, None
    cancel, reports = threading.Event(), []
    dialog._audio_player.close.side_effect = RuntimeError('The native device did not confirm silence')
    progress.unregister_player.side_effect = RuntimeError('The local progress view is detached')
    callback = environment['make'](autosave, cancel, service, timer, dialog,
        progress, parent_progress, lambda message, focus=False: reports.append(str(message)), executor)
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


@pytest.mark.parametrize(('result', 'expected'), [
    ({'status': 'paused'}, 'paused'),
    ({'status': 'failed'}, 'failed'),
    ({'status': 'cancelled'}, 'cancelled'),
    ({'status': 'queued'}, 'paused'),
    ({'status': 'complete', 'rows': [{'status': 'paused'}]}, 'paused'),
    ({'status': 'complete', 'results': [{'state': 'failed'}]}, 'failed'),
    ([{'status': 'complete'}, {'rows': [{'state': 'cancelled'}]}], 'cancelled'),
])
def test_broken_feedback_classifier_never_applies_explicit_or_nested_partial_result(
        studio, monkeypatch, result, expected):
    future = Future()
    studio.executor.submit.return_value = future
    def broken_feedback(_result):
        raise RuntimeError('Synthetic unavailable classifier feedback')
    monkeypatch.setitem(studio.environment, 'operation_outcome', broken_feedback)
    complete = Mock()
    studio.handler.run(lambda: result, complete)
    future.set_result(studio.executor.submit.call_args.args[0]())
    drain(studio)
    complete.assert_not_called()
    assert not studio.handler.is_busy()
    assert studio.progress.tracker.event.state == 'incomplete'
    assert not any(event.percent == 100 for event in studio.progress.history)
    assert studio.reports == ['Audio Studio operation: ' + expected + '. Saved work is retained.']
    assert 'Synthetic unavailable classifier feedback' in studio.progress.reporting_error


def test_broken_feedback_classifier_keeps_raw_result_callback_as_publication_owner(
        studio, tmp_path, monkeypatch):
    future = Future()
    studio.executor.submit.return_value = future
    output = tmp_path / 'callback-published.txt'
    payload = 'Reviewed data awaiting the existing completion callback.'
    def broken_feedback(_result):
        raise RuntimeError('Synthetic unavailable classifier feedback')
    monkeypatch.setitem(studio.environment, 'operation_outcome', broken_feedback)
    callbacks = []
    def publish(result):
        assert not output.exists()
        assert not any(event.percent == 100 for event in studio.progress.history)
        output.write_text(result, encoding='utf-8')
        callbacks.append(result)
        studio.report('Saved exact callback output to ' + str(output))
    studio.handler.run(lambda: payload, publish)
    future.set_result(studio.executor.submit.call_args.args[0]())
    drain(studio)
    assert callbacks == [payload]
    assert output.read_text(encoding='utf-8') == payload
    assert studio.reports == ['Saved exact callback output to ' + str(output)]
    assert studio.progress.tracker.event.state == 'incomplete'
    assert not any(event.percent == 100 for event in studio.progress.history)
    assert not studio.handler.is_busy()


@pytest.mark.parametrize('malformed', ['cyclic-receipt', 'validator-error'])
def test_failed_business_receipt_validation_cannot_be_replaced_by_feedback_success(
        studio, tmp_path, monkeypatch, malformed):
    from carr_library import progress as shared_progress
    future = Future()
    studio.executor.submit.return_value = future
    def broken_feedback(_result):
        raise RuntimeError('Synthetic unavailable classifier feedback')
    monkeypatch.setitem(studio.environment, 'operation_outcome', broken_feedback)
    result = {'status': 'complete'}
    if malformed == 'cyclic-receipt':
        # Exercise the real canonical traversal budget, not a fake outcome.
        result['rows'] = [result]
    else:
        def invalid_receipt(_result):
            raise ValueError('Synthetic canonical receipt validation failure')
        monkeypatch.setattr(shared_progress, 'operation_outcome', invalid_receipt)
    output = tmp_path / 'must-not-be-published.txt'
    complete = Mock(side_effect=lambda value: output.write_text('incorrect completion', encoding='utf-8'))
    studio.handler.run(lambda: result, complete)
    future.set_result(studio.executor.submit.call_args.args[0]())
    drain(studio)
    complete.assert_not_called()
    assert not output.exists() and not studio.handler.is_busy()
    assert studio.progress.tracker.event.state == 'incomplete'
    assert not any(event.percent == 100 for event in studio.progress.history)
    assert len(studio.reports) == 1 and 'completion state could not be verified' in studio.reports[0]
    expected = 'progress receipt limit' if malformed == 'cyclic-receipt' else 'canonical receipt validation failure'
    assert expected in studio.reports[0]
