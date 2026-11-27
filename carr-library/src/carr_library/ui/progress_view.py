"""Worker lifecycle helpers for the one shared native progress controller.

The host keeps its existing executor and service results. Native controls,
settings and notification ownership live in operation_progress.
"""
from ..progress import ProgressHub, finish_result


def shared_hub(frame, *, clock=None):
    hub = getattr(frame, 'progress_hub', None)
    if hub is None:
        frame.progress_hub = hub = ProgressHub(**({'clock': clock} if clock is not None else {}))
    return hub


def fail_operation(reporter, error):
    """Preserve a committed terminal result and the original service exception."""
    from ..audio.jobs import RenderCancelled
    try:
        if reporter.event.terminal:
            return
        if isinstance(error, (InterruptedError, RenderCancelled, KeyboardInterrupt)):
            reporter.cancel('The operation was stopped.')
        else:
            reporter.fail('The operation did not complete.')
    except Exception:
        pass  # A detached observer cannot replace a business/submission error.


def finish_observed(reporter, result, *, cancelled=False):
    """Reporting cannot replace an already returned domain result."""
    try:
        if not reporter.event.terminal:
            finish_result(reporter, result, cancelled=cancelled)
    except Exception:
        try:
            if not reporter.event.terminal:
                reporter.pause('The operation returned; progress reporting is unavailable. Review its result.')
        except Exception:
            pass  # A detached observer cannot cause completed work to retry.


def run_observed(reporter, operation, *, finalize=True):
    """Bind one host reporter inside the actual existing worker thread."""
    try:
        with reporter.bind():
            result = operation()
    except BaseException as error:
        fail_operation(reporter, error)
        raise
    if finalize:
        finish_observed(reporter, result)
    return result
