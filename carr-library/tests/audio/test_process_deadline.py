"""Real owned children distinguish worker deadlines from final verification.

Only this module's audio clock receives a controlled offset. The selected child,
its arguments, wait/exit state, pipe readers, and owned cleanup remain real.
No sleep, worker allowance, profile verification, or cancellation is relaxed.
"""
from hashlib import sha256
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from carr_library.audio import process


@pytest.fixture
def observed(monkeypatch):
    actual_start = process._start_process
    actual_monotonic = time.monotonic
    state = SimpleNamespace(children=[], offset=0.0, wait_before_return=True)
    # Offset only the audio module's clock at the observed verification
    # boundary. Its owned POSIX waiter still uses the real sleep function;
    # subprocess/threading and every existing wait allowance stay unchanged.
    monkeypatch.setattr(process, 'time',
                        SimpleNamespace(monotonic=lambda: actual_monotonic() + state.offset,
                                        sleep=time.sleep))

    def start(arguments, **options):
        child = actual_start(arguments, **options)
        state.children.append(child)
        if state.wait_before_return:
            # Bound only fixture readiness. This produces an actual completed
            # child before the first final check, without a mocked poll result.
            child.wait(timeout=10)
        return child

    monkeypatch.setattr(process, '_start_process', start)
    try:
        yield state
    finally:
        for child in state.children:
            process._stop_owned(child)


def run(code, *, direct_stdio=False, **options):
    executable = sys.executable
    if direct_stdio and os.name == 'nt':
        # CPython's Windows venv redirector retains duplicate stdout/stderr
        # handles while waiting for its child. Only these stdlib-only fixtures
        # need to close the actual owned leader's pipes before that leader exits.
        # Product workers and every other test retain the selected interpreter.
        executable = getattr(sys, '_base_executable', None)
        assert sys.implementation.name == 'cpython'
        assert isinstance(executable, str) and Path(executable).is_absolute()
        assert Path(executable).is_file(), 'The active CPython base executable is unavailable.'
    return process.run_process([executable, '-I', '-B', '-c', code],
        owned_tree=True, budget=process.AudioBudget(timeout=5, reserve_disk_bytes=0,
                                                   **options.pop('budget_options', {})),
        **options)


def closed(observed):
    assert len(observed.children) == 1
    child = observed.children[0]
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    if os.name == 'nt':
        assert child._carr_job.handle is None
        assert child._carr_job.port is None
    return child


def late_review(observed, action=None):
    calls = []

    def review():
        if not observed.children:
            return False  # Existing pre-launch cancellation boundary.
        child = observed.children[0]
        assert child.poll() is not None
        calls.append(child.returncode)
        if len(calls) == 1:
            observed.offset += 6.0
        return False if action is None else action(len(calls))

    return review, calls


def test_completed_child_does_not_time_out_during_later_parent_verification(observed):
    review, calls = late_review(observed)
    result = run('import sys; sys.stdout.buffer.write(b"actual completed child")', cancelled=review)
    assert result.stdout == b'actual completed child'
    assert result.returncode == 0 and result.elapsed >= 6.0
    assert len(calls) >= 2, 'Both final verification boundaries must still run.'
    assert closed(observed).returncode == 0


@pytest.mark.parametrize('at_check', [1, 2], ids=['after-exit', 'after-pipe-drain'])
def test_late_cancel_remains_effective_after_observed_exit(observed, at_check):
    review, calls = late_review(observed, lambda number: number == at_check)
    with pytest.raises(process.AudioCancelled, match='cancelled'):
        run('print("completed, but caller cancelled")', cancelled=review)
    assert len(calls) == at_check
    assert closed(observed).returncode == 0


