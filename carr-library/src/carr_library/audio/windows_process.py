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
_BASE_POPEN = subprocess.Popen


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
        'SetInformationJobObject': ((handle, ctypes.c_int, ctypes.c_void_p, dword), boolean),
        'AssignProcessToJobObject': ((handle, handle), boolean),
        'ResumeThread': ((handle,), dword),
        'TerminateJobObject': ((handle, dword), boolean),
        'CloseHandle': ((handle,), boolean),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return SimpleNamespace(kernel=kernel, error=lambda: ctypes.WinError(ctypes.get_last_error()))


class _Job:
    def __init__(self):
        self.api = _kernel()
        self.handle = self.api.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise self.api.error()
        limits = _ExtendedLimits()
        limits.basic.flags = KILL_ON_JOB_CLOSE
        try:
            if not self.api.kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise self.api.error()
        except BaseException:
            self.close()
            raise

    def assign(self, process):
        if not self.api.kernel.AssignProcessToJobObject(self.handle, process):
            raise self.api.error()

    def resume(self, thread):
        previous = self.api.kernel.ResumeThread(thread)
        if previous == 0xFFFFFFFF:
            raise self.api.error()
        if previous != 1:
            raise OSError('The local worker initial thread did not resume from its owned suspended state.')

    def stop_and_wait(self, process=None):
        """Confirm exact member exit before a caller releases an idle lease.

        This is separate from close(): the existing launcher and crash cleanup
        keep their original semantics. A bounded failure does not certify idle.
        The caller must retain an unclean-owner receipt before releasing its
        lease after any failure or abrupt owner death.
        """
        if self.handle is None:
            raise OSError('Owned Job cleanup has already released its handle.')
        kernel = self.api.kernel
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
        for name, arguments, result in (
                ('QueryInformationJobObject', (handle, ctypes.c_int, ctypes.c_void_p, dword, ctypes.c_void_p), boolean),
                ('OpenProcess', (dword, boolean, dword), handle),
                ('GetProcessId', (handle,), dword),
                ('IsProcessInJob', (handle, handle, ctypes.POINTER(boolean)), boolean),
                ('TerminateProcess', (handle, dword), boolean),
                ('WaitForSingleObject', (handle, dword), dword)):
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result

        class ProcessIds(ctypes.Structure):
            _fields_ = [('assigned', dword), ('count', dword),
                        ('identities', ctypes.c_size_t * 256)]

        handles, owned = {}, []
        stop_errors = {}
        deadline = time.monotonic() + 5

        def members():
            value = ProcessIds()
            if not kernel.QueryInformationJobObject(
                    self.handle, 3, ctypes.byref(value), ctypes.sizeof(value), None):
                raise self.api.error()
            if value.assigned > 256 or value.count > 256 or value.count < value.assigned:
                raise OSError('The owned process tree exceeds its cleanup allowance.')
            return tuple(value.identities[:value.count])

        def status(value, milliseconds=0):
            result = kernel.WaitForSingleObject(value, milliseconds)
            if result not in (0, 258):
                raise self.api.error()
            return result

        try:
            if process is not None:
                identity = kernel.GetProcessId(process)
                if not identity:
                    raise self.api.error()
                # The caller retains this exact already-assigned Popen handle.
                # It is borrowed, and must not be closed by this method.
                handles[identity] = process
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Owned process cleanup exceeded its time allowance.')
                for identity in members():
                    if identity in handles:
                        continue
                    if len(handles) >= 256:
                        raise OSError('The owned process tree exceeds its cleanup allowance.')
                    value = kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, identity)
                    if not value:
                        if ctypes.get_last_error() == 87:
                            continue
                        raise self.api.error()
                    owned.append(value)
                    member = boolean()
                    if not kernel.IsProcessInJob(value, self.handle, ctypes.byref(member)):
                        raise self.api.error()
                    if not member.value:
                        raise OSError('Owned process identity changed during cleanup.')
                    handles[identity] = value
                for value in handles.values():
                    if status(value) == 258 and not kernel.TerminateProcess(value, 1):
                        # A concurrent exit may refuse another stop request
                        # before its process object signals. Only the bounded
                        # exact-handle wait below can acknowledge completion.
                        stop_errors[value] = ctypes.get_last_error()
                for value in handles.values():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or status(value, max(1, int(remaining * 1000))) != 0:
                        detail = (' (WinError ' + str(stop_errors[value]) + ')'
                                  if value in stop_errors else '')
                        raise TimeoutError('Owned process termination remains unconfirmed.' + detail)
                if all(identity in handles and status(handles[identity]) == 0
                       for identity in members()):
                    return
        finally:
            closed = True
            for value in owned:
                closed = bool(kernel.CloseHandle(value)) and closed
            if not closed:
                raise OSError('An owned cleanup process handle could not be released.')

    def close(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if not self.api.kernel.TerminateJobObject(handle, 1):
                raise self.api.error()
        finally:
            # KILL_ON_JOB_CLOSE is also the crash/failure cleanup boundary.
            if not self.api.kernel.CloseHandle(handle):
                raise self.api.error()


class _OwnedWinAPI:
    def __init__(self, native, job):
        self.native, self.job = native, job
        self.process = None
        self.thread_closed = False

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
        try:
            if self.process is not None:
                process, _, _, _ = self.process
                try:
                    self.native.TerminateProcess(process, 1)
                except OSError:
                    if self.native.WaitForSingleObject(process, 0) != 0:
                        raise
                if self.native.WaitForSingleObject(process, 5000) != 0:
                    raise TimeoutError('The suspended local worker could not be stopped.')
        finally:
            owner._child_created = False
            try:
                self.job.close()
            finally:
                if self.process is not None:
                    process, thread, _, _ = self.process
                    try:
                        if not self.thread_closed:
                            self.native.CloseHandle(thread)
                    finally:
                        retained = getattr(owner, '_handle', None)
                        if retained is not None and int(retained) == process:
                            retained.Close()
                        else:
                            self.native.CloseHandle(process)


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
    except BaseException:
        try:
            proxy.abort(owner)
        finally:
            owner._carr_job = None
        raise


class WindowsJobProcess(_BASE_POPEN):
    def _execute_child(self, *arguments, **keywords):
        return _execute_owned(self, _BASE_POPEN._execute_child, arguments, keywords)
