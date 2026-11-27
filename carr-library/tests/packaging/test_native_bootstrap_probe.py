"""Bootstrap guard/cleanup regressions with explicit synthetic OS observations.

These tests do not execute a PE file and do not count as native Windows or NVDA
acceptance. The separate fresh bootstrap probe is the actual Windows gate.
"""
from contextlib import contextmanager
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "packaging" / "native-launcher"


def load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.syspath_prepend(str(NATIVE))
    return load(NATIVE / "probe_bootstrap_windows.py", "bootstrap_probe_test", monkeypatch)


@pytest.fixture
def roots(tmp_path):
    bundle = tmp_path / "Program with spaces"
    appdata = tmp_path / "Local application data"
    bundle.mkdir()
    appdata.mkdir()
    return bundle, appdata


@pytest.mark.parametrize("existing", ("environment-directory", "environment-file", "data-directory", "data-file"))
def test_existing_installation_or_user_data_refuses_without_mutation(probe, roots, existing):
    bundle, appdata = roots
    target = bundle / ".venv" if existing.startswith("environment") else appdata / probe.DATA_DIRECTORY
    if existing.endswith("directory"):
        target.mkdir()
        sentinel = target / "keep.txt"
    else:
        sentinel = target
    sentinel.write_bytes(b"existing user data remains exact")
    with pytest.raises(ValueError, match="existing data is never removed"):
        probe._fresh_roots(bundle, appdata)
    assert sentinel.read_bytes() == b"existing user data remains exact"
    assert list(bundle.iterdir()) == ([target] if existing.startswith("environment") else [])


def test_missing_default_root_is_only_observed_not_precreated(probe, roots):
    bundle, appdata = roots
    data = probe._fresh_roots(bundle, appdata)
    assert data == appdata / probe.DATA_DIRECTORY
    assert not data.exists() and not (bundle / ".venv").exists()


@pytest.mark.parametrize("expected,code,state", (
    ("ready", 10, "ready"),
    ("ready", 0, "incomplete"),
    ("needs-install", 0, "needs-install"),
))
def test_actual_state_requires_both_exact_json_and_exit_code(probe, roots, monkeypatch, expected, code, state):
    bundle, _appdata = roots
    def run(argv, **options):
        assert argv == [str(bundle / "ResearchLibrary.exe"), "--launcher-check"]
        assert options["capture_output"] is True and options["timeout"] <= 15
        return subprocess.CompletedProcess(argv, code, json.dumps({"schema": 1, "state": state}).encode(), b"")
    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(ValueError, match="Expected actual launcher state"):
        probe._launcher_state(bundle, expected)


class ObservedOS:
    """A controlled observer, not a simulated claim of Windows process acceptance."""
    def __init__(self, probe, *, visible=True, exited=False, app_exit=0):
        self.probe, self.visible, self.exited = probe, visible, exited
        self.app_exit = app_exit
        self.closed = []

    @contextmanager
    def process(self, pid, job, executable):
        assert pid == 42 and job == 100
        assert executable.name == "ResearchLibrary.exe"
        yield "launcher"

    def running(self, process):
        return not self.exited

    def exit_code(self, process):
        return self.app_exit if process == "application" else 0

    def find_main(self, job, bundle):
        assert job == 100
        if self.visible:
            return self.probe.MainWindow(51, 71, self.probe.TITLE, "Synthetic wx frame")
        return None

    @contextmanager
    def close_main(self, window, job, bundle):
        assert job == 100 and bundle.name == "Program with spaces"
        self.closed.append(window)
        self.exited = True
        yield "application"


def install_marker(bundle):
    folder = bundle / ".venv"
    folder.mkdir()
    (folder / ".installation-complete").touch()


def test_created_environment_and_visible_window_cannot_substitute_for_ready_marker(probe, roots, monkeypatch):
    bundle, _appdata = roots
    (bundle / ".venv").mkdir()
    api = ObservedOS(probe)
    monkeypatch.setattr(probe, "_launcher_state", lambda *args, **kwargs: pytest.fail("must reject marker first"))
    with pytest.raises(FileNotFoundError):
        probe._observe(api, bundle, 42, 100, probe.time.monotonic() + 10)
    assert api.closed == []