def test_final_actual_output_size_is_checked_even_after_worker_deadline(observed, tmp_path):
    target = tmp_path / 'worker-result'
    before = b'Actual short child result'

    def enlarge(number):
        if number == 1:
            assert target.read_bytes() == before
            target.write_bytes(before + b'x' * 4096)
        return False

    review, calls = late_review(observed, enlarge)
    code = 'from pathlib import Path; Path(' + repr(str(target)) + ').write_bytes(' + repr(before) + ')'
    with pytest.raises(process.AudioError, match='output disk budget'):
        run(code, cancelled=review, watch=(target,), budget_options={'max_output_bytes': 4096})
    assert calls and target.stat().st_size > 4096
    assert closed(observed).returncode == 0


def test_final_diagnostic_limit_remains_enforced_after_worker_exit(observed):
    # Keep this below a native pipe's capacity so waiting for fixture readiness
    # cannot itself deadlock. It still exceeds the deliberately smaller cap.
    review, calls = late_review(observed)
    with pytest.raises(process.AudioError, match='diagnostic output budget'):
        run('import os; os.write(2, b"x" * 2048)', cancelled=review,
            budget_options={'max_log_bytes': 1024})
    assert calls
    assert closed(observed).returncode == 0


def test_final_byte_identity_failure_is_never_converted_to_success(observed, tmp_path):
    reviewed = tmp_path / 'reviewed-runtime-input'
    reviewed.write_bytes(b'Reviewed identity')
    expected = sha256(reviewed.read_bytes()).hexdigest()

    def verify(number):
        if number == 1:
            reviewed.write_bytes(b'Changed during final admission')
        if sha256(reviewed.read_bytes()).hexdigest() != expected:
            raise ValueError('The reviewed runtime identity changed.')
        return False

    review, calls = late_review(observed, verify)
    with pytest.raises(ValueError, match='reviewed runtime identity changed'):
        run('print("completed")', cancelled=review)
    assert calls and reviewed.read_bytes() == b'Changed during final admission'
    assert closed(observed).returncode == 0


def test_live_child_still_expires_when_parent_callback_crosses_allowance(observed):
    observed.wait_before_return = False
    sampled = []

    def review():
        if not observed.children:
            return False
        child = observed.children[0]
        assert child.poll() is None
        sampled.append(child.pid)
        observed.offset += 6.0
        return False

    with pytest.raises(process.AudioError, match='time allowance'):
        run('import time; time.sleep(30)', cancelled=review)
    assert len(sampled) == 1
    assert closed(observed).pid == sampled[0]


def test_exit_during_callback_is_not_backdated_when_first_observation_is_late(observed, tmp_path):
    observed.wait_before_return = False
    release = tmp_path / 'release-worker'
    sampled = []

    def review():
        if not observed.children:
            return False
        child = observed.children[0]
        assert child.poll() is None
        sampled.append(child.pid)
        # Advance before releasing the real child: every possible positive
        # observation, including an EOF reader, is now genuinely late.
        observed.offset += 6.0
        release.write_bytes(b'explicit fixture release')
        assert child.wait(timeout=10) == 0
        return False

    code = (
        'from pathlib import Path\n'
        'import time\n'
        'release = Path(' + repr(str(release)) + ')\n'
        'while not release.exists():\n'
        '    time.sleep(.005)\n'
        'print("released child")\n'
    )
    with pytest.raises(process.AudioError, match='time allowance'):
        run(code, cancelled=review)
    assert len(sampled) == 1 and release.is_file()
    assert closed(observed).returncode == 0



# The following cases keep real owned children and real reader threads. Only
# the already-scoped audio clock advances; process.wait/poll are never faked.
import threading


