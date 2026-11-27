"""Domain-result mapping preserves pending review, partial work and retries."""
from copy import deepcopy

import pytest

from carr_library.import_progress import batch_outcome, import_outcome, imports_outcome, report_batch
from carr_library.progress import ProgressHub, ProgressOutcome, bind_progress


@pytest.mark.parametrize('state,outcome', [
    ('complete', 'succeeded'), ('superseded', 'succeeded'), ('cancelled', 'cancelled'),
    ('quarantined', 'failed'), ('failed', 'failed'), ('conflict', 'failed'),
    ('timed_out', 'failed'), ('pending', 'failed'), ('running', 'failed'),
])
def test_import_results_are_not_inferred_from_returning_without_exception(state, outcome):
    original = dict(state=state, message='Preserved domain details.', edition='exact-edition')
    before = deepcopy(original)
    mapped = import_outcome(original)
    assert isinstance(mapped, ProgressOutcome) and mapped.state == outcome
    assert original == before and mapped.message


def test_partial_imports_keep_each_saved_result_and_expose_attention_before_cancellation():
    results = ({'state': 'complete'}, {'state': 'cancelled'}, {'state': 'quarantined'})
    before = deepcopy(results)
    result = imports_outcome(results)
    assert result.state == 'failed' and '1 imports completed' in result.message
    assert '1 need attention' in result.message and '1 cancelled' in result.message
    assert results == before
    assert imports_outcome(results[:2]).state == 'cancelled'
    assert imports_outcome(results[:1]).state == 'succeeded'


@pytest.mark.parametrize('state,outcome', [
    ('complete', 'succeeded'), ('paused', 'cancelled'), ('queued', 'cancelled'),
    ('attention', 'failed'), ('running', 'failed'),
])
def test_executed_batch_outcome_distinguishes_saved_queue_from_completed_work(state, outcome):
    assert batch_outcome({'state': state}).state == outcome


def test_retrying_a_failed_file_never_moves_accepted_file_progress_backwards():
    task = ProgressHub().start('Retry saved files', total=2, unit='files')
    with bind_progress(task):
        report_batch({'total': 2, 'counts': {'complete': 1, 'failed': 1}})
        assert task.snapshot().completed == 1 and task.snapshot().percent == 50
        report_batch({'total': 2, 'counts': {'complete': 1, 'running': 1}})
        assert task.snapshot().completed == 1
        report_batch({'total': 2, 'counts': {'complete': 2}})
        assert task.snapshot().completed == 2 and task.snapshot().percent == 99
    task.succeed(durable=True)
    assert task.snapshot().percent == 100


def test_observer_failure_does_not_raise_from_a_saved_batch_notification(monkeypatch):
    task = ProgressHub().start('Actual import', total=1)
    def unavailable(**kwargs):
        raise RuntimeError('Synthetic observer failure after checkpoint')
    monkeypatch.setattr(task, 'update', unavailable)
    with bind_progress(task):
        report_batch({'total': 1, 'counts': {'complete': 1}})
    assert task.snapshot().state == 'unavailable' and task.snapshot().percent != 100


@pytest.mark.parametrize('adapter,result', [
    (import_outcome, {'state': 'unknown'}), (imports_outcome, {'state': 'complete'}),
    (batch_outcome, {'state': 'unknown'}),
])
def test_unrecognized_results_cannot_invent_a_successful_outcome(adapter, result):
    with pytest.raises(ValueError):
        adapter(result)
