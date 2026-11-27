"""Cancellable bounded subprocess execution and atomic audio publication."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time

from .models import AudioBudget, AudioCancelled, AudioError


class CaptureCleanupError(AudioError):
    """Capture termination was requested but complete cleanup is unconfirmed."""


@dataclass(frozen=True)
class ProcessResult:
    stdout: bytes
    stderr: bytes
    elapsed: float
    returncode: int = 0
    stopped: bool = False


# The fixed bootstrap cannot open a device before lifetime admission. One raw
# byte avoids consuming FFmpeg's later q command from the same owned pipe. EOF
# before admission exits without starting the selected command. On Windows the
# wrapper is attached to its kill-on-close Job before admission, so its eventual
# FFmpeg child inherits that Job. No user-provided Python or shell is evaluated.
_CAPTURE_BOOTSTRAP = '''import os, subprocess, sys
if os.read(0, 1) != b"\\x01":
    raise SystemExit(125)
raise SystemExit(subprocess.call(sys.argv[1:], stdin=0, stdout=1, stderr=2,
    shell=False, close_fds=True,
    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
'''


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


def _capture_lifetime(process):
    """Keep a Windows capture in an owned job, including after a host crash.

    The job handle is not inherited by the child. Windows closes it when this
    host exits, and KILL_ON_JOB_CLOSE terminates the capture and its children.
    An unsupported or rejected job fails closed. Ordinary render workers keep
    their existing process behavior.
    https://learn.microsoft.com/windows/win32/procthread/job-objects
    """
    if os.name != 'nt':
        return lambda: None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
            ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
            ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
            ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
            ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
            ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
             'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
            ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
            ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, arguments, result in (
            ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ('SetInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ('OpenProcess', [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ('QueryInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            ('IsProcessInJob', [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            ('TerminateProcess', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL)):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise AudioError('Windows could not protect the microphone process lifetime.')
    child = None
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise AudioError('Windows could not configure microphone cleanup.')
        # Keep the admitted wrapper's exact handle until cleanup. Descendant
        # handles acquired below use the same query/termination/wait rights.
        rights = 0x00100000 | 0x1000 | 0x0100 | 0x0001
        child = kernel.OpenProcess(rights, False, process.pid)
        if not child or not kernel.AssignProcessToJobObject(job, child):
            raise AudioError('Windows could not attach microphone cleanup; recording was stopped.')
    except BaseException:
        if child:
            kernel.CloseHandle(child)
        kernel.CloseHandle(job)
        raise

    class ProcessIds(ctypes.Structure):
        _fields_ = [('NumberOfAssignedProcesses', wintypes.DWORD),
                    ('NumberOfProcessIdsInList', wintypes.DWORD),
                    ('ProcessIdList', ctypes.c_size_t * 256)]

    released = False

    def release():
        nonlocal released
        if released:
            return
        released = True
        handles = {process.pid: child}
        confirmed = False
        stop_errors = {}
        deadline = time.monotonic() + 5

        def members():
            info = ProcessIds()
            if not kernel.QueryInformationJobObject(job, 3, ctypes.byref(info), ctypes.sizeof(info), None):
                raise CaptureCleanupError('Windows could not inspect the owned capture processes.')
            if (info.NumberOfAssignedProcesses > 256
                    or info.NumberOfProcessIdsInList > 256
                    or info.NumberOfProcessIdsInList < info.NumberOfAssignedProcesses):
                raise CaptureCleanupError('The owned capture process list exceeded its cleanup allowance.')
            return tuple(info.ProcessIdList[:info.NumberOfProcessIdsInList])

        def status(handle, milliseconds=0):
            value = kernel.WaitForSingleObject(handle, milliseconds)
            if value not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
                raise CaptureCleanupError('Windows could not confirm capture process termination.')
            return value

        try:
            # CloseHandle(Job) and TerminateJobObject request asynchronous
            # termination. Neither proves that FFmpeg released capture.wav.
            # Pin members before stopping them, then wait on those exact
            # process handles. Re-scan before release so a descendant started
            # during the first snapshot is also stopped and acknowledged.
            # https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess
            while True:
                if time.monotonic() >= deadline:
                    raise CaptureCleanupError('Capture cleanup exceeded its time allowance.')
                for identity in members():
                    if identity in handles:
                        continue
                    if len(handles) >= 256:
                        raise CaptureCleanupError('The capture process tree exceeded its cleanup allowance.')
                    handle = kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, identity)
                    if not handle:
                        if ctypes.get_last_error() == 87:  # Process already disappeared.
                            continue
                        raise CaptureCleanupError('Windows could not retain an owned capture process.')
                    owned = wintypes.BOOL()
                    if not kernel.IsProcessInJob(handle, job, ctypes.byref(owned)):
                        kernel.CloseHandle(handle)
                        raise CaptureCleanupError('Windows could not verify capture process ownership.')
                    if not owned.value:  # A PID may have been reused; never terminate an unrelated process.
                        kernel.CloseHandle(handle)
                        raise CaptureCleanupError('Capture process ownership changed during cleanup.')
                    handles[identity] = handle
                for handle in handles.values():
                    if status(handle) == 258 and not kernel.TerminateProcess(handle, 1):
                        # A concurrent exit can reject a new termination request
                        # before the retained process object becomes signaled.
                        # The request's return is never a cleanup receipt: use
                        # the same bounded exact-handle wait on either outcome.
                        stop_errors[handle] = ctypes.get_last_error()
                for handle in handles.values():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or status(handle, max(1, int(remaining * 1000))) != 0:
                        detail = (' (WinError ' + str(stop_errors[handle]) + ')'
                                  if handle in stop_errors else '')
                        raise CaptureCleanupError('Capture process termination remains unconfirmed.' + detail)
                remaining_members = members()
                if all(identity in handles and status(handles[identity]) == 0
                       for identity in remaining_members):
                    # A Job may retain an exited member while its process
                    # handle is open. Require actual termination signals for
                    # every final member, not disappearance from accounting.
                    confirmed = True
                    break
                # A descendant appeared during the preceding snapshot. The
                # next pass retains, verifies, stops and waits for its handle.
        except BaseException as exc:
            raise CaptureCleanupError(
                'Windows could not confirm complete microphone cleanup. '
                'Incomplete output must be retained; close the application before another recording.') from exc
        finally:
            if not confirmed:
                kernel.TerminateJobObject(job, 1)  # Safety request, never a successful-cleanup receipt.
            closed = True
            for handle in handles.values():
                closed = bool(kernel.CloseHandle(handle)) and closed
            closed = bool(kernel.CloseHandle(job)) and closed
            if not closed:
                raise CaptureCleanupError('Windows could not release microphone cleanup ownership.')

    return release


def run_process(args, *, budget=None, cancelled=lambda: False,
                watch=(), progress=None, env=None, cwd=None,
                stop_requested=None, allow_failure=False):
    """Run an already constructed argument vector, never a shell expression.

    Progress callbacks receive rendered seconds from FFmpeg's progress stream.
    Ordinary stdout/stderr have a *total* byte cap, including a fast child which
    finishes between polls. Expected progress is streamed into a single latest
    value rather than accumulated for multi-hour renders.  Callbacks run on the
    caller's thread. No child inherits stdin. Explicit capture callers may
    request FFmpeg's fixed ``q`` command through an owned pipe to finalize PCM;
    cancellation still terminates and discards incomplete output. An unconfirmed
    capture cleanup is a distinct error, so its staging can be retained safely.
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
    if type(allow_failure) is not bool:
        raise ValueError('Diagnostic exit handling must be explicit.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before starting.')
    watched = tuple(Path(path) for path in watch)
    overflow = threading.Event()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    latest = [None]
    command = args
    if stop_requested is not None:
        if not sys.executable or not Path(sys.executable).is_absolute():
            raise AudioError('Capture admission requires the current isolated Python runtime.')
        command = [sys.executable, '-I', '-B', '-c', _CAPTURE_BOOTSTRAP, *args]
    process = subprocess.Popen(command, stdin=subprocess.PIPE if stop_requested is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
        cwd=cwd, env=native_audio_environment(env), start_new_session=os.name == 'posix',
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

    threads = [threading.Thread(target=read_pipe, args=(process.stdout, 'stdout'), daemon=True),
               threading.Thread(target=read_pipe, args=(process.stderr, 'stderr'), daemon=True)]
    last_progress = None
    release_lifetime = None

    def check():
        nonlocal last_progress, stopped_at
        if cancelled():
            raise AudioCancelled('Audio operation cancelled; incomplete output discarded.')
        if stop_requested is not None and stopped_at is None and stop_requested() and process.poll() is None:
            stopped_at = time.monotonic()
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                process.stdin.close()
            except BrokenPipeError:
                pass  # A concurrent normal exit is checked below.
        if stopped_at is not None and time.monotonic() - stopped_at > 5 and process.poll() is None:
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
        if stop_requested is not None:
            release_lifetime = _capture_lifetime(process)
            if not callable(release_lifetime):
                raise AudioError('Capture lifetime admission did not provide an owned cleanup handle.')
            if cancelled():
                raise AudioCancelled('Audio capture cancelled before lifetime admission.')
            process.stdin.write(b'\x01')
            process.stdin.flush()
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
                             time.monotonic() - started, process.returncode, stopped_at is not None)
    finally:
        try:
            if os.name == 'nt' and release_lifetime is not None:
                # Keep all Job members discoverable until their handles have
                # been retained. Killing the wrapper first loses that boundary.
                try:
                    release_lifetime()
                finally:
                    try:
                        _terminate(process)
                    except BaseException as exc:
                        raise CaptureCleanupError('The capture launcher did not acknowledge termination.') from exc
            else:
                try:
                    _terminate(process)
                finally:
                    if release_lifetime is not None:
                        release_lifetime()
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            for thread, pipe in zip(threads, (process.stdout, process.stderr)):
                if thread.ident is not None:
                    thread.join(timeout=2)
                else:
                    pipe.close()


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
