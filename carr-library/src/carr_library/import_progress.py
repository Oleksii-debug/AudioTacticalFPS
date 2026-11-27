"""Map durable import results to progress without changing those results."""
from .progress import ProgressOutcome, current_progress


def import_outcome(result):
    """An extracted or quarantined candidate is not a completed import."""
    if not isinstance(result, dict):
        raise ValueError('An import progress result must be a record.')
    state = result.get('state')
    if state in ('complete', 'superseded'):
        return ProgressOutcome('succeeded', 'The source was published; its saved revisions remain available.')
    if state == 'cancelled':
        return ProgressOutcome('cancelled', 'Import cancelled. Retained originals and saved checkpoints remain available.')
    messages = {
        'quarantined': 'Import needs an independent page review before it can finish.',
        'conflict': 'Import stopped because the saved source revision changed. Review before retrying.',
        'timed_out': 'Import reached its time limit. Saved checkpoints remain available.',
        'failed': 'Import did not finish. Review its detailed result before retrying.',
        'pending': 'Import has pending work. Resume it when ready.',
        'running': 'Import has not confirmed a terminal result. Inspect the saved request.',
    }
    if state not in messages:
        raise ValueError('Unsupported import completion state.')
    return ProgressOutcome('failed', messages[state])


def imports_outcome(results):
    """Keep partial failures/cancellation visible while retaining saved items."""
    if not isinstance(results, (list, tuple)):
        raise ValueError('Multiple import progress results must be a sequence.')
    outcomes = tuple(import_outcome(result) for result in results)
    counts = {state: sum(outcome.state == state for outcome in outcomes)
              for state in ('succeeded', 'failed', 'cancelled')}
    state = ('failed' if counts['failed'] else 'cancelled' if counts['cancelled'] else 'succeeded')
    return ProgressOutcome(state, f'{counts["succeeded"]} imports completed; '
        f'{counts["failed"]} need attention; {counts["cancelled"]} cancelled. '
        'Already published sources remain saved.')


def batch_outcome(batch):
    """Planning a queue and executing a queue have different success criteria."""
    if not isinstance(batch, dict):
        raise ValueError('An import batch progress result must be a record.')
    state = batch.get('state')
    if state == 'complete':
        return ProgressOutcome('succeeded', 'The saved import batch finished.')
    if state in ('paused', 'queued'):
        return ProgressOutcome('cancelled', 'The import batch paused. Remaining files stay in the saved queue.')
    if state in ('attention', 'running'):
        return ProgressOutcome('failed', 'The import batch needs attention. Inspect its saved file results.')
    raise ValueError('Unsupported import batch completion state.')


def report_batch(batch):
    """Count accepted/skipped files monotonically, including across retries."""
    task = current_progress()
    if task is None:
        return
    try:
        counts = batch['counts']
        completed = sum(counts.get(state, 0) for state in ('complete', 'duplicate', 'excluded'))
        task.update(completed=completed, total=batch['total'], phase='Importing the saved file queue')
    except Exception:
        # The batch has already checkpointed before notifying this observer.
        # Losing measurement must never turn that saved result into a retry.
        try:
            if not task.snapshot().terminal:
                task.unavailable('Progress measurement stopped. The import continues with its actual saved results.')
        except Exception:
            pass
