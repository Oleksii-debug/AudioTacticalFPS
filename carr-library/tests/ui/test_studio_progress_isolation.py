"""Actual nested Studio handlers with deterministic observer fault injection.

These are headless business/feedback-boundary tests, not native audio or NVDA
acceptance. Files represent only purpose-written synthetic publication data.
"""
import ast
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
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    run = next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
    factory = ast.parse('''
def make(progress, executor, wx, report, service):
    busy, alive = False, True
    current_job = 'saved-production'
    cancel = threading.Event()
    controls = lambda: None
    scope_current = lambda: alive
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
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    close = next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == 'close')
    factory = ast.parse('''
def make(autosave, cancel, service, timer, dialog, progress, parent_progress, report, executor):
    alive, busy = True, False
    current_job = None
    return close
''')
    factory.body[0].body.insert(-1, close)
    environment = {'wx': SimpleNamespace(ID_CLOSE=321)}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    autosave, service, timer, dialog, progress, parent_progress, executor = [Mock() for _ in range(7)]
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
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    dialog.EndModal.assert_called_once_with(321)
