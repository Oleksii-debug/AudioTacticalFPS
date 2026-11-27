"""Portable failure/ownership contracts only; no native GUI is claimed here."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


HERE = Path(__file__).resolve().parents[2] / "packaging/native-launcher"
spec = importlib.util.spec_from_file_location("fresh_launcher_probe", HERE / "probe_fresh_windows.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize("state", ["file", "incomplete", "complete"])
def test_existing_environment_is_preserved(tmp_path, state):
    environment = tmp_path / ".venv"
    if state == "file":
        environment.write_bytes(b"not an installation")
        retained = environment
    else:
        environment.mkdir()
        retained = environment / (".installation-complete" if state == "complete" else "keep.txt")
        retained.write_bytes(b"preserve this environment")
    before = retained.read_bytes()
    with pytest.raises(ValueError, match="absent .venv"):
        probe._fresh(tmp_path)
    assert retained.read_bytes() == before


def test_wrong_platform_never_starts_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "os", SimpleNamespace(name="posix"))
    with pytest.raises(RuntimeError, match="native Windows"):
        probe.inspect_fresh(tmp_path)
    assert not tuple(tmp_path.iterdir())


def test_real_platformdirs_configuration_and_temporary_paths_are_isolated(tmp_path, monkeypatch):
    inherited = {
        "APPDATA": "old-roaming", "localappdata": "old-local", "TMPDIR": "old-temp",
        "PYTHONPATH": "old-python", "CARR_PROGRAM_DIR": "old-program",
        "WIN_PD_OVERRIDE_LOCAL_APPDATA": "old-known-folder", "PIP_INDEX_URL": "old-index",
        "PATH": "system-executables",
    }
    environment = probe._environment(tmp_path, inherited)
    assert inherited["TMPDIR"] == "old-temp"
    assert not any(key.startswith(("PYTHON", "PIP_", "CARR_")) for key in environment)
    for name in ("TEMP", "TMP", "TMPDIR"):
        assert Path(environment[name]) == tmp_path / "Temp"
    assert environment["PATH"] == "system-executables"
    for key, value in environment.items():
        if key.startswith("WIN_PD_OVERRIDE_"):
            assert Path(value).is_relative_to(tmp_path)
            monkeypatch.setenv(key, value)
    from platformdirs.windows import Windows
    selected = Windows(probe.TITLE, appauthor=False, roaming=False)
    assert Path(selected.user_data_dir) == tmp_path / "Local" / probe.TITLE
    assert Path(selected.user_config_dir) == tmp_path / "Local" / probe.TITLE


@pytest.fixture
def sealed(tmp_path):
    files = {
        "install.lock": b"synthetic",
        # If this payload is ever imported, the test must fail. The probe must
        # use the source checkout's verifier rather than supplied Python code.
        "install_windows.py": b"raise AssertionError('untrusted bundle code executed')",
        "Install.cmd": b"synthetic", "ResearchLibrary.cmd": b"synthetic",
        "CheckInstallation.cmd": b"synthetic", "README.txt": b"synthetic",
        "wheelhouse/synthetic.whl": b"synthetic", "runtime/python.exe": b"synthetic",
        "ResearchLibrary.exe": b"synthetic", "launcher-provenance.json": b"synthetic",
    }
    for name, raw in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {"schema": 1, "source_commit": "a" * 40, "bundled_python": True,
                "bundled_native_launcher": True,
                "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    (tmp_path / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_sealed_verification_uses_trusted_code_and_rejects_changed_bytes(sealed):
    raw, manifest = probe._sealed(sealed)
    assert manifest["source_commit"] == "a" * 40
    assert probe._sealed(sealed, expected=raw)[0] == raw
    (sealed / "ResearchLibrary.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        probe._sealed(sealed, expected=raw)


def test_replaced_manifest_cannot_reapprove_changed_payload(sealed):
    raw, manifest = probe._sealed(sealed)
    (sealed / "ResearchLibrary.exe").write_bytes(b"changed")
    manifest["files"]["ResearchLibrary.exe"] = hashlib.sha256(b"changed").hexdigest()
    (sealed / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        probe._sealed(sealed, expected=raw)


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    """Only a workflow double: never returned as inspect_fresh native evidence."""
    def __init__(self, owner, argv, environment, bundle):
        self.owner, self.argv = owner, argv
        self.option = str(argv[1]) if len(argv) > 1 else None
        self.code = None if self.option is None else owner.codes.get(self.option, 0)
        self.remaining = 1 if self.code is None else 0
        self.closed_normally = False
        if self.option is None:
            (bundle / ".venv/Scripts").mkdir(parents=True)
            (bundle / ".venv/Scripts/python.exe").write_bytes(b"synthetic process marker")
            (bundle / ".venv/.installation-complete").touch()
            if owner.private_data:
                data = Path(environment["WIN_PD_OVERRIDE_LOCAL_APPDATA"]) / probe.TITLE
                data.mkdir()
                (data / "library.sqlite").write_bytes(b"synthetic database marker")

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        self.owner.cleaned.append(self.option)
        if kind is not None:
            self.owner.stopped.append(self.option)

    def poll(self):
        return self.code

    def active(self):
        return self.remaining

    def gui_exited(self):
        return self.code is not None and not self.remaining and not self.owner.gui_linger

    def window(self):
        return ({"handle": 1, "pid": 2, "title": probe.TITLE, "class": "wxWindowNR"}
                if self.owner.show_window else None)

    def close_window(self, record):
        assert record["title"] == probe.TITLE
        self.closed_normally = True
        self.code = 0
        self.remaining = 1 if self.owner.orphan else 0


class Processes:
    def __init__(self, *, show_window=True, orphan=False, gui_linger=False, private_data=True, codes=None):
        self.show_window, self.orphan, self.codes = show_window, orphan, codes or {}
        self.gui_linger, self.private_data = gui_linger, private_data
        self.started, self.cleaned, self.stopped = [], [], []

    def __call__(self, argv, environment, bundle):
        process = Process(self, argv, environment, bundle)
        self.started.append(process)
        return process


@pytest.fixture
def execution(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(probe, "time", clock)
    bundle, profile = tmp_path / "bundle", tmp_path / "profile"
    bundle.mkdir()
    profile.mkdir()
    return bundle, probe._environment(profile, {})


def test_fresh_no_arguments_then_normal_close_then_actual_diagnostic_options(execution):
    bundle, environment = execution
    processes = Processes()
    result = probe._exercise(bundle, environment, processes, deadline=2)
    assert [p.option for p in processes.started] == [
        None, "--launcher-check", "--check-installation", "--self-test"]
    assert processes.started[0].closed_normally
    assert result["launcher_exit_status"] == result["owned_processes_remaining"] == 0
    assert processes.stopped == []


@pytest.mark.parametrize("condition", ["no_gui", "orphan", "gui_linger", "wrong_data_location"])
def test_timeout_cleans_the_owned_tree_and_retains_created_environment(execution, condition):
    bundle, environment = execution
    processes = Processes(show_window=condition != "no_gui", orphan=condition == "orphan",
                          gui_linger=condition == "gui_linger", private_data=condition != "wrong_data_location")
    with pytest.raises(TimeoutError):
        probe._exercise(bundle, environment, processes, deadline=0.3)
    assert processes.stopped == [None]
    assert [p.option for p in processes.started] == [None]
    assert (bundle / ".venv/.installation-complete").is_file()


def test_nonzero_installed_check_cannot_be_reported_as_success(execution):
    bundle, environment = execution
    processes = Processes(codes={"--check-installation": 17})
    with pytest.raises(RuntimeError, match="exit status 17"):
        probe._exercise(bundle, environment, processes, deadline=2)
    assert [p.option for p in processes.started] == [None, "--launcher-check", "--check-installation"]
    assert processes.cleaned[-1] == "--check-installation"


class Kernel:
    def __init__(self, *, deny_assignment=False):
        self.events, self.deny_assignment, self.running = [], deny_assignment, False

    def CreateJobObjectW(self, *_):
        return 10

    def SetInformationJobObject(self, job, kind, data, size):
        assert job == 10 and kind == 9 and data._obj.basic.flags == 0x2000
        return True

    def CreateProcessW(self, executable, command, security, thread_security,
                       inherit, flags, environment, cwd, startup, info):
        assert inherit is False and flags & 0x4 and flags & 0x400
        self.events.append("created-suspended")
        info._obj.process, info._obj.thread = 20, 30
        self.running = True
        return True

    def AssignProcessToJobObject(self, job, process):
        assert (job, process) == (10, 20)
        self.events.append("assign")
        if self.deny_assignment:
            return False
        return True

    def ResumeThread(self, thread):
        assert thread == 30
        self.events.append("resume")
        return 1

    def TerminateProcess(self, process, status):
        assert process == 20
        self.events.append("stop-suspended")
        self.running = False
        return True

    def TerminateJobObject(self, job, status):
        assert job == 10
        self.events.append("stop-job")
        self.running = False
        return True

    def QueryInformationJobObject(self, job, kind, target, size, returned):
        target._obj.active = int(self.running)
        return True

    def WaitForSingleObject(self, handle, timeout):
        return 258 if self.running else 0

    def CloseHandle(self, handle):
        self.events.append(("close", handle))
        return True


@pytest.mark.parametrize("deny", [False, True])
def test_job_ownership_precedes_resume_and_assignment_failure_stops_exact_suspended_handle(tmp_path, monkeypatch, deny):
    kernel = Kernel(deny_assignment=deny)
    monkeypatch.setattr(probe, "_win32", lambda: (kernel, None, None))
    monkeypatch.setattr(probe.C, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(probe.C, "WinError", lambda code: OSError(code, "cannot own the created process"), raising=False)
    if deny:
        with pytest.raises(OSError, match="cannot own"):
            probe._OwnedProcess([tmp_path / "ResearchLibrary.exe"], {}, tmp_path)
        assert "resume" not in kernel.events
        assert "stop-suspended" in kernel.events
    else:
        with probe._OwnedProcess([tmp_path / "ResearchLibrary.exe"], {}, tmp_path):
            assert kernel.events[:3] == ["created-suspended", "assign", "resume"]
    assert ("close", 10) in kernel.events and ("close", 20) in kernel.events
    assert not kernel.running


def test_expired_deadline_never_launches_a_process(execution):
    bundle, environment = execution
    processes = Processes()
    with pytest.raises(TimeoutError, match="before launch"):
        probe._exercise(bundle, environment, processes, deadline=0)
    assert processes.started == []
    assert not (bundle / ".venv").exists()


class WindowKernel:
    """Small Win32 boundary double; never used for native acceptance."""
    def __init__(self, image):
        self.image, self.pid = str(image), 42
        self.belongs, self.alive = True, True
        self.opened, self.closed, self.membership = [], [], []

    def OpenProcess(self, access, inherit, pid):
        assert access == 0x1000 | 0x100000 and inherit is False
        self.opened.append(pid)
        return 30

    def GetProcessId(self, process):
        assert process == 30
        return self.pid

    def IsProcessInJob(self, process, job, result):
        self.membership.append((process, job))
        result._obj.value = self.belongs
        return True

    def WaitForSingleObject(self, process, timeout):
        assert process == 30
        return probe.WAIT_TIMEOUT if self.alive else probe.WAIT_OBJECT_0

    def QueryFullProcessImageNameW(self, process, flags, output, size):
        assert process == 30 and flags == 0
        output.value = self.image
        size._obj.value = len(self.image)
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


class WindowUser:
    def __init__(self):
        self.pid, self.message_success, self.message_result = 42, 1, 0
        self.messages, self.posted = [], []

    def IsWindowVisible(self, window):
        return True

    def IsWindowEnabled(self, window):
        return True

    def GetWindowTextLengthW(self, window):
        return len(probe.TITLE)

    def GetWindowTextW(self, window, output, count):
        output.value = probe.TITLE
        return len(output.value)

    def GetClassNameW(self, window, output, count):
        output.value = "wxWindowNR"
        return len(output.value)

    def GetWindowThreadProcessId(self, window, output):
        output._obj.value = self.pid
        return 7

    def SendMessageTimeoutW(self, window, message, wparam, lparam, flags, timeout, output):
        assert message == wparam == lparam == 0
        assert flags == 0x23 and 0 < timeout <= 1000
        output._obj.value = self.message_result
        self.messages.append(window)
        return self.message_success

    def PostMessageW(self, window, message, wparam, lparam):
        self.posted.append((window, message, wparam, lparam))
        return True


@pytest.fixture
def native_window_boundary(tmp_path):
    expected = tmp_path / "bundle/runtime/python.exe"
    process = object.__new__(probe._OwnedProcess)
    process.kernel, process.user = WindowKernel(expected), WindowUser()
    process.job, process.process, process.gui_process = 10, 20, None
    process.images = {expected.resolve()}
    return process


def test_owned_responsive_window_holds_the_original_process_handle_until_normal_close(native_window_boundary):
    process = native_window_boundary
    window = process._window(123)
    assert window["pid"] == 42 and process.gui_process == 30
    assert process.kernel.opened == [42] and process.kernel.closed == []
    # WM_NULL returned zero; successful API delivery is what matters.
    process.close_window(window)
    assert process.kernel.opened == [42]  # no PID-based reopen before WM_CLOSE
    assert process.kernel.membership == [(30, 10), (30, 10), (30, 10)]
    assert process.user.posted == [(123, 0x10, 0, 0)]
    assert not process.gui_exited()
    process.kernel.alive = False
    assert process.gui_exited()


@pytest.mark.parametrize("failure", ["other_job", "other_image", "already_exited", "message_timeout"])
def test_matching_window_cannot_substitute_for_owned_live_responsive_application(native_window_boundary, failure):
    process = native_window_boundary
    if failure == "other_job":
        process.kernel.belongs = False
    elif failure == "other_image":
        process.kernel.image = str(Path(process.kernel.image).parent / "foreign.exe")
    elif failure == "already_exited":
        process.kernel.alive = False
    else:
        process.user.message_success = 0
        process.user.message_result = 123  # must not turn API failure into success
    assert process._window(123) is None
    assert process.gui_process is None and process.kernel.closed == [30]
    assert process.user.posted == []


@pytest.mark.parametrize("failure", ["changed_pid", "changed_job", "exited"])
def test_normal_close_rechecks_held_process_before_sending_any_close(native_window_boundary, failure):
    process = native_window_boundary
    window = process._window(123)
    if failure == "changed_pid":
        process.user.pid = 77
    elif failure == "changed_job":
        process.kernel.belongs = False
    else:
        process.kernel.alive = False
    with pytest.raises(RuntimeError, match="changed before normal close"):
        process.close_window(window)
    assert process.user.posted == []
    assert process.kernel.opened == [42]
    assert process.kernel.closed == []  # retained until owner cleanup, never reused by PID


def test_missing_or_invalid_source_commit_never_reaches_bundle_code(sealed):
    manifest = sealed / "bundle-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["source_commit"] = "main"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="exact source commit"):
        probe._sealed(sealed)


def test_manifest_budget_is_checked_before_opening_file(sealed, monkeypatch):
    manifest = sealed / "bundle-manifest.json"
    with manifest.open("wb") as stream:
        stream.truncate(probe.MAX_MANIFEST + 1)
    original = Path.open

    def open_file(path, *args, **kwargs):
        if path == manifest:
            pytest.fail("Oversized manifest was opened.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(ValueError, match="size limit"):
        probe._sealed(sealed)


@pytest.mark.parametrize("location", ["existing", "inside_bundle"])
def test_invalid_report_destination_cannot_start_installation(tmp_path, monkeypatch, location):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    report = tmp_path / "result.json" if location == "existing" else bundle / "result.json"
    if location == "existing":
        report.write_bytes(b"retained evidence")
    monkeypatch.setattr(probe, "inspect_fresh", lambda *a, **k: pytest.fail("Installation started."))
    with pytest.raises(ValueError, match="new report path outside"):
        probe.main(["--bundle-root", str(bundle), "--report", str(report)])
    if location == "existing":
        assert report.read_bytes() == b"retained evidence"
    else:
        assert not report.exists()


def test_failed_probe_report_never_asserts_acceptance(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("No native acceptance was observed.")

    monkeypatch.setattr(probe, "inspect_fresh", fail)
    report = tmp_path / "failure.json"
    assert probe.main(["--bundle-root", str(tmp_path / "bundle"), "--report", str(report)]) == 1
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["status"] == "failed" and result["error_type"] == "TimeoutError"
    assert result["actual_fresh_installation_verified"] is False
    assert result["first_launch_gui_boot_close_verified"] is False
    assert result["manual_acceptance"] == {"nvda": "NOT_RUN", "signing": "NOT_RUN"}


@pytest.mark.parametrize("failure", ["changed_pid", "changed_job", "exited"])
def test_close_rechecks_ownership_after_responsiveness_wait(native_window_boundary, failure):
    process = native_window_boundary
    window = process._window(123)
    deliver = process.user.SendMessageTimeoutW

    def change_during_delivery(*args):
        success = deliver(*args)
        if failure == "changed_pid":
            process.user.pid = 77
        elif failure == "changed_job":
            process.kernel.belongs = False
        else:
            process.kernel.alive = False
        return success

    process.user.SendMessageTimeoutW = change_during_delivery
    with pytest.raises(RuntimeError, match="changed before normal close"):
        process.close_window(window)
    assert process.user.posted == []
    assert process.kernel.opened == [42]
    assert process.kernel.closed == []
