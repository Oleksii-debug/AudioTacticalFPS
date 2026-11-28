"""Real subprocess lifecycle and no-clobber publication regression tests."""
from pathlib import Path
import os
import json
import shutil
import stat
import subprocess
import sys
import time
import wave

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


def test_explicit_diagnostic_exit_is_returned_without_changing_default_failure_behavior():
    result = execute('import sys; print("device inventory",file=sys.stderr); raise SystemExit(1)',
                     allow_failure=True)
    assert result.returncode == 1 and b'device inventory' in result.stderr
    assert not result.stopped


def test_graceful_stop_sends_only_fixed_quit_and_returns_finalized_child_output():
    result = execute('import sys; value=sys.stdin.buffer.read(2); assert value == b"q\\n"; print("closed")',
                     stop_requested=lambda: True)
    assert result.stopped and result.returncode == 0 and result.stdout.splitlines() == [b'closed']


def test_owned_tree_launch_failure_never_falls_back_to_unowned_popen(monkeypatch):
    from types import SimpleNamespace
    from carr_library.audio import process as module
    from carr_library.audio import windows_job
    attempts = []
    def reject(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AudioError('Owned Windows process admission refused')
    monkeypatch.setattr(windows_job, 'JobProcess', reject)
    # Exercise the existing Windows dispatch without changing process-global
    # os.name or replacing the ordinary subprocess executable.
    monkeypatch.setattr(module, 'os', SimpleNamespace(**dict(vars(os), name='nt')))
    monkeypatch.setattr(module.subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('A rejected owned launch fell back to Popen'))
    with pytest.raises(AudioError, match='admission refused'):
        execute('raise AssertionError("An unowned child must not start")',
                stop_requested=lambda: False, owned_tree=True)
    assert len(attempts) == 1 and attempts[0][1]['stdin_pipe'] is True


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


@pytest.mark.parametrize('explicit', [False, True])
def test_real_ffmpeg_cannot_overwrite_an_inherited_report_destination(tmp_path, monkeypatch, explicit):
    executable = shutil.which('ffmpeg')
    if executable is None:
        pytest.skip('Actual native report-file regression requires installed FFmpeg.')
    source, sentinel = tmp_path / 'input.wav', tmp_path / 'unrelated-private.txt'
    sentinel.write_bytes(b'Unchanged unrelated private file')
    with wave.open(str(source), 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 2400)
    report = 'file=unrelated-private.txt:level=48'
    monkeypatch.setenv('FFREPORT', report)
    environment = dict(os.environ) if explicit else None
    before = dict(environment) if explicit else None
    run_process([str(Path(executable).resolve()), '-v', 'error', '-nostdin',
                 '-i', str(source), '-f', 'null', '-'], cwd=tmp_path, env=environment)
    assert sentinel.read_bytes() == b'Unchanged unrelated private file'
    assert os.environ['FFREPORT'] == report
    assert environment == before


def test_process_preserves_device_locale_and_explicit_environment_without_report_keys():
    environment = dict(os.environ, FFREPORT='file=not-allowed', ffreport='also-not-allowed',
                       SDL_AUDIODRIVER='dummy', LANG='C', CARR_HOST_SENTINEL='kept')
    before = dict(environment)
    result = execute('import json,os; print(json.dumps(dict(os.environ)))', env=environment)
    received = json.loads(result.stdout)
    assert not any(key.casefold() == 'ffreport' for key in received)
    assert received['SDL_AUDIODRIVER'] == 'dummy'
    assert received['LANG'] == 'C' and received['CARR_HOST_SENTINEL'] == 'kept'
    assert environment == before
