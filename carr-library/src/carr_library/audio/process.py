"""Cancellable bounded subprocess execution and atomic audio publication."""
from __future__ import annotations

from dataclasses import dataclass
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


def native_audio_environment(environment=None):
    """Keep required host settings without ambient FFmpeg report-file writes.

    FFREPORT can write to an arbitrary inherited path even for a read-only
    inspection. Diagnostic capture belongs to this application's bounded pipes.
    Work on a copy so callers' explicit environments and the host stay intact.
    """
    return {key: value for key, value in
            (os.environ if environment is None else environment).items()
            if not ((isinstance(key, str) and key.casefold() == 'ffreport')
                    or (isinstance(key, bytes) and key.lower() == b'ffreport'))}


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


def run_process(args, *, budget=None, cancelled=lambda: False,
                watch=(), progress=None, env=None, cwd=None, owned_windows_job=False,
                stop_requested=None, allow_failure=False):
    """Run an already constructed argument vector, never a shell expression.

    Progress callbacks receive rendered seconds from FFmpeg's progress stream.
    Ordinary stdout/stderr have a *total* byte cap, including a fast child which
    finishes between polls. Expected progress is streamed into a single latest
    value rather than accumulated for multi-hour renders.  Callbacks run on the
    caller's thread. No child inherits stdin. Explicit recording callers may
    request FFmpeg's fixed q command through an owned pipe; cancellation still
    terminates and discards incomplete output. The Windows-only ownership opt-in
    attaches a private process job at launch; ordinary callers and POSIX process
    groups keep their existing termination behavior.
    """
    budget = budget or AudioBudget()
    if not isinstance(args, (tuple, list)) or not args:
        raise ValueError('Audio worker requires an argument vector.')
    if any(not isinstance(a, str) or '\x00' in a for a in args):
        raise ValueError('Invalid audio worker argument.')
    if not Path(args[0]).is_absolute():
        raise ValueError('Audio worker must use a resolved executable.')
    if stop_requested is not None and not callable(stop_requested):
        raise ValueError('Graceful audio stop requires a callback.')
    if type(allow_failure) is not bool or type(owned_windows_job) is not bool:
        raise ValueError('Audio process ownership and diagnostic exits must be explicit.')
    if owned_windows_job and os.name != 'nt':
        raise ValueError('Owned Windows audio jobs require native Windows.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before starting.')
    watched = tuple(Path(path) for path in watch)
    overflow = threading.Event()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    latest = [None]
    if owned_windows_job and os.name == 'nt':
        from .windows_job import JobProcess
        process = JobProcess(args, cwd=cwd, env=env,
                             stdin_pipe=stop_requested is not None)
    else:
        process = subprocess.Popen(args,
            stdin=subprocess.PIPE if stop_requested is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            cwd=cwd, env=env, start_new_session=os.name == 'posix',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    started = time.monotonic()
    stopped_at = None

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
        finally:
            pipe.close()

    threads = []
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
        if (stopped_at is not None and time.monotonic() - stopped_at > 5
                and process.poll() is None):
            raise AudioError('Audio capture did not finalize within its stop allowance; incomplete output discarded.')
        if overflow.is_set():
            raise AudioError('Audio worker exceeded its diagnostic output budget.')
        if time.monotonic() - started > budget.timeout:
            raise AudioError('Audio worker exceeded its time allowance; completed chunks remain reusable.')
        total, count = 0, 0
        pending = list(watched)
        while pending:
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
        for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            threads.append(threading.Thread(target=read_pipe, args=(pipe, name), daemon=True))
        for thread in threads:
            thread.start()
        while process.poll() is None:
            check()
            try:
                process.wait(timeout=.05)
            except subprocess.TimeoutExpired:
                pass
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
                             time.monotonic() - started, process.returncode,
                             stopped_at is not None)
    finally:
        def cleanup_process():
            if owned_windows_job and os.name == 'nt':
                return process.close()
            _terminate(process)
            return True

        def release_streams():
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            for index, pipe in enumerate((process.stdout, process.stderr)):
                thread = threads[index] if index < len(threads) else None
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=2)
                elif pipe is not None:
                    pipe.close()

        if not owned_windows_job and stop_requested is None:
            # Existing render/conversion callers keep their termination path.
            _terminate(process)
            release_streams()
        else:
            try:
                cleanup_process()
            except Exception as exc:
                from .cleanup import pending_cleanup
                raise pending_cleanup(cleanup_process,
                    message='Audio process cleanup is still pending; its admission remains reserved.',
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
