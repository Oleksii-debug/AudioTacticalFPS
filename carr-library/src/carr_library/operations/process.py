"""Shared cancellable subprocess boundary reused from the reviewed ASR/audio runner."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

class ProcessError(RuntimeError):
    """A local audio operation failed without publishing an output."""


class ProcessCancelled(ProcessError):
    """An operation was cancelled; previous completed outputs are preserved."""


class ProcessUnavailable(ProcessError):
    """An optional installed engine or verified capability is unavailable."""


def finite(value, label, minimum=None, maximum=None):
    if type(value) not in (int, float):
        raise ValueError(f'{label} must be a finite number.')
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} must be a finite number.') from None
    if not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number.')
    if minimum is not None and value < minimum:
        raise ValueError(f'{label} must be at least {minimum}.')
    if maximum is not None and value > maximum:
        raise ValueError(f'{label} must be at most {maximum}.')
    return float(value)


def integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} must be an integer from {minimum} to {maximum}.')
    return value


@dataclass(frozen=True)
class ProcessBudget:
    """Explicit local resource allowances, independent of project duration.

    Output bytes include staging files.  Increase the budget intentionally for
    larger jobs; there is no ten-minute (or similar) project-duration ceiling.
    The FFmpeg per-allocation limit is not a whole-process memory sandbox.
    """
    timeout: float = 3600
    max_output_bytes: int = 32 * 1024**3
    max_log_bytes: int = 2 * 1024**2
    reserve_disk_bytes: int = 32 * 1024**2
    max_inputs: int = 64
    threads: int = 2
    max_alloc_bytes: int = 256 * 1024**2
    max_staging_files: int = 10000

    def __post_init__(self):
        finite(self.timeout, 'Timeout', .05, 365 * 86400)
        integer(self.max_output_bytes, 'Output budget', 4096, 2**63 - 1)
        integer(self.max_log_bytes, 'Log budget', 1024, 128 * 1024**2)
        integer(self.reserve_disk_bytes, 'Free disk reserve', 0, 2**63 - 1)
        integer(self.max_inputs, 'Simultaneous inputs', 1, 1024)
        integer(self.threads, 'Worker threads', 1, 64)
        integer(self.max_alloc_bytes, 'Allocation budget', 1024**2, 2**31 - 1)
        integer(self.max_staging_files, 'Staging file budget', 1, 1000000)





@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    elapsed: float


def _poll_process(process):
    """Observe a Linux session leader without releasing its owned group ID."""
    if sys.platform == 'linux' and process.returncode is None:
        try:
            status = os.waitid(os.P_PID, process.pid,
                               os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            raise ProcessError('The owned worker leader was reaped outside its lifecycle.') from None
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
    return process.poll()


def _wait_linux_group(group, *, timeout):
    """Confirm that no owned group member is still executing, within a bound.

    Exited orphan zombies no longer execute or retain diagnostic streams.
    This covers the inherited process group, not descendants that deliberately
    escape their session. It is not a native-code sandbox.
    """
    deadline = time.monotonic() + timeout
    expected = str(group).encode('ascii')
    while True:
        active = False
        with os.scandir('/proc') as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise TimeoutError('The owned worker group did not terminate in time.')
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (Path(entry.path) / 'stat').read_bytes()
                    fields = raw.rsplit(b') ', 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    try:
                        member = os.getpgid(int(entry.name)) == group
                    except ProcessLookupError:
                        continue
                    if member:
                        raise ProcessError('Cannot confirm termination of an owned worker.')
                    continue
                except (IndexError, ValueError):
                    raise ProcessError('Cannot parse the native worker-group observation.') from None
                if len(fields) < 3:
                    raise ProcessError('Incomplete native worker-group observation.')
                if fields[2] == expected and fields[0] not in {b'Z', b'X', b'x'}:
                    active = True
                    break
        if not active:
            return
        time.sleep(min(.01, max(0, deadline - time.monotonic())))


def _terminate(process):
    """Stop owned execution before reaping the Linux group leader.

    Unconfirmed shutdown raises; it never admits a normal result. The external
    broker still owns its existing context/release policy, so this function
    does not promise resource-lease retention after an error unwinds.
    Windows retains its existing direct-process termination boundary.
    """
    if sys.platform == 'linux':
        if process.returncode is not None:
            return
        # A waitable leader pins the group identity until its execution has
        # stopped. Do not signal a potentially reused ID after external reap.
        _poll_process(process)
        try:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                _wait_linux_group(process.pid, timeout=1)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _wait_linux_group(process.pid, timeout=5)
        except (OSError, TimeoutError, ProcessError) as exc:
            # Request forceful shutdown even if observation itself failed.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise ProcessError('Owned worker shutdown could not be confirmed; '
                               'the operation did not complete.') from exc
        finally:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return
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
    budget = budget or ProcessBudget()
    if not isinstance(args, (tuple, list)) or not args:
        raise ValueError('Audio worker requires an argument vector.')
    if any(not isinstance(a, str) or '\x00' in a for a in args):
        raise ValueError('Invalid audio worker argument.')
    if not Path(args[0]).is_absolute():
        raise ValueError('Audio worker must use a resolved executable.')
    if cancelled():
        raise ProcessCancelled('Audio operation cancelled before starting.')
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

    threads, started_pipes = [], set()
    last_progress = None

    def check():
        nonlocal last_progress
        if cancelled():
            raise ProcessCancelled('Audio operation cancelled; incomplete output discarded.')
        if overflow.is_set():
            raise ProcessError('Audio worker exceeded its diagnostic output budget.')
        if time.monotonic() - started > budget.timeout:
            raise ProcessError('Audio worker exceeded its time allowance; completed chunks remain reusable.')
        total, count = 0, 0
        pending = list(watched)
        while pending:
            path = pending.pop()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ProcessError('Audio worker output is not a regular file.')
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        count += 1
                        if count > budget.max_staging_files:
                            raise ProcessError('Audio worker exceeded its staging file budget.')
                        pending.append(Path(entry.path))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ProcessError('Audio worker output is not a regular file.')
            total += info.st_size
            if total > budget.max_output_bytes:
                raise ProcessError('Audio worker exceeded its output disk budget.')
        if watched and shutil.disk_usage(watched[0].parent).free < budget.reserve_disk_bytes:
            raise ProcessError('Audio render stopped to preserve the configured free disk reserve.')
        if progress is not None and latest[0] is not None and latest[0] != last_progress:
            progress(latest[0])
            last_progress = latest[0]

    try:
        # Reader allocation/start can fail after Popen (for example exhausted
        # thread resources). Keep it inside native-process ownership so that
        # callers cannot release their reservation while the child survives.
        for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            thread = threading.Thread(target=read_pipe, args=(pipe, name), daemon=True)
            threads.append(thread)
            thread.start()
            started_pipes.add(pipe)
        while _poll_process(process) is None:
            check()
            if sys.platform == 'linux':
                # wait(timeout) and poll() reap an exited leader. Keep it
                # waitable until descendants release their inherited pipes.
                time.sleep(.05)
            else:
                try:
                    process.wait(timeout=.05)
                except subprocess.TimeoutExpired:
                    pass
        # A successful leader can leave a child executing and holding stdout.
        # Close owned group execution before draining or accepting any result.
        _terminate(process)
        for thread in threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise ProcessError('Audio worker did not close its diagnostic streams.')
        # Required even for a child which exited before the first polling tick.
        check()
        if process.returncode:
            message = bytes(buffers['stderr']).decode('utf-8', errors='replace')[-3000:]
            raise ProcessError(f'Local audio worker failed (exit {process.returncode}): {message.strip()}')
        return ProcessResult(bytes(buffers['stdout']), bytes(buffers['stderr']),
                             time.monotonic() - started)
    finally:
        try:
            _terminate(process)
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=2)
            # A reader owns and closes its stream. Streams whose readers never
            # started still belong to this caller and must be closed here.
            for pipe in (process.stdout, process.stderr):
                if pipe not in started_pipes:
                    pipe.close()


def publish_file(staged, destination, *, cancelled=lambda: False):
    """Use the existing product's checked, atomic no-overwrite file publisher."""
    from ..audio.jobs import publish_new
    if cancelled():
        raise ProcessCancelled('Operation cancelled before publication.')
    publish_new(staged, destination)
