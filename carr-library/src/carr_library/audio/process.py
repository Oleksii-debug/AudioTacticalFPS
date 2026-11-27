"""Cancellable bounded subprocess execution and atomic audio publication."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import threading
import time

from .models import AudioBudget, AudioCancelled, AudioError


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    elapsed: float
    returncode: int = 0
    stopped: bool = False


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait(timeout=5)


# Fixed quit/owned-family controls adapted from reviewed PR123 (28a4e542).
def native_audio_environment(environment=None):
    """Keep required host settings without ambient FFmpeg report-file writes.

    FFREPORT can write to an arbitrary inherited path even for a read-only
    inspection. Diagnostic capture belongs to this application's bounded pipes.
    Work on a copy so callers' explicit environments and the host stay intact.
    """
    return {key: value for key, value in
            (os.environ if environment is None else environment).items()
            if not ((isinstance(key, str) and key.casefold() in ('ffreport', 'sslkeylogfile'))
                    or (isinstance(key, bytes) and key.lower() in (b'ffreport', b'sslkeylogfile')))}


def _stop_owned(process):
    """Keep the exact owned job handle until every native child has stopped."""
    if os.name != 'nt':
        _terminate(process)
        if process.poll() is None:
            raise RuntimeError('The capture process has not confirmed shutdown.')
        return True
    from .windows_process import _Accounting
    job = getattr(process, '_carr_job', None)
    if job is None:
        raise RuntimeError('Capture requires the existing owned Windows process job.')
    deadline = time.monotonic() + 5
    handle = job.handle
    if handle is not None and not job.api.kernel.TerminateJobObject(handle, 1):
        raise job.api.error()
    process.wait(timeout=max(0, deadline - time.monotonic()))
    # Release the leader reference before querying the full job's active count.
    retained = getattr(process, '_handle', None)
    if retained is not None:
        retained.Close()
    if handle is not None:
        query = job.api.kernel.QueryInformationJobObject
        query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                          ctypes.c_uint32, ctypes.c_void_p]
        query.restype = ctypes.c_int
        while True:
            accounting = _Accounting()
            if not query(handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise job.api.error()
            if accounting.active_processes == 0:
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, 5)
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
        # A failed close keeps the handle available to the retained owner.
        if not job.api.kernel.CloseHandle(handle):
            raise job.api.error()
        job.handle = None
    return True


def _start_process(args, *, owned_tree=False, **options):
    launcher = subprocess.Popen
    if owned_tree and os.name == 'nt':
        from .windows_process import WindowsJobProcess
        launcher = WindowsJobProcess
    return launcher(args, start_new_session=os.name == 'posix',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
        **options)


def run_process(args, *, budget=None, cancelled=lambda: False,
                watch=(), progress=None, env=None, cwd=None, owned_tree=False,
                stop_requested=None, allow_failure=False):
    """Run an already constructed argument vector, never a shell expression.

    Progress callbacks receive rendered seconds from FFmpeg's progress stream.
    Ordinary stdout/stderr have a *total* byte cap, including a fast child which
    finishes between polls. Expected progress is streamed into a single latest
    value rather than accumulated for multi-hour renders.  Callbacks run on the
    caller's thread. No child inherits stdin. Explicit recording calls may send
    only FFmpeg's fixed q command to an owned pipe; Cancel still discards output.
    Existing render callers retain their original process contract.
    """
    budget = budget or AudioBudget()
    if not isinstance(args, (tuple, list)) or not args:
        raise ValueError('Audio worker requires an argument vector.')
    if any(not isinstance(a, str) or '\x00' in a for a in args):
        raise ValueError('Invalid audio worker argument.')
    if not Path(args[0]).is_absolute():
        raise ValueError('Audio worker must use a resolved executable.')
    if type(owned_tree) is not bool or type(allow_failure) is not bool:
        raise ValueError('Audio process ownership and diagnostic exits must be explicit.')
    if stop_requested is not None and (not callable(stop_requested) or not owned_tree):
        raise ValueError('Graceful recording stop requires an owned process and callback.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before starting.')
    watched = tuple(Path(path) for path in watch)
    overflow = threading.Event()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    latest = [None]
    process = _start_process(args, owned_tree=owned_tree,
        stdin=subprocess.PIPE if stop_requested is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        cwd=cwd, env=native_audio_environment(env))
    started = time.monotonic()
    stopped_at = None
    reader_failures = []

    def read_pipe(pipe, name):
        count = 0
        partial = b''
        try:
            while True:
                data = pipe.read1(4096)
                if not data:
                    break
                if name == 'stdout' and progress is not None:
                    partial += data
                    if len(partial) > budget.max_log_bytes:
                        overflow.set()
                        partial = b''
                    lines = partial.split(b'\n')
                    partial = lines.pop()
                    for line in lines:
                        if len(line) > 1024:
                            overflow.set()
                        if line.startswith(b'out_time_us='):
                            try:
                                latest[0] = max(0, int(line.split(b'=', 1)[1]) / 1000000)
                            except (ValueError, OverflowError):
                                pass
                    continue
                count += len(data)
                if count > budget.max_log_bytes:
                    overflow.set()
                available = budget.max_log_bytes - len(buffers[name])
                buffers[name].extend(data[:max(0, available)])
        except Exception as exc:
            reader_failures.append(exc)
        finally:
            pipe.close()

    threads = [threading.Thread(target=read_pipe, args=(process.stdout, 'stdout'), daemon=True),
               threading.Thread(target=read_pipe, args=(process.stderr, 'stderr'), daemon=True)]
    last_progress = None

    def check():
        nonlocal last_progress, stopped_at
        if cancelled():
            raise AudioCancelled('Audio operation cancelled; incomplete output discarded.')
        if (stop_requested is not None and stopped_at is None
                and stop_requested() and process.poll() is None):
            stopped_at = time.monotonic()
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                process.stdin.close()
            except BrokenPipeError:
                pass  # A concurrent exit is checked by the normal result path.
        if stopped_at is not None and time.monotonic() - stopped_at > 5 and process.poll() is None:
            raise AudioError('Audio capture did not finalize within its stop allowance; incomplete output discarded.')
        if reader_failures:
            raise AudioError('Audio worker diagnostic stream could not be read.') from reader_failures[0]
        if overflow.is_set():
            raise AudioError('Audio worker exceeded its diagnostic output budget.')
        if time.monotonic() - started > budget.timeout:
            raise AudioError('Audio worker exceeded its time allowance; completed chunks remain reusable.')
        total, count = 0, 0
        pending = list(watched)
        while pending:
            if cancelled():
                raise AudioCancelled('Audio operation cancelled during staging inspection.')
            path = pending.pop()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise AudioError('Audio worker output is not a regular file.')
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if cancelled():
                            raise AudioCancelled('Audio operation cancelled during staging inspection.')
                        count += 1
                        if count > budget.max_staging_files:
                            raise AudioError('Audio worker exceeded its staging file budget.')
                        pending.append(Path(entry.path))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise AudioError('Audio worker output is not a regular file.')
            total += info.st_size
            if total > budget.max_output_bytes:
                raise AudioError('Audio worker exceeded its output disk budget.')
        if watched and shutil.disk_usage(watched[0].parent).free < budget.reserve_disk_bytes:
            raise AudioError('Audio render stopped to preserve the configured free disk reserve.')
        if progress is not None and latest[0] is not None and latest[0] != last_progress:
            progress(latest[0])
            last_progress = latest[0]

    try:
        for thread in threads:
            thread.start()
        while process.poll() is None:
            check()
            try:
                process.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                pass
        # Observe a cancellation even when the leader exits between polls;
        # an owned descendant may still hold its diagnostic pipes open.
        check()
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise AudioError('Audio worker did not close its diagnostic streams.')
        # Required even for a child which exited before the first polling tick.
        check()
        if process.returncode and not allow_failure:
            message = bytes(buffers['stderr']).decode('utf-8', errors='replace')[-3000:]
            raise AudioError(f'Local audio worker failed (exit {process.returncode}): {message.strip()}')
        return ProcessResult(bytes(buffers['stdout']), bytes(buffers['stderr']),
                             time.monotonic() - started, process.returncode, stopped_at is not None)
    finally:
        def cleanup_process():
            if owned_tree:
                return _stop_owned(process)
            _terminate(process)
            return True

        def release_streams():
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            for thread, pipe in zip(threads, (process.stdout, process.stderr)):
                if thread.ident is not None:
                    thread.join(timeout=2)
                elif pipe is not None:
                    pipe.close()

        try:
            cleanup_process()
        except Exception as exc:
            if not owned_tree:
                release_streams()
                raise
            from .cleanup import pending_cleanup
            raise pending_cleanup(cleanup_process,
                message='Audio process cleanup is still pending; capture remains reserved.',
                callbacks=(release_streams,)) from exc
        release_streams()


def fsync_directory(path):
    if os.name != 'posix':
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_file(staged, destination, *, cancelled=lambda: False):
    """Publish a closed staged regular file atomically and without overwrite.

    Staging is on the destination filesystem. A failing hard-link operation
    fails closed instead of falling back to a partial or overwriting copy.
    """
    staged, destination = Path(staged), Path(destination)
    info = staged.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise AudioError('A rendered artifact must be a regular file.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before publication.')
    # Windows requires a writable descriptor to flush the completed staging file.
    with staged.open('r+b') as source:
        os.fsync(source.fileno())
    # Flush may block long enough for the user to cancel. Check again at the
    # last pre-commit boundary; after link succeeds, publication is committed.
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before publication.')
    os.link(staged, destination, follow_symlinks=False)
    fsync_directory(destination.parent)
