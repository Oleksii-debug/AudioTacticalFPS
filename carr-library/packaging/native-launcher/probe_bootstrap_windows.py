"""Exercise the actual first EXE launch in a dedicated clean Windows account.

This trusted CI helper does not ship inside the bundle. It never changes the
approved launcher binary or its pinned C sources. Existing application data or
an existing .venv causes refusal before a process is started.

Requires windows_job.run_in_job(..., on_started=callback). That synchronous
callback receives (process_id, job_handle) while the helper retains the job
handle. Callback failure must unwind through the helper's owned-job cleanup.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time

from probe_installed_windows import _verify_bundle
from windows_job import run_in_job


TITLE = "Resonance"
DATA_DIRECTORY = "Universal Research Library"
LAUNCHER_ERROR_TITLE = "Universal Research Library"
MAX_TIMEOUT = 900.0
MAX_WINDOWS = 4096
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
UINT = ctypes.c_uint32
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t


def _plain(path, *, directory):
    """Reject links and nonlocal/reparse parents before examining a target."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An absolute local path without traversal is required.")
    if os.name == "nt" and (path.drive.startswith("\\") or not path.drive):
        raise ValueError("A local Windows drive path is required.")
    for item in reversed((path, *path.parents)):
        value = item.lstat()
        if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked or reparse paths are not accepted by this probe.")
        expected = stat.S_ISDIR if item != path or directory else stat.S_ISREG
        if not expected(value.st_mode):
            raise ValueError("A bootstrap path has an unexpected filesystem type.")
    return path


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _fresh_roots(bundle, local_appdata):
    bundle = _plain(Path(bundle), directory=True)
    local_appdata = _plain(Path(local_appdata), directory=True)
    data = local_appdata / DATA_DIRECTORY
    for target in (bundle / ".venv", data):
        if os.path.lexists(target):
            raise ValueError("Fresh launch requires an absent .venv and absent default application data. "
                             "Use a dedicated clean Windows CI account; existing data is never removed.")
    if bundle == data or data in bundle.parents or bundle in data.parents:
        raise ValueError("The program and fresh application data must be disjoint.")
    return data


