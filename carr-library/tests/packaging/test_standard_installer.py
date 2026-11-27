"""Real sealed-tree preparation checks; native setup acceptance is separate."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = runpy.run_path(str(Path(__file__).with_name("test_delivery_archive.py")))
bundle = BASE["bundle"]
COMMIT = BASE["COMMIT"]
spec = importlib.util.spec_from_file_location("carr_standard_installer_test", ROOT / "packaging/build_standard_installer.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.mark.parametrize('damage', ['none', 'manifest', 'payload', 'setup'])
def test_update_probe_rechecks_separately_retained_bundle_after_builder_copy_removed(
        native_bundle, tmp_path, damage):
    """Exercise input validation only; the setup is explicitly inert bytes."""
    prepared = prepare(native_bundle, tmp_path)
    output = Path(prepared['output'])
    report_path = output / 'installer-build.json'
    report = json.loads(report_path.read_bytes())
    raw_manifest = (native_bundle / 'bundle-manifest.json').read_bytes()
    manifest_sha = sha(raw_manifest)
    setup_name = 'dist/Resonance-Setup-' + COMMIT[:12] + '.exe'
    executable = output / setup_name
    executable.parent.mkdir(exist_ok=True)
    executable.write_bytes(b'INERT TEST SETUP, NEVER EXECUTE')
    setup_sha = sha(executable.read_bytes())
    report.update(status='compiled', installer={'path': setup_name,
        'bytes': executable.stat().st_size, 'sha256': setup_sha})
    report_path.write_text(json.dumps(report), encoding='utf-8')
    shutil.rmtree(output / 'payload')
    if damage == 'manifest':
        (native_bundle / 'bundle-manifest.json').write_bytes(raw_manifest + b'\n')
    elif damage == 'payload':
        (native_bundle / 'ResearchLibrary.exe').write_bytes(b'foreign payload')
    elif damage == 'setup':
        executable.write_bytes(b'foreign setup')
    spec = importlib.util.spec_from_file_location('carr_update_probe_inputs',
        ROOT / 'packaging/probe_standard_update_windows.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    if damage == 'none':
        checked, checked_report, checked_manifest = probe.build_inputs(output, COMMIT,
            setup_sha, manifest_sha, installer._delivery(), bundle=native_bundle)
        assert checked == executable and checked_manifest == raw_manifest
        assert json.loads(checked_report) == report
    else:
        with pytest.raises((AssertionError, ValueError)):
            probe.build_inputs(output, COMMIT, setup_sha, manifest_sha,
                installer._delivery(), bundle=native_bundle)
    assert not (output / 'payload').exists()


@pytest.fixture
def native_bundle(bundle):
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    for name in ("runtime/python.exe", "ResearchLibrary.exe", "native-launcher-build.json"):
        path = bundle / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"INERT SYNTHETIC FILE, NEVER EXECUTE\n")
        manifest["files"][name] = sha(path.read_bytes())
    manifest["bundled_python"] = True
    manifest["bundled_native_launcher"] = True
    (bundle / "bundle-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def prepare(bundle, tmp_path, **kwargs):
    return installer.prepare(bundle, COMMIT, sha((bundle / "bundle-manifest.json").read_bytes()),
                             tmp_path / "installer", **kwargs)


def test_prepare_preserves_exact_seal_and_excludes_user_files(native_bundle, tmp_path):
    for name in (".venv/private.txt", "drafts/unfinished.txt", "private.sqlite3"):
        path = native_bundle / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("user data sentinel", encoding="utf-8")
    before = {p.relative_to(native_bundle).as_posix(): p.read_bytes()
              for p in native_bundle.rglob("*") if p.is_file()}
    report = prepare(native_bundle, tmp_path)
    output = Path(report["output"])
    raw = (native_bundle / "bundle-manifest.json").read_bytes()
    manifest = json.loads(raw)
    payload = output / "payload"
    assert (payload / "bundle-manifest.json").read_bytes() == raw
    assert {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()} == {
        *manifest["files"], "bundle-manifest.json"}
    for name, expected in manifest["files"].items():
        assert sha((payload / name).read_bytes()) == expected
    assert before == {p.relative_to(native_bundle).as_posix(): p.read_bytes()
                      for p in native_bundle.rglob("*") if p.is_file()}
    assert report["status"] == "prepared"
    assert report["native_install_repair_uninstall"] == "NOT_RUN"
    assert report["authenticode_verified"] is False
    assert report["update_or_entitlement_authority"] is False
    assert report["managed_update_protocol"] == "resonance-standard-update/1"
    script = (output / "Resonance.iss").read_text(encoding="utf-8-sig")
    assert "@PAYLOAD" not in script and "@SOURCE" not in script
    assert 'Source: "payload\\ocr-corresponding-source.tar"' in script
    assert "AfterInstall: Bootstrap" not in script
    assert "function PrepareToInstall" in script
    assert "ExtractTemporaryFiles('{tmp}\\resonance-payload\\*')" in script
    assert "Result := 'Offline installation was not activated." in script
    assert "CloseApplications=no" in script and "PrivilegesRequired=lowest" in script
    assert "UninstallLogMode=append" in script
    assert "EnsureInstallationLease;" in script
    assert "SetupMutex=Resonance.StandardInstall,Global\\Resonance.StandardInstall" in script
    assert "--reserve-generation" in script
    assert "standard-update-pending.ini" in script
    assert "standard-update-prior.json" in script
    assert "Result := AcquireUninstallLease;" in script
    preparation = script.split("function PrepareToInstall", 1)[1].split("function PlainTree", 1)[0]
    assert "AcquireUninstallLease" not in preparation
    assert preparation.index("CheckPendingUpdate;") < preparation.index("CreateDir(PayloadDir")
    assert "procedure DeinitializeUninstall" in script
    assert 'Type: filesandordirs; Name: "{code:PayloadDir}\\.venv"' in script
    assert 'Type: filesandordirs; Name: "{app}"' not in script
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


@pytest.mark.parametrize("bad", ["0" * 64, "A" * 64, "short", None])
def test_manifest_identity_rejected_without_touching_old_output(native_bundle, tmp_path, bad):
    output = tmp_path / "previous installation"
    output.mkdir()
    original = output / "user-recording.wav"
    original.write_bytes(b"preserve")
    with pytest.raises((ValueError, FileExistsError)):
        installer.prepare(native_bundle, COMMIT, bad, output)
    assert original.read_bytes() == b"preserve"
    assert tuple(output.iterdir()) == (original,)


def test_valid_input_never_replaces_existing_destination(native_bundle, tmp_path):
    output = tmp_path / "installer"
    output.mkdir()
    (output / "saved.txt").write_bytes(b"previous")
    with pytest.raises(FileExistsError):
        prepare(native_bundle, tmp_path)
    assert (output / "saved.txt").read_bytes() == b"previous"


@pytest.mark.parametrize("mutation", ["source_commit", "missing_sources", "missing_python", "missing_launcher",
                                    "missing_launcher_proof", "unannounced_launcher", "audio", "inno_expression"])
def test_incomplete_unreviewed_or_executable_metadata_rejected(native_bundle, tmp_path, mutation):
    path = native_bundle / "bundle-manifest.json"
    manifest = json.loads(path.read_bytes())
    if mutation == "source_commit":
        manifest["source_commit"] = "b" * 40
    elif mutation == "missing_sources":
        del manifest["files"]["ocr-corresponding-source.tar"]
    elif mutation == "missing_python":
        manifest["bundled_python"] = False
    elif mutation == "missing_launcher":
        del manifest["files"]["ResearchLibrary.exe"]
    elif mutation == "missing_launcher_proof":
        del manifest["files"]["native-launcher-build.json"]
    elif mutation == "unannounced_launcher":
        manifest["bundled_native_launcher"] = False
    elif mutation == "audio":
        manifest["bundled_audio"] = True
    else:
        manifest["files"]["{#FileOpen('private.txt')}.txt"] = "0" * 64
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        prepare(native_bundle, tmp_path)
    assert not (tmp_path / "installer").exists()
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


def test_modified_payload_fails_trusted_verification_before_staging(native_bundle, tmp_path):
    (native_bundle / "ResearchLibrary.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        prepare(native_bundle, tmp_path)
    assert not (tmp_path / "installer").exists()
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


def test_changed_already_copied_file_is_rehashed_before_publication(native_bundle, tmp_path, monkeypatch):
    delivery = installer._delivery()
    monkeypatch.setattr(installer, "_delivery", lambda: delivery)
    stream = delivery._stream
    changed = False

    def copy_then_change(source, cancelled, *, destination=None, limit=delivery.MAX_TOTAL):
        nonlocal changed
        result = stream(source, cancelled, destination=destination, limit=limit)
        if destination is not None and not changed:
            staged = Path(destination.name).parent / "CheckInstallation.cmd"
            if staged.exists() and staged != Path(destination.name):
                staged.write_bytes(b"changed after copy")
                changed = True
        return result

    monkeypatch.setattr(delivery, "_stream", copy_then_change)
    with pytest.raises(ValueError, match="checksum"):
        prepare(native_bundle, tmp_path)
    assert changed
    assert not (tmp_path / "installer").exists()
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


def test_cancelled_copy_removes_only_its_own_staging(native_bundle, tmp_path, monkeypatch):
    delivery = installer._delivery()
    monkeypatch.setattr(installer, "_delivery", lambda: delivery)
    stream = delivery._stream
    cancelled = False

    def cancel_after_copy(source, check, **kwargs):
        nonlocal cancelled
        result = stream(source, check, **kwargs)
        if kwargs.get("destination") is not None:
            cancelled = True
        return result

    monkeypatch.setattr(delivery, "_stream", cancel_after_copy)
    sentinel = tmp_path / "other-operation"
    sentinel.mkdir()
    (sentinel / "saved.txt").write_bytes(b"untouched")
    with pytest.raises(InterruptedError):
        prepare(native_bundle, tmp_path, cancelled=lambda: cancelled)
    assert (sentinel / "saved.txt").read_bytes() == b"untouched"
    assert not (tmp_path / "installer").exists()
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


@pytest.mark.parametrize("arguments", [{"compiler": "ISCC.exe"}, {"compiler_sha256": "a" * 64}])
def test_compiler_requires_an_explicit_path_and_independent_pin(native_bundle, tmp_path, arguments):
    with pytest.raises(ValueError, match="both"):
        prepare(native_bundle, tmp_path, **arguments)


def test_competing_output_at_publication_is_preserved(native_bundle, tmp_path, monkeypatch):
    delivery = installer._delivery()
    monkeypatch.setattr(installer, "_delivery", lambda: delivery)
    publish = delivery._publish

    def competing(stage, output, parent_fd):
        output.mkdir()
        (output / "latest.txt").write_bytes(b"other operation")
        return publish(stage, output, parent_fd)

    monkeypatch.setattr(delivery, "_publish", competing)
    with pytest.raises(OSError):
        prepare(native_bundle, tmp_path)
    assert (tmp_path / "installer/latest.txt").read_bytes() == b"other operation"
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


@pytest.fixture
def native_probe():
    spec = importlib.util.spec_from_file_location(
        "carr_standard_probe_test", ROOT / "packaging/probe_standard_installer_windows.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("fields,payload", [
    ({"bundled_audio": True}, None),
    ({"bundled_audio_runtime": True}, None),
    ({"bundled_audio": 0}, None),
    ({"bundled_audio_runtime": None}, None),
    ({}, "audio/bin/ffmpeg.exe"),
    ({}, "audio-runtime/bin/ffplay.exe"),
    ({"bundled_audio": False}, "AuDiO/bin/ffprobe.exe"),
    ({"bundled_audio_runtime": False, "public_distribution_clearance": True},
     "AUDIO-RUNTIME/provenance/source-lock.json"),
], ids=["legacy-flag", "current-flag", "nonboolean-legacy", "nonboolean-current",
        "legacy-inventory", "current-inventory", "false-flag-inventory", "forged-clearance"])
def test_direct_installer_cannot_bypass_current_audio_transfer_gate(
        native_bundle, tmp_path, fields, payload):
    path = native_bundle / "bundle-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(fields)
    if payload is not None:
        file = native_bundle / payload
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"synthetic owner-local audio runtime")
        manifest["files"][payload] = sha(file.read_bytes())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="distribution clearance"):
        prepare(native_bundle, tmp_path)
    assert not (tmp_path / "installer").exists()
    assert not list(tmp_path.glob(".carr-standard-installer-*"))


def test_native_probe_reuses_current_trusted_seal_without_executing_payload(native_bundle, native_probe):
    raw = (native_bundle / "bundle-manifest.json").read_bytes()
    actual, identity = native_probe._verified_seal(native_bundle, COMMIT)
    assert actual == raw and identity == {"manifest_sha256": sha(raw)}
    (native_bundle / "ResearchLibrary.exe").write_bytes(b"changed native launcher")
    with pytest.raises(ValueError, match="checksum"):
        native_probe._verified_seal(native_bundle, COMMIT)


def test_negative_bootstrap_really_retains_an_uncompleted_environment(tmp_path):
    fixture = runpy.run_path(str(Path(__file__).with_name("build_standard_failure_fixture.py")))
    generation = tmp_path / "negative generation"
    generation.mkdir()
    sentinel = generation / "unowned.txt"
    sentinel.write_bytes(b"do not erase")
    script = generation / "install_windows.py"
    script.write_bytes(fixture["NEGATIVE_BOOTSTRAP"])
    process = subprocess.run([sys.executable, "-I", "-B", str(script)],
                             capture_output=True, timeout=30)
    assert process.returncode == 23, process.stderr.decode("utf-8", "replace")
    assert (generation / "bootstrap-negative-reached.txt").read_bytes() == b"23\n"
    assert (generation / ".venv/failed-install-state.txt").read_bytes() == b"preserve failed environment\n"
    assert not (generation / ".venv/.installation-complete").exists()
    assert sentinel.read_bytes() == b"do not erase"


@pytest.mark.parametrize("code", [0, 23], ids=["successful-child", "negative-child"])
def test_probe_runs_actual_owned_child_and_keeps_exact_result(tmp_path, native_probe, code):
    log = tmp_path / "child.log"
    actual = native_probe._run([sys.executable, "-I", "-B", "-c",
        "import sys; print('synthetic native probe child', flush=True); raise SystemExit(int(sys.argv[1]))",
        str(code)], log, timeout=30, require_success=False)
    assert actual == code
    assert log.read_bytes().strip() == b"synthetic native probe child"


def test_probe_rejects_nonzero_actual_child(tmp_path, native_probe):
    with pytest.raises(RuntimeError, match=r"failed \(23\)"):
        native_probe._run([sys.executable, "-I", "-B", "-c", "raise SystemExit(23)"],
                          tmp_path / "failed.log", timeout=30)


def test_probe_timeout_stops_the_started_owned_child(tmp_path, native_probe, monkeypatch):
    import time
    from carr_library.audio import engines
    ready = tmp_path / "ready"
    children = []
    original = engines._start_process
    def start(arguments, **kwargs):
        child = original(arguments, **kwargs)
        children.append(child)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists(), "The actual owned child did not reach its controlled wait"
            return child
        except BaseException:
            engines._stop(child)
            raise
    monkeypatch.setattr(engines, "_start_process", start)
    with pytest.raises(subprocess.TimeoutExpired):
        native_probe._run([sys.executable, "-I", "-B", "-c",
            "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_bytes(b'ready'); time.sleep(60)",
            str(ready)], tmp_path / "timeout.log", timeout=.1)
    assert ready.read_bytes() == b"ready"
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize("mode", ["complete", "timeout", "query-error"])
def test_native_job_drain_waits_for_clone_and_refuses_unknown_completion(native_probe, monkeypatch, mode):
    import ctypes
    from types import SimpleNamespace
    calls, pauses = [], []
    counts = iter((2, 1, 0)) if mode == "complete" else iter((1,))
    def query(handle, kind, output, size, returned):
        calls.append((handle, kind, size))
        assert kind == 1 and size == ctypes.sizeof(native_probe._JobAccounting)
        if mode == "query-error":
            return 0
        ctypes.cast(output, ctypes.POINTER(native_probe._JobAccounting)).contents.active_processes = next(counts)
        return 1
    process = SimpleNamespace(_carr_job=SimpleNamespace(handle=123,
        api=SimpleNamespace(kernel=SimpleNamespace(QueryInformationJobObject=query),
                            error=lambda: OSError("Job query failed"))))
    monkeypatch.setattr(native_probe.time, "monotonic", lambda: 10)
    monkeypatch.setattr(native_probe.time, "sleep", pauses.append)
    if mode == "complete":
        native_probe._wait_owned_descendants(process, 20)
        assert len(calls) == 3 and pauses == [.02, .02]
    elif mode == "timeout":
        with pytest.raises(TimeoutError, match="descendants"):
            native_probe._wait_owned_descendants(process, 9)
        assert len(calls) == 1 and not pauses
    else:
        with pytest.raises(OSError, match="query failed"):
            native_probe._wait_owned_descendants(process, 20)
        assert len(calls) == 1 and not pauses
