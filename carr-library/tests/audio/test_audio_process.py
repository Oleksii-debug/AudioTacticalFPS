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
    return run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', code], **kwargs)


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
def windows_watch_processes(monkeypatch):
    from carr_library.audio import process as runner

    assert runner._USE_WINDOWS_LIVE_FILE_STATS == (os.name == 'nt')
    monkeypatch.setattr(runner, '_USE_WINDOWS_LIVE_FILE_STATS', True)
    children = []
    original = runner.subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runner.subprocess, 'Popen', spawn)
    return children


def test_windows_watch_never_lstats_live_status_but_checks_it_fresh_after_exit(
        tmp_path, monkeypatch, windows_watch_processes):
    from carr_library.audio import process as runner

    stage = tmp_path / 'worker'
    stage.mkdir()
    status, release = stage / 'job_status.json', tmp_path / 'release-worker'
    original = Path.lstat
    live_directories, live_files, final_files = [], [], []
    original_live = runner._live_file_info

    def observed_live(path):
        live = windows_watch_processes[-1].poll() is None
        if path == stage and live:
            live_directories.append(path)
        if path == status and live:
            live_files.append(path)
        return original_live(path)

    def observed(path, *args, **kwargs):
        live = windows_watch_processes and windows_watch_processes[-1].poll() is None
        if path == status:
            assert not live, 'The budget watcher opened changing worker status on Windows.'
            final_files.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', observed)
    monkeypatch.setattr(runner, '_live_file_info', observed_live)
    notices = []

    def release_after_budget_check(value):
        notices.append(value)
        release.write_bytes(b'Continue the real synthetic worker.')

    code = f'''
import os, time
from pathlib import Path
root, release = Path({str(stage)!r}), Path({str(release)!r})
status = root / 'job_status.json'
status.write_bytes(b'initial')
print('out_time_us=1000', flush=True)
deadline = time.monotonic() + 4
while not release.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('The real parent did not observe worker progress.')
    time.sleep(.005)
for index in range(50):
    temporary = root / 'status.pending'
    temporary.write_bytes(('generation-' + str(index)).encode('ascii'))
    os.replace(temporary, status)
    time.sleep(.01)
'''
    execute(code, watch=(stage,), progress=release_after_budget_check,
            budget=AudioBudget(timeout=8))

    assert notices == [.001]
    assert live_directories and live_files and final_files
    assert len(windows_watch_processes) == 1 and windows_watch_processes[0].returncode == 0
    assert status.read_bytes() == b'generation-49'


@pytest.mark.parametrize('watch_file', [False, True])
def test_windows_cached_watch_detects_actual_growth_while_child_keeps_file_open(
        tmp_path, windows_watch_processes, watch_file):
    stage = tmp_path / 'worker'
    stage.mkdir()
    output, finished = stage / 'open-output-українська-\U0001f4da', tmp_path / 'finished'
    code = f'''
import time
from pathlib import Path
output = open({str(output)!r}, 'wb', buffering=0)
output.write(b'x' * 200000)
time.sleep(10)
output.close()
Path({str(finished)!r}).write_bytes(b'Normal completion')
'''
    with pytest.raises(AudioError, match='disk budget'):
        execute(code, watch=(output if watch_file else stage,),
                budget=AudioBudget(timeout=2, max_output_bytes=4096))

    assert len(windows_watch_processes) == 1 and windows_watch_processes[0].poll() is not None
    assert not finished.exists()
    assert output.stat().st_size == 200000


@pytest.mark.parametrize('limits, count, size, message', [
    ({'max_output_bytes': 4096}, 2, 3000, 'disk budget'),
    ({'max_staging_files': 2}, 5, 1, 'staging file budget'),
])
def test_windows_cached_watch_preserves_staging_budgets(
        tmp_path, windows_watch_processes, limits, count, size, message):
    stage = tmp_path / 'worker'
    stage.mkdir()
    code = (f'from pathlib import Path; import time; root=Path({str(stage)!r}); '
            f'[(root/str(index)).write_bytes(b"x"*{size}) for index in range({count})]; '
            'time.sleep(10)')
    with pytest.raises(AudioError, match=message):
        execute(code, budget=AudioBudget(timeout=2, **limits), watch=(stage,))
    assert windows_watch_processes and all(child.poll() is not None for child in windows_watch_processes)


def test_windows_metadata_query_uses_full_width_size_and_fixed_win32_layout(tmp_path, monkeypatch):
    import ctypes
    from types import SimpleNamespace
    from carr_library.audio import process as runner

    calls = []

    class Query:
        def __call__(self, name, level, destination):
            info = destination._obj
            assert ctypes.sizeof(info) == 36
            assert type(info).size_high.offset == 28 and type(info).size_low.offset == 32
            assert level == 0
            info.attributes, info.size_high, info.size_low = 0x20, 2, 17
            calls.append(name)
            return 1

    query = Query()

    def library(name, *, use_last_error):
        assert name == 'kernel32' and use_last_error
        return SimpleNamespace(GetFileAttributesExW=query)

    monkeypatch.setattr(ctypes, 'WinDLL', library, raising=False)
    runner._windows_metadata_query.cache_clear()
    try:
        path = tmp_path / 'українська-\U0001f4da'
        info = runner._windows_metadata_query()(path)
        assert info.st_size == (2 << 32) + 17
        assert stat.S_ISREG(info.st_mode) and info.st_file_attributes == 0x20
        assert len(calls) == 1 and calls[0].startswith('\\\\?\\')
        assert calls[0].endswith(path.name)
        assert query.restype is ctypes.c_int32
    finally:
        runner._windows_metadata_query.cache_clear()


@pytest.mark.parametrize('failure', [FileNotFoundError, PermissionError])
def test_windows_metadata_query_propagates_native_errors_without_cached_fallback(
        tmp_path, monkeypatch, failure):
    import ctypes
    from types import SimpleNamespace
    from carr_library.audio import process as runner

    class Query:
        def __call__(self, *args):
            return 0

    expected = failure('Native metadata query failed.')
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **kw: SimpleNamespace(
        GetFileAttributesExW=Query()), raising=False)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, 'WinError', lambda code: expected, raising=False)
    runner._windows_metadata_query.cache_clear()
    try:
        with pytest.raises(failure) as caught:
            runner._windows_metadata_query()(tmp_path / 'output')
        assert caught.value is expected
    finally:
        runner._windows_metadata_query.cache_clear()


@pytest.mark.parametrize('attributes', [0x400, 0x40])
def test_windows_live_metadata_still_rejects_reparse_and_device_output(
        tmp_path, monkeypatch, windows_watch_processes, attributes):
    from carr_library.audio import process as runner

    stage = tmp_path / 'worker'
    stage.mkdir()
    output = stage / 'output'
    original = runner._live_file_info

    def observed(path):
        info = original(path)
        if path == output:
            return runner._FileObservation(0 if attributes == 0x40 else stat.S_IFREG,
                                           info.st_size, attributes)
        return info

    monkeypatch.setattr(runner, '_live_file_info', observed)
    code = (f'from pathlib import Path; import time; '
            f'Path({str(output)!r}).write_bytes(b"x"); time.sleep(10)')
    with pytest.raises(AudioError, match='not a regular file'):
        execute(code, watch=(stage,), budget=AudioBudget(timeout=2))
    assert windows_watch_processes and all(child.poll() is not None for child in windows_watch_processes)
