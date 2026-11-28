"""Actual Windows family objects created across the shutdown snapshot boundary.

Selected only by Windows gates; there is no simulated kernel or skip fallback.
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from carr_library.audio import process, windows_process
from carr_library.audio.cleanup import retained_temporary_directory


_LEADER = r"""
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
executable = sys.argv[2]
def publish(name, value):
    path = root / name
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temporary, path)
publish('leader.json', os.getpid())
while not (root / 'create-children.json').exists():
    time.sleep(.005)
children = [subprocess.Popen(
    [executable, '-I', '-c', 'import time; time.sleep(30)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(2)]
publish('children.json', [child.pid for child in children])
time.sleep(30)
"""


def _read_marker(path, deadline):
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'Actual Windows child readiness exceeded its existing bound.'
        try:
            with path.open('rb') as source:
                raw = source.read(257)
        except FileNotFoundError:
            time.sleep(min(.005, remaining))
            continue
        assert 0 < len(raw) <= 256
        return json.loads(raw)


@pytest.mark.parametrize('caller_failure', [False, True],
                         ids=['normal-return', 'original-source-error'])
def test_real_late_job_objects_require_retained_native_handles_and_exit(
        tmp_path, monkeypatch, caller_failure):
    assert sys.platform == 'win32' and os.name == 'nt', 'This native gate requires Windows.'
    executable = Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True)
    original_retain = windows_process._Job._retain_members
    owner = []
    observations = []
    late_handles = {}
    original_proof = []
    native_handles = []
    source_error = FileNotFoundError('Original source rejection must survive confirmed cleanup.')

    with retained_temporary_directory(prefix='carr-late-job-', dir=tmp_path) as directory:
        root = Path(directory)

        def retain_then_create(job, deadline):
            if not owner or job is not owner[0]._carr_job:
                return original_retain(job, deadline)
            assert owner and job is owner[0]._carr_job
            original_retain(job, deadline)
            assert len(job._shutdown_members) == job._shutdown_total
            original_proof.append((job._shutdown_total, job._shutdown_members))
            native_handles.append((job.handle, job.port))
            request = root / 'create-children.json'
            request.with_suffix('.pending').write_bytes(b'true')
            os.replace(request.with_suffix('.pending'), request)
            pids = _read_marker(root / 'children.json', deadline)
            assert len(pids) == 2 and len(set(pids)) == 2
            assert all(type(pid) is int and 0 < pid <= 0xFFFFFFFF
                       and pid not in job._shutdown_members for pid in pids)
            # The private completion port supplies positive identities. The
            # original lifetime proof is not enumerated again or overwritten.
            while not all(pid in job._members and job._members[pid][1]
                          and pid in job._notification_pids for pid in pids):
                job._drain_notifications(deadline)
                if all(pid in job._members and job._members[pid][1]
                       and pid in job._notification_pids for pid in pids):
                    break
                time.sleep(min(.005, job._remaining(deadline)))
            assert (job._shutdown_total, job._shutdown_members) == original_proof[0]
            late_handles.update({pid: job._members[pid][0] for pid in pids})
            assert len(set(late_handles.values())) == 2
            native = job.api.kernel
            native_error = job.api.error

            class ObserveNativeCalls:
                def __getattr__(self, name):
                    return getattr(native, name)

                def TerminateJobObject(self, handle, status):
                    result = native.TerminateJobObject(handle, status)
                    if result:
                        observations.append(('terminate', handle))
                    return result

                def WaitForSingleObject(self, handle, milliseconds):
                    result = native.WaitForSingleObject(handle, milliseconds)
                    if result == 0 and handle in late_handles.values():
                        observations.append(('signaled', handle))
                    return result

                def CloseHandle(self, handle):
                    result = native.CloseHandle(handle)
                    if result:
                        observations.append(('closed', handle))
                    return result

            # This private owner-only observer delegates every native call once
            # with unchanged arguments and results; it creates no handles.
            job.api = SimpleNamespace(kernel=ObserveNativeCalls(), error=native_error)

        monkeypatch.setattr(windows_process._Job, '_retain_members', retain_then_create)
        expected = pytest.raises(FileNotFoundError) if caller_failure else nullcontext()
        with expected as caught:
            with process.owned_process(
                    [str(executable), '-I', '-c', _LEADER, str(root), str(executable)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL) as child:
                owner.append(child)
                assert _read_marker(root / 'leader.json', time.monotonic() + 5) == child.pid
                if caller_failure:
                    raise source_error
        if caller_failure:
            assert caught.value is source_error
        assert len(original_proof) == 1
        child, job = owner[0], owner[0]._carr_job
        assert child.returncode is not None
        assert job.handle is None and job.port is None and job._leader_signaled
        assert not job._members
        assert job._shutdown_total == original_proof[0][0] + 2
        assert job._shutdown_members == original_proof[0][1] | set(late_handles)
        assert set(late_handles) <= job._completed_members
        assert len(observations) >= 5
        assert observations[0][0] == 'terminate'
        for handle in late_handles.values():
            assert observations.count(('signaled', handle)) == 1
            assert observations.count(('closed', handle)) == 1
            assert observations.index(('signaled', handle)) < observations.index(('closed', handle))
        # The real family owner closes its job only after each actual late
        # process handle returned WAIT_OBJECT_0, never from a zero active count.
        job_handle, port_handle = native_handles[0]
        assert observations.count(('closed', job_handle)) == 1
        assert observations.count(('closed', port_handle)) == 1
        assert max(observations.index(('closed', handle)) for handle in late_handles.values()) < (
            observations.index(('closed', job_handle))) < observations.index(('closed', port_handle))
