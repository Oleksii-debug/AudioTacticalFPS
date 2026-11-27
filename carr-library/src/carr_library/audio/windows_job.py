"""Windows process-family ownership for the canonical audio subprocess runner.

This reuses the reviewed native-launcher acceptance helper's JOB_LIST creation
boundary. The job owns descendants from process creation and is never inherited.
Only explicitly listed standard-stream handles reach the child. No PID search,
shell command or machine-wide process termination is used.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time

DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
BOOL = ctypes.c_int32


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', DWORD),
                ('MinimumWorkingSetSize', SIZE_T), ('MaximumWorkingSetSize', SIZE_T),
                ('ActiveProcessLimit', DWORD), ('Affinity', SIZE_T),
                ('PriorityClass', DWORD), ('SchedulingClass', DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', ctypes.c_uint64 * 6),
                ('ProcessMemoryLimit', SIZE_T), ('JobMemoryLimit', SIZE_T),
                ('PeakProcessMemoryUsed', SIZE_T), ('PeakJobMemoryUsed', SIZE_T)]


class _Accounting(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                ('ThisPeriodTotalUserTime', ctypes.c_int64),
                ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                ('TotalPageFaultCount', DWORD), ('TotalProcesses', DWORD),
                ('ActiveProcesses', DWORD), ('TotalTerminatedProcesses', DWORD)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', DWORD), ('lpReserved', ctypes.c_wchar_p),
                ('lpDesktop', ctypes.c_wchar_p), ('lpTitle', ctypes.c_wchar_p),
                ('dwX', DWORD), ('dwY', DWORD), ('dwXSize', DWORD), ('dwYSize', DWORD),
                ('dwXCountChars', DWORD), ('dwYCountChars', DWORD),
                ('dwFillAttribute', DWORD), ('dwFlags', DWORD), ('wShowWindow', WORD),
                ('cbReserved2', WORD), ('lpReserved2', ctypes.c_void_p),
                ('hStdInput', HANDLE), ('hStdOutput', HANDLE), ('hStdError', HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _StartupInfo), ('lpAttributeList', ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', HANDLE), ('hThread', HANDLE),
                ('dwProcessId', DWORD), ('dwThreadId', DWORD)]


def _kernel():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
        'SetInformationJobObject': ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], BOOL),
        'QueryInformationJobObject': ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
                                       ctypes.POINTER(DWORD)], BOOL),
        'InitializeProcThreadAttributeList': ([ctypes.c_void_p, DWORD, DWORD, ctypes.POINTER(SIZE_T)], BOOL),
        'UpdateProcThreadAttribute': ([ctypes.c_void_p, DWORD, SIZE_T, ctypes.c_void_p,
                                      SIZE_T, ctypes.c_void_p, ctypes.c_void_p], BOOL),
        'DeleteProcThreadAttributeList': ([ctypes.c_void_p], None),
        'CreateProcessW': ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                           ctypes.c_void_p, BOOL, DWORD, ctypes.c_void_p,
                           ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_ProcessInfo)], BOOL),
        'AssignProcessToJobObject': ([HANDLE, HANDLE], BOOL),
        'ResumeThread': ([HANDLE], DWORD),
        'WaitForSingleObject': ([HANDLE, DWORD], DWORD),
        'GetExitCodeProcess': ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
        'TerminateJobObject': ([HANDLE, DWORD], BOOL),
        'CloseHandle': ([HANDLE], BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class JobProcess:
    """The small Popen interface used by run_process, with a private job owner."""
    def __init__(self, args, *, cwd=None, env=None, stdin_pipe=False):
        if os.name != 'nt':
            raise RuntimeError('Windows process jobs require native Windows.')
        import msvcrt
        self.args, self.returncode = args, None
        self._api, self._process, self._job = _kernel(), _ProcessInfo(), None
        self.stdin = self.stdout = self.stderr = None
        self._cleanup_lock = threading.Lock()
        attributes, initialized, descriptors = None, False, []
        cleanup_deferred = False
        try:
            self._job = self._api.CreateJobObjectW(None, None)
            if not self._job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not self._api.SetInformationJobObject(self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            size = SIZE_T()
            self._api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not 0 < size.value <= 1024 * 1024:
                raise RuntimeError('Windows did not provide a bounded process attribute list.')
            attributes = ctypes.create_string_buffer(size.value)
            if not self._api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            initialized = True
            jobs = (HANDLE * 1)(self._job)
            if not self._api.UpdateProcThreadAttribute(attributes, 0, 0x0002000D,
                    jobs, ctypes.sizeof(jobs), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if stdin_pipe:
                input_fd, input_write = os.pipe()
                descriptors.extend((input_fd, input_write))
            else:
                input_fd = os.open(os.devnull, os.O_RDONLY)
                input_write = None
                descriptors.append(input_fd)
            stdout_read, stdout_write = os.pipe()
            descriptors.extend((stdout_read, stdout_write))
            stderr_read, stderr_write = os.pipe()
            descriptors.extend((stderr_read, stderr_write))
            child_fds = (input_fd, stdout_write, stderr_write)
            for descriptor in child_fds:
                os.set_inheritable(descriptor, True)
            handles = (HANDLE * 3)(*(msvcrt.get_osfhandle(fd) for fd in child_fds))
            if not self._api.UpdateProcThreadAttribute(attributes, 0, 0x00020002,
                    handles, ctypes.sizeof(handles), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = handles[0]
            startup.StartupInfo.hStdOutput = handles[1]
            startup.StartupInfo.hStdError = handles[2]
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
            environment = None
            if env is not None:
                if any(not isinstance(k, str) or not k or '=' in k or '\x00' in k
                       or not isinstance(v, str) or '\x00' in v for k, v in env.items()):
                    raise ValueError('Invalid Windows subprocess environment.')
                environment = ctypes.create_unicode_buffer(
                    '\x00'.join(k + '=' + v for k, v in sorted(env.items(), key=lambda pair: pair[0].upper()))
                    + '\x00\x00')
            # NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | UNICODE_ENVIRONMENT.
            if not self._api.CreateProcessW(args[0], command, None, None, True,
                    0x08080400, environment, None if cwd is None else str(cwd),
                    ctypes.byref(startup), ctypes.byref(self._process)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.pid = self._process.dwProcessId
            if input_write is not None:
                self.stdin = os.fdopen(input_write, 'wb')
                descriptors.remove(input_write)
            self.stdout = os.fdopen(stdout_read, 'rb')
            descriptors.remove(stdout_read)
            self.stderr = os.fdopen(stderr_read, 'rb')
            descriptors.remove(stderr_read)
        except BaseException as original:
            def close_streams():
                for pipe in (self.stdin, self.stdout, self.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
            try:
                self.close()
            except Exception:
                cleanup_deferred = True
                from .cleanup import pending_cleanup
                raise pending_cleanup(self.close,
                    message='Audio startup failed; owned process cleanup is still pending.',
                    callbacks=(close_streams,)) from original
            close_streams()
            raise
        finally:
            if initialized:
                self._api.DeleteProcThreadAttributeList(attributes)
            for descriptor in descriptors:
                os.close(descriptor)
            if (not cleanup_deferred and self._process.hThread
                    and self._api.CloseHandle(self._process.hThread)):
                self._process.hThread = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        outcome = self._api.WaitForSingleObject(self._process.hProcess, 0)
        if outcome == 0x102:
            return None
        if outcome != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = DWORD()
        if not self._api.GetExitCodeProcess(self._process.hProcess, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.returncode = code.value
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is not None:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(0xFFFFFFFE, int(timeout * 1000)))
        outcome = self._api.WaitForSingleObject(self._process.hProcess, milliseconds)
        if outcome == 0x102:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if outcome != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def terminate(self):
        if self._job and not self._api.TerminateJobObject(self._job, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    kill = terminate

    def _active_processes(self):
        accounting = _Accounting()
        if not self._api.QueryInformationJobObject(self._job, 1,
                ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.ActiveProcesses

    def close(self):
        """Confirm the whole owned job is empty before dropping its last handle.

        A failed native call keeps every unconfirmed handle for a later retry.
        The runner exposes that retained owner instead of declaring silence.
        """
        if not self._cleanup_lock.acquire(blocking=False):
            raise RuntimeError('Audio process cleanup is already in progress.')
        deadline = time.monotonic() + 5
        try:
            if self._job and self._active_processes():
                if not self._api.TerminateJobObject(self._job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            if self._process.hProcess:
                timeout = max(0, min(5000, int((deadline - time.monotonic()) * 1000)))
                outcome = self._api.WaitForSingleObject(self._process.hProcess, timeout)
                if outcome == 0x102:
                    raise subprocess.TimeoutExpired(self.args, 5)
                if outcome != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
                code = DWORD()
                if not self._api.GetExitCodeProcess(self._process.hProcess, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.returncode = code.value
                if not self._api.CloseHandle(self._process.hProcess):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._process.hProcess = None
            if self._process.hThread:
                if not self._api.CloseHandle(self._process.hThread):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._process.hThread = None
            if self._job:
                while self._active_processes():
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(self.args, 5)
                    time.sleep(min(.02, max(0, deadline - time.monotonic())))
                if not self._api.CloseHandle(self._job):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._job = None
            return True
        finally:
            self._cleanup_lock.release()


class LifetimeJob:
    def __init__(self):
        from types import SimpleNamespace
        self.api = SimpleNamespace(kernel=_kernel(),
            error=lambda: ctypes.WinError(ctypes.get_last_error()))
        self.handle = self.api.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise self.api.error()
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
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
        """Retain an unconfirmed Job handle so the caller can retry cleanup."""
        if self.handle is None:
            return True
        self.stop_and_wait()
        if not self.api.kernel.CloseHandle(self.handle):
            raise self.api.error()
        self.handle = None
        return True

