"""Actual child admission barriers; synthetic commands never open a device.

The diagnostic-exit and fixed-quit cases retain the prepared upstream process
contracts. The admission cases independently prove that the supplied command
cannot act until lifetime protection succeeds, including when it is refused.
"""
from pathlib import Path
import sys
import time

import pytest

from carr_library.audio import AudioError
from carr_library.audio import process as module


def _execute(code, **kwargs):
    return module.run_process(
        [str(Path(sys.executable).resolve()), '-I', '-B', '-c', code], **kwargs)


def _observe_before_admission(child, marker):
    # A real unprotected command has ample time to reveal the old race. A
    # protected launcher remains alive and blocked throughout this interval.
    deadline = time.monotonic() + 1
    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)


def test_explicit_diagnostic_exit_preserves_default_failure_contract():
    result = _execute('import sys; print("device inventory",file=sys.stderr); raise SystemExit(1)',
                      allow_failure=True)
    assert result.returncode == 1 and b'device inventory' in result.stderr
    assert not result.stopped
    with pytest.raises(AudioError, match='exit 1'):
        _execute('raise SystemExit(1)')


def test_admitted_capture_receives_only_fixed_quit_and_finalizes_output():
    result = _execute('import sys; value=sys.stdin.buffer.read(2); '
                      'assert value == b"q\\n"; print("closed")',
                      stop_requested=lambda: True)
    assert result.stopped and result.returncode == 0
    assert result.stdout.splitlines() == [b'closed']


def test_capture_command_waits_for_successful_lifetime_admission(tmp_path, monkeypatch):
    marker = tmp_path / 'capture-activated'
    protected, released = [], []

    def protect(child):
        _observe_before_admission(child, marker)
        assert not marker.exists(), 'Capture activated before lifetime ownership.'
        assert child.poll() is None
        protected.append(child)
        return lambda: released.append(child.pid)

    monkeypatch.setattr(module, '_capture_lifetime', protect)
    result = _execute(
        f'from pathlib import Path; Path({str(marker)!r}).write_text("activated"); print("complete")',
        stop_requested=lambda: False)
    assert marker.read_text() == 'activated'
    assert result.stdout.splitlines() == [b'complete'] and result.returncode == 0
    assert len(protected) == 1 and released == [protected[0].pid]
    assert protected[0].poll() == 0


def test_failed_lifetime_admission_never_activates_capture_and_reaps_launcher(
        tmp_path, monkeypatch):
    marker = tmp_path / 'must-not-activate'
    children = []

    def refuse(child):
        children.append(child)
        _observe_before_admission(child, marker)
        raise AudioError('Capture lifetime admission refused')

    monkeypatch.setattr(module, '_capture_lifetime', refuse)
    with pytest.raises(AudioError, match='admission refused'):
        _execute(f'from pathlib import Path; Path({str(marker)!r}).write_text("bad")',
                 stop_requested=lambda: False)
    assert not marker.exists(), 'A refused capture command performed a side effect.'
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed and children[0].stderr.closed


def test_cleanup_failure_still_reaps_launcher_and_closes_owned_pipes(tmp_path, monkeypatch):
    children = []

    def protect(child):
        children.append(child)
        def unconfirmed():
            raise module.CaptureCleanupError('Synthetic cleanup acknowledgement failure')
        return unconfirmed

    monkeypatch.setattr(module, '_capture_lifetime', protect)
    with pytest.raises(module.CaptureCleanupError, match='acknowledgement'):
        _execute('print("finished")', stop_requested=lambda: False)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed and children[0].stderr.closed
