"""Real subprocess lifecycle and no-clobber publication regression tests."""
from pathlib import Path
import os
import stat
import sys
import time

import pytest

from carr_library.audio import AudioBudget, AudioCancelled, AudioError
from carr_library.audio.process import publish_file, run_process


def execute(code, **kwargs):
    return run_process([str(Path(sys.executable).resolve()), '-I', '-c', code], **kwargs)


def test_fast_stdout_overflow_is_detected_after_child_exit():
    with pytest.raises(AudioError, match='diagnostic'):
        execute('import os; os.write(1,b"x"*100000)', budget=AudioBudget(max_log_bytes=1024))


def test_fast_stderr_overflow_is_detected_after_child_exit():
    with pytest.raises(AudioError, match='diagnostic'):
        execute('import os; os.write(2,b"x"*100000)', budget=AudioBudget(max_log_bytes=1024))


def test_real_timeout_terminates_worker_promptly():
    started = time.monotonic()
    with pytest.raises(AudioError, match='time allowance'):
        execute('import time; time.sleep(30)', budget=AudioBudget(timeout=.1))
    assert time.monotonic() - started < 5


def test_cancel_before_spawn_prevents_side_effect(tmp_path):
    marker = tmp_path / 'never'
    with pytest.raises(AudioCancelled):
        execute(f'from pathlib import Path; Path({str(marker)!r}).write_text("bad")', cancelled=lambda: True)
    assert not marker.exists()


def test_output_disk_budget_is_enforced_even_on_fast_exit(tmp_path):
    destination = tmp_path / 'too-large'
    with pytest.raises(AudioError, match='disk budget'):
        execute(f'from pathlib import Path; Path({str(destination)!r}).write_bytes(b"x"*200000)',
                budget=AudioBudget(max_output_bytes=4096), watch=(destination,))


def test_all_worker_staging_files_count_against_disk_budget(tmp_path):
    stage = tmp_path / 'worker'
    stage.mkdir()
    code = (f'from pathlib import Path; root=Path({str(stage)!r}); '
            '(root/"extra.wav").write_bytes(b"x"*3000); '
            '(root/"generated.wav").write_bytes(b"x"*3000)')
    with pytest.raises(AudioError, match='disk budget'):
        execute(code, budget=AudioBudget(max_output_bytes=4096), watch=(stage,))


def test_staging_entry_count_is_bounded(tmp_path):
    stage = tmp_path / 'worker'
    stage.mkdir()
    code = f'from pathlib import Path; root=Path({str(stage)!r}); [(root/str(i)).write_bytes(b"x") for i in range(5)]'
    with pytest.raises(AudioError, match='staging file budget'):
        execute(code, budget=AudioBudget(max_staging_files=2), watch=(stage,))


def test_nonzero_exit_is_not_success():
    with pytest.raises(AudioError, match='exit 7'):
        execute('raise SystemExit(7)')


def test_progress_is_streamed_to_caller_without_waiting_for_full_buffer():
    seen = []
    execute('import time; print("out_time_us=500000",flush=True); time.sleep(.15)', progress=seen.append)
    assert seen == [.5]


def test_executable_must_be_resolved_and_no_shell_is_accepted():
    with pytest.raises(ValueError, match='resolved'):
        run_process(['python', '-V'])
    with pytest.raises(ValueError, match='vector'):
        run_process('python -V')


def test_publication_does_not_overwrite_or_publish_on_cancellation(tmp_path):
    staged, target = tmp_path / 'staged', tmp_path / 'target'
    staged.write_bytes(b'new')
    target.write_bytes(b'original')
    with pytest.raises(FileExistsError):
        publish_file(staged, target)
    assert target.read_bytes() == b'original'
    target.unlink()
    with pytest.raises(AudioCancelled):
        publish_file(staged, target, cancelled=lambda: True)
    assert not target.exists()
    publish_file(staged, target)
    assert target.read_bytes() == b'new'


