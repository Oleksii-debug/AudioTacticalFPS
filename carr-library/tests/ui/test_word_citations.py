"""Existing DesktopExecutor lifecycle with real SQLite/CSL and modeled Word."""
from concurrent.futures import CancelledError
import importlib.util
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from carr_library.academic_integration.word_com import WordComBridge
from carr_library.ui.word_citations import WordActions, show_word_citations
from carr_library.ui.worker_lifetime import DesktopExecutor

_spec = importlib.util.spec_from_file_location("_word_model_ui",
    Path(__file__).resolve().parents[1] / "fixtures" / "word_model.py")
model = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = model
_spec.loader.exec_module(model)


class Frame:
    """Faithful dispatcher ordering; the executor is the actual product class."""

    def __init__(self, library, workspace):
        self.library, self.active = library, (workspace, None)
        self._closing = self.working = self.busy = False
        self.cancel_event = threading.Event()
        self.executor = DesktopExecutor()
        self.calls, self.completions = [], []

    def run_job(self, title, work, done, failed, *, discard):
        assert not self.working
        self.cancel_event.clear()
        self.working = self.busy = True
        self.calls.append(title)
        future = self.executor.submit(work)
        self.completions.append((future, done, failed, discard))
        # Real DesktopFrame.run_job returns None; callers must not treat it as a Future.
        return None

    @staticmethod
    def _await_completed(future, timeout):
        completed = threading.Event()
        future.add_done_callback(lambda _future: completed.set())
        return completed.wait(timeout)

    def finish(self):
        future, done, failed, discard = self.completions[0]
        started = time.monotonic()
        if not self._await_completed(future, 10):
            # The real desktop receives completion only from add_done_callback.
            # A test readiness timeout must not manufacture a business failure
            # or revoke a still-running operation's callback/ownership.
            frames = sys._current_frames()
            workers = []
            for thread in tuple(self.executor._threads):
                frame = frames.get(thread.ident)
                if frame is not None:
                    workers.append("".join(traceback.format_stack(frame, limit=12))[-6000:])
            raise TimeoutError("Desktop test completion was not ready within 10 seconds: " + repr({
                "elapsed_seconds": time.monotonic() - started,
                "future_done": future.done(), "future_running": future.running(),
                "future_cancelled": future.cancelled(), "worker_stacks": workers,
            }))
        self.completions.pop(0)
        try:
            value = future.result()
        except BaseException as exc:
            self.working = self.busy = False
            if not self._closing:
                failed(exc)
            return exc
        self.working = self.busy = False
        if self._closing:
            discard(value)
        else:
            done(value)
        return value


class TrackingBridge(WordComBridge):
    def __init__(self, app):
        super().__init__(connect=lambda: app, identity=lambda doc: doc.identity)
        self.close_threads = []
        self.refuse_once = False

    def close(self):
        self.close_threads.append(threading.get_ident())
        if self.refuse_once:
            self.refuse_once = False
            return False
        return super().close()


@pytest.fixture
def ready(tmp_path):
    values = model.library_fixture(tmp_path)
    app = model.App()
    frame = Frame(values[0], values[1])
    bridge = TrackingBridge(app)
    actions = WordActions(frame, bridge=bridge)
    reports = []
    actions.on_report = reports.append
    try:
        yield values, app, frame, bridge, actions, reports
    finally:
        actions.close()
        frame.executor.shutdown(wait=True, cancel_futures=True)


def captured(ready):
    _, _, frame, _, actions, _ = ready
    assert actions.capture()
    frame.finish()
    assert actions.context is not None
    return actions.context


def previewed(ready):
    values, app, frame, bridge, actions, reports = ready
    captured(ready)
    assert actions.preview(dict(operation="insert", reference_ids=(values[4].id,)))
    frame.finish()
    assert actions.plan is not None, reports
    return actions.plan


def test_real_formatter_and_native_owner_use_one_existing_executor(ready):
    values, app, frame, bridge, actions, reports = ready
    original = values[0].assets.resolve(values[-1]["asset"]).read_bytes()
    plan = previewed(ready)
    confirms = []
    assert actions.apply(plan, confirm=lambda value: confirms.append((threading.get_ident(), value)) or True)
    receipt = frame.finish()
    assert receipt.token == plan.token
    assert confirms == [(threading.get_ident(), plan)]
    assert actions.context is None and actions.plan is None
    assert len(frame.calls) == 3
    assert "Document:" in reports[-1]
    assert values[0].assets.resolve(values[-1]["asset"]).read_bytes() == original
    actions.close()
    actions._cleanup_future.result(timeout=10)
    assert len(set(app.read_threads)) == 1
    assert app.read_threads[0] != threading.get_ident()
    assert bridge.close_threads == [app.read_threads[0]]
    assert actions._unregister is None and bridge._closed