def tracked_readers(monkeypatch, observed, *, after_read=None, wait_hook=None):
    real_thread = threading.Thread
    actual_start = process._start_process
    tracked = SimpleNamespace(threads=[], completed=[], waits=[])

    def create(*arguments, **options):
        target = options['target']

        def read(*args, **kwargs):
            try:
                return target(*args, **kwargs)
            finally:
                tracked.completed.append(threading.get_ident())
                if after_read is not None:
                    after_read()

        options = dict(options, target=read)
        thread = real_thread(*arguments, **options)
        tracked.threads.append(thread)
        return thread

    # Do not replace threading.Thread globally: existing CleanupOwner retries
    # and the test's orchestration thread keep their unchanged implementation.
    monkeypatch.setattr(process, 'threading',
                        SimpleNamespace(**{**vars(threading), 'Thread': create}))

    def start(arguments, **options):
        child = actual_start(arguments, **options)
        actual_wait = child.wait

        def wait(timeout=None):
            if threading.current_thread() in tracked.threads:
                tracked.waits.append((threading.get_ident(), timeout))
                if wait_hook is not None:
                    return wait_hook(child, actual_wait, timeout)
            return actual_wait(timeout=timeout)

        child.wait = wait
        return child

    monkeypatch.setattr(process, '_start_process', start)
    return tracked


def wait_for_file(path):
    until = time.monotonic() + 10
    while not path.exists() and time.monotonic() < until:
        time.sleep(.005)
    assert path.is_file(), 'The actual fixture child did not reach its gate.'


def join_readers(tracked):
    assert len(tracked.threads) == 2
    for thread in tracked.threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(set(tracked.completed)) == 2


def gated_code(tmp_path, *, early_eof=False, stderr_bytes=0, exit_code=0):
    ready, release = tmp_path / 'worker-ready', tmp_path / 'release-worker'
    if early_eof:
        # Publish a closed complete PID marker, never a just-created empty file.
        marker = ready.with_name(ready.name + '.pending')
        readiness = ('marker = Path(' + repr(str(marker)) + ')\n'
                     'marker.write_bytes(str(os.getpid()).encode("ascii"))\n'
                     'os.replace(marker, ' + repr(str(ready)) + ')\n')
    else:
        readiness = 'Path(' + repr(str(ready)) + ').write_bytes(b"ready")\n'
    code = (
        'from pathlib import Path\n'
        'import os,time\n'
        + ('import sys\n'
           + 'assert (sys.implementation.name, sys.version_info[:3], sys.maxsize) == '
           + repr((sys.implementation.name, sys.version_info[:3], sys.maxsize)) + '\n'
           + 'os.close(1)\nos.close(2)\n' if early_eof else '')
        + readiness
        + 'release = Path(' + repr(str(release)) + ')\n'
        'while not release.exists():\n'
        '    time.sleep(.005)\n'
        + ('os.write(2, b"x" * ' + str(stderr_bytes) + ')\n' if stderr_bytes else '')
        + ('' if early_eof else 'os.write(1, b"verified child\\n")\n')
        + (('os._exit(' if early_eof else 'raise SystemExit(') + str(exit_code) + ')\n')
    )
    return ready, release, code


@pytest.mark.parametrize('exit_code', [0, 7])
def test_reader_observes_real_exit_during_slow_caller_validation(
        observed, monkeypatch, tmp_path, exit_code):
    observed.wait_before_return = False
    tracked = tracked_readers(monkeypatch, observed)
    ready, release, code = gated_code(tmp_path, exit_code=exit_code)
    caller = threading.get_ident()
    calls = []

    def review():
        if not observed.children:
            return False
        assert threading.get_ident() == caller
        calls.append(caller)
        if len(calls) == 1:
            wait_for_file(ready)
            assert observed.children[0].poll() is None
            release.write_bytes(b'release during the caller checkpoint')
            # Only the actual EOF readers can record exit during this callback.
            join_readers(tracked)
            assert tracked.waits and all(who != caller for who, _ in tracked.waits)
            assert all(0 < allowance <= .05 for _, allowance in tracked.waits)
            observed.offset += 6.0
        return False

    result = run(code, cancelled=review, allow_failure=True)
    assert result.returncode == exit_code and result.stdout == b'verified child\n'
    assert result.elapsed >= 6.0 and len(calls) >= 3
    assert closed(observed).returncode == exit_code


