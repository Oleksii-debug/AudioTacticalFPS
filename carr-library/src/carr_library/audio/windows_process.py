"""Private Windows process trees, assigned before the initial thread can run.

The stdlib launcher owns quoting, redirected pipes and handle inheritance.
Each instance receives a copied function-global namespace with a private Win32
proxy; no global subprocess function or Win32 module is modified.
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import subprocess
import time
from types import FunctionType, SimpleNamespace


CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
KILL_ON_JOB_CLOSE = 0x00002000
_MAX_JOB_MEMBERS = 4096
_MAX_JOB_NOTIFICATIONS = 4 * _MAX_JOB_MEMBERS
_BASE_POPEN = subprocess.Popen


class _Accounting(ctypes.Structure):
    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, information class 1.
    # https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
    _fields_ = [(name, ctypes.c_int64) for name in
                ('user_time', 'kernel_time', 'period_user_time', 'period_kernel_time')] + [
                (name, ctypes.c_uint32) for name in
                ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]


class _ProcessIds(ctypes.Structure):
    # JOBOBJECT_BASIC_PROCESS_ID_LIST, information class 3. The fixed bound
    # covers nested jobs without an unbounded native allocation.
    _fields_ = [('assigned', ctypes.c_uint32), ('listed', ctypes.c_uint32),
                ('processes', ctypes.c_size_t * _MAX_JOB_MEMBERS)]


class _CompletionPort(ctypes.Structure):
    _fields_ = [('key', ctypes.c_void_p), ('port', ctypes.c_void_p)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', ctypes.c_uint32), ('minimum_working_set', ctypes.c_size_t),
                ('maximum_working_set', ctypes.c_size_t), ('active_processes', ctypes.c_uint32),
                ('affinity', ctypes.c_size_t), ('priority', ctypes.c_uint32),
                ('scheduling', ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('read_operations', 'write_operations', 'other_operations',
                 'read_bytes', 'write_bytes', 'other_bytes')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', _IoCounters),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


@lru_cache(maxsize=1)
def _kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        'CreateJobObjectW': ((ctypes.c_void_p, ctypes.c_wchar_p), handle),
        'CreateIoCompletionPort': ((handle, handle, ctypes.c_size_t, dword), handle),
        'SetHandleInformation': ((handle, dword, dword), boolean),
        'GetQueuedCompletionStatus': ((handle, ctypes.POINTER(dword),
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(handle), dword), boolean),
        'SetInformationJobObject': ((handle, ctypes.c_int, ctypes.c_void_p, dword), boolean),
        'AssignProcessToJobObject': ((handle, handle), boolean),
        'ResumeThread': ((handle,), dword),
        'TerminateJobObject': ((handle, dword), boolean),
        'QueryInformationJobObject': ((handle, ctypes.c_int, ctypes.c_void_p, dword, ctypes.c_void_p), boolean),
        'OpenProcess': ((dword, boolean, dword), handle),
        'GetProcessId': ((handle,), dword),
        'IsProcessInJob': ((handle, handle, ctypes.POINTER(boolean)), boolean),
        'WaitForSingleObject': ((handle, dword), dword),
        'CloseHandle': ((handle,), boolean),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return SimpleNamespace(kernel=kernel, error=lambda: ctypes.WinError(ctypes.get_last_error()))


class _Job:
    def __init__(self):
        self.api = _kernel()
        self.assigned = False
        self._members = {}
        self._completed_members = set()
        self._leader_handle = self._leader_pid = None
        self._leader_signaled = self._leader_closed = False
        self._shutdown_total = None
        self._shutdown_members = frozenset()
        self._admission_failure = None
        self._membership_query = None
        self._membership_query_count = 0
        self._notification_pids = set()
        self._notification_count = 0
        self.port = None
        self.handle = self.api.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise self.api.error()
        limits = _ExtendedLimits()
        limits.basic.flags = KILL_ON_JOB_CLOSE
        try:
            if not self.api.kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise self.api.error()
            # Associate while empty, before any initial thread can run. This
            # private port records positive member identities, never an
            # inference from absent EXIT/ACTIVE_ZERO notifications.
            self.port = self.api.kernel.CreateIoCompletionPort(
                ctypes.c_void_p(-1).value, None, 0, 0)
            if not self.port:
                raise self.api.error()
            if not self.api.kernel.SetHandleInformation(self.port, 1, 0):
                raise self.api.error()
            association = _CompletionPort(self.handle, self.port)
            if not self.api.kernel.SetInformationJobObject(
                    self.handle, 7, ctypes.byref(association), ctypes.sizeof(association)):
                raise self.api.error()
        except BaseException as failure:
            try:
                self.close()
            except Exception as cleanup:
                from .cleanup import pending_cleanup
                raise pending_cleanup(self.close,
                    message='Windows audio job cleanup is still pending.') from failure
            raise

    def assign(self, process):
        if self.assigned:
            raise RuntimeError('An audio job may own only one directly launched leader.')
        pid = self.api.kernel.GetProcessId(process)
        if not pid:
            raise self.api.error()
        if not self.api.kernel.AssignProcessToJobObject(self.handle, process):
            raise self.api.error()
        self._leader_handle, self._leader_pid = process, int(pid)
        self.assigned = True

    def resume(self, thread):
        previous = self.api.kernel.ResumeThread(thread)
        if previous == 0xFFFFFFFF:
            raise self.api.error()
        if previous != 1:
            raise OSError('The local worker initial thread did not resume from its owned suspended state.')

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('The owned Windows audio job has not confirmed shutdown.')
        return remaining

    def _query(self, kind, value):
        returned = ctypes.c_uint32() if kind == 3 else None
        succeeded = self.api.kernel.QueryInformationJobObject(
            self.handle, kind, ctypes.byref(value), ctypes.sizeof(value),
            None if returned is None else ctypes.byref(returned))
        # GetLastError is meaningful only after failure; capture it before any
        # other native operation. A successful query may leave a stale value.
        failure = None if succeeded else self.api.error()
        if kind == 3:
            self._membership_query_count += 1
            self._membership_query = {
                'attempt': self._membership_query_count,
                'api_success': int(bool(succeeded)),
                'assigned': int(value.assigned), 'listed': int(value.listed),
                'capacity': _MAX_JOB_MEMBERS, 'buffer_bytes': ctypes.sizeof(value),
                'returned_length': int(returned.value),
                'winerror': None if failure is None else getattr(failure, 'winerror', None),
            }
        if failure is not None:
            raise failure
        return value

    def record_leader_exit(self, handle):
        """Record actual native exit before an aborted launch releases its handle."""
        if not self.assigned:
            return
        if handle != self._leader_handle:
            raise OSError('The owned audio leader handle does not match its launch.')
        result = self.api.kernel.WaitForSingleObject(handle, 0)
        if result == 0xFFFFFFFF:
            raise self.api.error()
        if result != 0:
            raise OSError('The owned Windows audio leader has not confirmed exit.')
        self._leader_signaled = True

    def _validate_member(self, pid):
        handle, verified = self._members[pid]
        if verified:
            return
        belongs = ctypes.c_int()
        if not self.api.kernel.IsProcessInJob(handle, self.handle, ctypes.byref(belongs)):
            raise self.api.error()
        if not belongs.value:
            # A reused PID is not ownership. Close only our query handle,
            # never wait for or terminate the unrelated process.
            if not self.api.kernel.CloseHandle(handle):
                raise self.api.error()
            del self._members[pid]
            raise OSError('The Windows audio job membership changed during shutdown admission.')
        self._members[pid] = (handle, True)

    def _retain_pid(self, pid, deadline):
        self._remaining(deadline)
        if pid == self._leader_pid or pid in self._completed_members:
            return
        if pid in self._members:
            self._validate_member(pid)
            return
        if len(self._members) + len(self._completed_members) >= _MAX_JOB_MEMBERS:
            raise OSError('The Windows audio job exceeds its retained identity allowance.')
        # SYNCHRONIZE plus PROCESS_QUERY_LIMITED_INFORMATION only.
        handle = self.api.kernel.OpenProcess(0x00101000, False, pid)
        if not handle:
            error = self.api.error()
            if getattr(error, 'winerror', None) == 87:
                # Valid nonzero PID and fixed valid access mask: the old
                # process object no longer exists. Only a positive job notice
                # authorizes historical completion; absence alone proves none.
                # Microsoft ProcessManager.OpenProcess treats this same valid-
                # argument ERROR_INVALID_PARAMETER result as an exited object.
                if pid in self._notification_pids:
                    self._completed_members.add(pid)
                return
            raise error
        self._members[pid] = (handle, False)
        self._validate_member(pid)

    def _drain_notifications(self, deadline):
        # Ordinary job messages are not guaranteed. Missing messages therefore
        # cannot establish completion; the lifetime coverage check remains
        # mandatory. PID deduplication also prevents repeated/nested notices
        # from inventing a second process proof.
        while True:
            self._remaining(deadline)
            message, key, value = ctypes.c_uint32(), ctypes.c_size_t(), ctypes.c_void_p()
            received = self.api.kernel.GetQueuedCompletionStatus(
                self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(value), 0)
            if not received:
                error = self.api.error()
                if getattr(error, 'winerror', None) == 258 and value.value is None:
                    return
                raise error
            self._notification_count += 1
            if self._notification_count > _MAX_JOB_NOTIFICATIONS:
                raise OSError('The Windows audio job exceeds its bounded notification inventory.')
            if key.value != self.handle:
                raise OSError('A Windows audio job notification has a foreign completion key.')
            if message.value != 6:  # JOB_OBJECT_MSG_NEW_PROCESS
                continue
            pid = value.value
            if pid is None or not 0 < pid <= 0xFFFFFFFF:
                raise OSError('A Windows audio job notification has an invalid process identity.')
            self._notification_pids.add(int(pid))
            if len(self._notification_pids) > _MAX_JOB_MEMBERS:
                raise OSError('The Windows audio job exceeds its bounded notified identities.')
            self._retain_pid(int(pid), deadline)

    def _check_coverage(self):
        known = {self._leader_pid} | self._completed_members | {
            pid for pid, (_handle, verified) in self._members.items() if verified}
        if len(known) != self._shutdown_total:
            raise OSError('A historical Windows audio job member has no retained exit proof; '
                          'owned cleanup remains pending.')

    def _retain_members(self, deadline):
        # Hold exact process objects before termination, including nested jobs.
        # A job's accounting counter is not a per-process signaled-handle wait.
        while True:
            self._drain_notifications(deadline)
            self._remaining(deadline)
            before = self._query(1, _Accounting())
            if not 1 <= before.total_processes <= _MAX_JOB_MEMBERS:
                raise OSError('The Windows audio job exceeds its bounded lifetime inventory.')
            members = self._query(3, _ProcessIds())
            if members.assigned > _MAX_JOB_MEMBERS or members.listed > _MAX_JOB_MEMBERS:
                raise OSError('The Windows audio job exceeds its bounded membership inventory.')
            if members.listed > members.assigned:
                raise OSError('The Windows audio job returned invalid membership counts.')
            if members.listed < members.assigned:
                # A partial reply grants no process identity or shutdown proof.
                # Requery only before termination, consuming the same deadline
                # and rechecking notifications/accounting on the next iteration.
                time.sleep(min(.02, self._remaining(deadline)))
                continue
            identifiers = tuple(int(members.processes[index]) for index in range(members.listed))
            if (len(set(identifiers)) != len(identifiers)
                    or any(not 0 < pid <= 0xFFFFFFFF for pid in identifiers)):
                raise OSError('The Windows audio job returned an invalid process inventory.')
            for pid in tuple(self._members):
                self._remaining(deadline)
                self._validate_member(pid)
            for pid in identifiers:
                self._retain_pid(pid, deadline)
            self._remaining(deadline)
            after = self._query(1, _Accounting())
            if after.total_processes == before.total_processes:
                self._shutdown_total = int(after.total_processes)
                self._shutdown_members = frozenset(
                    {self._leader_pid} | self._completed_members | {
                        pid for pid, (_handle, verified) in self._members.items() if verified})
                return
            # New members entered while handles were being retained. Enumerate
            # again within the same deadline before issuing termination.

    def _check_membership_fence(self):
        current = self._query(1, _Accounting())
        if current.total_processes != self._shutdown_total:
            # A late positive notice may already have supplied an exact retained
            # process object. Extend only a complete original proof, with every
            # added identity still held and verified in this job. A notice plus
            # ERROR_INVALID_PARAMETER, an empty PID list, or zero active count
            # cannot admit growth. All admitted handles must still be waited.
            known = {self._leader_pid} | self._completed_members | {
                pid for pid, (_handle, verified) in self._members.items() if verified}
            added = known - self._shutdown_members
            if (self._shutdown_total < current.total_processes <= _MAX_JOB_MEMBERS
                    and len(self._shutdown_members) == self._shutdown_total
                    and self._shutdown_members <= known
                    and len(known) == current.total_processes
                    and added <= self._notification_pids
                    and all(pid in self._members and self._members[pid][1] for pid in added)):
                self._shutdown_total = int(current.total_processes)
                self._shutdown_members = frozenset(known)
            else:
                failure = OSError('Windows audio job membership changed across termination; '
                                  'owned cleanup remains pending.')
                failure.add_note('Windows audio job membership fence: ' + str({
                    'admitted_total': self._shutdown_total,
                    'current_total': int(current.total_processes),
                    'active': int(current.active_processes),
                    'admitted_identities': len(self._shutdown_members),
                    'known_identities': len(known),
                    'retained_verified': sum(verified for _handle, verified in self._members.values()),
                    'completed_identities': len(self._completed_members),
                    'notified_identities': len(self._notification_pids),
                }))
                raise failure
        return current

    def _wait_leader(self, leader, deadline):
        if leader is None:
            if not self._leader_signaled:
                raise OSError('The owned Windows audio leader has no recorded exit proof.')
            return
        retained = getattr(leader, '_handle', None)
        if (retained is None or int(retained) != self._leader_handle
                or leader.pid != self._leader_pid):
            raise OSError('The owned audio leader does not match its launch handle and identity.')
        if not self._leader_signaled:
            leader.wait(timeout=self._remaining(deadline))
            self.record_leader_exit(self._leader_handle)
        if not self._leader_closed:
            retained.Close()
            self._leader_closed = True

    def _wait_members(self, deadline):
        for pid, (handle, verified) in tuple(self._members.items()):
            if not verified:
                raise OSError('An owned audio process handle has not passed membership verification.')
            if pid not in self._completed_members:
                milliseconds = min(5000, int(self._remaining(deadline) * 1000))
                result = self.api.kernel.WaitForSingleObject(handle, milliseconds)
                if result == 0x102:
                    raise TimeoutError('An owned Windows audio process has not confirmed exit.')
                if result == 0xFFFFFFFF:
                    raise self.api.error()
                if result != 0:
                    raise OSError('The owned Windows audio process returned an invalid wait result.')
                self._completed_members.add(pid)
            if not self.api.kernel.CloseHandle(handle):
                raise self.api.error()
            del self._members[pid]

    def _close_port(self):
        if self.port is not None:
            if not self.api.kernel.CloseHandle(self.port):
                raise self.api.error()
            self.port = None
        return True

    def close(self, *, leader=None):
        handle = self.handle
        if handle is None:
            return self._close_port()
        deadline = time.monotonic() + 5
        if (self.assigned and self._shutdown_total is None
                and self._admission_failure is None):
            try:
                self._retain_members(deadline)
            except Exception as failure:
                if self._membership_query is not None:
                    failure.add_note('Last Windows audio job membership query: '
                                     + str(self._membership_query))
                self._admission_failure = failure
        # Even failed or timed-out inventory admission must stop the exact
        # owned job. It must not authorize a fresh post-termination inventory,
        # discard uncertainty, or release the resource/staging cleanup owner.
        if not self.api.kernel.TerminateJobObject(handle, 1):
            raise self.api.error()
        if self._admission_failure is not None:
            raise self._admission_failure
        self._remaining(deadline)
        if self.assigned:
            self._drain_notifications(deadline)
            self._check_membership_fence()
            self._check_coverage()
            self._wait_leader(leader, deadline)
            self._wait_members(deadline)
            while True:
                self._remaining(deadline)
                accounting = self._check_membership_fence()
                if accounting.active_processes == 0:
                    break
                time.sleep(min(.02, self._remaining(deadline)))
            if (not self._leader_signaled
                    or len(self._completed_members | {self._leader_pid}) != self._shutdown_total):
                raise OSError('The Windows audio job does not have an exit proof for every member.')
        if not self.api.kernel.CloseHandle(handle):
            raise self.api.error()
        self.handle = None
        return self._close_port()


class _OwnedWinAPI:
    def __init__(self, native, job):
        self.native, self.job = native, job
        self.process = None
        self.thread_closed = False
        self.process_closed = False

    def __getattr__(self, name):
        return getattr(self.native, name)

    def CreateProcess(self, *arguments):
        if self.process is not None:
            raise RuntimeError('A local worker launcher may create only its own process.')
        arguments = list(arguments)
        arguments[5] |= CREATE_SUSPENDED | CREATE_NO_WINDOW
        self.process = self.native.CreateProcess(*arguments)
        process, thread, _, _ = self.process
        self.job.assign(process)
        self.job.resume(thread)
        return self.process

    def CloseHandle(self, handle):
        result = self.native.CloseHandle(handle)
        if self.process is not None and handle == self.process[1]:
            self.thread_closed = True
        return result

    def abort(self, owner):
        # Assignment may have failed before this child entered the job. Stop
        # the exact suspended process independently; an empty job is not proof.
        if self.process is not None and not self.process_closed:
            process, thread, _, _ = self.process
            try:
                self.native.TerminateProcess(process, 1)
            except OSError:
                if self.native.WaitForSingleObject(process, 0) != 0:
                    raise
            if self.native.WaitForSingleObject(process, 5000) != 0:
                raise TimeoutError('The suspended local worker could not be stopped.')
            self.job.record_leader_exit(process)
            if not self.thread_closed:
                self.native.CloseHandle(thread)
                self.thread_closed = True
            retained = getattr(owner, '_handle', None)
            if retained is not None and int(retained) == process:
                retained.Close()
            else:
                self.native.CloseHandle(process)
            self.process_closed = True
            owner._child_created = False
        # Leader handles have been released; wait for every assigned descendant
        # before recording staging or its device reservation can be released.
        self.job.close()
        owner._child_created = False
        owner._carr_job = None
        return True


def _execute_owned(owner, source, arguments, keywords):
    if not isinstance(source, FunctionType) or source.__globals__.get('_winapi') is None:
        raise OSError('The native Windows Python process launcher is unavailable.')
    native = source.__globals__['_winapi']
    job = _Job()
    proxy = _OwnedWinAPI(native, job)
    owner._carr_job = job
    try:
        namespace = dict(source.__globals__, _winapi=proxy)
        launch = FunctionType(source.__code__, namespace, source.__name__,
                              source.__defaults__, source.__closure__)
        launch.__kwdefaults__ = source.__kwdefaults__
        return launch(owner, *arguments, **keywords)
    except BaseException as failure:
        try:
            proxy.abort(owner)
        except Exception as cleanup:
            from .cleanup import pending_cleanup
            raise pending_cleanup(lambda: proxy.abort(owner),
                message='Windows audio launch failed; owned cleanup is still pending.') from failure
        raise


class WindowsJobProcess(_BASE_POPEN):
    def _execute_child(self, *arguments, **keywords):
        return _execute_owned(self, _BASE_POPEN._execute_child, arguments, keywords)
