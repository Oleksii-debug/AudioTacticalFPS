"""Real subprocess lifecycle and no-clobber publication regression tests."""
from pathlib import Path
import os
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
