"""Current-host timer/queued-scope tests; no substitute background executor."""
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup
from carr_library.local_schedules import ScheduleStore
from carr_library.ui import local_schedules as ui


class Timer:
    def __init__(self):
        self.starts = []
        self.stops = 0
        self.fail_once = False

    def Start(self, milliseconds):
        self.starts.append(milliseconds)

    def Stop(self):
        self.stops += 1
        if self.fail_once:
            self.fail_once = False
            raise OSError('Actual native timer release is unavailable.')


class View:
    def __init__(self):
        self.closed = False
        self.statuses = []
        self.busy = []
        self.views = []
        self.selections = 0
        self.close_calls = 0

    def set_busy(self, value):
        self.busy.append(value)

    def set_status(self, value, *, focus=False):
        self.statuses.append((value, focus))

    def set_rules(self, values):
        self.views = list(values)

    def put_rule(self, value):
        self.views = [item for item in self.views if item.id != value.id] + [value]

    def selection_changed(self):
        self.selections += 1

    def close_view(self):
        self.closed = True
        self.close_calls += 1


class Frame:
    def __init__(self, app):
        self.library = app
        self.working = False
        self._closing = False
        self.cancel_event = Event()
        self.calls = []
        self.reports = []
        self.refuse = False

    def run_job(self, title, work, done, failed, *, with_progress):
        assert with_progress is True
        if self.refuse:
            return None
        self.cancel_event.clear()
        future = Future()
        self.calls.append((title, work, done, failed, future))
        self.working = True
        return future

    def report(self, value):
        self.reports.append(value)

    def complete(self, index=-1):
        title, work, done, failed, future = self.calls[index]
        try:
            value = work(lambda *args: None)
        except BaseException as error:
            future.set_exception(error)
            self.working = False
            failed(error)
            return error
        else:
            future.set_result(value)
            self.working = False
            done(value)
            return value


