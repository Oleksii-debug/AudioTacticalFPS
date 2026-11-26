"""An observer failure is separate from the producer's actual work result."""
import pytest

from carr_library.progress import ProgressError, ProgressHub


def test_unavailable_observer_does_not_invent_operation_failure_or_success():
    hub = ProgressHub(max_tasks=1)
    task = hub.start('Committed work with incomplete reporting', total=4)
    task.advance()
    with pytest.raises(ProgressError, match='incomplete'):
        task.succeed(durable=True)
    value = task.unavailable('The operation succeeded; its unit reports were incomplete.')
    assert value.terminal and value.state == 'unavailable'
    assert value.percent == 25 and value.eta_seconds is None
    assert 'operation result' in value.describe()
    assert 'operation succeeded' in value.message.casefold()
    assert not hub.snapshots(active_only=True)
    # Faulty reporters cannot fill all active slots after their work finished.
    another = hub.start('Next actual operation')
    assert hub.snapshot().operation_id == another.operation_id


def test_successful_terminal_result_is_never_reclassified_by_a_late_observer_error():
    task = ProgressHub().start('Actual durable success')
    previous = task.succeed(durable=True)
    with pytest.raises(ProgressError, match='terminal'):
        task.unavailable('Late observer error')
    assert task.snapshot() == previous and previous.percent == 100
