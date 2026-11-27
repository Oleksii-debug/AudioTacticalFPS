"""Controlled group cleanup boundaries, with no native-execution claim.

The unchanged test_calibre_process_ownership and test_owned_process_tree suites
exercise real descendants. These controls cover uncertain waits and PID races.
"""
from contextlib import contextmanager
import io
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.audio import process as module


class Group:
    def __init__(self):
        self.members = ((21, 110),)
        self.identities = {21: (11, 110)}
        self.opened = []
        self.closed = []
        self.waited = []
        self.confirmed = False
        self.allow_exit = True
        self.retain_member_after_exit = False

    def snapshot(self, group, deadline):
        assert group == 11 and deadline > 0
        return self.members

    def open(self, pid, flags):
        assert flags == 0
        self.opened.append(pid)
        return pid + 200

    def wait(self, handle, milliseconds):
        assert handle not in self.closed
        self.waited.append((handle, milliseconds))
        if milliseconds and self.allow_exit:
            self.confirmed = True
            if not self.retain_member_after_exit:
                self.members = ()
        return self.confirmed

    def close(self, handle):
        assert handle not in self.closed
        self.closed.append(handle)


@pytest.fixture
def group(monkeypatch):
    value = Group()
    monkeypatch.setattr(module, 'os', SimpleNamespace(pidfd_open=value.open, close=value.close))
    monkeypatch.setattr(module, '_posix_group_snapshot', value.snapshot)
    monkeypatch.setattr(module, '_posix_process_identity', lambda pid: value.identities[pid])
    monkeypatch.setattr(module, '_posix_process_exited', value.wait)
    return value


def test_exact_member_wait_precedes_success_when_group_list_disappears(group):
    retained = {}
    assert module._wait_posix_group(11, retained, use_pidfds=True) is True
    assert group.opened == [21]
    assert any(handle == 221 and milliseconds > 0 for handle, milliseconds in group.waited)
    assert group.confirmed and group.closed == [221] and retained == {}


def test_waited_zombie_membership_does_not_require_parent_to_reap(group):
    retained = {}
    group.retain_member_after_exit = True
    assert module._wait_posix_group(11, retained, use_pidfds=True) is True
    assert group.members == ((21, 110),)
    assert group.waited[-1] == (221, 0)
    assert group.closed == [221]


def test_unconfirmed_process_keeps_exact_descriptor_for_retry(group):
    retained = {}
    group.allow_exit = False
    with pytest.raises(TimeoutError, match='unconfirmed'):
        module._wait_posix_group(11, retained, use_pidfds=True)
    assert retained == {(21, 110): 221}
    assert group.closed == [] and group.opened == [21]
    group.members = ()
    group.allow_exit = True
    assert module._wait_posix_group(11, retained, use_pidfds=True) is True
    assert group.opened == [21] and group.closed == [221] and retained == {}


def test_recycled_pid_is_not_adopted_as_the_selected_process(group, monkeypatch):
    group.identities[21] = (99, 999)
    snapshots = iter([((21, 110),), ()])
    monkeypatch.setattr(module, '_posix_group_snapshot', lambda *_args: next(snapshots))
    retained = {}
    assert module._wait_posix_group(11, retained, use_pidfds=True) is True
    assert group.opened == [21] and group.closed == [221]
    assert group.waited == [] and retained == {}


def test_new_member_is_captured_before_success(group, monkeypatch):
    snapshots = iter([((21, 110),), ((22, 120),), ((22, 120),), ()])
    group.identities[22] = (11, 120)
    monkeypatch.setattr(module, '_posix_group_snapshot', lambda *_args: next(snapshots))
    retained = {}
    assert module._wait_posix_group(11, retained, use_pidfds=True) is True
    assert group.opened == [21, 22] and group.closed == [221, 222]
    assert any(handle == 222 and milliseconds > 0 for handle, milliseconds in group.waited)


def test_group_disappearance_is_required_without_pidfd_capability(monkeypatch):
    calls = []
    def inspect(group, signal):
        calls.append((group, signal))
        if len(calls) == 3:
            raise ProcessLookupError()
    monkeypatch.setattr(module, 'os', SimpleNamespace(killpg=inspect))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 1, sleep=lambda _seconds: None))
    assert module._wait_posix_group(11, {}, use_pidfds=False) is True
    assert calls == [(11, 0)] * 3


def test_unsupported_host_cannot_turn_a_live_group_into_success(monkeypatch):
    clocks = iter([0, 6])
    calls = []
    monkeypatch.setattr(module, 'os', SimpleNamespace(killpg=lambda *args: calls.append(args)))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(clocks)))
    with pytest.raises(TimeoutError, match='unconfirmed'):
        module._wait_posix_group(11, {}, use_pidfds=False)
    assert calls == [(11, 0)]