@pytest.fixture
def bound(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Scheduled UI library')
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    watch = tmp_path / 'watched'
    watch.mkdir()
    frame, timer, view = Frame(app), Timer(), View()
    controller = ui.LocalScheduleController(frame, timer=timer)
    controller._attach(view)
    clock = [datetime.now(timezone.utc)]
    controller.store.clock = lambda: clock[0]
    return app, workspace, repository, watch, frame, timer, view, controller, clock


def saved(bound, kind='watch'):
    app, workspace, repository, watch, frame, timer, view, controller, clock = bound
    return controller.store.create(kind, 'Локальний розклад', repository if kind == 'backup' else watch,
        first_due=clock[0], interval_seconds=60, lateness_seconds=300,
        **({'workspace': workspace} if kind == 'watch' else {}))


def mutate(bound, change, tmp_path):
    app, _, _, _, frame, _, _, controller, _ = bound
    if change == 'same-object-new-root':
        app.root = tmp_path / 'different'
        app.root.mkdir()
    elif change == 'same-path-new-object':
        frame.library = LibraryApp(app.root)
    elif change == 'same-path-replaced-directory':
        app.root.rename(tmp_path / 'old-library')
        LibraryApp(app.root)
    elif change == 'closing':
        frame._closing = True
    elif change == 'closed-reopened-window':
        controller.dismiss()
        controller._attach(View())
    else:
        raise AssertionError(change)


@pytest.mark.parametrize('change', [
    'same-object-new-root', 'same-path-new-object', 'same-path-replaced-directory',
    'closing', 'closed-reopened-window',
])
def test_queued_save_never_crosses_exact_host_root_or_window_generation(bound, tmp_path, change):
    app, workspace, repository, watch, frame, timer, view, controller, clock = bound
    old_root = app.root
    controller.save('watch', 'Pending explicit rule', watch,
                    first_due=clock[0], workspace=workspace)
    baseline = (list(view.statuses), list(view.busy), view.selections)
    mutate(bound, change, tmp_path)
    outcome = frame.complete()
    assert isinstance(outcome, InterruptedError)
    assert (view.statuses, view.busy, view.selections) == baseline
    assert not tuple((old_root / 'state' / 'local-schedules').glob('*.json'))
    assert not timer.starts


@pytest.mark.parametrize('change', [
    'same-object-new-root', 'same-path-new-object', 'same-path-replaced-directory',
    'closing', 'closed-reopened-window',
])
def test_late_committed_save_does_not_update_stale_native_view(bound, tmp_path, change):
    app, workspace, repository, watch, frame, timer, view, controller, clock = bound
    controller.save('watch', 'Returned before scope change', watch,
                    first_due=clock[0], workspace=workspace)
    _, work, done, failed, future = frame.calls[-1]
    result = work(lambda *args: None)
    assert result.record['last'] is None
    baseline = (list(view.statuses), list(view.busy), view.selections)
    mutate(bound, change, tmp_path)
    done(result)
    failed(RuntimeError('Late old-scope completion'))
    assert (view.statuses, view.busy, view.selections) == baseline
    assert not timer.starts


def test_default_no_arm_does_not_dispatch_or_start_timer(bound, monkeypatch):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    reviews = []
    def no(parent, title, text):
        reviews.append((title, text))
        return False
    monkeypatch.setattr(ui, '_confirm', no)
    assert controller.arm(rule) is None
    assert reviews and rule.sha256 in reviews[0][1] and rule.record['target'] in reviews[0][1]
    assert frame.calls == [] and timer.starts == [] and controller.session.armed == ()


def test_reviewed_rule_arms_on_existing_worker_then_real_timer_tick_is_single(bound, monkeypatch):
    app, workspace, repository, watch, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    monkeypatch.setattr(ui, '_confirm', lambda *args: True)
    controller.arm(rule)
    assert not timer.starts and controller.session.armed == ()
    assert frame.complete() == rule.id
    assert timer.starts == [500] and controller.session.armed == (rule.id,)
    controller.on_timer()
    assert len(frame.calls) == 2
    controller.on_timer()
    assert len(frame.calls) == 2, 'A busy host must not queue another occurrence.'
    outcome = frame.complete()
    assert outcome['status'] == 'complete' and outcome['result']['batch_id'] is None
    assert view.views[-1].record['sequence'] == 1
    assert any('Scheduled operation: complete' in row[0] for row in view.statuses)


def test_busy_host_does_not_read_policy_files_from_timer(bound, monkeypatch):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    controller.session.arm(rule.id, expected=rule.sha256)
    frame.working = True
    monkeypatch.setattr(controller.store, 'get', lambda *args: pytest.fail('Timer performed file I/O.'))
    controller.on_timer()
    assert frame.calls == []


def test_disarm_does_not_cancel_an_unrelated_host_operation(bound):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    controller.session.arm(rule.id, expected=rule.sha256)
    frame.working = True
    controller.disarm()
    assert controller.session.armed == () and not frame.cancel_event.is_set()
    assert timer.stops and frame.calls == []


def test_same_window_reopen_cannot_run_an_old_queued_operation(bound):
    app, workspace, repository, watch, frame, timer, view, controller, clock = bound
    controller.save('watch', 'Old queued rule', watch, first_due=clock[0], workspace=workspace)
    old_session = controller.session
    controller.dismiss()
    replacement = View()
    controller._attach(replacement)
    outcome = frame.complete()
    assert isinstance(outcome, InterruptedError)
    assert old_session.armed == controller.session.armed == ()
    assert replacement.views == replacement.statuses == replacement.busy == []
    assert controller.store.list() == ()


def test_root_change_closes_only_schedule_view_and_disarms(bound):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    controller.session.arm(rule.id, expected=rule.sha256)
    old_session = controller.session
    controller.change_root()
    assert view.closed and view.close_calls == 1
    assert old_session.armed == () and controller.session is None
    assert not frame.cancel_event.is_set() and not hasattr(controller, 'executor')


def test_close_retries_native_timer_release_and_retains_own_handle(bound):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    controller.session.arm(rule.id, expected=rule.sha256)
    timer.fail_once = True
    with pytest.raises(OSError):
        controller.close()
    assert controller.timer is timer and not controller.current()
    controller.close()
    assert controller.session.armed == () and view.closed
    assert timer.stops == 2


def test_confirmation_scope_change_cannot_arm_a_new_window(bound, monkeypatch):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    def changed(*args):
        controller.dismiss()
        controller._attach(View())
        return True
    monkeypatch.setattr(ui, '_confirm', changed)
    with pytest.raises(InterruptedError):
        controller.arm(rule)
    assert frame.calls == [] and controller.session.armed == () and not timer.starts


def test_late_cancel_preserves_returned_result_and_disarms_repetition(bound):
    *_, frame, timer, view, controller, clock = bound
    rule = saved(bound)
    controller.session.arm(rule.id, expected=rule.sha256)
    controller.on_timer()
    _, work, done, _, _ = frame.calls[-1]
    outcome = work(lambda *args: None)
    assert outcome['status'] == 'complete'
    frame.cancel_event.set()
    frame.working = False
    done(outcome)
    assert controller.session.armed == ()
    assert any('Scheduled operation: complete' in row[0] for row in view.statuses)


def test_submit_refusal_reenables_current_controls_without_new_owner(bound):
    *_, frame, timer, view, controller, clock = bound
    frame.refuse = True
    assert controller.refresh() is None
    assert view.busy == [True, False] and not timer.starts and frame.calls == []


def test_review_text_names_exact_scope_and_inert_restart_contract(bound):
    rule = saved(bound)
    text = ui.review_text(rule)
    for expected in ('Локальний розклад', rule.record['target'], rule.record['data_dir'],
                     rule.record['workspace'], rule.sha256, 'not armed', 'restoring'):
        assert expected in text
    assert 'Armed in this window.' in ui.review_text(rule, armed=True)
