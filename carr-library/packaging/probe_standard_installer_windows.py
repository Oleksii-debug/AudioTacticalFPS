"""Disposable-CI-only real Inno install, side-by-side repair and uninstall probe.

Run on native Windows with the reviewed compiler-built setup and a complete real
offline application payload. It refuses an existing standard installation or
Start-menu entry. This is neither an owner-workstation tool nor an NVDA/signing
test. The current owned process launcher retains descendants through completion
and cleans up the same job on timeouts.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
REGISTRY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Resonance.StandardInstall_is1"


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JobAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ("user_time", "kernel_time", "period_user_time", "period_kernel_time")] + [
                (name, ctypes.c_uint32) for name in
                ("page_faults", "total_processes", "active_processes", "terminated_processes")]


def _wait_owned_descendants(process, deadline):
    # Inno's uninstaller clone outlives its initial process. Let that same
    # owned job finish naturally before releasing its KILL_ON_JOB_CLOSE handle.
    job = getattr(process, "_carr_job", None)
    if job is None:
        if os.name == "nt":
            raise RuntimeError("Native setup did not receive an owned process job.")
        return
    query = job.api.kernel.QueryInformationJobObject
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                      ctypes.c_uint32, ctypes.c_void_p]
    query.restype = ctypes.c_int
    while True:
        accounting = _JobAccounting()
        if not query(job.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise job.api.error()
        if accounting.active_processes == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Native setup descendants exceeded the operation deadline.")
        time.sleep(.02)


def _run(arguments, log, *, environment=None, timeout=900, require_success=True):
    from carr_library.audio.engines import _start_process, _stop
    deadline = time.monotonic() + timeout
    with log.open("xb") as output:
        process = _start_process(arguments, stdout=output, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=environment, shell=False)
        try:
            code = process.wait(timeout=timeout)
            _wait_owned_descendants(process, deadline)
        finally:
            _stop(process)
    if code and require_success:
        raise RuntimeError(f"Native setup command failed ({code}); see {log.name}")
    return code


def _verified_seal(bundle, source_commit):
    delivery = _module(HERE / "create_delivery.py", "carr_standard_probe_seal")
    raw = delivery._read_manifest(bundle, lambda: False)
    manifest = delivery._manifest(raw, source_commit)
    delivery._require_transferable_audio_inventory(manifest)
    delivery._trusted_verify(bundle, manifest_bytes=raw)
    if delivery._read_manifest(bundle, lambda: False) != raw:
        raise AssertionError("The installed manifest changed during verification.")
    return raw, {"manifest_sha256": hashlib.sha256(raw).hexdigest()}


def _registration():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0]
                    for name in ("InstallLocation", "UninstallString", "ModifyPath")}
    except FileNotFoundError:
        return None


def _shortcut():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
        programs = winreg.QueryValueEx(key, "Programs")[0]
    return Path(programs) / "Resonance.lnk"


def _shortcut_target(shortcut, work, phase):
    environment = dict(os.environ, CARR_INSTALLER_PROBE_SHORTCUT=str(shortcut))
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
              "(New-Object -ComObject WScript.Shell).CreateShortcut("
              "$env:CARR_INSTALLER_PROBE_SHORTCUT).TargetPath")
    log = work / (phase + "-shortcut.txt")
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], log,
         environment=environment, timeout=30)
    return Path(log.read_text(encoding="utf-8-sig").strip()).resolve()


def inspect(setup, setup_sha256, source_commit, manifest_sha256, work, *,
            failed_setup, failed_setup_sha256, disposable_ci=False):
    if os.name != "nt" or not disposable_ci or os.environ.get("CI", "").lower() != "true":
        raise RuntimeError("Use a disposable native Windows CI account and --disposable-ci.")
    from carr_library.audio.engines import _start_process, _stop
    delivery = _module(HERE / "create_delivery.py", "carr_installer_probe_delivery")
    installed = _module(HERE / "native-launcher/probe_installed_windows.py", "carr_installer_probe_native")
    setup, failed_setup, work = map(delivery._absolute, (setup, failed_setup, work))
    for value, size in ((setup_sha256, 64), (failed_setup_sha256, 64),
                        (manifest_sha256, 64), (source_commit, 40)):
        if not isinstance(value, str) or len(value) != size or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Exact lowercase source and installer/manifest digests are required.")
    for executable, expected in ((setup, setup_sha256), (failed_setup, failed_setup_sha256)):
        with delivery._file(executable) as stream:
            if delivery._stream(stream, lambda: False)[0] != expected:
                raise ValueError("Setup differs from its independently recorded digest.")
    if _registration() is not None or _shortcut().exists():
        raise RuntimeError("A standard installation or shortcut already exists; this probe will not change it.")
    if os.path.lexists(work):
        raise FileExistsError("Use a new probe work directory.")
    delivery._directories(work.parent)
    work.mkdir()
    root = work / "program"
    shortcut = _shortcut()
    sentinel = work / "research" / "state" / "pending-import.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b'{"state":"paused","user_text":"preserve all edits"}\n')
    sentinel_bytes = sentinel.read_bytes()
    launched = None
    checked, completed_generations = [], []
    failed = None
    failed_state = b"preserve failed environment\n"
    try:
        for phase in ("install", "repair"):
            before = set((root / "versions").iterdir()) if (root / "versions").exists() else set()
            _run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                  "/DIR=" + str(root), "/LOG=" + str(work / (phase + "-setup.log"))],
                 work / (phase + "-process.log"))
            after = set((root / "versions").iterdir())
            added = after - before
            if len(added) != 1 or not before <= after:
                raise AssertionError("Each install/repair must add exactly one fresh generation.")
            payload = added.pop()
            raw, identity = _verified_seal(payload, source_commit)
            if identity["manifest_sha256"] != manifest_sha256:
                raise AssertionError("The installed generation differs from the pinned manifest.")
            application = installed.inspect_installed(payload)
            if (application.get("status") != "passed"
                    or application.get("actual_installed_app_self_test_verified") is not True
                    or _verified_seal(payload, source_commit)[0] != raw):
                raise AssertionError("The actual installed application or its sealed payload did not verify.")
            completed_generations.append(payload)
            entry = _registration()
            if entry is None or Path(entry["InstallLocation"]).resolve() != root.resolve():
                raise AssertionError("Windows did not register the expected per-user installation.")
            if _shortcut_target(shortcut, work, phase) != (payload / "ResearchLibrary.exe").resolve():
                raise AssertionError("The single Start-menu entry does not select the completed generation.")
            cached = payload / "maintenance-setup.exe"
            if (hashlib.sha256(cached.read_bytes()).hexdigest() != setup_sha256
                    or str(cached).casefold() not in entry["ModifyPath"].casefold()):
                raise AssertionError("Windows Modify must reuse the exact offline installer.")
            if phase == "install":
                original, original_raw = payload, raw
                ready = work / "old-process-ready.txt"
                # Keep the real installed entrypoint lease alive across repair.
                # The child resolves its own installed sys.prefix and package.
                held_application = (
                    "from pathlib import Path; import sys,time\n"
                    "from carr_library.installation_lease import application_lease\n"
                    "with application_lease():\n"
                    "    Path(sys.argv[1]).write_text('ready')\n"
                    "    time.sleep(3600)\n")
                launched = _start_process([str(payload / ".venv/Scripts/python.exe"), "-I", "-B",
                    "-c", held_application, str(ready)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 20
                while not ready.exists() and launched.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not ready.exists() or launched.poll() is not None:
                    raise AssertionError("The previous installed process did not start.")
                uninstallers = tuple(root.glob("unins*.exe"))
                if len(uninstallers) != 1:
                    raise AssertionError("Expected the actual registered uninstaller.")
                prior_entry, prior_shortcut = _registration(), shortcut.read_bytes()
                blocked_code = _run([str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                    "/NORESTART", "/LOG=" + str(work / "blocked-uninstall-setup.log")],
                    work / "blocked-uninstall-process.log", require_success=False)
                if (blocked_code == 0 or launched.poll() is not None
                        or _registration() != prior_entry or shortcut.read_bytes() != prior_shortcut
                        or _verified_seal(original, source_commit)[0] != original_raw
                        or not (original / ".venv/.installation-complete").is_file()
                        or sentinel.read_bytes() != sentinel_bytes):
                    raise AssertionError("Uninstall did not preserve the active application and registration.")
                checked.append("active-application-blocks-uninstall")
                (root / "unowned-notes.txt").write_bytes(b"do not recursively erase program parents")
                (original / "unowned-recording.wav").write_bytes(b"preserve unknown user file")
                previous_entry = _registration()
                previous_shortcut = shortcut.read_bytes()
                before_failure = set((root / "versions").iterdir())
                code = _run([str(failed_setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                             "/DIR=" + str(root), "/LOG=" + str(work / "failed-bootstrap-setup.log")],
                            work / "failed-bootstrap-process.log", require_success=False)
                failed_generations = set((root / "versions").iterdir()) - before_failure
                if code == 0 or len(failed_generations) != 1:
                    raise AssertionError("The failed prerequisite must return failure with one unactivated generation.")
                failed = failed_generations.pop()
                if ((failed / "bootstrap-negative-reached.txt").read_bytes() != b"23\n"
                        or (failed / ".venv/failed-install-state.txt").read_bytes() != failed_state
                        or (failed / ".venv/.installation-complete").exists()):
                    raise AssertionError("The actual nonzero bootstrap fixture was not exercised safely.")
                if (_registration() != previous_entry or shortcut.read_bytes() != previous_shortcut
                        or _verified_seal(original, source_commit)[0] != original_raw
                        or launched.poll() is not None):
                    raise AssertionError("Failed bootstrap changed the previous registration, shortcut, payload or process.")
                if hashlib.sha256(cached.read_bytes()).hexdigest() != setup_sha256:
                    raise AssertionError("Failed bootstrap replaced the existing Modify installer.")
                checked.append("failed-bootstrap-preserves-previous")
            else:
                if launched.poll() is not None:
                    raise AssertionError("Repair closed or broke the previous running process.")
                if _verified_seal(original, source_commit)[0] != original_raw:
                    raise AssertionError("Repair changed the previous sealed generation.")
            if sentinel.read_bytes() != sentinel_bytes:
                raise AssertionError("Install/repair changed the pending user work.")
            checked.append(phase)
        if (failed / ".venv/failed-install-state.txt").read_bytes() != failed_state:
            raise AssertionError("Repair changed the retained failed environment.")
        _stop(launched)
        launched = None
        uninstallers = tuple(root.glob("unins*.exe"))
        if len(uninstallers) != 1:
            raise AssertionError("Expected one standard append-log uninstaller.")
        _run([str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
              "/LOG=" + str(work / "uninstall-setup.log")], work / "uninstall-process.log")
        if _registration() is not None or shortcut.exists():
            raise AssertionError("Uninstall left its registered program or Start-menu entry.")
        if len(completed_generations) != 2 or any((path / ".venv").exists() for path in completed_generations):
            raise AssertionError("Uninstall left an owned completed application environment.")
        if (root / "unowned-notes.txt").read_bytes() != b"do not recursively erase program parents":
            raise AssertionError("Uninstall erased an unknown file in the program parent.")
        if (original / "unowned-recording.wav").read_bytes() != b"preserve unknown user file":
            raise AssertionError("Uninstall erased an unknown file in a generation.")
        if sentinel.read_bytes() != sentinel_bytes:
            raise AssertionError("Uninstall changed the pending user work.")
        if ((failed / ".venv/failed-install-state.txt").read_bytes() != failed_state
                or (failed / ".venv/.installation-complete").exists()):
            raise AssertionError("Uninstall changed the preserved uncompleted environment.")
        checked.extend(("uninstall", "failed-environment-preserved"))
        return {"schema": 1, "status": "passed", "source_commit": source_commit,
                "installer_sha256": setup_sha256, "manifest_sha256": manifest_sha256,
                "failed_installer_sha256": failed_setup_sha256,
                "checks": checked, "old_process_survived_repair": True,
                "active_application_blocks_uninstall": True,
                "failed_bootstrap_preserves_previous_registration": True,
                "failed_environment_preserved": True,
                "research_and_unknown_files_preserved": True, "one_start_menu_entry": True,
                "native_install_repair_uninstall": True, "actual_installed_app_self_test": True,
                "authenticode_verified": False, "nvda_verified": False}
    finally:
        if launched is not None:
            _stop(launched)
        # On failure preserve logs/staging for diagnosis; never substitute a
        # forced recursive deletion for the standard uninstaller's contract.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--setup-sha256", required=True)
    parser.add_argument("--failed-setup", type=Path, required=True,
                        help="Explicit CI-only installer with a sealed bootstrap that exits 23")
    parser.add_argument("--failed-setup-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--disposable-ci", action="store_true")
    args = parser.parse_args(argv)
    result = inspect(args.setup, args.setup_sha256, args.source_commit, args.manifest_sha256,
                     args.work, failed_setup=args.failed_setup,
                     failed_setup_sha256=args.failed_setup_sha256, disposable_ci=args.disposable_ci)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
