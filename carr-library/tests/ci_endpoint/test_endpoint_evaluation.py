"""Portable contract/negative controls; no Windows installation or audio claim."""
from __future__ import annotations

import copy
import ctypes
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import zipfile
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci_audio_endpoint.py"
SPEC = importlib.util.spec_from_file_location("carr_endpoint_evaluation_under_test", PATH)
endpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(endpoint)


def environment():
    return {
        "GITHUB_ACTIONS": "true", "RUNNER_OS": "Windows",
        "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64",
        "GITHUB_REPOSITORY": endpoint.REPOSITORY,
        "GITHUB_REF": "refs/heads/" + endpoint.BRANCH,
        "GITHUB_REF_NAME": endpoint.BRANCH, "GITHUB_EVENT_NAME": "push",
    }


def check(env, **kwargs):
    args = {"os_name": "nt", "system": "Windows", "machine": "AMD64", "pointer_bytes": 8}
    args.update(kwargs)
    endpoint.check_environment(env, **args)


@pytest.mark.parametrize("key", tuple(environment()))
def test_environment_refuses_missing_or_changed_authority(key):
    env = environment()
    env[key] = "wrong"
    with pytest.raises(endpoint.EvaluationError):
        check(env)
    del env[key]
    with pytest.raises(endpoint.EvaluationError):
        check(env)


@pytest.mark.parametrize("change", [
    {"os_name": "posix"}, {"system": "Linux"}, {"machine": "ARM64"},
    {"pointer_bytes": 4},
])
def test_platform_guard_rejects_non_native_x64(change):
    with pytest.raises(endpoint.EvaluationError):
        check(environment(), **change)


def test_guard_accepts_only_exact_reviewed_context():
    check(environment())
    env = environment()
    env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
    with pytest.raises(endpoint.EvaluationError):
        check(env)