def test_early_eof_waits_for_actual_exit_before_freezing_deadline(observed, monkeypatch, tmp_path):
    observed.wait_before_return = False
    entered = threading.Event()

    def wait_hook(child, actual_wait, timeout):
        entered.set()
        return actual_wait(timeout=timeout)

    tracked = tracked_readers(monkeypatch, observed, wait_hook=wait_hook)
    ready, release, code = gated_code(tmp_path, early_eof=True)
    calls = []

    def review():
        if not observed.children:
            return False
        calls.append(threading.get_ident())
        if len(calls) == 1:
            wait_for_file(ready)
            assert int(ready.read_bytes()) == observed.children[0].pid
            assert entered.wait(10)
            assert observed.children[0].poll() is None
            release.write_bytes(b'exit only after an actual reader wait started')
            join_readers(tracked)
            observed.offset += 6.0
        return False

    result = run(code, direct_stdio=True, cancelled=review)
    assert result.returncode == 0 and result.stdout == result.stderr == b''
    assert result.elapsed >= 6.0 and tracked.waits
    assert closed(observed).returncode == 0


def test_closed_diagnostic_pipes_do_not_stop_a_live_child_deadline(
        observed, monkeypatch, tmp_path):
    observed.wait_before_return = False
    entered = threading.Event()

    def wait_hook(child, actual_wait, timeout):
        entered.set()
        return actual_wait(timeout=timeout)

    tracked = tracked_readers(monkeypatch, observed, wait_hook=wait_hook)
    ready, release, code = gated_code(tmp_path, early_eof=True)
    calls = []

    def review():
        if not observed.children:
            return False
        calls.append(threading.get_ident())
        wait_for_file(ready)
        assert int(ready.read_bytes()) == observed.children[0].pid
        assert entered.wait(10)
        assert observed.children[0].poll() is None
        observed.offset += 6.0
        return False

    with pytest.raises(process.AudioError, match='time allowance'):
        run(code, direct_stdio=True, cancelled=review)
    assert len(calls) == 1 and tracked.waits and not release.exists()
    closed(observed)


def test_late_eof_cannot_backdate_a_leader_exit_behind_inherited_pipes(
        observed, monkeypatch, tmp_path):
    observed.wait_before_return = False
    tracked = tracked_readers(monkeypatch, observed)
    ready, release = tmp_path / 'leader-ready', tmp_path / 'release-leader'
    descendant_ready, descendant_release = tmp_path / 'descendant-ready', tmp_path / 'release-descendant'
    descendant_code = (
        'from pathlib import Path\nimport time\n'
        'Path(' + repr(str(descendant_ready)) + ').write_bytes(b"ready")\n'
        'release = Path(' + repr(str(descendant_release)) + ')\n'
        'while not release.exists():\n    time.sleep(.005)\n'
        'print("descendant completed")\n'
    )
    code = (
        'from pathlib import Path\nimport subprocess,sys,time,os\n'
        'child = subprocess.Popen([sys.executable, "-I", "-B", "-c", '
        + repr(descendant_code) + '])\n'
        'Path(' + repr(str(ready)) + ').write_bytes(b"ready")\n'
        'release = Path(' + repr(str(release)) + ')\n'
        'while not release.exists():\n    time.sleep(.005)\n'
        'os._exit(0)\n'
    )
    calls = []

    def review():
        if not observed.children:
            return False
        calls.append(threading.get_ident())
        wait_for_file(ready)
        wait_for_file(descendant_ready)
        assert observed.children[0].poll() is None
        release.write_bytes(b'leader finishes; descendant still holds both pipes')
        assert observed.children[0].wait(timeout=10) == 0
        assert not tracked.completed and not tracked.waits
        observed.offset += 6.0
        descendant_release.write_bytes(b'EOF is now after the original allowance')
        join_readers(tracked)
        return False

    with pytest.raises(process.AudioError, match='time allowance'):
        run(code, cancelled=review)
    assert len(calls) == 1 and release.exists() and descendant_release.exists()
    assert closed(observed).returncode == 0