def test_audio_publication_flushes_a_writable_handle(tmp_path, monkeypatch):
    staged, target = tmp_path / 'staged.wav', tmp_path / 'published.wav'
    staged.write_bytes(b'Exact unchanged audio bytes')
    actual = os.fsync
    flushed = []

    def require_write_access(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            # A zero-byte write checks descriptor access without changing data.
            # This models the Windows flush requirement on every test platform.
            assert os.write(descriptor, b'') == 0
            flushed.append(descriptor)
        actual(descriptor)

    monkeypatch.setattr(os, 'fsync', require_write_access)
    publish_file(staged, target)
    assert flushed and target.read_bytes() == staged.read_bytes() == b'Exact unchanged audio bytes'


@pytest.fixture
def owned_watched_child(monkeypatch):
    """Keep the real owned process and inspect its actual completed cleanup."""
    from types import SimpleNamespace
    from carr_library.audio import process
    actual_start, actual_stop = process._start_process, process._stop_owned
    children = []

    def start(*args, **kwargs):
        assert kwargs['owned_tree'] is True
        child = actual_start(*args, **kwargs)
        children.append(child)
        return child

    def run(code, *, watch, budget=None, cancelled=lambda: False):
        return execute(code, owned_tree=True, watch=watch, cancelled=cancelled,
                       budget=budget or AudioBudget(timeout=5, reserve_disk_bytes=0))

    monkeypatch.setattr(process, '_start_process', start)
    try:
        yield SimpleNamespace(module=process, children=children, run=run)
    finally:
        for child in children:
            actual_stop(child)


def _assert_watched_child_closed(state):
    assert len(state.children) == 1
    child = state.children[0]
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    if os.name == 'nt':
        assert child._carr_job.handle is None
        assert child._carr_job.port is None
    return child


def _install_watched_scandir(monkeypatch, state, scan):
    # Replace only this module's scanner. Path operations, subprocess, native
    # ownership and all other os calls continue using the actual platform.
    class WatchOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def scandir(self, path):
            return scan(path)

    monkeypatch.setattr(state.module, 'os', WatchOS())


def _remove_at_scandir(monkeypatch, state, victim, *, after=None):
    """Place a real filesystem removal after the production lstat succeeds."""
    actual_scandir = os.scandir
    observed = []

    def scan(path):
        if Path(path) != victim:
            return actual_scandir(path)
        victim.rmdir()
        try:
            return actual_scandir(path)
        except FileNotFoundError as exc:
            observed.append(exc)
            if after is not None:
                after()
            raise

    _install_watched_scandir(monkeypatch, state, scan)
    return observed


@pytest.mark.parametrize('exit_code', [0, 7], ids=['successful-worker', 'original-worker-error'])
def test_owned_worker_removes_temporary_directory_between_stat_and_scan(
        tmp_path, monkeypatch, owned_watched_child, exit_code):
    state = owned_watched_child
    root = tmp_path / 'watched'
    root.mkdir()
    victim = root / '.carr-original-race'
    victim.mkdir()
    release = tmp_path / 'release-child'
    actual_scandir = os.scandir
    observed = []

    def scan(path):
        if Path(path) != victim:
            return actual_scandir(path)
        # The actual child removes this directory only after production has
        # positively observed it. No clock, poll result or native owner is fake.
        release.write_bytes(b'go')
        deadline = time.monotonic() + 5
        while True:
            try:
                victim.lstat()
            except FileNotFoundError:
                break
            assert time.monotonic() < deadline, 'The real child did not remove its stage.'
            time.sleep(.005)
        try:
            return actual_scandir(path)
        except FileNotFoundError as exc:
            observed.append(exc)
            raise

    _install_watched_scandir(monkeypatch, state, scan)
    code = (
        'from pathlib import Path\nimport os, time\n'
        f'release = Path({str(release)!r})\n'
        'while not release.exists(): time.sleep(.005)\n'
        f'Path({str(victim)!r}).rmdir()\n'
        'os.write(1, b"actual owned worker")\n'
        f'raise SystemExit({exit_code})\n')
    if exit_code:
        with pytest.raises(AudioError, match='exit 7'):
            state.run(code, watch=(root,))
    else:
        result = state.run(code, watch=(root,))
        assert result.returncode == 0
        assert result.stdout == b'actual owned worker'
    assert len(observed) == 1
    assert not victim.exists() and root.is_dir()
    assert _assert_watched_child_closed(state).returncode == exit_code


@pytest.mark.parametrize('replacement', ['directory', 'regular-file'])
def test_reappeared_watched_object_does_not_turn_scan_error_into_absence(
        tmp_path, monkeypatch, owned_watched_child, replacement):
    state = owned_watched_child
    victim = tmp_path / '.carr-original-race'
    victim.mkdir()

    def replace():
        if replacement == 'directory':
            victim.mkdir()
            (victim / 'untouched').write_bytes(b'replacement contents')
        else:
            victim.write_bytes(b'replacement contents')

    observed = _remove_at_scandir(monkeypatch, state, victim, after=replace)
    with pytest.raises(FileNotFoundError) as caught:
        state.run('import time; time.sleep(30)', watch=(victim,))
    assert len(observed) == 1 and caught.value is observed[0]
    retained = victim / 'untouched' if replacement == 'directory' else victim
    assert retained.read_bytes() == b'replacement contents'
    _assert_watched_child_closed(state)


@pytest.mark.parametrize('kind', ['permission', 'not-directory', 'io'])
def test_watched_scan_open_errors_are_not_ignored(
        tmp_path, monkeypatch, owned_watched_child, kind):
    import errno
    state = owned_watched_child
    victim = tmp_path / 'watched'
    victim.mkdir()
    failures = {
        'permission': PermissionError(errno.EACCES, 'deliberate scan denial', str(victim)),
        'not-directory': NotADirectoryError(errno.ENOTDIR, 'deliberate type change', str(victim)),
        'io': OSError(errno.EIO, 'deliberate scan I/O error', str(victim)),
    }
    failure = failures[kind]
    actual_scandir = os.scandir
    calls = []

    def scan(path):
        if Path(path) == victim:
            calls.append(path)
            raise failure
        return actual_scandir(path)

    _install_watched_scandir(monkeypatch, state, scan)
    with pytest.raises(type(failure)) as caught:
        state.run('import time; time.sleep(30)', watch=(victim,))
    assert caught.value is failure and len(calls) == 1
    assert victim.is_dir()
    _assert_watched_child_closed(state)


def test_missing_error_during_watched_iteration_still_closes_and_fails(
        tmp_path, monkeypatch, owned_watched_child):
    import errno
    state = owned_watched_child
    victim = tmp_path / 'watched'
    victim.mkdir()
    failure = FileNotFoundError(errno.ENOENT, 'iteration failed after opening', str(victim))
    actual_scandir = os.scandir
    closed = []

    class BrokenIterator:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return self

        def __iter__(self):
            return self

        def __next__(self):
            raise failure

        def __exit__(self, *args):
            self.entries.close()
            closed.append(True)

    def scan(path):
        entries = actual_scandir(path)
        return BrokenIterator(entries) if Path(path) == victim else entries

    _install_watched_scandir(monkeypatch, state, scan)
    with pytest.raises(FileNotFoundError) as caught:
        state.run('import time; time.sleep(30)', watch=(victim,))
    assert caught.value is failure and closed == [True]
    _assert_watched_child_closed(state)


@pytest.mark.parametrize('kind', ['permission', 'io'])
def test_scan_absence_confirmation_propagates_other_metadata_errors(
        tmp_path, monkeypatch, owned_watched_child, kind):
    import errno
    state = owned_watched_child
    victim = tmp_path / 'watched'
    victim.mkdir()
    failure = (PermissionError(errno.EACCES, 'confirmation denied', str(victim))
               if kind == 'permission' else OSError(errno.EIO, 'confirmation I/O', str(victim)))
    original_lstat = Path.lstat
    removed = []

    def lstat(path, *args, **kwargs):
        if path == victim and removed:
            raise failure
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    observed = _remove_at_scandir(monkeypatch, state, victim, after=lambda: removed.append(True))
    with pytest.raises(type(failure)) as caught:
        state.run('import time; time.sleep(30)', watch=(victim,))
    assert caught.value is failure and len(observed) == 1 and removed == [True]
    _assert_watched_child_closed(state)


@pytest.mark.parametrize('guard', ['output-bytes', 'entry-count', 'cancel'])
def test_vanished_stage_does_not_skip_remaining_watch_guards(
        tmp_path, monkeypatch, owned_watched_child, guard):
    state = owned_watched_child
    victim = tmp_path / '.carr-original-race'
    victim.mkdir()
    remaining = tmp_path / 'remaining'
    if guard == 'entry-count':
        remaining.mkdir()
        for index in range(3):
            (remaining / str(index)).write_bytes(b'x')
    else:
        remaining.write_bytes(b'x' * 8192)
    cancelled = []
    observed = _remove_at_scandir(
        monkeypatch, state, victim,
        after=lambda: cancelled.append(True) if guard == 'cancel' else None)
    budget = AudioBudget(timeout=5, reserve_disk_bytes=0,
                         max_output_bytes=4096, max_staging_files=2)
    error, message = ((AudioCancelled, 'cancelled') if guard == 'cancel'
                      else (AudioError, 'staging file budget' if guard == 'entry-count'
                            else 'output disk budget'))
    with pytest.raises(error, match=message):
        state.run('import time; time.sleep(30)', watch=(remaining, victim),
                  budget=budget, cancelled=lambda: bool(cancelled))
    assert len(observed) == 1 and not victim.exists()
    _assert_watched_child_closed(state)


@pytest.mark.parametrize('kind', ['symlink', 'reparse'])
def test_reappeared_dangling_link_is_observed_without_following_it(
        tmp_path, monkeypatch, owned_watched_child, kind):
    import errno
    from types import SimpleNamespace
    state = owned_watched_child
    victim = tmp_path / 'watched'
    victim.mkdir()
    original_lstat, original_stat = Path.lstat, Path.stat
    replaced, followed = [], []

    def replace():
        # Model the link/reparse metadata facet without requiring Windows
        # symlink privileges. The backing object and scanner error stay real.
        victim.write_bytes(b'unchanged replacement')
        replaced.append(True)

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == victim and replaced:
            return SimpleNamespace(
                st_mode=(stat.S_IFLNK | 0o777) if kind == 'symlink' else info.st_mode,
                st_file_attributes=0x400 if kind == 'reparse' else 0)
        return info

    def followed_stat(path, *args, **kwargs):
        if path == victim and replaced and kwargs.get('follow_symlinks', True) is not False:
            followed.append(True)
            raise FileNotFoundError(errno.ENOENT, 'dangling target must not be followed', str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(Path, 'stat', followed_stat)
    observed = _remove_at_scandir(monkeypatch, state, victim, after=replace)
    with pytest.raises(FileNotFoundError) as caught:
        state.run('import time; time.sleep(30)', watch=(victim,))
    assert len(observed) == 1 and caught.value is observed[0]
    assert followed == []
    assert victim.read_bytes() == b'unchanged replacement'
    _assert_watched_child_closed(state)
