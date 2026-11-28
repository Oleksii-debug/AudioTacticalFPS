"""Cancellable bounded subprocess execution and atomic audio publication."""
from __future__ import annotations

from dataclasses import dataclass
import json
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


def _posix_group_snapshot(group, deadline):
    """Read bounded kernel membership; never select a process from user files."""
    result = []
    count = 0
    with os.scandir('/proc') as entries:
        for entry in entries:
            count += 1
            if count > 100_000 or time.monotonic() >= deadline:
                raise TimeoutError('Owned process membership exceeded its cleanup allowance.')
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            try:
                identity = _posix_process_identity(int(entry.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if identity[0] == group:
                result.append((int(entry.name), identity[1]))
                if len(result) > 256:
                    raise OSError('The owned process group exceeds its cleanup allowance.')
    return tuple(result)


def _posix_process_identity(pid):
    with open('/proc/' + str(pid) + '/stat', 'rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096 or b')' not in raw:
        raise OSError('The kernel process identity exceeds its bounded format.')
    # The command field can contain spaces and parentheses. Fields after the
    # final ')' begin at state; pgrp is index2 and starttime is index19.
    fields = raw.rsplit(b')', 1)[1].split()
    if len(fields) < 20:
        raise OSError('The kernel process identity is incomplete.')
    return int(fields[2]), int(fields[19])


def _capture_posix_group(group, retained, deadline):
    for identity in _posix_group_snapshot(group, deadline):
        if identity in retained:
            continue
        if len(retained) >= 256:
            raise OSError('The owned process group exceeds its cleanup allowance.')
        pid, started = identity
        try:
            handle = os.pidfd_open(pid, 0)
        except ProcessLookupError:
            continue
        try:
            try:
                current = _posix_process_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                current = None
            if current != (group, started):
                continue
            retained[identity] = handle
            handle = None
        finally:
            if handle is not None:
                os.close(handle)


def _posix_process_exited(handle, milliseconds):
    import select
    observer = select.poll()
    observer.register(handle, select.POLLIN)
    events = observer.poll(milliseconds)
    if not events:
        return False
    if any(descriptor != handle or flags & (select.POLLERR | select.POLLNVAL)
           for descriptor, flags in events):
        raise OSError('The owned process exit handle could not be observed.')
    # POLLIN confirms final-thread exit; POLLHUP confirms subsequent reaping.
    return any(flags & (select.POLLIN | select.POLLHUP) for _descriptor, flags in events)


def _wait_posix_group(group, retained, *, use_pidfds):
    """Confirm quiescence, preserving exact handles after an uncertain wait."""
    deadline = time.monotonic() + 5
    if not use_pidfds:
        # Other POSIX hosts have no portable wait for non-child process objects.
        # Only disappearance of the same owned group confirms completion.
        while True:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                return True
            if time.monotonic() >= deadline:
                raise TimeoutError('Owned process-group exit remains unconfirmed.')
            time.sleep(min(.01, max(0, deadline - time.monotonic())))
    while True:
        _capture_posix_group(group, retained, deadline)
        for handle in retained.values():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not _posix_process_exited(handle, max(1, int(remaining * 1000))):
                raise TimeoutError('Owned process termination remains unconfirmed.')
        current = _posix_group_snapshot(group, deadline)
        if all(identity in retained and _posix_process_exited(retained[identity], 0)
               for identity in current):
            for identity, handle in tuple(retained.items()):
                os.close(handle)
                del retained[identity]
            return True
        if time.monotonic() >= deadline:
            raise TimeoutError('Owned process-group exit remains unconfirmed.')


def _can_own_posix_group():
    # Keep the leader unreaped while its numeric process-group ID is authority.
    # Linux pidfds then confirm final-thread exit without mistaking zombies for
    # running work. Unsupported hosts are refused before a child is created.
    return (all(hasattr(os, name) for name in
                ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT', 'CLD_EXITED', 'pidfd_open'))
            and Path('/proc/self/stat').is_file())


def _scratch_overflow_sample(root, limit, total, count, regular_count,
                             pending_count, trigger, largest):
    """Diagnostic only: original lstat samples from one sequential check.

    This is a sampled prefix, not simultaneous physical disk allocation.
    No paths are opened, resolved, or sampled again while formatting it.
    """
    def row(sample):
        path, info = sample
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            name = str(path)
        return {'path': name[-192:], 'path_truncated': len(name) > 192,
                'size': info.st_size, 'dev': info.st_dev, 'ino': info.st_ino,
                'mode': info.st_mode, 'nlink': info.st_nlink,
                'attributes': int(getattr(info, 'st_file_attributes', 0))}

    spelling = str(root)
    value = {'schema': 'carr-scratch-original-lstat-prefix/1',
             'root': spelling[-256:], 'root_truncated': len(spelling) > 256,
             'limit': limit, 'sampled_prefix_bytes': total,
             'enumerated_entries': count, 'sampled_regular_files': regular_count,
             'pending_paths': pending_count, 'trigger': row(trigger),
             'largest_prior_samples': [row(sample) for sample in largest],
             'truncated': regular_count > len(largest) + 1}
    while True:
        encoded = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
        if len(encoded.encode('ascii')) <= 1700:
            return encoded
        value['truncated'] = True
        if value['largest_prior_samples']:
            value['largest_prior_samples'].pop()
        elif len(value['root']) > 32:
            value['root'] = value['root'][-max(32, len(value['root']) // 2):]
            value['root_truncated'] = True
        elif len(value['trigger']['path']) > 32:
            value['trigger']['path'] = value['trigger']['path'][-max(
                32, len(value['trigger']['path']) // 2):]
            value['trigger']['path_truncated'] = True
        else:
            # Native stat integers have finite platform widths; this also
            # preserves the original refusal if formatting ever exceeds them.
            return '{"schema":"carr-scratch-original-lstat-prefix/1","truncated":true}'


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

    With owned_tree, the original native deadline is observed independently of
    synchronous caller validation. A timely native root exit does not expire
    while those callbacks verify completed files; the same retained family is
    still stopped at its absolute bound. All final stop/integrity checks and
    confirmed owner cleanup remain mandatory before return.
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
    owned_posix = owned_tree and os.name == 'posix'
    if owned_posix and not _can_own_posix_group():
        raise AudioError('This host cannot safely observe an owned process group before reaping its leader.')
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

    def root_status():
        if not owned_posix or process.returncode is not None:
            return process.poll() if not owned_posix else process.returncode
        try:
            observed = os.waitid(os.P_PID, process.pid,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError as exc:
            raise AudioError('The owned process leader was reaped outside its cleanup owner.') from exc
        if observed is None:
            return None
        return observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status

    # The native deadline belongs to the retained process owner. A synchronous
    # caller callback may verify large files after the native root already
    # exited; that work must remain cancellable without becoming native time.
    deadline = started + budget.timeout
    deadline_lock = threading.Lock()
    deadline_stop = threading.Event()
    deadline_state = {'exit_observed': None, 'expired': False, 'error': None}
    deadline_thread = None
    successful_result = False

    def observed_root_status():
        if not owned_tree:
            return root_status()
        with deadline_lock:
            status = root_status()
            observed = time.monotonic()
            if status is not None and deadline_state['exit_observed'] is None:
                deadline_state['exit_observed'] = observed
                if observed > deadline:
                    deadline_state['expired'] = True
            return status

    def signal_owned_deadline():
        if owned_posix:
            # Main-thread reaping occurs only after this observer is joined.
            # WNOWAIT must still confirm the exact retained group leader.
            root_status()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            # JobProcess keeps the exact Job/member handles and serializes its
            # bounded stop/wait. It never closes the owner here.
            process.kill()

    def observe_deadline():
        try:
            while not deadline_stop.is_set():
                status = observed_root_status()
                now = time.monotonic()
                if now >= deadline:
                    with deadline_lock:
                        if deadline_state['exit_observed'] is None:
                            deadline_state['expired'] = True
                    # Even a timely root can leave owned descendants. Stop that
                    # same retained family at the original absolute bound while
                    # caller verification is still running.
                    signal_owned_deadline()
                    return
                deadline_stop.wait(min(.05, deadline - now))
        except BaseException as error:
            with deadline_lock:
                deadline_state['error'] = error
            # Fail closed without releasing ownership. Cleanup retries on the
            # caller thread only after this observer has actually stopped.
            try:
                signal_owned_deadline()
            except BaseException:
                pass

    def check_native_deadline(*, observe=True):
        if not owned_tree:
            expired = time.monotonic() - started > budget.timeout
            failure = None
        else:
            if observe:
                observed_root_status()
            with deadline_lock:
                failure = deadline_state['error']
                expired = deadline_state['expired']
                if deadline_state['exit_observed'] is None and time.monotonic() > deadline:
                    expired = True
                    deadline_state['expired'] = True
        if failure is not None:
            raise AudioError('Audio worker deadline observation failed; its owned cleanup remains required.') from failure
        if expired:
            raise AudioError('Audio worker exceeded its time allowance; completed chunks remain reusable.')

    def stop_deadline_observer():
        deadline_stop.set()
        if deadline_thread is not None and deadline_thread.ident is not None:
            # The owner stop above has its existing five-second native bound.
            # A thread that cannot join retains this same cleanup owner.
            deadline_thread.join(timeout=6)
            if deadline_thread.is_alive():
                raise TimeoutError('Audio deadline observer has not stopped; its process owner remains retained.')

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
    posix_handles = {}
    last_progress = None

    def check():
        nonlocal last_progress, stopped_at
        if cancelled():
            raise AudioCancelled('Audio operation cancelled; incomplete output discarded.')
        if (stop_requested is not None and stopped_at is None
                and stop_requested() and observed_root_status() is None):
            stopped_at = time.monotonic()
            try:
                process.stdin.write(b'q\n')
                process.stdin.flush()
                process.stdin.close()
            except BrokenPipeError:
                pass  # A concurrent exit is checked by the normal result path.
        if (stopped_at is not None and time.monotonic() - stopped_at > 5
                and observed_root_status() is None):
            raise AudioError('Audio capture did not finalize within its stop allowance; incomplete output discarded.')
        if overflow.is_set():
            raise AudioError('Audio worker exceeded its diagnostic output budget.')
        check_native_deadline()
        total, count = 0, 0
        # Diagnostic-only bounded retention of the original stat observations.
        regular_count, largest_samples = 0, []
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
                try:
                    with os.scandir(path) as entries:
                        for entry in entries:
                            count += 1
                            if count > budget.max_staging_files:
                                raise AudioError('Audio worker exceeded its staging file budget.')
                            pending.append(Path(entry.path))
                except FileNotFoundError:
                    # Native converters may remove their private scratch
                    # directory after lstat. Only confirmed absence is benign;
                    # replacement, permission and other read errors still fail.
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    raise AudioError('Audio worker staging directory changed while being inspected.') from None
                except PermissionError as exc:
                    # Windows can report access denied while a native worker
                    # removes its own directory. Only an independent current
                    # ENOENT proves that race; any extant/denied/unknown path
                    # retains the original failure.
                    if os.name != 'nt' or getattr(exc, 'winerror', None) != 5:
                        raise
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        raise exc
                    raise
                continue
            if not stat.S_ISREG(info.st_mode):
                raise AudioError('Audio worker output is not a regular file.')
            regular_count += 1
            total += info.st_size
            if total > budget.max_output_bytes:
                detail = _scratch_overflow_sample(watched[0], budget.max_output_bytes,
                    total, count, regular_count, len(pending), (path, info), largest_samples)
                raise AudioError('Audio worker exceeded its output disk budget. ' + detail)
            largest_samples.append((path, info))
            largest_samples.sort(key=lambda sample: sample[1].st_size, reverse=True)
            del largest_samples[4:]
        if watched and shutil.disk_usage(watched[0].parent).free < budget.reserve_disk_bytes:
            raise AudioError('Audio render stopped to preserve the configured free disk reserve.')
        if progress is not None and latest[0] is not None and latest[0] != last_progress:
            progress(latest[0])
            last_progress = latest[0]

    try:
        # Reader allocation/start can fail after Popen (for example exhausted
        # thread resources). Keep it inside native-process ownership so that
        # callers cannot release their reservation while the child survives.
        if owned_tree:
            deadline_thread = threading.Thread(target=observe_deadline,
                name='carr-audio-native-deadline', daemon=True)
            deadline_thread.start()
        for pipe, name in ((process.stdout, 'stdout'), (process.stderr, 'stderr')):
            thread = threading.Thread(target=read_pipe, args=(pipe, name), daemon=True)
            threads.append(thread)
            thread.start()
            started_pipes.add(pipe)
        while observed_root_status() is None:
            check()
            if owned_posix:
                # WNOWAIT above observes completion without releasing the
                # leader identity that still authorizes group cleanup.
                time.sleep(.05)
            else:
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
        returncode = observed_root_status()
        if returncode and not allow_failure:
            message = bytes(buffers['stderr']).decode('utf-8', errors='replace')[-3000:]
            raise AudioError(f'Local audio worker failed (exit {returncode}): {message.strip()}')
        successful_result = True
        return ProcessResult(bytes(buffers['stdout']), bytes(buffers['stderr']),
                             time.monotonic() - started, returncode,
                             stopped_at is not None)
    finally:
        def cleanup_process():
            stop_deadline_observer()
            if owned_tree and os.name == 'nt':
                return process.close()
            if owned_posix:
                if process.returncode is not None:
                    # Only this path reaps our root, after confirmed group exit.
                    return True
                root_status()  # Refuse signals if another owner already reaped it.
                _capture_posix_group(process.pid, posix_handles, time.monotonic() + 5)
                if root_status() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    gentle_deadline = time.monotonic() + 1
                    while root_status() is None and time.monotonic() < gentle_deadline:
                        time.sleep(.01)
                # The unreaped leader prevents its PID/PGID being recycled into
                # a foreign group before this last signal and member barrier.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _wait_posix_group(process.pid, posix_handles, use_pidfds=True)
                process.wait(timeout=5)
                return True
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
            # Only readers that actually started own their streams. Retain
            # startup-failure cleanup even when allocation/start fails midway.
            for pipe in (process.stdout, process.stderr):
                if pipe not in started_pipes:
                    pipe.close()

        try:
            cleanup_process()
        except (Exception, KeyboardInterrupt) as exc:
            from .cleanup import pending_cleanup
            raise pending_cleanup(cleanup_process,
                message='Audio process cleanup is still pending; its admission remains reserved.',
                callbacks=(release_streams,)) from exc
        release_streams()
        if successful_result and owned_tree:
            # The observer is joined and the owner closed. Observe only its
            # synchronized recorded result; never touch a released native handle.
            check_native_deadline(observe=False)


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