def test_main_guard_failure_writes_receipt_before_any_network_or_native_call(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    def forbidden(*args, **kwargs):
        pytest.fail("guard failure reached a side effect")
    monkeypatch.setattr(endpoint, "NativeWindows", forbidden)
    monkeypatch.setattr(endpoint, "download_archive", forbidden)
    report = tmp_path / "receipt.json"
    assert endpoint.main(["--receipt", str(report)]) == 1
    result = json.loads(report.read_text())
    assert result["status"] == "FAILED"
    assert result["device_state"] == "not_attempted"
    assert result["package_state"] == "not_attempted"
    assert result["vendor_setup_exe_executed"] is False
    assert "GITHUB_ACTIONS" in result["error"]["message"]


def archive_fixture(tmp_path, *, extra=None, compression=zipfile.ZIP_DEFLATED, symlink=False):
    entries = {
        endpoint.INF_NAME: b"synthetic INF",
        endpoint.SYS_NAME: b"synthetic SYS",
        endpoint.ARM_SYS_NAME: b"synthetic passive ARM64 SYS",
        endpoint.CAT_NAME: b"synthetic CAT",
        "readme.txt": b"synthetic original notice",
        "VBCABLE_Setup_x64.exe": b"must never be extracted or executed",
    }
    entries.update(extra or {})
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w", compression=compression) as package:
        for name, data in entries.items():
            if symlink and name == endpoint.SYS_NAME:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                package.writestr(info, data)
            else:
                package.writestr(name, data)
    pins = {
        "archive_size": path.stat().st_size,
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "archive_members": {name: len(data) for name, data in entries.items()},
        "notice_sha256": hashlib.sha256(entries["readme.txt"]).hexdigest(),
        "stage_files": {name: {"size": len(entries[name]),
                              "sha256": hashlib.sha256(entries[name]).hexdigest()}
                        for name in endpoint.STAGED},
    }
    return path, pins


def test_only_four_pinned_driver_inputs_are_staged(tmp_path):
    archive, pins = archive_fixture(tmp_path)
    stage = tmp_path / "staged"
    paths = endpoint.stage_archive(archive, stage, pins)
    assert set(p.name for p in stage.iterdir()) == set(endpoint.STAGED)
    assert set(paths) == set(endpoint.STAGED)
    endpoint.verify_staged(paths, pins)
    paths[endpoint.SYS_NAME].write_bytes(b"changed")
    with pytest.raises(endpoint.EvaluationError, match="Staged input changed"):
        endpoint.verify_staged(paths, pins)


@pytest.mark.parametrize("name", [
    "../escape", "nested/file", "nested\\file", "C:stream",
    endpoint.INF_NAME.swapcase(),
])
def test_archive_rejects_unsafe_or_case_alias_names(tmp_path, name):
    archive, pins = archive_fixture(tmp_path, extra={name: b"untrusted"})
    with pytest.raises(endpoint.EvaluationError, match="unsafe"):
        endpoint.stage_archive(archive, tmp_path / "staged", pins)


def test_zip_raw_nul_name_is_rejected(tmp_path):
    archive, pins = archive_fixture(tmp_path)
    old = b"VBCABLE_Setup_x64.exe"
    new = b"VBCABLE_Setup_x64\x00exe"
    assert len(old) == len(new)
    data = archive.read_bytes().replace(old, new)
    archive.write_bytes(data)
    pins["archive_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(endpoint.EvaluationError, match="unsafe"):
        endpoint.stage_archive(archive, tmp_path / "staged", pins)


@pytest.mark.parametrize("mode", ["symlink", "compression", "total"])
def test_archive_rejects_symlink_unsupported_compression_and_size(tmp_path, monkeypatch, mode):
    archive, pins = archive_fixture(
        tmp_path, symlink=mode == "symlink",
        compression=zipfile.ZIP_BZIP2 if mode == "compression" else zipfile.ZIP_DEFLATED)
    if mode == "total":
        monkeypatch.setattr(endpoint, "MAX_UNCOMPRESSED", 1)
    with pytest.raises(endpoint.EvaluationError):
        endpoint.stage_archive(archive, tmp_path / "staged", pins)


def test_hash_is_checked_before_zip_parser(tmp_path, monkeypatch):
    archive, pins = archive_fixture(tmp_path)
    pins["archive_sha256"] = "0" * 64
    def forbidden(*args, **kwargs):
        pytest.fail("unverified bytes reached ZIP parsing")
    monkeypatch.setattr(endpoint.zipfile, "ZipFile", forbidden)
    with pytest.raises(endpoint.EvaluationError, match="SHA-256"):
        endpoint.stage_archive(archive, tmp_path / "staged", pins)


def test_staged_member_pin_is_independent_of_archive_pin(tmp_path):
    archive, pins = archive_fixture(tmp_path)
    pins["stage_files"][endpoint.SYS_NAME]["sha256"] = "0" * 64
    with pytest.raises(endpoint.EvaluationError, match="member mismatch"):
        endpoint.stage_archive(archive, tmp_path / "staged", pins)


def signatures():
    pins, _ = endpoint.load_pins()
    result = []
    for name in endpoint.SIGNED:
        fingerprint, subject = next(iter(pins["signers"][name].items()))
        result.append({"file": name, "status": "Valid",
                       "certificate_sha256": fingerprint, "simple_name": subject})
    return pins, result


def test_reviewed_signers_match_independent_original_der_extraction():
    # Independent fixed oracle: SHA-256 of the actual certificates in the
    # official SYS/CAT, not values echoed back from the pins under test.
    pins, _ = endpoint.load_pins()
    vendor = "03c73f8446369bdcbe07edbc2009e335d0fac3907d6550558d27ae11787ebc25"
    microsoft = "735521239659d359cc4aaef67a4432d36ca585b4db26e93593905a77f5b3efa4"
    assert pins["signers"][endpoint.SYS_NAME] == {
        vendor: "BUREL VINCENT Entrepreneur individuel",
        microsoft: "Microsoft Windows Hardware Compatibility Publisher",
    }
    assert pins["signers"][endpoint.CAT_NAME] == {
        microsoft: "Microsoft Windows Hardware Compatibility Publisher"}
    assert pins["signers"][endpoint.ARM_SYS_NAME] == pins["signers"][endpoint.SYS_NAME]


@pytest.mark.parametrize("bad_key", ["5" * 65, "a" * 63, "G" * 64, "A" * 64])
def test_malformed_certificate_pin_fails_before_signature_verification(tmp_path, monkeypatch, bad_key):
    pins, _ = endpoint.load_pins()
    allowed = pins["signers"][endpoint.SYS_NAME]
    old = next(key for key, value in allowed.items() if value.startswith("BUREL"))
    allowed[bad_key] = allowed.pop(old)
    (tmp_path / "ci_audio_endpoint_pins.json").write_text(json.dumps(pins))
    monkeypatch.setattr(endpoint, "__file__", str(tmp_path / "ci_audio_endpoint.py"))
    with pytest.raises(endpoint.EvaluationError, match="signer certificate pin"):
        endpoint.load_pins()


def test_valid_pinned_vendor_and_nested_microsoft_sys_signers():
    pins, reports = signatures()
    endpoint.validate_signature_reports(reports, pins)
    for fingerprint, subject in pins["signers"][endpoint.SYS_NAME].items():
        reports[0].update(certificate_sha256=fingerprint, simple_name=subject)
        endpoint.validate_signature_reports(reports, pins)


@pytest.mark.parametrize("change", [
    {"status": "NotTrusted"}, {"status": "UnknownError"},
    {"certificate_sha256": "0" * 64}, {"simple_name": "Different publisher"},
    {"file": "unreviewed.sys"},
])
def test_signature_must_be_valid_and_match_exact_certificate_and_publisher(change):
    pins, reports = signatures()
    reports[0].update(change)
    with pytest.raises(endpoint.EvaluationError):
        endpoint.validate_signature_reports(reports, pins)


def test_signature_duplicate_and_missing_reports_fail_closed():
    pins, reports = signatures()
    for value in (reports[:1], [reports[0], reports[0]], {}, None):
        with pytest.raises(endpoint.EvaluationError):
            endpoint.validate_signature_reports(value, pins)


@pytest.mark.parametrize("module_key", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
def test_python_writes_exact_three_target_json_request_for_fresh_powershell(tmp_path, monkeypatch, module_key):
    pins, reports = signatures()
    paths = {name: tmp_path / name for name in endpoint.STAGED}
    observed = []
    inherited_path = r"C:\Program Files\PowerShell\7\Modules"
    monkeypatch.setenv(module_key, inherited_path)
    monkeypatch.setenv("CARR_SIGNATURE_ENV_SENTINEL", "preserved")
    def run(command, **kwargs):
        payload = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        observed.extend(payload)
        assert command[1:4] == ["-NoLogo", "-NoProfile", "-NonInteractive"]
        assert command[4] == "-File"
        assert command[-2] == "-InputPath"
        assert "Bypass" not in command
        assert kwargs["stdin"] == endpoint.subprocess.DEVNULL
        assert not any(key.upper() == "PSMODULEPATH" for key in kwargs["env"])
        assert kwargs["env"]["CARR_SIGNATURE_ENV_SENTINEL"] == "preserved"
        return SimpleNamespace(returncode=0, stdout=json.dumps(reports).encode(), stderr=b"")
    monkeypatch.setattr(endpoint.subprocess, "run", run)
    native = SimpleNamespace(system_directory=tmp_path / "System32")
    endpoint.verify_signatures(paths, pins, native, tmp_path)
    assert observed == [{"file": name, "path": str(paths[name])} for name in endpoint.SIGNED]
    assert len(observed) == 3 and len({item["file"] for item in observed}) == 3
    assert endpoint.os.environ[module_key] == inherited_path


@pytest.mark.parametrize("name", [endpoint.SYS_NAME, endpoint.ARM_SYS_NAME, endpoint.CAT_NAME])
def test_every_signed_member_including_passive_arm64_requires_its_exact_signer(name):
    pins, reports = signatures()
    selected = next(item for item in reports if item["file"] == name)
    selected["certificate_sha256"] = "0" * 64
    with pytest.raises(endpoint.EvaluationError, match="publisher verification"):
        endpoint.validate_signature_reports(reports, pins)


def test_redirect_never_changes_download_authority():
    with pytest.raises(endpoint.EvaluationError, match="redirect"):
        endpoint.NoRedirect().redirect_request(None, None, 302, "", {},
                                               "https://unreviewed.invalid/driver.zip")


def test_winmm_wire_format_is_exact_reader_fixture():
    observed = []
    class WinMM:
        def waveOutOpen(self, output, device, form, callback, context, flags):
            value = ctypes.cast(form, ctypes.POINTER(endpoint.WAVEFORMATEX)).contents
            observed.append((output, device, value.wFormatTag, value.nChannels,
                             value.nSamplesPerSec, value.nAvgBytesPerSec,
                             value.nBlockAlign, value.wBitsPerSample, value.cbSize,
                             callback, context, flags))
            return 2
    native = endpoint.NativeWindows.__new__(endpoint.NativeWindows)
    native.winmm = WinMM()
    assert native.query_format(0xffffffff) == 2
    assert observed == [(None, 0xffffffff, 1, 1, 16000, 32000, 2, 16, 0, 0, 0, 1)]
    assert ctypes.sizeof(endpoint.WAVEFORMATEX) == 18


def install_fixture(tmp_path, monkeypatch, *, registration_flags=0,
                    update_flags=0, update_reboot=False):
    """Exercise the actual install orchestrator with no native library loaded."""
    observed = []
    archive, pins = archive_fixture(tmp_path)
    paths = endpoint.stage_archive(archive, tmp_path / "stage", pins)
    native = endpoint.NativeWindows.__new__(endpoint.NativeWindows)
    native.class_guid = endpoint.GUID.from_buffer_copy(
        endpoint.uuid.UUID(endpoint.CLASS_GUID).bytes_le)

    class Setup:
        def SetupDiGetINFClassW(self, path, guid, name, size, required):
            ctypes.memmove(guid, ctypes.byref(native.class_guid), ctypes.sizeof(native.class_guid))
            name.value = "MEDIA"
            return 1
        def SetupDiCreateDeviceInfoList(self, guid, parent):
            return 123
        def SetupSetNonInteractiveMode(self, value):
            observed.append(("mode", bool(value)))
            return 0
        def SetupDiCreateDeviceInfoW(self, handle, name, guid, description, parent, flags, info):
            observed.append(("create", flags))
            return 1
        def SetupDiSetDeviceRegistryPropertyW(self, handle, info, number, data, count):
            observed.append(("property", number, ctypes.string_at(data, count)))
            return 1
        def SetupDiCallClassInstaller(self, operation, handle, info):
            observed.append(("class", operation))
            return 1

    class NewDev:
        def UpdateDriverForPlugAndPlayDevicesW(self, parent, hardware, inf, flags, reboot):
            observed.append(("update", parent, hardware, inf, flags, reboot is not None))
            ctypes.cast(reboot, ctypes.POINTER(endpoint.BOOL)).contents.value = update_reboot
            return 1

    native.setup = Setup()
    native.newdev = NewDev()
    native._instance = lambda *args: "ROOT\\MEDIA\\0000"
    flags = iter((registration_flags, update_flags))
    native._restart_flags = lambda *args: next(flags)
    native._destroy = lambda handle: observed.append(("destroy", handle))
    # Expected rejections occur before status polling or identity inspection.
    report = endpoint.Receipt(tmp_path / "receipt.json")
    return native, paths, pins, report, observed


@pytest.mark.parametrize("flag", [endpoint.DI_NEEDREBOOT, endpoint.DI_NEEDRESTART])
def test_registration_restart_prevents_update_and_still_closes_handles(tmp_path, monkeypatch, flag):
    native, paths, pins, receipt, observed = install_fixture(
        tmp_path, monkeypatch, registration_flags=flag)
    with pytest.raises(endpoint.EvaluationError, match="needs restart"):
        native.install(paths, pins, receipt)
    assert not any(item[0] == "update" for item in observed)
    assert ("class", endpoint.DIF_REGISTERDEVICE) in observed
    assert ("property", 1, (endpoint.HWID + "\x00\x00").encode("utf-16le")) in observed
    assert observed[-2:] == [("mode", False), ("destroy", 123)]
    assert receipt.value["registration_restart_flags"] == flag
    assert receipt.value["handles_closed"] is True
    assert receipt.value["device_state"] == "registered_retained_until_VM_teardown"


@pytest.mark.parametrize("kind", ["output", "restart_flag", "reboot_flag"])
def test_update_is_noninteractive_not_force_and_reboot_never_qualifies(tmp_path, monkeypatch, kind):
    native, paths, pins, receipt, observed = install_fixture(
        tmp_path, monkeypatch,
        update_reboot=kind == "output",
        update_flags={"restart_flag": endpoint.DI_NEEDRESTART,
                      "reboot_flag": endpoint.DI_NEEDREBOOT}.get(kind, 0))
    with pytest.raises(endpoint.EvaluationError, match="requires reboot"):
        native.install(paths, pins, receipt)
    update = next(item for item in observed if item[0] == "update")
    assert update[1:] == (None, endpoint.HWID, str(paths[endpoint.INF_NAME]), 4, True)
    assert receipt.value["handles_closed"] is True
    assert receipt.value["package_state"] == "installed_retained_until_VM_teardown"


def test_failed_receipt_cannot_be_overwritten_as_pass(tmp_path):
    receipt = endpoint.Receipt(tmp_path / "receipt.json")
    assert receipt.finish("FAILED", error={"message": "persistent refusal"})
    assert not receipt.finish("PASS")
    receipt.update("later", device_state="incorrect")
    actual = json.loads(receipt.path.read_text())
    assert actual["status"] == "FAILED"
    assert actual["device_state"] == "not_attempted"


def test_watchdog_preserves_failure_receipt_before_hard_exit(tmp_path, monkeypatch):
    receipt = endpoint.Receipt(tmp_path / "receipt.json")
    def exit_control(code):
        assert code == 124
        raise SystemExit(code)
    monkeypatch.setattr(endpoint.os, "_exit", exit_control)
    with pytest.raises(SystemExit):
        receipt.timeout()
    actual = json.loads(receipt.path.read_text())
    assert actual["status"] == "FAILED"
    assert actual["phase"] == "deadline"
    assert actual["uninstall_attempted"] is False