@pytest.mark.parametrize("change", ["cancel", "close", "frame-close", "library", "scope", "options"],
                         ids=["cancel", "dialog-close", "desktop-close", "library-changed", "scope-changed", "preview-invalidated"])
def test_modal_return_cannot_clear_revoked_approval_before_dispatch(ready, change):
    values, app, frame, bridge, actions, reports = ready
    plan = previewed(ready)
    before = len(frame.calls)
    def confirm(value):
        assert value is plan
        if change == "cancel":
            frame.cancel_event.set()
        elif change == "close":
            actions.close()
        elif change == "frame-close":
            frame._closing = True
        elif change == "library":
            frame.library = object()
        elif change == "scope":
            frame.active = (values[2], None)
        else:
            actions.invalidate()
        return True
    with pytest.raises(InterruptedError):
        actions.apply(plan, confirm=confirm)
    assert len(frame.calls) == before and not app.document.mutations
    if change == "cancel":
        assert frame.cancel_event.is_set()


@pytest.mark.parametrize("answer", [False, None, 1], ids=["no", "closed-confirmation", "non-boolean"])
def test_declined_confirmation_never_submits_a_word_write(ready, answer):
    values, app, frame, bridge, actions, reports = ready
    plan = previewed(ready)
    assert actions.apply(plan, confirm=lambda value: answer) is False
    assert len(frame.calls) == 2 and not app.document.mutations
    assert "declined" in reports[-1]


def test_source_change_inside_confirmation_is_rechecked_on_actual_worker(ready):
    values, app, frame, bridge, actions, reports = ready
    plan = previewed(ready)
    def confirm(value):
        values[3].update(values[4], {"title": "Changed during the native confirmation"})
        return True
    assert actions.apply(plan, confirm=confirm)
    failure = frame.finish()
    assert isinstance(failure, (ValueError, RuntimeError))
    assert not app.document.mutations and actions.pending is None
    assert any("changed" in message.lower() for message in reports)


def test_closed_dialog_ignores_a_completed_queued_capture_but_releases_on_worker(ready):
    values, app, frame, bridge, actions, reports = ready
    assert actions.capture()
    frame.completions[0][0].result(timeout=10)
    before = list(reports)
    actions.close()
    frame.finish()
    actions._cleanup_future.result(timeout=10)
    assert reports == before and actions.context is None and actions.pending is None
    assert bridge._closed and bridge.close_threads == [app.read_threads[0]]


def test_shutdown_discard_acknowledges_only_completed_value_and_keeps_finalizer(ready):
    values, app, frame, bridge, actions, reports = ready
    assert actions.capture()
    frame.completions[0][0].result(timeout=10)
    frame._closing = True
    actions.close()
    frame.executor.shutdown(wait=True, cancel_futures=True)
    frame.finish()
    assert actions.pending is None and bridge._closed
    assert actions._unregister is None and not reports


def test_busy_desktop_rejects_new_owner_without_registering_another_worker(ready):
    values, app, frame, bridge, actions, reports = ready
    frame.busy = True
    count = len(frame.executor._owner_finalizers)
    with pytest.raises(ValueError, match="Wait"):
        WordActions(frame, bridge=bridge)
    assert len(frame.executor._owner_finalizers) == count
    assert not app.read_threads


def test_repeated_request_while_running_does_not_create_a_second_task(ready):
    values, app, frame, bridge, actions, reports = ready
    assert actions.capture()
    assert actions.capture() is False
    assert len(frame.calls) == 1 and len(frame.completions) == 1
    frame.finish()
    assert actions.context is not None


def test_inflight_write_survives_close_owned_until_actual_rpc_returns(ready):
    values, app, frame, bridge, actions, reports = ready
    plan = previewed(ready)
    entered, release = threading.Event(), threading.Event()
    def blocking(kind, value):
        if kind == "result":
            entered.set()
            assert release.wait(5), "release the exact modeled native mutation"
    app.document.before_mutation = blocking
    try:
        assert actions.apply(plan, confirm=lambda value: True)
        assert entered.wait(3)
        operation = actions.pending
        actions.close()
        assert actions.pending is operation
        assert not bridge._closed and not actions._cleanup_future.done()
        release.set()
        failure = frame.finish()
        assert "partial update" in str(failure)
        actions._cleanup_future.result(timeout=10)
        assert bridge._closed and actions.pending is None
        assert [kind for kind, _ in app.document.mutations] == ["add", "result"]
        assert bridge.close_threads == [app.read_threads[0]]
    finally:
        release.set()


