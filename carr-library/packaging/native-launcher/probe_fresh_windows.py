"""Actual first-launch installation and GUI boot/close acceptance on Windows.

Use a fresh private bundle. This probe never repairs or deletes its .venv.
Synthetic portable tests do not establish native, NVDA or signing acceptance.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

TITLE = "Universal Research Library"
HANDLE, DWORD, BOOL = C.c_void_p, C.c_uint32, C.c_int32
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
MAX_MANIFEST = 8 * 1024 * 1024


class _Startup(C.Structure):
    _fields_ = [
        ("cb", DWORD), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p),
        ("title", C.c_wchar_p), ("x", DWORD), ("y", DWORD), ("cx", DWORD),
        ("cy", DWORD), ("chars_x", DWORD), ("chars_y", DWORD), ("fill", DWORD),
        ("flags", DWORD), ("show", C.c_uint16), ("reserved_size", C.c_uint16),
        ("reserved_bytes", C.c_void_p), ("stdin", HANDLE), ("stdout", HANDLE),
        ("stderr", HANDLE),
    ]


class _ProcessInfo(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class _BasicLimits(C.Structure):
    _fields_ = [
        ("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", DWORD),
        ("min_working_set", C.c_size_t), ("max_working_set", C.c_size_t),
        ("process_count", DWORD), ("affinity", C.c_size_t), ("priority", DWORD),
        ("scheduling", DWORD),
    ]


class _Limits(C.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", C.c_uint64 * 6),
        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
        ("peak_process_memory", C.c_size_t), ("peak_job_memory", C.c_size_t),
    ]


class _Accounting(C.Structure):
    _fields_ = [
        ("user", C.c_int64), ("kernel", C.c_int64), ("period_user", C.c_int64),
        ("period_kernel", C.c_int64), ("faults", DWORD), ("total", DWORD),
        ("active", DWORD), ("terminated", DWORD),
    ]


def _win32():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    user = C.WinDLL("user32", use_last_error=True)
    callback = C.WINFUNCTYPE(BOOL, HANDLE, C.c_ssize_t)
    signatures = {
        "CreateJobObjectW": (HANDLE, C.c_void_p, C.c_wchar_p),
        "SetInformationJobObject": (BOOL, HANDLE, C.c_int, C.c_void_p, DWORD),
        "AssignProcessToJobObject": (BOOL, HANDLE, HANDLE),
        "TerminateJobObject": (BOOL, HANDLE, DWORD),
        "QueryInformationJobObject": (BOOL, HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p),
        "CreateProcessW": (BOOL, C.c_wchar_p, C.c_wchar_p, C.c_void_p, C.c_void_p,
                           BOOL, DWORD, C.c_void_p, C.c_wchar_p,
                           C.POINTER(_Startup), C.POINTER(_ProcessInfo)),
        "ResumeThread": (DWORD, HANDLE),
        "TerminateProcess": (BOOL, HANDLE, DWORD),
        "WaitForSingleObject": (DWORD, HANDLE, DWORD),
        "GetExitCodeProcess": (BOOL, HANDLE, C.POINTER(DWORD)),
        "GetProcessId": (DWORD, HANDLE),
        "CloseHandle": (BOOL, HANDLE),
        "OpenProcess": (HANDLE, DWORD, BOOL, DWORD),
        "IsProcessInJob": (BOOL, HANDLE, HANDLE, C.POINTER(BOOL)),
        "QueryFullProcessImageNameW": (BOOL, HANDLE, DWORD, C.c_wchar_p, C.POINTER(DWORD)),
    }
    for name, (result, *arguments) in signatures.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    signatures = {
        "EnumWindows": (BOOL, callback, C.c_ssize_t),
        "IsWindowVisible": (BOOL, HANDLE),
        "IsWindowEnabled": (BOOL, HANDLE),
        "GetWindowTextLengthW": (C.c_int, HANDLE),
        "GetWindowTextW": (C.c_int, HANDLE, C.c_wchar_p, C.c_int),
        "GetClassNameW": (C.c_int, HANDLE, C.c_wchar_p, C.c_int),
        "GetWindowThreadProcessId": (DWORD, HANDLE, C.POINTER(DWORD)),
        "SendMessageTimeoutW": (C.c_ssize_t, HANDLE, DWORD, C.c_size_t, C.c_ssize_t,
                                DWORD, DWORD, C.POINTER(C.c_size_t)),
        "PostMessageW": (BOOL, HANDLE, DWORD, C.c_size_t, C.c_ssize_t),
    }
    for name, (result, *arguments) in signatures.items():
        function = getattr(user, name)
        function.restype, function.argtypes = result, arguments
    return kernel, user, callback


class _OwnedProcess:
    """An exact non-breakaway job; process handles survive PID/window rechecks."""

    def __init__(self, argv, environment, bundle):
        self.kernel, self.user, self.callback = _win32()
        self.job = self.process = self.gui_process = None
        self.assigned = False
        self.images = {path.resolve() for path in (
            bundle / ".venv/Scripts/python.exe", bundle / "runtime/python.exe")}
        thread = None
        try:
            self.job = self.kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise C.WinError(C.get_last_error())
            limits = _Limits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
            if not self.kernel.SetInformationJobObject(self.job, 9, C.byref(limits), C.sizeof(limits)):
                raise C.WinError(C.get_last_error())
            startup, info = _Startup(), _ProcessInfo()
            startup.cb = C.sizeof(startup)
            command = C.create_unicode_buffer(subprocess.list2cmdline([str(item) for item in argv]))
            block = C.create_unicode_buffer("".join(
                key + "=" + environment[key] + "\0" for key in sorted(environment)) + "\0")
            if not self.kernel.CreateProcessW(str(argv[0]), command, None, None, False,
                    0x4 | 0x400, block, str(bundle), C.byref(startup), C.byref(info)):
                raise C.WinError(C.get_last_error())
            self.process, thread = info.process, info.thread
            if not self.kernel.AssignProcessToJobObject(self.job, self.process):
                raise C.WinError(C.get_last_error())
            self.assigned = True
            if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                raise C.WinError(C.get_last_error())
        except BaseException:
            # A failed assignment leaves a suspended process outside our job.
            # Terminate that exact retained handle before closing the job.
            if self.process and not self.assigned:
                self.kernel.TerminateProcess(self.process, 1)
                self.kernel.WaitForSingleObject(self.process, 5000)
            self.close()
            raise
        finally:
            if thread:
                self.kernel.CloseHandle(thread)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def poll(self):
        state = self.kernel.WaitForSingleObject(self.process, 0)
        if state == WAIT_TIMEOUT:
            return None
        if state != WAIT_OBJECT_0:
            raise C.WinError(C.get_last_error())
        code = DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, C.byref(code)):
            raise C.WinError(C.get_last_error())
        return code.value

    def active(self):
        value = _Accounting()
        if not self.kernel.QueryInformationJobObject(self.job, 1, C.byref(value), C.sizeof(value), None):
            raise C.WinError(C.get_last_error())
        return value.active

    def gui_exited(self):
        if self.gui_process is None:
            return True
        state = self.kernel.WaitForSingleObject(self.gui_process, 0)
        if state not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
            raise C.WinError(C.get_last_error())
        return state == WAIT_OBJECT_0

    def _window(self, window, process=None):
        if (not self.user.IsWindowVisible(window) or not self.user.IsWindowEnabled(window)
                or self.user.GetWindowTextLengthW(window) != len(TITLE)):
            return None
        title, kind = C.create_unicode_buffer(len(TITLE) + 1), C.create_unicode_buffer(256)
        self.user.GetWindowTextW(window, title, len(title))
        self.user.GetClassNameW(window, kind, len(kind))
        if title.value != TITLE or not kind.value.startswith("wxWindow"):
            return None
        pid = DWORD()
        if not self.user.GetWindowThreadProcessId(window, C.byref(pid)) or not pid.value:
            return None
        opened, retained = process is None, False
        handle = process or self.kernel.OpenProcess(0x1000 | 0x100000, False, pid.value)
        if not handle:
            return None
        try:
            if self.kernel.GetProcessId(handle) != pid.value:
                return None
            belongs = BOOL()
            if (not self.kernel.IsProcessInJob(handle, self.job, C.byref(belongs))
                    or not belongs.value
                    or self.kernel.WaitForSingleObject(handle, 0) != WAIT_TIMEOUT):
                return None
            path, length = C.create_unicode_buffer(32768), DWORD(32768)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, path, C.byref(length)):
                return None
            if Path(path.value).resolve() not in self.images:
                return None
            result = C.c_size_t()
            # WM_NULL checks the real message loop. Its result is usually zero;
            # the API return, not that result, establishes bounded delivery.
            if not self.user.SendMessageTimeoutW(window, 0, 0, 0, 0x23, 1000, C.byref(result)):
                return None
            if opened:
                if self.gui_process is not None:
                    raise RuntimeError("A GUI process has already been accepted.")
                self.gui_process, retained = handle, True
            return {"handle": int(window), "pid": pid.value, "title": title.value, "class": kind.value}
        finally:
            if opened and not retained:
                self.kernel.CloseHandle(handle)

    def window(self):
        found, errors = [], []
        scan_deadline = time.monotonic() + 2

        def inspect(window, _):
            try:
                if time.monotonic() >= scan_deadline:
                    return False
                record = self._window(window)
                if record:
                    found.append(record)
                    return False
                return True
            except BaseException as error:
                errors.append(error)
                return False

        # EnumWindows returns false when our callback intentionally stops.
        result = self.user.EnumWindows(self.callback(inspect), 0)
        if errors:
            raise errors[0]
        if found:
            return found[0]
        if not result and time.monotonic() < scan_deadline:
            raise C.WinError(C.get_last_error())
        return None

    def close_window(self, record):
        if self.gui_process is None or self._window(record["handle"], self.gui_process) != record:
            raise RuntimeError("The accepted native window changed before normal close.")
        # WM_NULL above can wait: recheck ownership after it, immediately before
        # posting. Win32 does not atomically bind HWND messages to process handles.
        pid, belongs = DWORD(), BOOL()
        if (not self.user.GetWindowThreadProcessId(record["handle"], C.byref(pid))
                or pid.value != record["pid"]
                or pid.value != self.kernel.GetProcessId(self.gui_process)
                or self.kernel.WaitForSingleObject(self.gui_process, 0) != WAIT_TIMEOUT
                or not self.kernel.IsProcessInJob(self.gui_process, self.job, C.byref(belongs))
                or not belongs.value):
            raise RuntimeError("The accepted native window changed before normal close.")
        if not self.user.PostMessageW(record["handle"], 0x10, 0, 0):  # WM_CLOSE
            raise C.WinError(C.get_last_error())

    def close(self):
        try:
            if self.job:
                self.kernel.TerminateJobObject(self.job, 1)
                deadline = time.monotonic() + 5
                while self.active() and time.monotonic() < deadline:
                    time.sleep(0.05)
        finally:
            # Last-handle close is the independent kill-on-close fallback.
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
            for name in ("gui_process", "process"):
                handle = getattr(self, name)
                if handle:
                    self.kernel.WaitForSingleObject(handle, 5000)
                    self.kernel.CloseHandle(handle)
                    setattr(self, name, None)


def _plain(path, *, directory=False):
    for item in (path, *path.parents):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Linked or reparse-point paths are not accepted.")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("A parent path is not a directory.")
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError("Expected a plain directory." if directory else "Expected a regular file.")
    return info


def _fresh(bundle):
    if os.path.lexists(bundle / ".venv"):
        raise ValueError("Fresh acceptance requires an absent .venv; existing environments are preserved.")


def _sealed(bundle, expected=None):
    _plain(bundle, directory=True)
    path = bundle / "bundle-manifest.json"
    size = _plain(path).st_size
    if not 0 < size <= MAX_MANIFEST:
        raise ValueError("The sealed bundle manifest exceeds its size limit.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) != size:
        raise ValueError("The sealed bundle manifest changed while being read.")
    if expected is not None and raw != expected:
        raise ValueError("The sealed bundle manifest changed during acceptance.")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("bundled_python") is not True
            or value.get("bundled_native_launcher") is not True):
        raise ValueError("Fresh acceptance requires sealed bundled Python and the native EXE.")
    if not re.fullmatch("[0-9a-f]{40}", str(value.get("source_commit", ""))):
        raise ValueError("The bundle must record its exact source commit.")
    # Never import Python supplied inside the bundle being tested.
    trusted = Path(__file__).resolve().parents[1] / "install_windows.py"
    spec = importlib.util.spec_from_file_location("trusted_fresh_probe_installer", trusted)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.verify(bundle, manifest_bytes=raw)
    with path.open("rb") as stream:
        if stream.read(len(raw) + 1) != raw:
            raise ValueError("The sealed bundle manifest changed while verifying files.")
    return raw, value


def _environment(root, inherited):
    environment = {key.upper(): value for key, value in inherited.items()
        if not key.upper().startswith(("PYTHON", "PIP_", "CARR_", "WIN_PD_OVERRIDE_",
                                       "HF_", "HUGGINGFACE_", "TRANSFORMERS_"))
        and key.upper() not in {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "HOME", "TESSDATA_PREFIX"}}
    paths = {
        "APPDATA": "Roaming", "LOCALAPPDATA": "Local", "USERPROFILE": "Profile",
        "PROGRAMDATA": "Common", "ALLUSERSPROFILE": "Common",
        "TEMP": "Temp", "TMP": "Temp", "TMPDIR": "Temp",
        "XDG_DATA_HOME": "Local", "XDG_CONFIG_HOME": "Roaming", "XDG_CACHE_HOME": "Cache",
        "HF_HOME": "Cache/HuggingFace", "HF_HUB_CACHE": "Cache/HuggingFace/hub",
        "HUGGINGFACE_HUB_CACHE": "Cache/HuggingFace/hub",
        "TRANSFORMERS_CACHE": "Cache/Transformers", "TORCH_HOME": "Cache/Torch",
    }
    # Pinned platformdirs uses Known Folder APIs, which ignore APPDATA alone.
    # These supported overrides make the actual application data path private.
    known = {
        "LOCAL_APPDATA": "Local", "APPDATA": "Roaming", "COMMON_APPDATA": "Common",
        "PERSONAL": "Profile/Documents", "DOWNLOADS": "Profile/Downloads",
        "MYPICTURES": "Profile/Pictures", "MYVIDEO": "Profile/Videos",
        "MYMUSIC": "Profile/Music", "DESKTOPDIRECTORY": "Profile/Desktop",
        "PROGRAMS": "Profile/Programs", "COMMON_PROGRAMS": "Common/Programs",
    }
    paths.update({"WIN_PD_OVERRIDE_" + key: value for key, value in known.items()})
    for name, relative in paths.items():
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path)
    return environment


def _wait(process, deadline, label):
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            if code:
                raise RuntimeError(f"{label} failed with exit status {code}.")
            if not process.active() and process.gui_exited():
                return
        time.sleep(0.1)
    raise TimeoutError(f"{label} exceeded its owned-process deadline.")


def _exercise(bundle, environment, factory, *, deadline):
    _fresh(bundle)
    if time.monotonic() >= deadline:
        raise TimeoutError("The first-launch acceptance deadline expired before launch.")
    launcher = bundle / "ResearchLibrary.exe"
    database = Path(environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"]) / TITLE / "library.sqlite"
    with factory([launcher], environment, bundle) as process:
        window = None
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                raise RuntimeError(f"The launcher exited with {code} before installation and native desktop were observed.")
            try:
                _plain(bundle / ".venv/.installation-complete")
                _plain(bundle / ".venv/Scripts/python.exe")
                _plain(database)
            except FileNotFoundError:
                pass
            else:
                window = process.window()
                if window:
                    break
            time.sleep(0.1)
        if window is None:
            raise TimeoutError("Fresh installation did not reach its actual native desktop before the deadline.")
        process.close_window(window)
        _wait(process, min(deadline, time.monotonic() + 30), "Normal native window close")
        first_exit, remaining = process.poll(), process.active()
    checks = []
    for option in ("--launcher-check", "--check-installation", "--self-test"):
        if time.monotonic() >= deadline:
            raise TimeoutError("The first-launch acceptance deadline expired before diagnostics.")
        with factory([launcher, option], environment, bundle) as process:
            _wait(process, deadline, "Native launcher " + option)
        checks.append(option)
    return {"launcher_exit_status": first_exit, "owned_processes_remaining": remaining,
            "window": window, "checks": checks}


def inspect_fresh(bundle, *, timeout=600):
    if os.name != "nt":
        raise RuntimeError("Fresh installation acceptance requires native Windows.")
    if not 30 <= timeout <= 1800:
        raise ValueError("Choose a native acceptance timeout from 30 to 1800 seconds.")
    # Check the supplied path before resolution so a linked root is not hidden.
    bundle = Path(os.path.abspath(bundle))
    _plain(bundle, directory=True)
    bundle = bundle.resolve(strict=True)
    _fresh(bundle)
    before, manifest = _sealed(bundle)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="carr-first-launch-") as temporary:
        environment = _environment(Path(temporary).resolve(strict=True), os.environ)
        result = _exercise(bundle, environment, _OwnedProcess, deadline=started + timeout)
        _sealed(bundle, expected=before)
    return {
        "schema": 1, "status": "passed", "source_commit": manifest["source_commit"],
        "launcher_sha256": manifest["files"]["ResearchLibrary.exe"],
        "manifest_sha256": hashlib.sha256(before).hexdigest(),
        "elapsed_seconds": round(time.monotonic() - started, 3), **result,
        "sealed_bundle_unchanged": True, "actual_fresh_installation_verified": True,
        "actual_installed_app_self_test_verified": True,
        "first_launch_gui_boot_close_verified": True,
        "gui_verified": False, "nvda_verified": False, "authenticode_verified": False,
        "manual_acceptance": {"nvda": "NOT_RUN", "signing": "NOT_RUN"},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    bundle, report = Path(os.path.abspath(args.bundle_root)), Path(os.path.abspath(args.report))
    _plain(report.parent, directory=True)
    if os.path.lexists(report) or report.is_relative_to(bundle):
        raise ValueError("Choose a new report path outside the bundle.")
    try:
        result = inspect_fresh(bundle, timeout=args.timeout)
    except (Exception, KeyboardInterrupt) as error:
        result = {"schema": 1, "status": "failed", "error_type": type(error).__name__,
                  "message": str(error)[:2000], "actual_fresh_installation_verified": False,
                  "actual_installed_app_self_test_verified": False,
                  "first_launch_gui_boot_close_verified": False,
                  "gui_verified": False, "nvda_verified": False, "authenticode_verified": False,
                  "manual_acceptance": {"nvda": "NOT_RUN", "signing": "NOT_RUN"}}
    with report.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
