"""Actual child-process boundary regressions for the current FFmpeg runner.

The selected command is wrapped only at Popen to run an isolated Python child
with controlled diagnostics. These cases exercise the actual temporary log,
polling and cleanup; they do not stand in for actual FFmpeg signal tests.
"""
from pathlib import Path
import subprocess
import sys
import time

import pytest

from carr_library.audio import ffmpeg
from carr_library.audio.jobs import RenderCancelled


LIMIT = 1024 * 1024


@pytest.fixture
def children(monkeypatch):
    original = subprocess.Popen
    launched, logs = [], []

    def select(code, *, finished=False):
        def start(command, **options):
            assert command == [str(Path(sys.executable).resolve()), '-hide_banner',
                               '-nostdin', '-loglevel', 'error', 'fixture-only']
            assert options['stdin'] == subprocess.DEVNULL
            assert options['stdout'] == subprocess.DEVNULL
            logs.append(options['stderr'])
            child = original([sys.executable, '-I', '-B', '-c', code], **options)
            launched.append(child)
            if finished:
                # Force the actual fast-exit boundary, independent of scheduler
                # timing: the runner's first poll must already see completion.
                child.wait(timeout=10)
            return child
        monkeypatch.setattr(subprocess, 'Popen', start)

    yield select, launched, logs
    # Retain ownership even when a regression makes the assertions fail.
    for child in launched:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def invoke(**options):
    return ffmpeg.run_ffmpeg(Path(sys.executable).resolve(), ['fixture-only'], **options)


@pytest.mark.parametrize('exit_code', [0, 7])
def test_fast_finished_child_cannot_bypass_final_diagnostic_budget(children, exit_code):
    select, launched, logs = children
    select('import sys;sys.stderr.buffer.write(b"x" * ' + str(LIMIT + 1) +
           ');sys.stderr.buffer.flush();raise SystemExit(' + str(exit_code) + ')',
           finished=True)
    with pytest.raises(RuntimeError, match='excessive diagnostic output'):
        invoke()
    assert len(launched) == 1 and launched[0].poll() == exit_code
    assert len(logs) == 1 and logs[0].closed


def test_exact_final_diagnostic_budget_still_returns_complete_output(children):
    select, launched, logs = children
    select('import sys;sys.stderr.buffer.write(b"x" * ' + str(LIMIT) +
           ');sys.stderr.buffer.flush()', finished=True)
    assert invoke() == 'x' * LIMIT
    assert len(launched) == 1 and launched[0].poll() == 0
    assert len(logs) == 1 and logs[0].closed


def test_running_child_is_settled_before_diagnostic_overflow_returns(children):
    select, launched, logs = children
    select('import sys,time;sys.stderr.buffer.write(b"x" * ' + str(LIMIT + 1) +
           ');sys.stderr.buffer.flush();time.sleep(30)')
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='excessive diagnostic output'):
        invoke()
    assert time.monotonic() - started < 10
    assert len(launched) == 1 and launched[0].poll() is not None
    assert len(logs) == 1 and logs[0].closed


def test_cancel_before_spawn_does_not_start_a_child(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled FFmpeg work must not start a process.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(RenderCancelled):
        invoke(cancelled=lambda: True)


def test_bounded_nonzero_child_remains_failure_with_its_diagnostic(children):
    select, launched, logs = children
    select('import sys;sys.stderr.write("fixture failure");raise SystemExit(7)',
           finished=True)
    with pytest.raises(RuntimeError, match='could not complete.*fixture failure'):
        invoke()
    assert len(launched) == 1 and launched[0].poll() == 7
    assert len(logs) == 1 and logs[0].closed


def test_launch_failure_closes_only_the_owned_diagnostic_file(monkeypatch):
    logs = []
    original_error = OSError('Synthetic child admission failure')
    def fail(command, **options):
        logs.append(options['stderr'])
        raise original_error
    monkeypatch.setattr(subprocess, 'Popen', fail)
    with pytest.raises(OSError, match='child admission') as outcome:
        invoke()
    assert outcome.value is original_error
    assert len(logs) == 1 and logs[0].closed
