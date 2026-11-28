"""Bounded metadata subprocesses and borrowed-context lifetime for voice packages.

Only the installed owner's fixed package API runs here. This is not a second
synthesizer, training service or process supervisor.
"""
from contextlib import contextmanager
import math
import os
import tempfile

from hashlib import sha256
from pathlib import Path

from .models import AudioBudget, AudioCleanupPending
from .process import run_process
from .voice_ownership import settle_voice_cleanup

MAX_LOG_BYTES = 2 * 1024 * 1024


def _pending_errors(error):
    pending, seen, found = [error], set(), {}
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, AudioCleanupPending):
            found[id(current.cleanup_owner)] = current
        pending.extend((current.__cause__, current.__context__))
    return tuple(found.values())


@contextmanager
def retained_context(context):
    """Settle existing native owners before releasing this borrowed context."""
    with context as value:
        try:
            yield value
        except BaseException as error:
            interrupted = None
            for pending in _pending_errors(error):
                try:
                    settle_voice_cleanup(pending)
                except (KeyboardInterrupt, SystemExit) as deferred:
                    if interrupted is None:
                        interrupted = deferred
            if interrupted is not None:
                raise interrupted from error
            raise


@contextmanager
def retained_temporary_directory(*, prefix, dir):
    with retained_context(tempfile.TemporaryDirectory(prefix=prefix, dir=dir)) as temporary:
        yield temporary


def run_metadata(command, *, data=b'', cancelled=lambda: False, timeout=120, env=None, cwd=None):
    """Use current bounded run_process and Windows Job with a hashed request file."""
    if (type(data) is not bytes or len(data) > 2 * 1024 * 1024
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 600
            or not callable(cancelled)):
        raise ValueError('Choose a bounded local package operation.')
    if cancelled():
        raise InterruptedError('Local voice package operation cancelled.')
    with retained_context(tempfile.TemporaryDirectory(prefix='carr-voice-request-')) as temporary:
        root = Path(temporary).resolve(strict=True)
        request = root / 'request.json'
        request.write_bytes(data)
        checksum = sha256(data).hexdigest()
        result = run_process([*[os.fspath(item) for item in command], str(request), checksum],
            budget=AudioBudget(timeout=timeout, threads=1,
                max_alloc_bytes=256 * 1024**2, max_log_bytes=MAX_LOG_BYTES,
                max_output_bytes=4 * 1024**2, max_inputs=1, max_staging_files=8),
            cwd=cwd, env=env, cancelled=cancelled, watch=(root,),
            owned_windows_job=os.name == 'nt', allow_failure=True)
        if result.returncode:
            # Native diagnostics may contain private source data or paths.
            raise RuntimeError('The local owner package API failed (exit ' + str(result.returncode) + ').')
        if len(result.stdout) > MAX_LOG_BYTES:
            raise ValueError('The owner package API exceeded its metadata byte budget.')
        if request.read_bytes() != data:
            raise ValueError('The voice management request changed during its owned operation.')
        return result.stdout
