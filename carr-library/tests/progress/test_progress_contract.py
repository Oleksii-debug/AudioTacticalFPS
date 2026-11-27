"""Independent state, measurement and concurrency acceptance for V3 progress."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import math
from threading import Event, Thread

import pytest

from carr_library.progress import ProgressHub, bind_progress, current_progress


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_measured_quarters_never_claim_completion_before_durable_success():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Synthetic import', total=100, unit='pages')
    assert task.snapshot().percent == 0
    assert task.snapshot().completed == 0
    for completed, percent in ((25, 25), (50, 50), (75, 75), (100, 99)):
        clock.advance(1)
        task.update(completed=completed, phase='Importing source pages')
        state = task.snapshot()
        assert state.completed == completed and state.total == 100
        assert state.percent == percent and state.state == 'running'
        assert state.phase == 'Importing source pages'
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.succeed(durable=False)
    assert task.snapshot() == before
    task.succeed(durable=True)
    assert task.snapshot().percent == 100 and task.snapshot().state == 'succeeded'


def test_unknown_work_stays_indeterminate_without_a_clock_based_percentage():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Unknown-duration query')
    for duration in (0, 1, 30, 3600):
        clock.advance(duration)
        task.update(phase='Searching', message='Still processing actual sources')
        state = task.snapshot()
        assert state.total is None and state.percent is None and state.eta_seconds is None
        assert state.state == 'running'
    task.succeed(durable=True)
    assert task.snapshot().state == 'succeeded' and task.snapshot().percent == 100


def test_known_empty_operation_reaches_completion_only_when_confirmed():
    task = ProgressHub().start('Empty checked folder', total=0, unit='files')
    assert task.snapshot().percent == 0 and task.snapshot().completed == 0
    task.succeed(durable=True)
    assert task.snapshot().percent == 100


@pytest.mark.parametrize('wrong', [None, 0, 1, 'yes', [], {}])
def test_durable_success_requires_an_explicit_boolean_true(wrong):
    task = ProgressHub().start('Synthetic durable write')
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.succeed(durable=wrong)
    assert task.snapshot() == before


def test_incomplete_known_units_cannot_be_reported_as_success():
    task = ProgressHub().start('Known source count', total=4)
    task.advance(3)
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.succeed(durable=True)
    assert task.snapshot() == before


@pytest.mark.parametrize('wrong', [-1, True, False, 1.2, '3', [], {}])
def test_invalid_count_updates_do_not_mutate_the_previous_snapshot(wrong):
    task = ProgressHub().start('Validated units', total=10)
    task.advance(3)
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.update(completed=wrong)
    assert task.snapshot() == before


@pytest.mark.parametrize('wrong', [-1, True, False, 1.5, '10', [], {}])
def test_invalid_total_updates_do_not_mutate_the_previous_snapshot(wrong):
    task = ProgressHub().start('Validated total', total=10)
    task.advance(3)
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.update(total=wrong)
    assert task.snapshot() == before


def test_fixed_total_progress_cannot_go_backwards_or_exceed_real_total():
    task = ProgressHub().start('Measured units', total=10)
    task.update(completed=7)
    before = task.snapshot()
    for completed in (6, 11):
        with pytest.raises(ValueError):
            task.update(completed=completed)
        assert task.snapshot() == before
    with pytest.raises(ValueError):
        task.update(total=6)
    assert task.snapshot() == before


def test_changed_total_is_disclosed_instead_of_preserving_a_fictitious_percentage():
    task = ProgressHub().start('Discovered files', total=10, unit='files')
    task.advance(5)
    assert task.snapshot().percent == 50
    task.update(total=20)
    state = task.snapshot()
    assert state.completed == 5 and state.total == 20 and state.percent == 25
    assert 'total' in state.message.casefold() and '20' in state.message
    task.update(total=None)
    assert task.snapshot().percent is None and task.snapshot().eta_seconds is None


def test_eta_uses_actual_unit_measurements_and_does_not_count_down_on_poll():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Measured work', total=100)
    task.advance(10)
    assert task.snapshot().eta_seconds is None  # Zero measured elapsed time.
    clock.advance(2)
    task.advance(10)
    measured = task.snapshot()
    assert measured.eta_seconds is not None and math.isfinite(measured.eta_seconds)
    assert measured.eta_seconds > 0
    clock.advance(500)
    assert task.snapshot() == measured  # Reading a status never fabricates work.
    task.update(phase='New phase')
    assert task.snapshot().eta_seconds is None


def test_pause_resume_excludes_paused_time_from_throughput_estimate():
    clock = Clock()
    task = ProgressHub(clock=clock).start('Pausable render', total=100)
    clock.advance(2)
    task.advance(20)
    task.pause()
    assert task.snapshot().state == 'paused'
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.advance()
    assert task.snapshot() == before
    clock.advance(500)
    task.resume()
    assert task.snapshot().state == 'running' and task.snapshot().eta_seconds is None
    clock.advance(1)
    task.advance(10)
    assert 0 < task.snapshot().eta_seconds < 30


def test_cancellation_request_is_distinct_from_acknowledged_cancellation():
    task = ProgressHub().start('Cooperative conversion', total=10)
    task.advance(4)
    task.request_cancel()
    assert task.snapshot().state == 'cancelling' and task.snapshot().percent != 100
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.advance()
    assert task.snapshot() == before
    task.cancelled('Worker confirmed cancellation before publication')
    assert task.snapshot().state == 'cancelled' and task.snapshot().percent != 100


def test_success_after_a_late_cancellation_request_is_not_falsely_called_cancelled():
    task = ProgressHub().start('Durable write', total=1)
    task.advance()
    task.request_cancel()
    task.succeed(durable=True)
    assert task.snapshot().state == 'succeeded' and task.snapshot().percent == 100


@pytest.mark.parametrize('ending', ['failed', 'cancelled', 'succeeded'])
def test_terminal_operations_cannot_receive_late_worker_updates(ending):
    task = ProgressHub().start('Finished worker', total=1)
    task.advance()
    if ending == 'failed':
        task.fail('Synthetic publication error')
    elif ending == 'cancelled':
        task.cancelled('Synthetic cancellation')
    else:
        task.succeed(durable=True)
    before = task.snapshot()
    with pytest.raises(ValueError):
        task.update(phase='Late worker callback')
    assert task.snapshot() == before
    assert (before.percent == 100) is (ending == 'succeeded')


def test_threaded_increments_are_atomic_and_observed_counts_never_regress():
    hub = ProgressHub()
    task = hub.start('Concurrent measured producers', total=1600)
    stop = Event()
    observed = []

    def observe():
        while not stop.is_set():
            observed.append(task.snapshot().completed)
            stop.wait(0.0005)

    observer = Thread(target=observe)
    observer.start()
    try:
        def produce(_):
            for _ in range(200):
                task.advance()
        with ThreadPoolExecutor(max_workers=8) as workers:
            tuple(workers.map(produce, range(8)))
    finally:
        stop.set()
        observer.join(timeout=3)
    assert not observer.is_alive()
    assert observed == sorted(observed)
    assert task.snapshot().completed == 1600 and task.snapshot().percent == 99
    task.succeed(durable=True)
    assert hub.snapshot(task.snapshot().operation_id).percent == 100


def test_subtasks_do_not_fabricate_overall_completion_or_share_counters():
    hub = ProgressHub()
    parent = hub.start('Whole production')
    child = hub.start('Measured child', total=2, parent_id=parent.snapshot().operation_id)
    child.advance(2)
    child.succeed(durable=True)
    assert child.snapshot().parent_id == parent.snapshot().operation_id
    assert parent.snapshot().state == 'running' and parent.snapshot().percent is None
    assert parent.snapshot().completed == 0
    assert {snapshot.operation_id for snapshot in hub.snapshots(active_only=True)} == {
        parent.snapshot().operation_id}


def test_history_is_bounded_without_silently_removing_running_operations():
    hub = ProgressHub(max_tasks=3)
    first = hub.start('Active first')
    second = hub.start('Active second')
    third = hub.start('Active third')
    before = hub.snapshots()
    with pytest.raises(ValueError):
        hub.start('Cannot evict active work')
    assert hub.snapshots() == before
    second.succeed(durable=True)
    fourth = hub.start('New active operation')
    remaining = {snapshot.operation_id for snapshot in hub.snapshots()}
    assert len(remaining) == 3
    assert first.snapshot().operation_id in remaining
    assert third.snapshot().operation_id in remaining
    assert fourth.snapshot().operation_id in remaining


def test_snapshots_are_immutable_values_and_do_not_change_after_later_updates():
    task = ProgressHub().start('Snapshot isolation', total=2)
    initial = task.snapshot()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        initial.completed = 2
    task.advance()
    assert initial.completed == 0 and task.snapshot().completed == 1
    assert initial.revision < task.snapshot().revision


def test_worker_binding_is_scoped_nested_and_isolated_between_threads():
    hub = ProgressHub()
    outer, inner = hub.start('Outer task'), hub.start('Inner task')
    assert current_progress() is None
    with bind_progress(outer):
        assert current_progress() is outer
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert workers.submit(current_progress).result(timeout=3) is None
        with pytest.raises(RuntimeError):
            with bind_progress(inner):
                assert current_progress() is inner
                raise RuntimeError('Synthetic callback failure')
        assert current_progress() is outer
    assert current_progress() is None


def test_progress_description_exposes_actual_state_phase_and_units():
    task = ProgressHub().start('Synthetic import', total=4, unit='pages')
    task.advance(1, phase='Indexing verified text')
    text = task.snapshot().describe()
    assert '25' in text and '1' in text and '4' in text and 'pages' in text
    assert 'Indexing verified text' in text and 'Synthetic import' in text
    task.fail('Synthetic storage failure')
    assert 'fail' in task.snapshot().describe().casefold()