def _launcher_state(bundle, expected, *, timeout=15.0):
    result = subprocess.run(
        [str(bundle / "ResearchLibrary.exe"), "--launcher-check"],
        cwd=bundle, capture_output=True, timeout=timeout, check=False)
    if len(result.stdout) > 4096 or len(result.stderr) > 4096:
        raise ValueError("The pinned launcher returned an oversized state response.")
    try:
        value = json.loads(result.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ValueError("The actual launcher did not return its state JSON.") from error
    code = {"needs-install": 10, "ready": 0}[expected]
    if result.returncode != code or value != {"schema": 1, "state": expected}:
        raise ValueError(f"Expected actual launcher state {expected!r}; got "
                         f"exit {result.returncode}, {value!r}.")
    return value


def _ready_marker(bundle):
    marker = _plain(bundle / ".venv" / ".installation-complete", directory=False)
    info = marker.stat()
    if info.st_size != 0 or info.st_nlink != 1:
        raise ValueError("The installation completion marker is not a fresh plain marker.")


def _local_appdata():
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = (
        HANDLE, ctypes.c_int32, HANDLE, DWORD, ctypes.c_wchar_p)
    shell.SHGetFolderPathW.restype = ctypes.c_int32
    value = ctypes.create_unicode_buffer(260)
    # The application's platformdirs implementation uses the current local
    # appdata known folder. Changing only the environment is not equivalent.
    result = shell.SHGetFolderPathW(None, 0x001C, None, 0, value)
    if result != 0 or not value.value:
        raise OSError("Windows did not provide the current local appdata folder.")
    root = _plain(Path(value.value), directory=True)
    declared = os.environ.get("LOCALAPPDATA")
    if not declared or not _same_path(root, declared):
        raise ValueError("Installer LOCALAPPDATA and the actual Windows application-data folder differ.")
    return root


@dataclass(frozen=True)
class MainWindow:
    hwnd: int
    pid: int
    title: str
    window_class: str
    image: str = ""
    venv_redirector_pid: int | None = None


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD), ("cntUsage", DWORD), ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", DWORD),
        ("cntThreads", DWORD), ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_int32), ("dwFlags", DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _Windows:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.enum_proc = ctypes.WINFUNCTYPE(BOOL, HANDLE, LPARAM)
        declarations = (
            (self.kernel, "OpenProcess", (DWORD, BOOL, DWORD), HANDLE),
            (self.kernel, "CloseHandle", (HANDLE,), BOOL),
            (self.kernel, "IsProcessInJob", (HANDLE, HANDLE, ctypes.POINTER(BOOL)), BOOL),
            (self.kernel, "QueryFullProcessImageNameW",
             (HANDLE, DWORD, ctypes.c_wchar_p, ctypes.POINTER(DWORD)), BOOL),
            (self.kernel, "WaitForSingleObject", (HANDLE, DWORD), DWORD),
            (self.kernel, "GetExitCodeProcess", (HANDLE, ctypes.POINTER(DWORD)), BOOL),
            (self.kernel, "CreateToolhelp32Snapshot", (DWORD, DWORD), HANDLE),
            (self.kernel, "Process32FirstW", (HANDLE, ctypes.POINTER(_ProcessEntry)), BOOL),
            (self.kernel, "Process32NextW", (HANDLE, ctypes.POINTER(_ProcessEntry)), BOOL),
            (self.user, "EnumWindows", (self.enum_proc, LPARAM), BOOL),
            (self.user, "IsWindowVisible", (HANDLE,), BOOL),
            (self.user, "GetWindowThreadProcessId", (HANDLE, ctypes.POINTER(DWORD)), DWORD),
            (self.user, "GetWindowTextLengthW", (HANDLE,), ctypes.c_int32),
            (self.user, "GetWindowTextW", (HANDLE, ctypes.c_wchar_p, ctypes.c_int32), ctypes.c_int32),
            (self.user, "GetClassNameW", (HANDLE, ctypes.c_wchar_p, ctypes.c_int32), ctypes.c_int32),
            (self.user, "GetWindow", (HANDLE, UINT), HANDLE),
            (self.user, "SendMessageTimeoutW",
             (HANDLE, UINT, WPARAM, LPARAM, UINT, UINT, ctypes.POINTER(WPARAM)), LPARAM),
            (self.user, "PostMessageW", (HANDLE, UINT, WPARAM, LPARAM), BOOL),
        )
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def _owned_image(self, process, job):
        owned = BOOL()
        if not self.kernel.IsProcessInJob(process, job, ctypes.byref(owned)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not owned.value:
            return None
        size = DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not self.kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return image.value

    @contextmanager
    def process(self, pid, job, executable):
        handle = self.kernel.OpenProcess(0x00101000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            image = self._owned_image(handle, job)
            if image is None or not _same_path(image, executable):
                raise ValueError("A candidate process is not the expected executable in this exact job.")
            yield handle
        finally:
            self.kernel.CloseHandle(handle)

    def running(self, process):
        result = self.kernel.WaitForSingleObject(process, 0)
        if result == 0x102:
            return True
        if result == 0:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def parent_pid(self, pid):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            examined = 0
            while present:
                examined += 1
                if examined > 65536:
                    raise RuntimeError("The process-parent enumeration budget was exceeded.")
                if entry.th32ProcessID == pid:
                    parent = entry.th32ParentProcessID
                    if not parent or parent == pid:
                        break
                    return parent
                present = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            raise RuntimeError("The owned application has no verifiable live venv redirector parent.")
        finally:
            self.kernel.CloseHandle(snapshot)

    def _application_parent(self, image, pid, bundle):
        installed = bundle / ".venv" / "Scripts" / "python.exe"
        if _same_path(image, installed):
            return None
        if _same_path(image, bundle / "runtime" / "python.exe"):
            # Windows venv python.exe redirects to the base runtime. Accept
            # that image only with its exact installed venv parent in our job.
            # https://docs.python.org/3.11/library/venv.html
            return self.parent_pid(pid)
        raise ValueError("The owned window is not an installed Python application.")

    def exit_code(self, process):
        value = DWORD()
        if not self.kernel.GetExitCodeProcess(process, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    def _window_text(self, hwnd):
        length = self.user.GetWindowTextLengthW(hwnd)
        if not 0 < length < 1024:
            return ""
        text = ctypes.create_unicode_buffer(length + 1)
        if not self.user.GetWindowTextW(hwnd, text, len(text)):
            return ""
        return text.value

    def _window_class(self, hwnd):
        value = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, value, len(value)):
            return ""
        return value.value

    def _window_pid(self, hwnd):
        pid = DWORD()
        if not self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return 0
        return pid.value

    def find_main(self, job, bundle):
        matches, failures = [], []
        seen = 0
        installed = bundle / ".venv" / "Scripts" / "python.exe"
        launcher = bundle / "ResearchLibrary.exe"

        def inspect(hwnd, _unused):
            nonlocal seen
            try:
                seen += 1
                if seen > MAX_WINDOWS:
                    raise RuntimeError("The native window enumeration budget was exceeded.")
                title = self._window_text(hwnd)
                if not self.user.IsWindowVisible(hwnd) or title not in (TITLE, LAUNCHER_ERROR_TITLE):
                    return True
                pid = self._window_pid(hwnd)
                if not pid:
                    return True
                handle = self.kernel.OpenProcess(0x00101000, False, pid)
                if not handle:
                    # Other applications can have protected processes. They
                    # must never become targets merely because a title matches.
                    return True
                try:
                    image = self._owned_image(handle, job)
                    if image is None:
                        return True
                    kind = self._window_class(hwnd)
                    if kind == "#32770" and any(_same_path(image, candidate) for candidate in
                                                (launcher, installed, bundle / "runtime" / "python.exe")):
                        raise RuntimeError("The owned launcher/application displayed an error dialog. "
                                           "Inspect the retained installer diagnostics.")
                    if (title == TITLE and any(_same_path(image, candidate) for candidate in
                            (installed, bundle / "runtime" / "python.exe"))
                            and kind and kind != "#32770" and not self.user.GetWindow(hwnd, 4)):
                        parent = self._application_parent(image, pid, bundle)
                        if parent is not None:
                            with self.process(parent, job, installed):
                                pass
                        matches.append(MainWindow(int(hwnd), pid, TITLE, kind, image, parent))
                finally:
                    self.kernel.CloseHandle(handle)
            except BaseException as error:
                failures.append(error)
                return False
            return True

        callback = self.enum_proc(inspect)
        ctypes.set_last_error(0)
        success = self.user.EnumWindows(callback, 0)
        if failures:
            raise failures[0]
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())
        if len(matches) > 1:
            raise RuntimeError("More than one owned installed main window matched the probe.")
        return matches[0] if matches else None

    @contextmanager
    def close_main(self, window, job, bundle):
        """Validate again immediately before posting close; retain process identity."""
        executable = window.image or str(bundle / ".venv" / "Scripts" / "python.exe")
        with self.process(window.pid, job, executable) as process:
            parent = self._application_parent(executable, window.pid, bundle)
            if parent != window.venv_redirector_pid:
                raise RuntimeError("The installed application's venv parent changed before close.")
            if parent is not None:
                with self.process(parent, job, bundle / ".venv" / "Scripts" / "python.exe"):
                    pass
            if (self._window_pid(window.hwnd) != window.pid
                    or self._window_text(window.hwnd) != TITLE
                    or self._window_class(window.hwnd) != window.window_class
                    or not self.user.IsWindowVisible(window.hwnd)
                    or self.user.GetWindow(window.hwnd, 4)):
                raise RuntimeError("The owned main window changed before the close request.")
            result = WPARAM()
            # Nonzero timeout; no NOTIMEOUTIFNOTHUNG flag. WM_NULL proves that
            # this actual top-level window responds, without changing focus.
            ctypes.set_last_error(0)
            if not self.user.SendMessageTimeoutW(
                    window.hwnd, 0, 0, 0, 0x0001 | 0x0002 | 0x0020, 1000, ctypes.byref(result)):
                raise RuntimeError("The owned installed main window did not respond.")
            if self._window_pid(window.hwnd) != window.pid:
                raise RuntimeError("The main window owner changed before close.")
            if not self.user.PostMessageW(window.hwnd, 0x0010, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            yield process


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("The fresh first-launch deadline was exceeded.")
    return value


def _observe(api, bundle, pid, job, deadline):
    with api.process(pid, job, bundle / "ResearchLibrary.exe") as launcher:
        while True:
            _remaining(deadline)
            if not api.running(launcher):
                raise RuntimeError("The actual launcher exited before its installed main window appeared; "
                                   f"exit {api.exit_code(launcher)}.")
            window = api.find_main(job, bundle)
            if window is not None:
                # A created .venv is not proof of installation. Both the real
                # completion marker and the launcher's exact ready state matter.
                _ready_marker(bundle)
                state = _launcher_state(bundle, "ready", timeout=min(15.0, _remaining(deadline)))
                with api.close_main(window, job, bundle) as application:
                    close_deadline = min(deadline, time.monotonic() + 30.0)
                    while api.running(application) or api.running(launcher):
                        _remaining(close_deadline)
                        time.sleep(0.05)
                    app_exit = api.exit_code(application)
                    launcher_exit = api.exit_code(launcher)
                    if app_exit != 0 or launcher_exit != 0:
                        raise RuntimeError("The owned application did not close successfully: "
                                           f"application={app_exit}, launcher={launcher_exit}.")
                return {
                    "main_window": {"pid": window.pid, "title": window.title,
                                    "class": window.window_class, "image": window.image,
                                    "venv_redirector_pid": window.venv_redirector_pid},
                    "ready_state": state,
                    "installed_application_exit_code": app_exit,
                    "launcher_exit_code": launcher_exit,
                    "graceful_close": True,
                }
            time.sleep(min(0.1, _remaining(deadline)))


def inspect_bootstrap(bundle, *, timeout=MAX_TIMEOUT):
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Actual fresh bootstrap acceptance requires 64-bit Windows.")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 30.0 <= timeout <= MAX_TIMEOUT):
        raise ValueError("Bootstrap timeout must be between 30 and 900 seconds.")
    bundle = Path(bundle).absolute()
    before = _verify_bundle(bundle)
    local_appdata = _local_appdata()
    data = _fresh_roots(bundle, local_appdata)
    initial_state = _launcher_state(bundle, "needs-install")
    # Recheck after the harmless state process, immediately before launch.
    _fresh_roots(bundle, local_appdata)
    api = _Windows()
    started = time.monotonic()
    deadline = started + timeout
    evidence = {}

    def observe(pid, job):
        evidence.update(_observe(api, bundle, pid, job, deadline))

    result = run_in_job(
        bundle / "ResearchLibrary.exe", [], cwd=bundle,
        timeout=timeout, on_started=observe)
    if result.returncode != 0 or not evidence:
        raise RuntimeError("The actual first EXE launch did not finish with verified main-window evidence.")
    _ready_marker(bundle)
    final_state = _launcher_state(bundle, "ready")
    _plain(data, directory=True)
    database = _plain(data / "library.sqlite", directory=False)
    with database.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\0":
            raise ValueError("The fresh application did not create its expected SQLite library.")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ValueError("The newly initialized library failed SQLite verification.")
    finally:
        connection.close()
    after = _verify_bundle(bundle)
    if before != after:
        raise ValueError("The sealed bundle changed during the actual bootstrap.")
    return {
        "schema": 1, "status": "PASS",
        "bundle_manifest_sha256": before[0], "source_commit": before[1],
        "initial_state": initial_state, "final_state": final_state,
        "clean_default_application_data_before_launch": True,
        "default_application_data": str(data),
        "fresh_installation_via_actual_exe": True,
        "actual_owned_main_window_observed": True,
        "owned_job_cleanup": True, "sealed_bundle_unchanged": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "maximum_launch_seconds": timeout,
        "installer_child_stdout_captured": False,
        "nvda_acceptance": "NOT_RUN",
        "audio_hardware_acceptance": "NOT_RUN",
        "authenticode_acceptance": "NOT_RUN",
        **evidence,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=MAX_TIMEOUT)
    arguments = parser.parse_args(argv)
    try:
        report = inspect_bootstrap(arguments.bundle_root, timeout=arguments.timeout)
    except Exception as error:
        report = {
            "schema": 1, "status": "FAIL",
            "error_type": type(error).__name__,
            "error": "".join(c if ord(c) >= 32 and ord(c) != 127 else " "
                             for c in str(error))[:4000],
            "nvda_acceptance": "NOT_RUN", "audio_hardware_acceptance": "NOT_RUN",
            "authenticode_acceptance": "NOT_RUN",
        }
        outcome = 1
    else:
        outcome = 0
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())

