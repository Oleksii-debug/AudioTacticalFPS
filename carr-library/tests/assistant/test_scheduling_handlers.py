"""Actual assistant handlers reuse one executor for schedule review and execution."""
from datetime import datetime, timedelta, timezone

import pytest

from carr_library.assistant import AssistantService
from carr_library.assistant.scheduling import ScheduleManager
from carr_library.ui import assistant_schedule
from tests.assistant.test_planning_handlers import native_handlers


@pytest.fixture
def scheduling_handlers(native_handlers, monkeypatch):
    env = native_handlers
    env.clock = [datetime(2026, 11, 27, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(ScheduleManager, '_now', lambda self: env.clock[0])
    env.selected_action = 'arm'
    env.time_review = lambda parent, plan, pin: (env.clock[0] + timedelta(seconds=2), 60)
    def choose_time(parent, plan, pin):
        assert not env.executor.in_worker
        return env.time_review(parent, plan, pin)
    def choose_schedule(parent, rows, armed):
        assert not env.executor.in_worker
        assert len(rows) == 1
        view, plan = rows[0]
        assert view.plan_sha256 and plan['steps']
        return env.selected_action, view.id, view.schedule_sha256
    monkeypatch.setattr(assistant_schedule, 'choose_time', choose_time)
    monkeypatch.setattr(assistant_schedule, 'choose_schedule', choose_schedule)
    env.timers = []
    class Timer:
        def __init__(self, owner):
            self.owner, self.running, self.starts, self.stops = owner, False, 0, 0
            env.timers.append(self)
        def Start(self, milliseconds):
            assert milliseconds == 1000
            self.running, self.starts = True, self.starts + 1
        def Stop(self):
            self.running, self.stops = False, self.stops + 1
    env.wx.Timer, env.wx.EVT_TIMER = Timer, 'native-schedule-timer'
    env.tick = lambda: env.dialog.bindings[env.wx.EVT_TIMER](None)
    original_review, original_create = ScheduleManager.review, ScheduleManager.create
    def review(self, **kwargs):
        assert env.executor.in_worker, 'Schedule journal enumeration ran on the event thread.'
        return original_review(self, **kwargs)
    def create(self, *args, **kwargs):
        assert env.executor.in_worker, 'Schedule journal publication ran on the event thread.'
        return original_create(self, *args, **kwargs)
    monkeypatch.setattr(ScheduleManager, 'review', review)
    monkeypatch.setattr(ScheduleManager, 'create', create)
    return env


def prepare_and_save(env):
    env.controls['instruction'].ChangeValue('Створи бібліотеку "Scheduled native handlers"')
    env.press('preview')
    env.finish()
    assert env.controls['schedule'].enabled
    env.press('schedule')
    assert len(env.executor.pending) == 1 and AssistantService(env.app).jobs() == ()
    env.finish()  # Read review -> native time choice -> queue save on same executor.
    assert len(env.executor.pending) == 1 and AssistantService(env.app).jobs() == ()
    env.finish()
    job, = AssistantService(env.app).jobs()
    assert job.status == 'queued' and env.app.workspaces() == ()
    return job.id


def arm_saved(env):
    env.press('schedules')
    env.finish()  # Exact journal snapshot -> native selection.
    assert len(env.executor.pending) == 1
    env.finish()
    assert env.timers[-1].running and env.controls['disarm'].enabled


def test_save_then_explicit_arm_then_timer_uses_one_existing_executor_once(scheduling_handlers):
    def scenario(env):
        identity = prepare_and_save(env)
        assert not env.timers and not env.controls['disarm'].enabled
        arm_saved(env)
        env.tick()
        assert not env.executor.pending and env.app.workspaces() == ()
        env.clock[0] += timedelta(seconds=2)
        env.tick()
        assert len(env.executor.pending) == 1 and env.app.workspaces() == ()
        assert not env.controls['schedules'].enabled
        env.finish()
        record = AssistantService(env.app).store.load(identity)
        assert record['status'] == record['schedule']['state'] == 'complete'
        assert len(env.app.workspaces()) == 1
        env.tick()
        assert not env.executor.pending and len(env.app.workspaces()) == 1
    scheduling_handlers.show(scenario)


def test_close_disarms_and_late_timer_never_reopens_a_destroyed_window(scheduling_handlers):
    def scenario(env):
        prepare_and_save(env)
        arm_saved(env)
        env.press('close')
        assert not env.timers[-1].running
        env.clock[0] += timedelta(seconds=3)
        env.tick()
        assert not env.executor.pending and env.app.workspaces() == ()
    scheduling_handlers.show(scenario)


def test_explicit_disarm_cancels_due_submission_before_real_dispatch(scheduling_handlers):
    def scenario(env):
        identity = prepare_and_save(env)
        arm_saved(env)
        env.clock[0] += timedelta(seconds=3)
        env.tick()
        assert len(env.executor.pending) == 1
        env.press('disarm')
        env.finish()
        assert env.app.workspaces() == ()
        record = AssistantService(env.app).store.load(identity)
        assert record['status'] == 'queued' and record['schedule']['state'] == 'pending'
        assert not env.timers[-1].running
    scheduling_handlers.show(scenario)


def test_changed_selected_goal_during_time_review_creates_nothing(scheduling_handlers):
    def scenario(env):
        env.controls['instruction'].ChangeValue('Створи бібліотеку "Original"')
        env.press('preview'); env.finish()
        def changed(parent, plan, pin):
            env.controls['instruction'].ChangeValue('Створи бібліотеку "Changed"')
            return env.clock[0] + timedelta(seconds=3), 60
        env.time_review = changed
        env.press('schedule'); env.finish()
        assert not env.executor.pending and AssistantService(env.app).jobs() == ()
        assert 'changed during review' in env.controls['review'].GetValue()
    scheduling_handlers.show(scenario)


def test_committed_queued_id_survives_schedule_publication_failure(scheduling_handlers, monkeypatch):
    def unavailable(self, *args, **kwargs):
        assert scheduling_handlers.executor.in_worker
        raise OSError('Schedule publication unavailable after the job was queued.')
    monkeypatch.setattr(ScheduleManager, 'create', unavailable)
    def scenario(env):
        env.controls['instruction'].ChangeValue('Створи бібліотеку "Durable queued"')
        env.press('preview'); env.finish()
        env.press('schedule'); env.finish(); env.finish()
        row, = AssistantService(env.app).jobs()
        assert row.status == 'queued' and env.app.workspaces() == ()
        assert env.controls['run'].enabled and not env.timers
        assert 'after the job was queued' in env.controls['review'].GetValue()
        env.press('run'); env.finish()
        assert AssistantService(env.app).get(row.id).status == 'complete'
        assert len(env.app.workspaces()) == 1
    scheduling_handlers.show(scenario)


def test_pause_of_arm_action_disarms_before_any_timer_can_dispatch(scheduling_handlers):
    def scenario(env):
        identity = prepare_and_save(env)
        env.press('schedules'); env.finish()
        env.executor.complete()  # Arm succeeded; GUI acknowledgement remains queued.
        env.press('pause')
        env.drain()
        assert not env.timers and not env.controls['disarm'].enabled
        env.clock[0] += timedelta(seconds=3)
        record = AssistantService(env.app).store.load(identity)
        assert record['status'] == 'queued' and env.app.workspaces() == ()
    scheduling_handlers.show(scenario)


def test_root_switch_discards_schedule_review_before_any_publication(scheduling_handlers):
    def scenario(env):
        env.press('preview'); env.finish()
        env.press('schedule')
        env.executor.complete()
        env.frame.library = object()
        env.drain()
        assert not env.executor.pending and not env.timers
        assert AssistantService(env.app).jobs() == ()
    scheduling_handlers.show(scenario)


def test_native_review_size_has_an_explicit_bound():
    with pytest.raises(ValueError, match='review allowance'):
        assistant_schedule.plan_text({'summary': 'x' * (assistant_schedule.MAX_REVIEW_CHARACTERS + 1)})


@pytest.mark.parametrize('action', ['schedule-save', 'run'], ids=['schedule-save', 'ordinary-run'])
def test_failed_thread_start_cannot_replay_a_retained_mutation_on_a_later_submission(
        scheduling_handlers, monkeypatch, action):
    env = scheduling_handlers
    original_submit = env.executor.submit
    reject = [False]
    def queued_then_rejected(callback, *args, **kwargs):
        future = original_submit(callback, *args, **kwargs)
        if reject[0]:
            reject[0] = False
            raise RuntimeError('Synthetic native thread-start failure after enqueue.')
        return future
    monkeypatch.setattr(env.executor, 'submit', queued_then_rejected)
    def scenario(e):
        e.controls['instruction'].ChangeValue('Create library "Must not replay"')
        e.press('preview'); e.finish()
        if action == 'schedule-save':
            e.press('schedule')
            # The review succeeds; its callback tries to enqueue the mutation.
            e.executor.complete()
            reject[0] = True
            e.drain()
        else:
            reject[0] = True
            e.press('run')
        assert 'thread-start failure' in e.controls['review'].GetValue()
        assert len(e.executor.pending) == 1 and e.controls['preview'].enabled
        e.controls['instruction'].ChangeValue('List libraries')
        e.press('preview')
        assert len(e.executor.pending) == 2
        e.finish()  # A later worker reaches the old retained work item first.
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
        e.finish()  # The newly accepted read-only preview still works.
        assert e.controls['run'].enabled and AssistantService(e.app).jobs() == ()
    env.show(scenario)