@pytest.mark.parametrize('failure', ['cancel', 'identity', 'diagnostic', 'staging'])
def test_timely_reader_exit_preserves_fresh_caller_guards(
        observed, monkeypatch, tmp_path, failure):
    observed.wait_before_return = False
    tracked = tracked_readers(monkeypatch, observed)
    ready, release, code = gated_code(tmp_path, stderr_bytes=2048 if failure == 'diagnostic' else 0)
    reviewed = tmp_path / 'reviewed-runtime'
    reviewed.write_bytes(b'exact reviewed content')
    approved = sha256(reviewed.read_bytes()).hexdigest()
    output = tmp_path / 'worker-output'
    output.write_bytes(b'accepted-size')
    caller = threading.get_ident()
    calls = []

    def review():
        if not observed.children:
            return False
        assert threading.get_ident() == caller
        calls.append(caller)
        if len(calls) == 1:
            wait_for_file(ready)
            assert observed.children[0].poll() is None
            release.write_bytes(b'complete before parent final checks')
            join_readers(tracked)
            observed.offset += 6.0
            if failure == 'identity':
                reviewed.write_bytes(b'changed after the child actually exited')
            if failure == 'staging':
                output.write_bytes(b'x' * 8192)
        if sha256(reviewed.read_bytes()).hexdigest() != approved:
            raise ValueError('reviewed runtime identity changed')
        return failure == 'cancel'

    error, message = {
        'cancel': (process.AudioCancelled, 'cancelled'),
        'identity': (ValueError, 'runtime identity changed'),
        'diagnostic': (process.AudioError, 'diagnostic'),
        'staging': (process.AudioError, 'disk budget'),
    }[failure]
    with pytest.raises(error, match=message):
        run(code, cancelled=review, watch=(output,),
            budget_options={'max_log_bytes': 1024, 'max_output_bytes': 4096})
    assert calls and tracked.waits
    assert closed(observed).returncode == 0


def test_eof_wait_failure_still_rejects_completed_real_child(observed, monkeypatch, tmp_path):
    observed.wait_before_return = False
    wait_error = OSError('synthetic exact-process wait failure')

    def wait_hook(child, actual_wait, timeout):
        actual_wait(timeout=timeout)
        raise wait_error

    tracked = tracked_readers(monkeypatch, observed, wait_hook=wait_hook)
    ready, release, code = gated_code(tmp_path)
    calls = []

    def review():
        if not observed.children:
            return False
        calls.append(True)
        if len(calls) == 1:
            wait_for_file(ready)
            release.write_bytes(b'complete with observable wait failure')
            join_readers(tracked)
        return False

    with pytest.raises(process.AudioError, match='diagnostic stream') as failure:
        run(code, cancelled=review)
    assert failure.value.__cause__ is wait_error and tracked.waits
    assert closed(observed).returncode == 0


def test_pending_reader_completion_retains_stage_after_native_family_stops(
        observed, monkeypatch, tmp_path):
    from carr_library.audio.cleanup import AudioCleanupPending, retained_temporary_directory

    observed.wait_before_return = False
    release_reader = threading.Event()
    native_stops = []
    actual_stop = process._stop_owned
    owner = None
    stage = None

    def stop(child):
        # Other retained owners may retry on the same module during this test.
        # Observe only this exact run; every native family still gets its stop.
        if observed.children and child is observed.children[0]:
            native_stops.append(child.pid)
        return actual_stop(child)

    def after_read():
        release_reader.wait()

    tracked = tracked_readers(monkeypatch, observed, after_read=after_read)
    monkeypatch.setattr(process, '_stop_owned', stop)
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            with retained_temporary_directory(prefix='reader-owned-', dir=tmp_path) as directory:
                stage = Path(directory)
                (stage / 'retained').write_bytes(b'owned until both readers end')
                run('print("completed before reader acknowledgement")')
        owner = failure.value.cleanup_owner
        assert len(native_stops) == 1 and len(tracked.completed) == 2
        assert all(thread.is_alive() for thread in tracked.threads)
        assert stage.is_dir() and not owner.quiescent
        assert observed.children[0].poll() is not None
        released = threading.Event()
        owner.add_quiescent_callback(released.set)
        release_reader.set()
        assert released.wait(10)
        assert owner.quiescent and owner.close() is True
        assert len(native_stops) == 1 and not stage.exists()
        assert all(not thread.is_alive() for thread in tracked.threads)
        closed(observed)
    finally:
        release_reader.set()
        if owner is not None:
            owner.close()
            owner.quiescent_event.wait(10)
        for thread in tracked.threads:
            thread.join(timeout=10)