def test_failed_owner_close_is_retained_and_retry_runs_on_same_worker(ready):
    values, app, frame, bridge, actions, reports = ready
    captured(ready)
    bridge.refuse_once = True
    actions.close()
    with pytest.raises(RuntimeError, match="confirmed release"):
        actions._cleanup_future.result(timeout=10)
    assert actions._unregister is not None and not bridge._closed
    assert len(frame.executor._owner_finalizers) == 1
    actions.close()
    assert actions._cleanup_future.result(timeout=10) is True
    assert actions._unregister is None and bridge._closed
    assert set(bridge.close_threads) == {app.read_threads[0]}


def test_modal_constructor_failure_still_queues_existing_owner_cleanup(ready, monkeypatch):
    values, app, frame, bridge, actions, reports = ready
    from carr_library.ui import word_citations
    seen = []
    def owner(frame, *, bridge):
        seen.append(actions)
        return actions
    monkeypatch.setattr(word_citations, "WordActions", owner)
    def fail(frame, actions):
        raise RuntimeError("Synthetic native dialog constructor failure")
    monkeypatch.setattr(word_citations, "_show_word_citations", fail)
    with pytest.raises(RuntimeError, match="constructor"):
        show_word_citations(frame, bridge=bridge)
    assert seen == [actions]
    assert actions._cleanup_future.result(timeout=10) is True
    assert bridge._closed and actions._unregister is None

def test_native_probe_requires_exact_explicit_office_fixture_authorization():
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[2] / "scripts" / "windows_word_citations_smoke.py"
    spec = importlib.util.spec_from_file_location("_word_probe_argument_controls", path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    valid = dict(mode="office", allow_word_write=True,
                 expected_document="Exact synthetic fixture.docx", fixture_token="a" * 32)
    probe.validate_options(SimpleNamespace(**valid))
    for field, value in (("allow_word_write", False), ("expected_document", ""),
                         ("fixture_token", "A" * 32), ("fixture_token", "a" * 33)):
        altered = {**valid, field: value}
        with pytest.raises(ValueError, match="requires"):
            probe.validate_options(SimpleNamespace(**altered))
    probe.validate_options(SimpleNamespace(mode="modeled", allow_word_write=False,
                                          expected_document=None, fixture_token=None))
    with pytest.raises(ValueError, match="do not apply"):
        probe.validate_options(SimpleNamespace(**{**valid, "mode": "modeled"}))


def test_pending_completion_does_not_dispatch_a_word_failure(ready, monkeypatch):
    values, app, frame, bridge, actions, reports = ready
    entered, release = threading.Event(), threading.Event()
    deliveries = []
    value = object()

    def work():
        entered.set()
        assert release.wait(5), "release the exact still-owned worker"
        return value

    frame.run_job("Pending modeled desktop completion", work,
                  lambda result: deliveries.append(("done", result)),
                  lambda error: deliveries.append(("failed", error)),
                  discard=lambda result: deliveries.append(("discard", result)))
    queued = frame.completions[0]
    future = queued[0]
    actual_wait = Frame._await_completed
    observed = []

    def pending_at_readiness_boundary(selected, timeout):
        observed.append((selected, timeout))
        # Model expiration of the existing readiness bound without spending it.
        # The actual worker remains blocked and the real Future stays pending.
        return actual_wait(selected, 0)

    try:
        assert entered.wait(3)
        monkeypatch.setattr(Frame, "_await_completed",
                            staticmethod(pending_at_readiness_boundary))
        with pytest.raises(TimeoutError, match="completion was not ready within 10 seconds"):
            frame.finish()
        assert observed == [(future, 10)]
        assert frame.completions == [queued]
        assert frame.working and frame.busy
        assert future.running() and not future.done()
        assert deliveries == []
        release.set()
        monkeypatch.setattr(Frame, "_await_completed", staticmethod(actual_wait))
        assert frame.finish() is value
        assert deliveries == [("done", value)]
        assert frame.completions == [] and not frame.working and not frame.busy
    finally:
        release.set()


@pytest.mark.parametrize("error_type", [TimeoutError, ValueError],
                         ids=["worker-timeout", "worker-value-error"])
def test_completed_worker_exception_still_uses_business_failure_delivery(ready, error_type):
    values, app, frame, bridge, actions, reports = ready
    error = error_type()
    deliveries = []

    def work():
        raise error

    frame.run_job("Completed modeled worker failure", work,
                  lambda result: deliveries.append(("done", result)),
                  lambda failure: deliveries.append(("failed", failure)),
                  discard=lambda result: deliveries.append(("discard", result)))
    future = frame.completions[0][0]
    assert Frame._await_completed(future, 10)
    assert frame.finish() is error
    assert deliveries == [("failed", error)]
    assert frame.completions == [] and not frame.working and not frame.busy