def test_visible_window_and_marker_cannot_substitute_for_actual_ready_state(probe, roots, monkeypatch):
    bundle, _appdata = roots
    install_marker(bundle)
    api = ObservedOS(probe)
    def not_ready(*args, **kwargs):
        raise ValueError("actual launcher still incomplete")
    monkeypatch.setattr(probe, "_launcher_state", not_ready)
    with pytest.raises(ValueError, match="still incomplete"):
        probe._observe(api, bundle, 42, 100, probe.time.monotonic() + 10)
    assert api.closed == []


@pytest.mark.parametrize("exit_code", (0, 7))
def test_only_ready_application_can_be_closed_and_nonzero_exit_is_failure(probe, roots, monkeypatch, exit_code):
    bundle, _appdata = roots
    install_marker(bundle)
    api = ObservedOS(probe, app_exit=exit_code)
    monkeypatch.setattr(probe, "_launcher_state",
                        lambda *args, **kwargs: {"schema": 1, "state": "ready"})
    if exit_code:
        with pytest.raises(RuntimeError, match="did not close successfully"):
            probe._observe(api, bundle, 42, 100, probe.time.monotonic() + 10)
    else:
        evidence = probe._observe(api, bundle, 42, 100, probe.time.monotonic() + 10)
        assert evidence["graceful_close"] and evidence["installed_application_exit_code"] == 0
        assert evidence["main_window"]["pid"] == 71
    assert len(api.closed) == 1


def test_early_launcher_exit_never_claims_window_acceptance(probe, roots):
    bundle, _appdata = roots
    api = ObservedOS(probe, exited=True)
    with pytest.raises(RuntimeError, match="exited before"):
        probe._observe(api, bundle, 42, 100, probe.time.monotonic() + 10)
    assert api.closed == []


