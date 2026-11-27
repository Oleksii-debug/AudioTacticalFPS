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

from .capture_types import AudioBudget, AudioCancelled, AudioError


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
            if not ((isinstance(key, str) and key.casefold() in ('ffreport', 'sslkeylogfile'))
                    or (isinstance(key, bytes) and key.lower() in (b'ffreport', b'sslkeylogfile')))}


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


def wait_for_cleanup(error):
    """Settle a retained owner before a synchronous caller unwinds its scopes.

    Call this on an operation worker, never the GUI thread. Files, explicit
    streams and admission leases stay owned until the whole native family is
    quiescent. Interrupts are delivered after that boundary, not discarded.
    """
    from .capture_types import AudioCleanupPending
    if not isinstance(error, AudioCleanupPending):
        raise TypeError('An owned pending-cleanup error is required.')
    owner = error.cleanup_owner
    interrupted = None
    while True:
        try:
            if owner.quiescent_event.is_set():
                break
            owner.close()
            if not owner.quiescent_event.is_set():
                owner.quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as exc:
            if interrupted is None:
                interrupted = exc
    if interrupted is not None:
        raise interrupted


def start_owned_process(args, *, cwd=None, env=None, stdin_pipe=False,
                        stdin_file=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """Start one native-owned family with only explicit standard streams.

    This is the synchronous-call boundary used by legacy speech, rendering,
    ingestion and transcription adapters. It introduces no execution deadline;
    their existing operation/segment budgets and cancellation remain in force.
    """
    from .capture_types import AudioCleanupPending
    from .windows_job import JobProcess, _stream
    if (not isinstance(args, (tuple, list)) or not args
            or any(type(item) is not str or '\x00' in item for item in args)
            or not Path(args[0]).is_absolute()):
        raise ValueError('Use an argument vector with an absolute local executable.')
    if type(stdin_pipe) is not bool or (stdin_pipe and stdin_file is not None):
        raise ValueError('Owned processes require one explicit stdin mode.')
    if stdin_file is not None:
        _stream(stdin_file, input_stream=True)
    for selected in (stdout, stderr):
        if type(selected) is int and selected in (subprocess.PIPE, subprocess.DEVNULL):
            continue
        _stream(selected)
    environment = native_audio_environment(env)
    try:
        if os.name == 'nt':
            return JobProcess(args, cwd=cwd, env=environment, stdin_pipe=stdin_pipe,
                              stdin_file=stdin_file, stdout=stdout, stderr=stderr)
        return subprocess.Popen(args, cwd=cwd, env=environment, shell=False,
            start_new_session=True,
            stdin=subprocess.PIPE if stdin_pipe else (
                subprocess.DEVNULL if stdin_file is None else stdin_file),
            stdout=stdout, stderr=stderr)
    except AudioCleanupPending as exc:
        wait_for_cleanup(exc)
        if isinstance(exc.__cause__, (KeyboardInterrupt, SystemExit)):
            raise exc.__cause__
        raise


def _posix_group_quiescent(group):
    """Confirm no live member remains; Linux zombies cannot retain writers.

    A container's PID 1 may leave an orphan zombie unreaped indefinitely. It
    owns no descriptors and cannot execute. Only an actual bounded /proc scan
    of this exact killed group can distinguish that from a live descendant.
    Other POSIX platforms keep the stronger group-disappearance check.
    """
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    import sys
    if not sys.platform.startswith('linux'):
        return False
    seen = False
    try:
        with os.scandir('/proc') as entries:
            for count, entry in enumerate(entries, 1):
                if count > 100000:
                    return False
                if not entry.name.isdecimal():
                    continue
                try:
                    if os.getpgid(int(entry.name)) != group:
                        continue
                    with open('/proc/' + entry.name + '/stat', 'rb') as stream:
                        raw = stream.read(4097)
                    if len(raw) > 4096:
                        return False
                    fields = raw.rsplit(b')', 1)[1].split()
                    if len(fields) < 3 or int(fields[2]) != group:
                        return False
                    seen = True
                    if fields[0] not in (b'Z', b'X'):
                        return False
                except ProcessLookupError:
                    continue
                except FileNotFoundError:
                    continue
    except (OSError, IndexError, ValueError):
        return False
    if seen:
        return True
    # No visible member is not proof under hidepid or a changing process table.
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    return False


def _close_owned_family(process):
    if getattr(process, '_carr_family_quiescent', False):
        return True
    if os.name == 'nt':
        if process.close() is False:
            raise AudioError('The owned native family has not confirmed shutdown.')
        process._carr_family_quiescent = True
        return True
    _terminate(process)
    # The session leader can exit before an inference or codec descendant.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while not _posix_group_quiescent(process.pid):
        if time.monotonic() >= deadline:
            raise AudioError('The owned audio process family has not confirmed shutdown.')
        time.sleep(.02)
    process._carr_family_quiescent = True
    return True


def stop_owned_process(process):
    """Do not release a caller's stage/lease until its exact family is stopped."""
    try:
        return _close_owned_family(process)
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        from .cleanup import pending_cleanup
        error = pending_cleanup(lambda: _close_owned_family(process),
            message='Owned process cleanup is pending; operation resources remain reserved.')
        wait_for_cleanup(error)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        # A transient native cleanup failure has now recovered. The caller's
        # original cancellation/error, if any, resumes through its own finally.
        return True


def run_process(args, *, budget=None, cancelled=lambda: False,
                watch=(), progress=None, env=None, cwd=None, owned_tree=False,
                stop_requested=None, allow_failure=False):
    """Run an already constructed argument vector, never a shell expression.

    Progress callbacks receive rendered seconds from FFmpeg's progress stream.
    Ordinary stdout/stderr have a *total* byte cap, including a fast child which
    finishes between polls. Expected progress is streamed into a single latest
    value rather than accumulated for multi-hour renders.  Callbacks run on the
    caller's thread. No child inherits stdin. Explicit recording callers may
    request FFmpeg's fixed q command through an owned pipe; cancellation still
    terminates and discards incomplete output.
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
    if type(allow_failure) is not bool or type(owned_tree) is not bool:
        raise ValueError('Audio process ownership and diagnostic exits must be explicit.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before starting.')
    watched = tuple(Path(path) for path in watch)
    overflow = threading.Event()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    latest = [None]
    if owned_tree and os.name == 'nt':
        from .windows_job import JobProcess
        process = JobProcess(args, cwd=cwd, env=native_audio_environment(env),
                             stdin_pipe=stop_requested is not None)
    else:
        process = subprocess.Popen(args,
            stdin=subprocess.PIPE if stop_requested is not None else subprocess.DEVNULL,
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
            # Device names, local paths and captured metadata never enter UI errors.
            raise AudioError(f'Local audio worker failed (exit {process.returncode}).')
        return ProcessResult(bytes(buffers['stdout']), bytes(buffers['stderr']),
                             time.monotonic() - started, process.returncode,
                             stopped_at is not None)
    finally:
        def cleanup_process():
            if owned_tree:
                return _close_owned_family(process)
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

        try:
            if cleanup_process() is False:
                raise AudioError('The native capture family has not confirmed shutdown.')
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            from .cleanup import pending_cleanup
            raise pending_cleanup(cleanup_process,
                message='Audio process cleanup is still pending; its admission remains reserved.',
                callbacks=(release_streams,)) from exc
        release_streams()



def publish_file(staged, destination, *, cancelled=lambda: False):
    """Use the existing independent no-replace audio publication boundary."""
    from .jobs import publish_noreplace, regular_file
    staged, destination = Path(staged), Path(destination)
    before = regular_file(staged)
    if before.st_nlink != 1:
        raise ValueError('Captured originals cannot have hard-link aliases.')
    if cancelled():
        raise AudioCancelled('Audio operation cancelled before publication.')
    # This moves the closed stage; there is no intermediate hard-link alias.
    publish_noreplace(staged, destination)