def test_failed_reader_pipe_close_remains_retryable_before_stage_release(
        observed, monkeypatch, tmp_path):
    from carr_library.audio.cleanup import AudioCleanupPending, retained_temporary_directory

    observed.wait_before_return = False
    allow_close = threading.Event()
    actual_start, actual_stop = process._start_process, process._stop_owned
    native_stops, close_attempts = [], []
    owner = None

    class RetainedPipe:
        def __init__(self, stream):
            self.stream = stream

        def read1(self, size):
            return self.stream.read1(size)

        @property
        def closed(self):
            return self.stream.closed

        def close(self):
            close_attempts.append(allow_close.is_set())
            if not allow_close.is_set():
                raise OSError('synthetic reader pipe close failure')
            return self.stream.close()

    def start(arguments, **options):
        child = actual_start(arguments, **options)
        child.stdout = RetainedPipe(child.stdout)
        return child

    def stop(child):
        # Other retained owners may retry on the same module during this test.
        # Observe only this exact run; every native family still gets its stop.
        if observed.children and child is observed.children[0]:
            native_stops.append(child.pid)
        return actual_stop(child)

    monkeypatch.setattr(process, '_start_process', start)
    monkeypatch.setattr(process, '_stop_owned', stop)
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            with retained_temporary_directory(prefix='pipe-owned-', dir=tmp_path) as directory:
                stage = Path(directory)
                (stage / 'retained').write_bytes(b'close must finish before deletion')
                run('print("actual completed bytes")')
        owner = failure.value.cleanup_owner
        assert close_attempts and len(native_stops) == 1
        assert not observed.children[0].stdout.closed
        assert stage.is_dir() and not owner.quiescent
        released = threading.Event()
        owner.add_quiescent_callback(released.set)
        allow_close.set()
        assert released.wait(10)
        assert owner.quiescent and owner.close() is True
        assert len(native_stops) == 1 and not stage.exists()
        closed(observed)
    finally:
        allow_close.set()
        if owner is not None:
            owner.close()
            owner.quiescent_event.wait(10)


