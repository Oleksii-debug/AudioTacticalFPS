"""Cancellable bounded subprocess execution and atomic audio publication."""
from __future__ import annotations

from contextlib import contextmanager
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


class _PosixGroupProcess:
    """Keep a Linux session leader waitable until its owned group is quiet.

    Popen.poll()/wait() normally reap the leader and release its numeric identity.
    This adapter observes it with WNOWAIT instead. The existing new-session
    launch then pins the process-group identity while verified pidfds prove
    member exit. This is group ownership, not a process sandbox.
    """

    _MAX_PROC_ENTRIES = 100000
    _MAX_MEMBERS = 4096

    def __init__(self, args, **options):
        import sys
        if (not sys.platform.startswith('linux')
                or not all(hasattr(os, name) for name in ('waitid', 'WNOWAIT', 'pidfd_open'))
                or not hasattr(signal, 'pidfd_send_signal')):
            raise RuntimeError('Owned local audio requires Linux waitid/pidfd support or the Windows process job.')
        # Check native availability before creating a child. The leader must
        # remain waitable; an explicitly ignored SIGCHLD would defeat that pin.
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            raise RuntimeError('Owned audio requires a waitable child; SIGCHLD is explicitly ignored.')
        self._stat(os.getpid())
        probe = os.pidfd_open(os.getpid(), 0)
        os.close(probe)
        self._members = {}
        self._lock = threading.RLock()
        self._observed = None
        self._disposal_error = None
        self._group_quiet = self._reaped = self._closed = False
        self._process = subprocess.Popen(args, **options)

    def __getattr__(self, name):
        return getattr(self._process, name)

    @property
    def returncode(self):
        return self._observed

    @staticmethod
    def _stat(pid):
        with open('/proc/' + str(pid) + '/stat', 'rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise RuntimeError('The native process identity exceeds its bounded inventory.')
        before, separator, after = raw.rpartition(b') ')
        fields = after.split()
        if (not separator or len(fields) < 20 or not before.startswith(str(pid).encode('ascii') + b' (')):
            raise RuntimeError('The native process identity could not be verified.')
        return int(fields[2]), int(fields[3]), int(fields[19])

    def poll(self):
        with self._lock:
            if self._reaped:
                return self._observed
            value = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if value is not None and value.si_pid:
                if value.si_pid != self.pid:
                    raise RuntimeError('The owned process wait returned a different identity.')
                if value.si_code == os.CLD_EXITED:
                    self._observed = value.si_status
                elif value.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
                    self._observed = -value.si_status
                else:
                    raise RuntimeError('The owned process did not return an exit status.')
            return self._observed

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.01 if deadline is None else min(.01, max(0, deadline - time.monotonic())))
        return self.returncode

    def send_signal(self, value):
        with self._lock:
            if self.poll() is None:
                # The exact unreaped child still owns this PID.
                os.kill(self.pid, value)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    @staticmethod
    def _ready(descriptor):
        import select
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        rows = poller.poll(0)
        if not rows:
            return False
        flags = rows[0][1]
        if flags & (select.POLLERR | select.POLLNVAL):
            raise RuntimeError('An owned process handle lost its native exit proof.')
        return bool(flags & (select.POLLIN | select.POLLHUP))

    @staticmethod
    def _deadline(deadline):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired('owned audio process group cleanup', 5)

    def _inventory(self, deadline):
        # The caller has not reaped its session leader; another session cannot
        # acquire this process-group ID while its original leader remains.
        self.poll()
        if self._stat(self.pid)[:2] != (self.pid, self.pid):
            raise RuntimeError('The selected audio process group lost its leader identity.')
        with os.scandir('/proc') as entries:
            for index, entry in enumerate(entries):
                self._deadline(deadline)
                if index >= self._MAX_PROC_ENTRIES:
                    raise RuntimeError('The native process inventory exceeded its bounded allowance.')
                if not entry.name.isascii() or not entry.name.isdecimal():
                    continue
                pid = int(entry.name)
                try:
                    identity = self._stat(pid)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if identity[:2] != (self.pid, self.pid):
                    continue
                key = pid, identity[2]
                if key in self._members:
                    continue
                if len(self._members) >= self._MAX_MEMBERS:
                    raise RuntimeError('The owned audio group exceeded its member allowance.')
                try:
                    descriptor = os.pidfd_open(pid, 0)
                except ProcessLookupError:
                    # The kernel no longer has this process object. A final
                    # inventory still checks every remaining group member.
                    continue
                try:
                    try:
                        after = self._stat(pid)
                    except (FileNotFoundError, ProcessLookupError):
                        if not self._ready(descriptor):
                            raise RuntimeError('A native process disappeared before its exit could be proved.')
                        after = identity
                    if after != identity:
                        raise RuntimeError('A native process changed identity during group admission.')
                except BaseException:
                    try:
                        os.close(descriptor)
                    except OSError as exc:
                        # Linux can release the number even when close fails.
                        # Never retain/retry an unverified raw descriptor number.
                        self._disposal_error = exc
                    raise
                self._members[key] = descriptor
        if not any(pid == self.pid for pid, _ in self._members):
            raise RuntimeError('The native inventory omitted the retained audio leader.')

    def close(self):
        with self._lock:
            if self._closed:
                return True
            deadline = time.monotonic() + 5
            if not self._group_quiet:
                while True:
                    self._deadline(deadline)
                    try:
                        self._inventory(deadline)
                    except BaseException:
                        # A failed inventory never proves quiescence. Still
                        # stop the group if its exact unreaped leader can be
                        # authenticated, and stop known handles independently.
                        try:
                            self.poll()
                            if self._stat(self.pid)[:2] == (self.pid, self.pid):
                                os.killpg(self.pid, signal.SIGKILL)
                        except Exception:
                            pass
                        for descriptor in self._members.values():
                            try:
                                if not self._ready(descriptor):
                                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            except Exception:
                                pass
                        raise
                    # WNOWAIT still pins the leader here, so this signal cannot
                    # select a recycled group. Keep every admitted handle.
                    os.killpg(self.pid, signal.SIGKILL)
                    for descriptor in self._members.values():
                        if not self._ready(descriptor):
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                    if all(self._ready(fd) for fd in self._members.values()):
                        prior = set(self._members)
                        self._inventory(deadline)
                        if prior == set(self._members) and all(self._ready(fd) for fd in self._members.values()):
                            self._group_quiet = True
                            break
                    time.sleep(min(.01, max(0, deadline - time.monotonic())))
            if not self._reaped:
                self._deadline(deadline)
                self._observed = self._process.wait(timeout=max(0, deadline - time.monotonic()))
                self._reaped = True
            # Linux may release a descriptor number even when close reports
            # an error. Never retry that integer: another thread could reuse it.
            # Native exit proof remains retained and the failure stays explicit.
            for key, descriptor in tuple(self._members.items()):
                del self._members[key]
                try:
                    os.close(descriptor)
                except OSError as exc:
                    self._disposal_error = exc
            if self._disposal_error is not None:
                raise RuntimeError('Native audio stopped, but a Linux process handle could not be disposed '
                                   'with certainty. Close the application before retrying this operation.') from self._disposal_error
            self._closed = True
            return True


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
    """Retain the exact owned family until each native member has stopped."""
    if os.name != 'nt':
        if isinstance(process, _PosixGroupProcess):
            return process.close()
        _terminate(process)
        if process.poll() is None:
            raise RuntimeError('The capture process has not confirmed shutdown.')
        return True
    job = getattr(process, '_carr_job', None)
    if job is None:
        raise RuntimeError('Capture requires the existing owned Windows process job.')
    return job.close(leader=process)


