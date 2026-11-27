"""Bound a Windows acceptance process and its descendants to one private job.

The child is assigned at process creation, before it can start another process.
Closing the job terminates any remaining descendants. This helper deliberately
does not capture the launcher's Python child's output.

The JOB_LIST process attribute requires Windows 10 / Windows Server 2016 or later.
See https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
and https://learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import time


DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
HANDLE = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
BOOL = ctypes.c_int32


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", SIZE_T),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", DWORD), ("dwY", DWORD),
        ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
        ("wShowWindow", WORD), ("cbReserved2", WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


def _kernel():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], HANDLE),
        "SetInformationJobObject": ([HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD], BOOL),
        "InitializeProcThreadAttributeList": ([ctypes.c_void_p, DWORD, DWORD, ctypes.POINTER(SIZE_T)], BOOL),
        "UpdateProcThreadAttribute": ([ctypes.c_void_p, DWORD, SIZE_T, ctypes.c_void_p,
                                       SIZE_T, ctypes.c_void_p, ctypes.c_void_p], BOOL),
        "DeleteProcThreadAttributeList": ([ctypes.c_void_p], None),
        "CreateProcessW": ([ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                            ctypes.c_void_p, BOOL, DWORD, ctypes.c_void_p,
                            ctypes.c_wchar_p, ctypes.c_void_p,
                            ctypes.POINTER(_ProcessInfo)], BOOL),
        "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
        "GetExitCodeProcess": ([HANDLE, ctypes.POINTER(DWORD)], BOOL),
        "CloseHandle": ([HANDLE], BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = arguments
        function.restype = result
    return api


def run_in_job(executable, arguments, *, cwd, timeout, on_started=None):
    """Run one explicit executable, returning its exit code or a bounded timeout.

    An optional trusted synchronous observer receives (pid, job_handle) after
    creation. It must bound its own native calls and must not close or duplicate
    the borrowed job handle. Observer errors use the same owned-job cleanup as
    a process failure. Its elapsed time is included in the wait allowance.
    """
    if os.name != "nt":
        raise RuntimeError("Windows process jobs require native Windows.")
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0 < timeout <= 3600):
        raise ValueError("A process timeout between zero and one hour is required.")
    if on_started is not None and not callable(on_started):
        raise ValueError("A process observer must be callable.")
    executable, cwd = Path(executable), Path(cwd)
    if not executable.is_absolute() or not cwd.is_absolute():
        raise ValueError("Process and working-directory paths must be absolute.")
    argv = [str(executable), *map(str, arguments)]
    command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    api = _kernel()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    initialized = False
    attributes = None
    process = _ProcessInfo()
    try:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        required = SIZE_T()
        api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(required))
        if not 0 < required.value <= 1024 * 1024:
            raise RuntimeError("Windows did not provide a bounded process attribute list.")
        attributes = ctypes.create_string_buffer(required.value)
        if not api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(required)):
            raise ctypes.WinError(ctypes.get_last_error())
        initialized = True
        jobs = (HANDLE * 1)(job)
        if not api.UpdateProcThreadAttribute(attributes, 0, 0x0002000D, jobs,
                                             ctypes.sizeof(jobs), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
        # CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT. The job handle is not
        # inherited; the attribute assigns membership at creation instead.
        if not api.CreateProcessW(str(executable), command, None, None, False,
                                  0x08080000, None, str(cwd), ctypes.byref(startup),
                                  ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        remaining = timeout
        if on_started is not None:
            observed_at = time.monotonic()
            on_started(process.dwProcessId, job)
            remaining -= time.monotonic() - observed_at
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
        outcome = api.WaitForSingleObject(process.hProcess, max(1, int(remaining * 1000)))
        if outcome == 0x102:
            raise subprocess.TimeoutExpired(argv, timeout)
        if outcome != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        exit_code = DWORD()
        if not api.GetExitCodeProcess(process.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return subprocess.CompletedProcess(argv, exit_code.value)
    finally:
        if initialized:
            api.DeleteProcThreadAttributeList(attributes)
        # This is the sole job handle; close it on success, timeout and errors.
        # Windows terminates remaining descendants when this handle closes.
        api.CloseHandle(job)
        if process.hProcess:
            api.WaitForSingleObject(process.hProcess, 5000)
            api.CloseHandle(process.hProcess)
        if process.hThread:
            api.CloseHandle(process.hThread)
