"""Real-service worker-backed UI ownership tests; native wx is a separate gate."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.transcription import TranscriptionOptions, TranscriptionBudget
from carr_library.ui.transcription import (TranscriptionActions, NativeTranscriptionDialog,
                                           describe_request)


def frame_for(service, workspace):
    return SimpleNamespace(library=service.app, active=(workspace, 'Selected library'), _closing=False)


def captured(setup):
    service, workspace, profile, source = setup()
    frame = frame_for(service, workspace)
    actions = TranscriptionActions(frame, service=service)
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation, future = actions.prepare(executor, profile, [source],
            options=TranscriptionOptions(language='uk'), budget=TranscriptionBudget())
        prepared = future.result(timeout=10)
        assert actions.finish(generation)
        actions.accept(prepared)
    return frame, actions, prepared, profile, source


def test_preparation_is_separate_from_explicit_recognition_and_preserves_host_selection(setup):
    frame, actions, prepared, _, source = captured(setup)
    directory = actions.service.jobs_root / prepared.id
    assert not (directory / 'logs' / 'fixture.started').exists()
    assert source.name in describe_request(prepared)
    assert prepared.request_sha256 in describe_request(prepared)
    assert 'not quotation evidence' in describe_request(prepared)
    assert 'English only' in describe_request(prepared)
    selection = frame.active
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation, future = actions.run(executor)
        receipt = future.result(timeout=10)
        assert actions.finish(generation)
    assert receipt.quotation_authority is False and frame.active == selection


def test_run_without_review_does_not_create_a_job(setup):
    service, workspace, _, _ = setup()
    actions = TranscriptionActions(frame_for(service, workspace), service=service)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(PermissionError, match='review'):
            actions.run(executor)
    assert not service.jobs_root.exists()


def test_restart_requires_selecting_and_reviewing_saved_request_again(setup):
    frame, actions, prepared, _, _ = captured(setup)
    reopened = TranscriptionActions(frame)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(PermissionError, match='review'):
            reopened.run(executor)
        selected, _ = reopened.service.review(prepared.id)
        reopened.accept(selected)
        generation, future = reopened.run(executor)
        receipt = future.result(timeout=10)
        assert reopened.finish(generation)
    assert receipt.request_sha256 == prepared.request_sha256


@pytest.mark.parametrize('change', ['workspace', 'host', 'path', 'physical_root', 'closing', 'close'])
def test_queued_operation_cannot_execute_into_stale_root_or_scope(setup, change):
    service, workspace, profile, source = setup()
    frame = frame_for(service, workspace)
    actions = TranscriptionActions(frame, service=service)
    release = Event()
    old_root = service.app.root
    moved = old_root.with_name('retained-original-root')
    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(release.wait)
        generation, future = actions.prepare(executor, profile, [source],
            options=TranscriptionOptions(), budget=TranscriptionBudget())
        if change == 'workspace':
            frame.active = ('different', 'Other library')
        elif change == 'host':
            frame.library = LibraryApp(old_root.parent / 'different-root')
        elif change == 'path':
            frame.library.root = old_root.parent / 'missing'
        elif change == 'physical_root':
            old_root.rename(moved)
            old_root.mkdir()
        elif change == 'closing':
            frame._closing = True
        else:
            actions.close()
        try:
            release.set()
            blocker.result(timeout=5)
            with pytest.raises(InterruptedError):
                future.result(timeout=5)
            assert not actions.finish(generation)
            assert not service.jobs_root.exists()
        finally:
            service.app.root = old_root
            if change == 'physical_root':
                old_root.rmdir()
                moved.rename(old_root)


def test_submission_failure_restores_idle_controls_and_keeps_exact_review(setup):
    _, actions, prepared, _, _ = captured(setup)
    executor = ThreadPoolExecutor(max_workers=1)
    executor.shutdown()
    running = []
    with pytest.raises(RuntimeError):
        actions.run(executor, set_running=running.append)
    assert running == [True, False]
    assert not actions.busy and actions.control.stopped
    assert actions.prepared is prepared


def test_foreign_workspace_review_is_refused(setup):
    _, actions, prepared, _, _ = captured(setup)
    with pytest.raises(PermissionError, match='another library'):
        actions.accept(replace(prepared, workspace='different'))
    assert actions.prepared is prepared


def test_option_edit_invalidates_previous_explicit_review(setup):
    _, actions, _, _, _ = captured(setup)
    actions.invalidate()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(PermissionError, match='review'):
            actions.run(executor)


class RecordingExecutor:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.futures = []
    def submit(self, callback):
        future = self.executor.submit(callback)
        self.futures.append(future)
        return future
    def shutdown(self, **kwargs):
        self.executor.shutdown(**kwargs)


def minimal_view(frame, actions):
    view = NativeTranscriptionDialog.__new__(NativeTranscriptionDialog)
    view.frame, view.actions = frame, actions
    view.alive = view.window_alive = True
    view.dialog = object()
    view._window_id = 7311
    view.measured = None
    view.executor = RecordingExecutor()
    view.callbacks = []
    view.wx = SimpleNamespace(CallAfter=view.callbacks.append)
    view.running = []
    view.set_running = lambda value: view.running.append(value) if view.current() else None
    view.messages = []
    view.status = SimpleNamespace(SetLabel=view.messages.append)
    return view


@pytest.mark.parametrize('event_kind', ['root', 'destroy', 'destroy_alias'])
def test_queued_native_completion_never_touches_stale_or_destroyed_controls(setup, event_kind):
    frame, actions, prepared, _, _ = captured(setup)
    view = minimal_view(frame, actions)
    applied = []
    try:
        view._launch(lambda: actions.start(view.executor,
            lambda stopped, control, report: actions.service.review(prepared.id)[0],
            set_running=view.set_running), applied.append)
        view.executor.futures[0].result(timeout=5)
        deadline = time.monotonic() + 5
        while not view.callbacks:
            assert time.monotonic() < deadline
            time.sleep(.01)
        if event_kind == 'root':
            frame.library = LibraryApp(frame.library.root.parent / 'new-host-root')
        else:
            skipped = []
            wrapper = view.dialog if event_kind == 'destroy' else object()
            event = SimpleNamespace(GetId=lambda: view._window_id,
                GetEventObject=lambda: wrapper, Skip=lambda: skipped.append(True))
            view.destroyed(event)
            assert skipped == [True] and not view.window_alive
        count = len(view.running)
        for callback in view.callbacks:
            callback()
        assert not applied and not view.messages and len(view.running) == count
    finally:
        actions.close()
        view.executor.shutdown(wait=True)


def test_child_window_destruction_does_not_cancel_parent_operation(setup):
    frame, actions, _, _, _ = captured(setup)
    view = minimal_view(frame, actions)
    try:
        event = SimpleNamespace(GetId=lambda: view._window_id + 1,
                                GetEventObject=object, Skip=lambda: None)
        view.destroyed(event)
        assert view.alive and view.window_alive and not actions.closed
    finally:
        view.executor.shutdown(wait=True)


def test_real_running_child_stops_when_window_scope_closes(setup):
    service, workspace, profile, source = setup('stuck')
    actions = TranscriptionActions(frame_for(service, workspace), service=service)
    prepared = service.prepare(workspace, profile, [source],
        budget=TranscriptionBudget(stop_grace_seconds=.05, max_seconds=10))
    actions.accept(prepared)
    with ThreadPoolExecutor(max_workers=1) as executor:
        generation, future = actions.run(executor)
        started = service.jobs_root / prepared.id / 'logs' / 'fixture.started'
        deadline = time.monotonic() + 5
        while not started.exists():
            assert not future.done() and time.monotonic() < deadline
            time.sleep(.01)
        actions.close()
        with pytest.raises(InterruptedError):
            future.result(timeout=6)
    assert not actions.finish(generation)
    assert not (service.jobs_root / prepared.id / 'receipt.json').exists()


@pytest.mark.parametrize('withdrawal', ['scope', 'destroy'])
def test_withdrawn_real_job_terminalizes_only_its_observer_after_owned_future_stops(setup, withdrawal):
    from carr_library.progress import ProgressHub
    service, workspace, profile, source = setup('stuck')
    frame = frame_for(service, workspace)
    actions = TranscriptionActions(frame, service=service)
    prepared = service.prepare(workspace, profile, [source],
        budget=TranscriptionBudget(stop_grace_seconds=.05, max_seconds=10))
    actions.accept(prepared)
    view = minimal_view(frame, actions)
    future_done_at_finish = []
    class ObservedHub(ProgressHub):
        def finish(self, handle, **kwargs):
            future_done_at_finish.append(view.executor.futures[0].done())
            return super().finish(handle, **kwargs)
    hub = ObservedHub()
    frame.progress_actions = SimpleNamespace(panel=hub)
    applied = []
    try:
        view._launch(lambda: actions.run(view.executor, set_running=view.set_running), applied.append)
        future = view.executor.futures[0]
        started = service.jobs_root / prepared.id / 'logs' / 'fixture.started'
        deadline = time.monotonic() + 5
        while not started.exists():
            assert not future.done() and time.monotonic() < deadline
            time.sleep(.01)
        if withdrawal == 'scope':
            frame.active = ('another-library', 'Other')
        else:
            view.destroyed(SimpleNamespace(GetId=lambda: view._window_id,
                GetEventObject=object, Skip=lambda: None))
        with pytest.raises(InterruptedError):
            future.result(timeout=6)
        deadline = time.monotonic() + 5
        while hub.snapshot()[0].state != 'cancelled':
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert future_done_at_finish == [True]
        assert not hub.snapshot()[0].verified and not applied
        assert not (service.jobs_root / prepared.id / 'receipt.json').exists()
    finally:
        actions.close()
        view.executor.shutdown(wait=True)
