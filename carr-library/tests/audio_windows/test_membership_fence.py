"""Actual Windows family objects created across the shutdown snapshot boundary.

Selected only by Windows gates; there is no simulated kernel or skip fallback.
"""
from __future__ import annotations

from contextlib import nullcontext
import ctypes
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
child_source = ("import json, os, pathlib, sys, time; "
                "p = pathlib.Path(sys.argv[1]); t = p.with_suffix('.pending'); "
                "t.write_text(json.dumps(os.getpid()), encoding='utf-8'); "
                "os.replace(t, p); time.sleep(30)")
children = [subprocess.Popen(
    [executable, '-I', '-c', child_source, str(root / f'child-{index}.json')],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW) for index in range(2)]
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
        tmp_path, monkeypatch, caller_failure, capsys):
    assert sys.platform == 'win32' and os.name == 'nt', 'This native gate requires Windows.'
    executable = Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True)
    original_retain = windows_process._Job._retain_members
    owner = []
    observations = []
    late_handles = {}
    late_family_handles = {}
    native_notices = set()
    native_proof = {}
    original_proof = []
    native_handles = []
    source_error = FileNotFoundError('Original source rejection must survive confirmed cleanup.')

    with retained_temporary_directory(prefix='carr-late-job-', dir=tmp_path) as directory:
        root = Path(directory)

        def retain_then_create(job, deadline):
            if not owner or job is not owner[0]._carr_job:
                return original_retain(job, deadline)
            assert owner and job is owner[0]._carr_job
            native = job.api.kernel
            native_error = job.api.error

            class ObserveNativeCalls:
                def __getattr__(self, name):
                    return getattr(native, name)

                def GetQueuedCompletionStatus(self, port, message, key, value, milliseconds):
                    result = native.GetQueuedCompletionStatus(port, message, key, value, milliseconds)
                    # Read only successful replies. A failed call's LastError
                    # and indeterminate output fields remain untouched.
                    if result and ctypes.cast(message, ctypes.POINTER(ctypes.c_uint32)).contents.value == 6:
                        received_key = ctypes.cast(key, ctypes.POINTER(ctypes.c_size_t)).contents.value
                        pid = ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p)).contents.value
                        if received_key == job.handle and pid is not None:
                            native_notices.add(int(pid))
                    return result

                def TerminateJobObject(self, handle, status):
                    result = native.TerminateJobObject(handle, status)
                    if result:
                        observations.append(('terminate', handle))
                    return result

                def WaitForSingleObject(self, handle, milliseconds):
                    result = native.WaitForSingleObject(handle, milliseconds)
                    if result == 0 and handle in late_family_handles.values():
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

            def actual_accounting():
                assert time.monotonic() < deadline
                value = windows_process._Accounting()
                if not native.QueryInformationJobObject(
                        job.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
                    raise native_error()
                assert time.monotonic() < deadline
                assert 1 <= value.total_processes <= 4096
                return int(value.total_processes)

            original_retain(job, deadline)
            assert len(job._shutdown_members) == job._shutdown_total
            original_proof.append((job._shutdown_total, job._shutdown_members))
            native_handles.append((job.handle, job.port))
            before_total = actual_accounting()
            assert before_total == original_proof[0][0]
            request = root / 'create-children.json'
            request.with_suffix('.pending').write_bytes(b'true')
            os.replace(request.with_suffix('.pending'), request)
            pids = _read_marker(root / 'children.json', deadline)
            assert len(pids) == 2 and len(set(pids)) == 2
            assert all(type(pid) is int and 0 < pid <= 0xFFFFFFFF
                       and pid not in job._shutdown_members for pid in pids)
            for index, pid in enumerate(pids):
                assert _read_marker(root / f'child-{index}.json', deadline) == pid
            # Count native objects independently from the owner's retained PID
            # sets. Two requested Python actors need not mean two Job objects.
            # Every extra positive native identity must still have an exact
            # verified retained handle before this fixture permits termination.
            while True:
                job._drain_notifications(deadline)
                after_total = actual_accounting()
                added = native_notices - original_proof[0][1]
                if (set(pids) <= added and after_total == before_total + len(added)
                        and all(pid in job._members and job._members[pid][1]
                                and pid in job._notification_pids for pid in added)):
                    break
                time.sleep(min(.005, job._remaining(deadline)))
            assert (job._shutdown_total, job._shutdown_members) == original_proof[0]
            late_handles.update({pid: job._members[pid][0] for pid in pids})
            assert len(set(late_handles.values())) == 2
            late_family_handles.update({pid: job._members[pid][0] for pid in added})
            assert set(late_handles) <= set(late_family_handles)
            assert len(set(late_family_handles.values())) == len(added)
            # Use a separate ctypes binding only for this read-only image
            # query; it neither opens handles nor changes the owner's kernel.
            image_api = ctypes.WinDLL('kernel32', use_last_error=True)
            image_api.QueryFullProcessImageNameW.argtypes = (
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32))
            image_api.QueryFullProcessImageNameW.restype = ctypes.c_int
            images = []
            for pid, handle in sorted(late_family_handles.items()):
                assert time.monotonic() < deadline
                assert native.GetProcessId(handle) == pid
                belongs = ctypes.c_int()
                assert native.IsProcessInJob(handle, job.handle, ctypes.byref(belongs))
                assert belongs.value
                buffer = ctypes.create_unicode_buffer(32768)
                length = ctypes.c_uint32(len(buffer))
                if not image_api.QueryFullProcessImageNameW(
                        handle, 0, buffer, ctypes.byref(length)):
                    raise ctypes.WinError(ctypes.get_last_error())
                assert 0 < length.value < len(buffer)
                images.append({'pid': pid, 'image': buffer.value,
                               'requested_actor': pid in late_handles})
                assert time.monotonic() < deadline
            assert actual_accounting() == after_total
            native_proof.update(before_total=before_total, after_total=after_total,
                requested_pids=list(pids), added_native_pids=sorted(added), images=images)

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
        # Keep exactly two requested actors, and compare all actual growth to
        # the separate native accounting/positive-message observations.
        assert len(late_handles) == 2
        assert len(original_proof[0][1] | set(late_handles)) == original_proof[0][0] + 2
        assert job._shutdown_total == native_proof['after_total'], native_proof
        assert job._shutdown_total == original_proof[0][0] + len(native_proof['added_native_pids'])
        assert job._shutdown_members == original_proof[0][1] | set(native_proof['added_native_pids'])
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
        assert set(late_family_handles) <= job._completed_members
        for handle in late_family_handles.values():
            assert observations.count(('signaled', handle)) == 1
            assert observations.count(('closed', handle)) == 1
            assert observations.index(('signaled', handle)) < observations.index(('closed', handle))
        assert max(observations.index(('closed', handle)) for handle in late_family_handles.values()) < (
            observations.index(('closed', job_handle)))
        # Emission occurs only after native cleanup and all ordering checks,
        # outside the unchanged five-second family shutdown deadline.
        with capsys.disabled():
            print('NATIVE_LATE_JOB_PROOF ' + json.dumps(dict(native_proof,
                selected_executable=str(executable), caller_failure=caller_failure,
                independently_checked_handles=len(late_family_handles),
                signal_before_close=True, job_closed_after_members=True)), flush=True)
