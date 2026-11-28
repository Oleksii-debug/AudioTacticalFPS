"""Real owned-process capture regressions; no physical microphone is required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from carr_library.audio import process
from carr_library.audio.cleanup import AudioCleanupPending


def observe_launch(monkeypatch):
    children = []
    original = process._start_process

    def start(command, **options):
        child = original(command, **options)
        children.append((child, dict(options)))
        return child

    monkeypatch.setattr(process, '_start_process', start)
    return children


def assert_closed(child):
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed
    if child.stdin is not None:
        assert child.stdin.closed
    if os.name == 'nt':
        assert child._carr_job.handle is None


def test_actual_capture_child_uses_owned_launcher_and_sanitized_environment(tmp_path, monkeypatch):
    children = observe_launch(monkeypatch)
    report, keys = tmp_path / 'unrequested-report.log', tmp_path / 'unrequested-keys.log'
    environment = os.environ.copy()
    environment.update(FFREPORT='file=' + str(report), SSLKEYLOGFILE=str(keys),
                       CARR_CAPTURE_TEST='same-request')
    original = dict(environment)
    code = ("import json,os; print(json.dumps({name:os.environ.get(name) for name in "
            "['FFREPORT','SSLKEYLOGFILE','CARR_CAPTURE_TEST']}))")
    result = process.run_process([sys.executable, '-I', '-B', '-c', code],
                                 env=environment, owned_tree=True, budget=process.AudioBudget(timeout=5))
    assert json.loads(result.stdout) == {
        'FFREPORT': None, 'SSLKEYLOGFILE': None, 'CARR_CAPTURE_TEST': 'same-request'}
    assert environment == original
    assert not report.exists() and not keys.exists()
    child, options = children[0]
    assert len(children) == 1 and options['shell'] is False
    assert options['stdin'] == subprocess.DEVNULL
    assert_closed(child)


@pytest.mark.parametrize('failed_start', [1, 2])
def test_actual_capture_reader_start_failure_closes_exact_owned_child(monkeypatch, failed_start):
    children = observe_launch(monkeypatch)
    original = threading.Thread.start
    starts = 0

    def start(thread):
        nonlocal starts
        starts += 1
        if starts == failed_start:
            raise RuntimeError('Synthetic diagnostic thread admission failure')
        return original(thread)

    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        with pytest.raises(RuntimeError, match='thread admission'):
            process.run_process([sys.executable, '-I', '-B', '-c',
                                 'import time; time.sleep(30)'],
                                owned_tree=True, budget=process.AudioBudget(timeout=5))
        assert len(children) == 1
        assert_closed(children[0][0])
    finally:
        # Never leave the test's exact owned child behind if an assertion fails.
        for child, _ in children:
            process._stop_owned(child)


def test_actual_capture_pending_cleanup_keeps_owned_child_until_confirmation(monkeypatch):
    children = observe_launch(monkeypatch)
    original = process._stop_owned
    release = threading.Event()
    attempts = 0
    pending = None

    def delayed_cleanup(child):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError('Synthetic first owned-child cleanup timeout')
        if not release.is_set():
            return False
        return original(child)

    monkeypatch.setattr(process, '_stop_owned', delayed_cleanup)
    try:
        with pytest.raises(AudioCleanupPending) as failure:
            process.run_process([sys.executable, '-I', '-B', '-c',
                                 'import time; time.sleep(30)'],
                                cancelled=lambda: bool(children),
                                owned_tree=True, budget=process.AudioBudget(timeout=5))
        pending = failure.value.cleanup_owner
        child = children[0][0]
        assert len(children) == 1 and child.poll() is None
        assert not pending.quiescent and not child.stdout.closed
        assert pending.close() is False
        callbacks_done = threading.Event()
        pending.add_quiescent_callback(callbacks_done.set)
        release.set()
        assert callbacks_done.wait(10)
        # The completion callbacks close owned pipes after the native wait.
        # An explicit retry is idempotent; it cannot target another process.
        assert pending.close() is True
        for stream in (child.stdout, child.stderr):
            assert stream.closed
        assert_closed(child)
    finally:
        release.set()
        if pending is not None:
            pending.close()
            pending.quiescent_event.wait(10)
        for child, _ in children:
            original(child)


def test_actual_capture_stop_sends_only_fixed_quit_command(tmp_path):
    ready = tmp_path / 'ready'
    code = (
        'import pathlib,sys;'
        'pathlib.Path(sys.argv[1]).write_text("ready");'
        'sys.stdout.buffer.write(sys.stdin.buffer.read())'
    )
    result = process.run_process([sys.executable, '-I', '-B', '-c', code, str(ready)],
        owned_tree=True, budget=process.AudioBudget(timeout=5), stop_requested=ready.exists)
    assert result.returncode == 0 and result.stopped
    assert result.stdout == b'q\n'


def test_expected_device_inspection_exit_does_not_bypass_diagnostic_budget():
    result = process.run_process([sys.executable, '-I', '-B', '-c',
        'import sys;sys.stderr.buffer.write(b"no capture devices");raise SystemExit(1)'],
        owned_tree=True, budget=process.AudioBudget(timeout=5), allow_failure=True)
    assert result.returncode == 1 and not result.stopped
    assert result.stderr == b'no capture devices'
    with pytest.raises(process.AudioError, match='diagnostic'):
        process.run_process([sys.executable, '-I', '-B', '-c',
            'import os;os.write(2,b"x"*20000);raise SystemExit(1)'],
            owned_tree=True, budget=process.AudioBudget(timeout=5, max_log_bytes=1024),
            allow_failure=True)
