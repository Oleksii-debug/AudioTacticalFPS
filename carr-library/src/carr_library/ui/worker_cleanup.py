"""Release an owned service on its serial worker after the active task exits."""


def close_after_worker(executor, service):
    """Queue cleanup after submitted work and return without waiting in the UI.

    The caller first requests cooperative cancellation and stops scheduling
    work. Its single-worker executor preserves task order, so a voice process
    is never closed while synthesis is still using it.
    """
    future = executor.submit(service.close)
    executor.shutdown(wait=False, cancel_futures=False)
    return future
