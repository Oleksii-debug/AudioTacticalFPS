"""Retain existing job/resource locks until an owned worker is quiescent."""
from contextlib import contextmanager, ExitStack
import os

from ..audio.cleanup import AudioCleanupPending
from ..durable_state import local_path, regular


def _pending(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, AudioCleanupPending):
            return current
        pending.extend((current.__cause__, current.__context__))
    return None


@contextmanager
def retained_context(context):
    """Borrow one existing context; transfer it only to its actual cleanup owner."""
    stack = ExitStack()
    try:
        value = stack.enter_context(context)
        try:
            yield value
        except BaseException as error:
            pending = _pending(error)
            if pending is not None:
                retained = stack.pop_all()
                pending.cleanup_owner.add_quiescent_callback(retained.close)
            raise
    finally:
        stack.close()


@contextmanager
def _lock(root, name):
    from filelock import FileLock, Timeout
    root = local_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ('.' + name + '.lock')
    if os.path.lexists(path):
        regular(path)
    try:
        # Cleanup may complete on the existing owner's retry thread.
        with FileLock(path, timeout=0, mode=0o600, thread_local=False):
            yield
    except Timeout:
        raise RuntimeError('This transcription job is already owned by another operation.') from None


@contextmanager
def operation_lock(root, name):
    with retained_context(_lock(root, name)):
        yield
