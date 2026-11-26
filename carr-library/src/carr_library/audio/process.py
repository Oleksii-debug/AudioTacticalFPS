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
                watch=(), progress=None, env=None, cwd=None):
    """Run an already constructed argument vector, never a shell expression.

    Progress callbacks receive rendered seconds from FFmpeg's progress stream.
    Ordinary stdout/stderr have a *total* byte cap, including a fast child which
    finishes between polls. Expected progress is streamed into a single latest
    value rather than accumulated for multi-hour renders.  Callbacks run on the
    caller's thread. No child inherits stdin.
    """
    budget = budget or AudioBudget()
    if not isinstance(args, (tuple, list)) or not args:
        raise ValueError('Audio worker requires an argument vector.')
    if any(not isinstance(a, str) or '\x00' in a for a in args):
        raise ValueError('Invalid audio worker argument.')
    if not Path(args[0]).is_absolute():
        raise ValueError('Audio worker must use a resolved executable.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before starting.')
    watched = tuple(Path(path) for path in watch)
    overflow = threading.Event()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    latest = [None]
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        cwd=cwd, env=env, start_new_session=os.name == 'posix',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    started = time.monotonic()

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

    threads = [threading.Thread(target=read_pipe, args=(process.stdout, 'stdout'), daemon=True),
               threading.Thread(target=read_pipe, args=(process.stderr, 'stderr'), daemon=True)]
    for thread in threads:
        thread.start()
    last_progress = None

    def check():
        nonlocal last_progress
        if cancelled():
            raise AudioCancelled('Audio operation cancelled; incomplete output discarded.')
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
        if process.returncode:
            message = bytes(buffers['stderr']).decode('utf-8', errors='replace')[-3000:]
            raise AudioError(f'Local audio worker failed (exit {process.returncode}): {message.strip()}')
        return ProcessResult(bytes(buffers['stdout']), bytes(buffers['stderr']),
                             time.monotonic() - started)
    finally:
        _terminate(process)
        for thread in threads:
            thread.join(timeout=2)


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
    os.link(staged, destination, follow_symlinks=False)
    fsync_directory(destination.parent)