def _start_process(args, *, owned_tree=False, **options):
    launcher = subprocess.Popen
    if owned_tree and os.name == 'nt':
        from .windows_process import WindowsJobProcess
        launcher = WindowsJobProcess
    elif owned_tree and os.name == 'posix':
        launcher = _PosixGroupProcess
    return launcher(args, start_new_session=os.name == 'posix',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0,
        **options)



@contextmanager
def owned_process(args, *, quiescent_callbacks=(), **options):
    """Use the same native family owner with an adapter's existing I/O protocol.

    File/pipe adapters keep their own bounded readers and legacy error contracts.
    Their outer retained contexts keep staging and handles until this owner has
    confirmed shutdown, including errors during reader allocation or startup.
    """
    callbacks = tuple(quiescent_callbacks)
    if any(not callable(callback) for callback in callbacks):
        raise TypeError('Owned process cleanup callbacks must be callable.')
    options['env'] = native_audio_environment(options.get('env'))
    process = _start_process(args, owned_tree=True, **options)

    stopped = False
    remaining = list(callbacks)

    def finish():
        nonlocal stopped
        if not stopped:
            if _stop_owned(process) is not True:
                raise RuntimeError('The owned audio process has not confirmed shutdown.')
            stopped = True
        # These are required I/O cleanup steps, not optional notifications.
        # A failed step stays owned and is retried before staging can be released.
        while remaining:
            if remaining[0]() is False:
                raise RuntimeError('An owned audio reader has not confirmed shutdown.')
            remaining.pop(0)
        return True

    try:
        yield process
    finally:
        try:
            finish()
        except Exception as exc:
            from .cleanup import pending_cleanup
            raise pending_cleanup(finish,
                message='Audio process cleanup is still pending; its resources remain reserved.') from exc


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
    The worker timeout ends at its first observed exit; later caller-side
    validation does not extend execution or erase cancellation/output checks.
    ProcessResult.elapsed still includes that validation and pipe collection.
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
    observed_exit_at = None
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

    threads, started_pipes = [], set()
    last_progress = None

    def check():
        nonlocal last_progress, stopped_at, observed_exit_at
        # A cancellation callback may also verify a completed checkpoint. Take
        # positive process evidence before that work, never backdate an exit
        # first observed after the allowance or during a slow callback.
        if observed_exit_at is None and process.poll() is not None:
            observed_exit_at = time.monotonic()
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
        worker_until = time.monotonic() if observed_exit_at is None else observed_exit_at
        if worker_until - started > budget.timeout:
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
        # Allocation and startup can both fail after the child is created.
        # Keep both inside its existing owned-process cleanup boundary.
        for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            thread = threading.Thread(target=read_pipe, args=(pipe, name), daemon=True)
            threads.append(thread)
            thread.start()
            started_pipes.add(pipe)
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
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=2)
            # A started reader closes its own pipe; allocation/start failures
            # leave the other streams owned by this cleanup, including retries.
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and pipe not in started_pipes:
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