def test_native_cleanup_waits_for_inflight_eof_wait_before_closing_process_handle(
        observed, monkeypatch, tmp_path):
    observed.wait_before_return = False
    entered, release_wait = threading.Event(), threading.Event()
    cleanup_started, cleanup_handoff = threading.Event(), threading.Event()
    runner = None
    trace, failures, native_stops = [], [], []
    actual_stop, actual_lock, actual_event = process._stop_owned, threading.Lock, threading.Event

    def wait_hook(child, actual_wait, timeout):
        trace.append('eof wait entered')
        entered.set()
        assert release_wait.wait(10)
        try:
            return actual_wait(timeout=timeout)
        finally:
            trace.append('eof wait returned')

    tracked = tracked_readers(monkeypatch, observed, wait_hook=wait_hook)

    def event():
        value = actual_event()
        actual_set = value.set

        def set_event():
            actual_set()
            if threading.current_thread() is runner and entered.is_set():
                cleanup_started.set()

        value.set = set_event
        return value

    class ObservedLock:
        def __init__(self):
            self.lock = actual_lock()

        def __enter__(self):
            if (threading.current_thread() is runner and cleanup_started.is_set()
                    and entered.is_set() and not release_wait.is_set()):
                # Observe the real lock acquisition attempt; do not fabricate
                # acquisition, release, process status, or a successful wait.
                cleanup_handoff.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_):
            self.lock.release()

    monkeypatch.setattr(process.threading, 'Event', event)
    monkeypatch.setattr(process.threading, 'Lock', ObservedLock)

    def stop(child):
        assert release_wait.is_set()
        assert trace[-1] == 'eof wait returned'
        native_stops.append(child.pid)
        trace.append('native cleanup')
        return actual_stop(child)

    monkeypatch.setattr(process, '_stop_owned', stop)
    ready, _, code = gated_code(tmp_path, early_eof=True)

    def cancel():
        if not observed.children:
            return False
        wait_for_file(ready)
        assert int(ready.read_bytes()) == observed.children[0].pid
        assert entered.wait(10)
        return True

    def execute():
        try:
            run(code, direct_stdio=True, cancelled=cancel)
        except BaseException as error:
            failures.append(error)

    runner = threading.Thread(target=execute, name='actual-audio-caller')
    try:
        runner.start()
        assert entered.wait(10) and cleanup_handoff.wait(10)
        assert runner.is_alive() and not native_stops
        assert not release_wait.is_set()
        release_wait.set()
        runner.join(timeout=10)
        assert not runner.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], process.AudioCancelled)
        assert len(native_stops) == 1 and tracked.waits
        assert trace.index('eof wait returned') < trace.index('native cleanup')
        assert all(not thread.is_alive() for thread in tracked.threads)
        closed(observed)
    finally:
        release_wait.set()
        runner.join(timeout=15)
        # The existing observed fixture performs idempotent cleanup again.
        # Keep its teardown outside this operation's exact-once/order observer.
        monkeypatch.setattr(process, '_stop_owned', actual_stop)


@pytest.mark.parametrize('scenario', [
    test_pending_reader_completion_retains_stage_after_native_family_stops,
    test_failed_reader_pipe_close_remains_retryable_before_stage_release,
], ids=['pending-reader', 'failed-pipe-close'])
def test_reader_stop_observation_excludes_a_real_retained_foreign_owner(
        request, monkeypatch, tmp_path, scenario):
    import subprocess
    from carr_library.audio.cleanup import pending_cleanup

    # Launch before requesting the observed fixture: this exact native family
    # belongs to an earlier operation, not to the run that the fixture records.
    actual_start, actual_stop = process._start_process, process._stop_owned
    foreign = actual_start([sys.executable, '-I', '-B', '-c',
        'import time; time.sleep(30)'], owned_tree=True, shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    allow_foreign_stop = threading.Event()
    foreign_quiet = threading.Event()
    forwarded = threading.Event()
    owner = None

    def finish_foreign():
        if not allow_foreign_stop.is_set():
            return False
        forwarded.set()
        return process._stop_owned(foreign)

    try:
        owner = pending_cleanup(finish_foreign,
            message='The independent real family remains owned.',
            callbacks=(foreign_quiet.set,)).cleanup_owner
        assert foreign.poll() is None and not owner.quiescent
        observed = request.getfixturevalue('observed')
        actual_run = run

        def run_with_foreign_cleanup(*args, **kwargs):
            try:
                return actual_run(*args, **kwargs)
            finally:
                # Both original scenarios have installed their stop observer.
                # Complete another real retained owner through that observer
                # before the original exact-once assertions are evaluated.
                assert len(observed.children) == 1
                assert foreign is not observed.children[0]
                assert foreign.poll() is None
                allow_foreign_stop.set()
                owner.close()
                assert foreign_quiet.wait(10)
                assert forwarded.is_set() and owner.quiescent
                assert foreign.poll() is not None

        monkeypatch.setattr(sys.modules[__name__], 'run', run_with_foreign_cleanup)
        scenario(observed, monkeypatch, tmp_path)
        assert owner.close() is True and foreign_quiet.is_set()
        if os.name == 'nt':
            assert foreign._carr_job.handle is None
            assert foreign._carr_job.port is None
    finally:
        allow_foreign_stop.set()
        if owner is None:
            assert actual_stop(foreign) is True
        else:
            owner.close()
            assert foreign_quiet.wait(10), 'The independent native family did not confirm shutdown.'