def test_no_window_observation_has_finite_deadline(probe, roots, monkeypatch):
    bundle, _appdata = roots
    api = ObservedOS(probe, visible=False)
    clock = SimpleNamespace(now=0.0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(probe, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    with pytest.raises(TimeoutError, match="deadline"):
        probe._observe(api, bundle, 42, 100, 0.25)
    assert 0.25 <= clock.now < 0.26 and api.closed == []


@pytest.mark.parametrize("image", (None, "different-program.exe"))
def test_process_outside_exact_job_or_executable_is_rejected_and_handle_closed(probe, monkeypatch, tmp_path, image):
    api = object.__new__(probe._Windows)
    closed = []
    api.kernel = SimpleNamespace(
        OpenProcess=lambda *args: 101,
        CloseHandle=lambda handle: closed.append(handle))
    api._owned_image = lambda process, job: image
    with pytest.raises(ValueError, match="not the expected executable"):
        with api.process(99, 100, tmp_path / "python.exe"):
            pytest.fail("unowned executable must not be accepted")
    assert closed == [101]


def test_nonjob_process_image_is_not_considered_an_owned_target(probe):
    api = object.__new__(probe._Windows)
    def is_in_job(process, job, output):
        assert process == 101 and job == 100
        ctypes.cast(output, ctypes.POINTER(probe.BOOL)).contents.value = 0
        return 1
    api.kernel = SimpleNamespace(
        IsProcessInJob=is_in_job,
        QueryFullProcessImageNameW=lambda *args: pytest.fail("must not inspect an unowned target image"))
    assert api._owned_image(101, 100) is None


def test_windows_venv_redirector_requires_exact_bundled_runtime_and_parent(probe, roots):
    bundle, _appdata = roots
    api = object.__new__(probe._Windows)
    api.parent_pid = lambda pid: 42 if pid == 71 else pytest.fail("unexpected process")
    assert api._application_parent(str(bundle / ".venv/Scripts/python.exe"), 71, bundle) is None
    assert api._application_parent(str(bundle / "runtime/python.exe"), 71, bundle) == 42
    with pytest.raises(ValueError, match="not an installed Python"):
        api._application_parent(str(bundle.parent / "unowned/python.exe"), 71, bundle)


def test_runtime_window_with_unowned_venv_parent_never_receives_close(probe, roots):
    bundle, _appdata = roots
    api = object.__new__(probe._Windows)
    api._application_parent = lambda *args: 42
    opened = []
    @contextmanager
    def process(pid, job, executable):
        opened.append(pid)
        if pid == 42:
            raise ValueError("not the expected executable in this exact job")
        yield 101
    api.process = process
    api.user = SimpleNamespace(PostMessageW=lambda *args: pytest.fail("must not close unowned parent window"))
    window = probe.MainWindow(51, 71, probe.TITLE, "Synthetic frame",
                              str(bundle / "runtime/python.exe"), 42)
    with pytest.raises(ValueError, match="exact job"):
        with api.close_main(window, 100, bundle):
            pytest.fail("unverified venv relationship must not be accepted")
    assert opened == [71, 42]


@pytest.fixture
def job_case(tmp_path, monkeypatch):
    job = load(NATIVE / "windows_job.py", "bootstrap_job_test", monkeypatch)
    fixtures = load(ROOT / "tests/packaging/test_native_launcher_bundle.py",
                    "bootstrap_existing_job_fixtures", monkeypatch)

    class Kernel(fixtures.FakeKernel):
        def __init__(self):
            super().__init__(job)
            self.timeouts = []

        def CreateProcessW(self, *args):
            result = super().CreateProcessW(*args)
            ctypes.cast(args[-1], ctypes.POINTER(job._ProcessInfo)).contents.dwProcessId = 42
            return result

        def WaitForSingleObject(self, handle, timeout):
            self.timeouts.append(timeout)
            return super().WaitForSingleObject(handle, timeout)

    fake = Kernel()
    monkeypatch.setattr(job, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(job, "_kernel", lambda: fake)
    return job, fake, tmp_path


def test_started_observer_sees_stable_exact_job_before_wait_and_cleanup(job_case, monkeypatch):
    job, fake, root = job_case
    clock = iter((0.0, 0.75))
    monkeypatch.setattr(job, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    observations = []
    def observed(pid, handle):
        assert fake.closed == [] and fake.waits == 0
        observations.append((pid, handle))
    result = job.run_in_job(root / "program.exe", [], cwd=root, timeout=1, on_started=observed)
    assert result.returncode == 17 and observations == [(42, 100)]
    assert fake.timeouts == [250, 5000]
    assert fake.closed == [100, 101, 102]


def test_observer_failure_closes_only_owned_job_and_process_handles(job_case):
    job, fake, root = job_case
    def fail(pid, handle):
        raise RuntimeError("bootstrap verification failed")
    with pytest.raises(RuntimeError, match="bootstrap verification failed"):
        job.run_in_job(root / "program.exe", [], cwd=root, timeout=1, on_started=fail)
    assert fake.closed == [100, 101, 102] and fake.deleted == 1
    assert fake.timeouts == [5000]


def test_observer_time_consumes_total_process_allowance(job_case, monkeypatch):
    job, fake, root = job_case
    clock = iter((0.0, 1.01))
    monkeypatch.setattr(job, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with pytest.raises(subprocess.TimeoutExpired):
        job.run_in_job(root / "program.exe", [], cwd=root, timeout=1, on_started=lambda *args: None)
    assert fake.closed == [100, 101, 102] and fake.timeouts == [5000]


def test_invalid_observer_refuses_before_any_process_or_job_is_started(job_case):
    job, fake, root = job_case
    with pytest.raises(ValueError, match="observer must be callable"):
        job.run_in_job(root / "program.exe", [], cwd=root, timeout=1, on_started=True)
    assert fake.closed == [] and fake.waits == 0

