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
    # Replacing this module reference leaves native wait/cleanup clocks and
    # subprocess/threading's own timing unchanged.
    monkeypatch.setattr(process, 'time',
                        SimpleNamespace(monotonic=lambda: actual_monotonic() + state.offset))

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


def run(code, **options):
    return process.run_process([sys.executable, '-I', '-B', '-c', code],
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
        release.write_bytes(b'explicit fixture release')
        assert child.wait(timeout=10) == 0
        # The runner did not observe an exit before entering this callback.
        # It cannot infer that completion preceded its deadline from a later
        # returncode, even when the callback itself waited for the real child.
        observed.offset += 6.0
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