def test_kernel_identity_parser_preserves_parentheses_and_exact_start_time(monkeypatch):
    fields = ['S', '1', '11'] + ['0'] * 16 + ['110']
    raw = ('21 (a name ) with brackets) ' + ' '.join(fields) + '\n').encode()
    def opened(path, mode):
        assert path == '/proc/21/stat' and mode == 'rb'
        return io.BytesIO(raw)
    monkeypatch.setattr(module, 'open', opened, raising=False)
    assert module._posix_process_identity(21) == (11, 110)


def test_kernel_identity_rejects_oversized_or_incomplete_rows(monkeypatch):
    for raw in (b'x' * 4097, b'21 (incomplete) S 1 11'):
        monkeypatch.setattr(module, 'open', lambda *_args: io.BytesIO(raw), raising=False)
        with pytest.raises(OSError, match='bounded format|incomplete'):
            module._posix_process_identity(21)


def test_membership_scan_stops_at_its_deadline_before_opening_processes(monkeypatch):
    consumed = []
    @contextmanager
    def entries(path):
        assert path == '/proc'
        def values():
            for name in ('21', '22'):
                consumed.append(name)
                yield SimpleNamespace(name=name)
        yield values()
    monkeypatch.setattr(module, 'os', SimpleNamespace(scandir=entries))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 2))
    monkeypatch.setattr(module, '_posix_process_identity',
                        lambda _pid: pytest.fail('No identity read after cleanup deadline'))
    with pytest.raises(TimeoutError, match='cleanup allowance'):
        module._posix_group_snapshot(11, 1)
    assert consumed == ['21']


@pytest.mark.parametrize('interrupted', [False, True])
def test_owned_root_is_not_reaped_before_quiescence_and_interruption_retains_retry(monkeypatch, interrupted):
    from carr_library.audio.models import AudioBudget, AudioCleanupPending
    actions, handles_seen = [], []
    confirmed = [False]
    attempts = [0]
    class Process:
        pid = 11
        args = ['synthetic.exe']
        returncode = None
        stdin = None
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        def poll(self):
            pytest.fail('Popen.poll must not reap the owned root before group cleanup')
        def wait(self, timeout=None):
            assert confirmed[0], 'Root identity must remain reserved until the member barrier'
            actions.append('reap')
            self.returncode = 0
            return 0
    selected = Process()
    def observe(kind, pid, options):
        assert (kind, pid, options) == (1, 11, 2 | 4 | 8)
        assert selected.returncode is None
        actions.append('observe')
        return SimpleNamespace(si_code=1, si_status=0)
    def capture(group, retained, deadline):
        assert group == 11 and deadline > 0
        retained.setdefault((21, 110), 221)
    def wait(group, retained, *, use_pidfds):
        assert group == 11 and use_pidfds is True
        handles_seen.append((id(retained), dict(retained)))
        attempts[0] += 1
        if interrupted and attempts[0] == 1:
            raise KeyboardInterrupt('Injected interruption during exact member wait')
        confirmed[0] = True
        retained.clear()
        actions.append('confirmed')
        return True
    def start(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        return selected
    monkeypatch.setattr(module, 'os', SimpleNamespace(
        name='posix', environ={}, waitid=observe, P_PID=1, WEXITED=2, WNOHANG=4, WNOWAIT=8,
        CLD_EXITED=1, killpg=lambda group, number: actions.append(('signal', group, number))))
    monkeypatch.setattr(module, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(module, '_can_own_posix_group', lambda: True)
    monkeypatch.setattr(module, '_capture_posix_group', capture)
    monkeypatch.setattr(module, '_wait_posix_group', wait)
    monkeypatch.setattr(module.subprocess, 'Popen', start)
    args = [str(Path(sys.executable).absolute()), '-B', '-c', 'pass']
    if interrupted:
        with pytest.raises(AudioCleanupPending) as raised:
            module.run_process(args, owned_tree=True, budget=AudioBudget(reserve_disk_bytes=0))
        assert raised.value.cleanup_owner.quiescent_event.wait(3)
        assert len(handles_seen) == 2 and handles_seen[0] == handles_seen[1]
    else:
        result = module.run_process(args, owned_tree=True, budget=AudioBudget(reserve_disk_bytes=0))
        assert result.returncode == 0
    assert confirmed[0] and actions.index('confirmed') < actions.index('reap')
    assert actions[-1] == 'reap' and selected.returncode == 0
    assert all(item[1:] == (11, 9) for item in actions if isinstance(item, tuple))


def test_unsupported_owned_group_is_refused_before_native_creation(monkeypatch):
    from carr_library.audio.models import AudioError
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(module, '_can_own_posix_group', lambda: False)
    monkeypatch.setattr(module.subprocess, 'Popen',
                        lambda *_args, **_kwargs: pytest.fail('No unowned fallback launch'))
    with pytest.raises(AudioError, match='before reaping'):
        module.run_process([str(Path(sys.executable).absolute()), '-B', '-c', 'pass'],
                           owned_tree=True)
