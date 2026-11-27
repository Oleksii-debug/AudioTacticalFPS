"""Native source/evidence admission and real-launch acceptance boundaries.

Packaging copying and PE mutation cases remain in test_windows_bundle.py.
Fixtures here are explicitly synthetic; only Windows CI executes the native app.
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "packaging" / "native-launcher"
BASE = runpy.run_path(str(Path(__file__).with_name("test_windows_bundle.py")))
builder, installer, wheel = BASE["builder"], BASE["installer"], BASE["wheel"]


def native_module(name):
    spec = importlib.util.spec_from_file_location("fixture_native_" + name, NATIVE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admission = native_module("verify_launcher")
pe_inspector = native_module("probe_pe")
installed_probe = native_module("probe_installed_windows")
fresh_probe = native_module("probe_fresh_windows")


@pytest.fixture
def native_inputs(tmp_path):
    # Reuse the current builder suite's inert PE fixture; never execute it.
    executable = BASE["WindowsBundleTests"].native_launcher(SimpleNamespace(root=tmp_path))
    source_commit = subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(ROOT.parent), "rev-parse", "HEAD"],
        text=True).strip()
    sources = {name: hashlib.sha256((NATIVE / name).read_bytes()).hexdigest()
               for name in admission.SOURCE_SETS["Windows x64 / MSVC"]}
    build = {
        "schema": 1, "target": "Windows x64 / MSVC",
        "toolchain": {name: {"sha256": "1" * 64, "version": "synthetic fixture"}
                      for name in ("cl.exe", "link.exe", "rc.exe")},
        "source_sha256": sources, "pe": pe_inspector.inspect_pe(executable),
        "release_status": "unsigned-private-candidate",
    }
    probe = {
        "schema": 1, "status": "passed", "checks": list(admission.CHECKS),
        "check_count": len(admission.CHECKS), "launcher_sha256": builder.sha256(executable),
        "synthetic_child_sha256": "2" * 64, "native_launcher_execution_verified": True,
        "actual_python_installation_verified": False, "nvda_verified": False,
        "authenticode_verified": False,
    }
    build_path, probe_path = tmp_path / "build.json", tmp_path / "native.json"
    build_path.write_text(json.dumps(build), encoding="utf-8-sig")
    probe_path.write_text(json.dumps(probe), encoding="utf-8")
    return SimpleNamespace(executable=executable, build=build_path, probe=probe_path,
                           source_commit=source_commit, build_record=build, probe_record=probe)


@pytest.fixture
def bundle_inputs(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    dependency = wheel(wheels, "fixture")
    lock = tmp_path / "windows.lock"
    lock.write_text("fixture==1.0 --hash=sha256:" + builder.sha256(dependency) + "\n")
    application = wheel(tmp_path, "universal_research_library",
                        requires=["fixture==1.0"], entrypoint=True)
    return SimpleNamespace(application=application, wheels=wheels, dependency=dependency,
                           lock=lock, output=tmp_path / "bundle")


def launcher_arguments(inputs):
    return dict(native_launcher=inputs.executable,
                native_launcher_sha256=builder.sha256(inputs.executable),
                source_commit=inputs.source_commit)


def build(inputs, **kwargs):
    return builder.build_bundle(inputs.application, inputs.wheels, inputs.lock, inputs.output, **kwargs)


def synthetic_application_identity(monkeypatch):
    # The existing source-wheel suite separately rejects synthetic application
    # wheels. This fixture isolates launcher admission and copying, not that gate.
    monkeypatch.setattr(builder, "verify_application_source",
                        lambda path, commit: {"wheel_sha256": builder.sha256(path),
                                              "source_commit": commit, "source_tree": "b" * 40})



def test_exact_sources_and_bom_evidence_return_only_admitted_bytes(native_inputs, tmp_path):
    expected = native_inputs.executable.read_bytes()
    (tmp_path / "windows_child_probe.exe").write_bytes(b"unshipped synthetic child sentinel")
    captured = admission.verify_launcher(native_inputs.executable, native_inputs.build,
                                         native_inputs.probe, native_inputs.source_commit)
    assert captured == expected
    assert b"unshipped synthetic child sentinel" not in captured


@pytest.mark.parametrize("commit", ["HEAD", "0" * 39, "F" * 40])
def test_source_preflight_requires_an_exact_immutable_commit(native_inputs, commit):
    with pytest.raises(ValueError, match="full source commit"):
        admission.verify_launcher(native_inputs.executable, native_inputs.build,
                                  native_inputs.probe, commit)


@pytest.mark.parametrize("report,key,value", [
    ("build", "schema", True),
    ("build", "target", "unknown toolchain"),
    ("build", "target", []),
    ("build", "release_status", "signed"),
    ("build", "source_sha256", {}),
    ("probe", "schema", 2),
    ("probe", "launcher_sha256", "0" * 64),
    ("probe", "native_launcher_execution_verified", False),
    ("probe", "authenticode_verified", True),
    ("probe", "checks", []),
])
def test_incompatible_or_mixed_evidence_cannot_certify_the_candidate(
        report, key, value, native_inputs):
    path = getattr(native_inputs, report)
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    record[key] = value
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        admission.verify_launcher(native_inputs.executable, native_inputs.build,
                                  native_inputs.probe, native_inputs.source_commit)


def test_duplicate_evidence_keys_are_rejected(native_inputs):
    native_inputs.build.write_bytes(b'{"schema":1,"schema":1}')
    with pytest.raises(ValueError, match="Duplicate"):
        admission.verify_launcher(native_inputs.executable, native_inputs.build,
                                  native_inputs.probe, native_inputs.source_commit)


@pytest.mark.parametrize("kind", ["directory", "oversized-report", "truncated-pe"])
def test_nonregular_or_unbounded_or_invalid_inputs_are_rejected(kind, native_inputs, tmp_path):
    if kind == "directory":
        executable = tmp_path / "not-a-file"
        executable.mkdir()
    else:
        executable = native_inputs.executable
    if kind == "oversized-report":
        native_inputs.build.write_bytes(b" " * (admission.MAX_REPORT + 1))
    if kind == "truncated-pe":
        executable.write_bytes(b"MZ")
    with pytest.raises(ValueError):
        admission.verify_launcher(executable, native_inputs.build, native_inputs.probe,
                                  native_inputs.source_commit)


def source_repository(tmp_path):
    repository = tmp_path / "source repository"
    source = repository / "carr-library" / "packaging" / "native-launcher"
    source.mkdir(parents=True)
    for name in admission.SOURCE_SETS["Windows x64 / MSVC"]:
        shutil.copyfile(NATIVE / name, source / name)

    def git(*arguments):
        return subprocess.check_output(
            ["git", "-C", str(repository), "-c", "core.autocrlf=false",
             "-c", "user.name=Native launcher fixture", "-c", "user.email=fixture@example.invalid",
             *arguments], stderr=subprocess.DEVNULL)

    git("init", "--quiet")
    git("add", ".")
    git("commit", "--quiet", "-m", "Original source fixture")
    return source, git, git("rev-parse", "HEAD").decode().strip()


@pytest.mark.parametrize("replacement_ref", [False, True])
def test_dirty_or_git_replaced_source_cannot_claim_the_original_commit(
        replacement_ref, native_inputs, tmp_path, monkeypatch):
    source, git, original_commit = source_repository(tmp_path)
    path = source / "launcher.c"
    path.write_bytes(path.read_bytes() + b"\n/* altered source */\n")
    if replacement_ref:
        git("add", ".")
        git("commit", "--quiet", "-m", "Changed source fixture")
        changed = git("rev-parse", "HEAD").decode().strip()
        git("replace", original_commit, changed)
        native_inputs.build_record["source_sha256"]["launcher.c"] = hashlib.sha256(path.read_bytes()).hexdigest()
        native_inputs.build.write_text(json.dumps(native_inputs.build_record), encoding="utf-8")
    monkeypatch.setattr(admission, "SOURCE", source)
    with pytest.raises(ValueError, match="differs from the exact commit"):
        admission.verify_launcher(native_inputs.executable, native_inputs.build,
                                  native_inputs.probe, original_commit)


def installed_fixture(bundle_inputs, native_inputs, monkeypatch):
    synthetic_application_identity(monkeypatch)
    python_archive = BASE["synthetic_runtime"](bundle_inputs.output.parent)
    python_hash = json.loads((ROOT / "packaging/windows-python.json").read_text(encoding="utf-8"))["download"]["sha256"]
    actual_sha = builder.sha256
    monkeypatch.setattr(builder, "sha256", lambda path: python_hash if Path(path) == python_archive else actual_sha(path))
    # The installed probe admits complete delivery bundles, including the OCR
    # inventory and its pinned corresponding sources. These are inert fixture
    # bytes; the genuine builder/verifiers run, but no OCR engine is executed.
    ocr = bundle_inputs.output.parent / "synthetic OCR"
    (ocr / "tessdata").mkdir(parents=True)
    for name, data in {
            "tesseract.exe": b"MZ synthetic non-executable OCR fixture",
            "helper.dll": b"MZ synthetic dependency fixture",
            "tessdata/eng.traineddata": b"synthetic language fixture",
            "LICENSE.txt": b"Synthetic test fixture only\n"}.items():
        (ocr / name).write_bytes(data)
    source = bundle_inputs.output.parent / "synthetic-ocr-source.tar"
    payload = b"Synthetic corresponding sources only.\n"
    with tarfile.open(source, "w") as archive:
        member = tarfile.TarInfo("source/README.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    manifest = {
        "schema": 1, "target": "win_amd64", "executable": "tesseract.exe", "tessdata": "tessdata",
        "files": {path.relative_to(ocr).as_posix(): {
            "size": path.stat().st_size, "sha256": builder.sha256(path)}
            for path in ocr.rglob("*") if path.is_file()},
        "provenance": {"corresponding_source": {
            "filename": "ocr-corresponding-source.tar", "size": source.stat().st_size,
            "sha256": builder.sha256(source)}},
    }
    (ocr / "ocr-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    build(bundle_inputs, **launcher_arguments(native_inputs), ocr_dir=ocr, ocr_source_archive=source,
          python_archive=python_archive)
    environment = bundle_inputs.output / ".venv"
    (environment / "Scripts").mkdir(parents=True)
    (environment / ".installation-complete").touch()
    (environment / "Scripts" / "python.exe").write_bytes(b"never executed unit-test sentinel")
    monkeypatch.setattr(installed_probe, "os", SimpleNamespace(name="nt"))


def test_installed_probe_rejects_tampered_seal_before_starting_any_process(
        bundle_inputs, native_inputs, monkeypatch):
    installed_fixture(bundle_inputs, native_inputs, monkeypatch)
    (bundle_inputs.output / "ResearchLibrary.exe").write_bytes(b"not the sealed launcher")
    calls = []
    monkeypatch.setattr(installed_probe.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="checksum"):
        installed_probe.inspect_installed(bundle_inputs.output, native_inputs.source_commit)
    assert calls == []


def test_installed_probe_rechecks_all_sealed_bytes_after_child_success(
        bundle_inputs, native_inputs, monkeypatch):
    installed_fixture(bundle_inputs, native_inputs, monkeypatch)
    calls = []

    def run(arguments, **kwargs):
        calls.append(arguments[-1])
        if arguments[-1] == "--launcher-check":
            return subprocess.CompletedProcess(arguments, 0, b'{"schema":1,"state":"ready"}', b"")
        (bundle_inputs.output / "ResearchLibrary.cmd").write_bytes(b"changed after child success")
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(installed_probe.subprocess, "run", run)
    with pytest.raises(ValueError, match="checksum"):
        installed_probe.inspect_installed(bundle_inputs.output, native_inputs.source_commit)
    assert calls == ["--launcher-check", "--check-installation"]


def test_installed_probe_records_exact_identity_and_requires_expected_source(
        bundle_inputs, native_inputs, monkeypatch):
    installed_fixture(bundle_inputs, native_inputs, monkeypatch)

    def run(arguments, **kwargs):
        output = b'{"schema":1,"state":"ready"}' if arguments[-1] == "--launcher-check" else b""
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(installed_probe.subprocess, "run", run)
    with pytest.raises(ValueError, match="source commit"):
        installed_probe.inspect_installed(bundle_inputs.output, "0" * 40)
    report = installed_probe.inspect_installed(bundle_inputs.output, native_inputs.source_commit)
    manifest = json.loads((bundle_inputs.output / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert report["source_commit"] == native_inputs.source_commit
    assert report["launcher_sha256"] == manifest["files"]["ResearchLibrary.exe"]
    assert report["application_wheel_sha256"] == manifest["files"][report["application_wheel"]]
    assert report["sealed_bundle_verified_before_and_after"] is True
    assert report["source_commit_independently_specified"] is True
    assert report["actual_fresh_installation_verified"] is False
    assert report["gui_verified"] is report["nvda_verified"] is report["authenticode_verified"] is False


@pytest.mark.parametrize("previously_present", [False, True])
def test_fresh_probe_restores_only_its_owned_settings_bytes(tmp_path, previously_present):
    settings = tmp_path / "settings.json"
    original = b'{"owner":"retained"}\r\n'
    if previously_present:
        settings.write_bytes(original)
    before = fresh_probe.settings_snapshot(settings)
    settings.write_bytes(b'{"active_data_dir":"owned synthetic data"}\r\n')
    owned = fresh_probe.settings_snapshot(settings)
    assert fresh_probe.restore_settings(settings, before, owned) is True
    if previously_present:
        assert settings.read_bytes() == original
    else:
        assert not settings.exists()


@pytest.mark.parametrize("same_bytes", [False, True])
def test_fresh_probe_preserves_an_intervening_settings_write(tmp_path, same_bytes):
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"previous owner settings")
    before = fresh_probe.settings_snapshot(settings)
    settings.write_bytes(b"probe-owned settings")
    owned = fresh_probe.settings_snapshot(settings)
    replacement = tmp_path / "other-settings.json"
    changed = owned.content if same_bytes else b"another writer's settings"
    replacement.write_bytes(changed)
    replacement.replace(settings)
    assert fresh_probe.restore_settings(settings, before, owned) is False
    assert settings.read_bytes() == changed


def test_fresh_probe_process_ancestry_is_scoped_and_cycle_bounded():
    parents = {15: 12, 12: 10, 10: 1, 22: 21, 21: 22, 99: 1}
    assert fresh_probe.descendant(15, 10, parents)
    assert not fresh_probe.descendant(99, 10, parents)
    assert not fresh_probe.descendant(22, 10, parents)


@pytest.mark.parametrize("host,actions", [("posix", "true"), ("nt", "false")])
def test_fresh_probe_refuses_non_ci_or_non_windows_execution(tmp_path, monkeypatch, host, actions):
    monkeypatch.setattr(fresh_probe, "os",
                        SimpleNamespace(name=host, environ={"GITHUB_ACTIONS": actions}))
    with pytest.raises(RuntimeError, match="restricted"):
        fresh_probe.inspect_fresh(tmp_path, source_proof=tmp_path / "proof.json",
                                  source_commit="0" * 40)
    assert list(tmp_path.iterdir()) == []


def test_fresh_probe_preserves_an_existing_environment_before_any_launch(tmp_path, monkeypatch):
    import os
    environment = tmp_path / ".venv"
    environment.mkdir()
    sentinel = environment / "keep.txt"
    sentinel.write_bytes(b"existing environment remains")
    monkeypatch.setattr(fresh_probe, "os",
                        SimpleNamespace(name="nt", environ={"GITHUB_ACTIONS": "true"}, path=os.path))
    with pytest.raises(RuntimeError, match="absent .venv"):
        fresh_probe.inspect_fresh(tmp_path, source_proof=tmp_path / "proof.json",
                                  source_commit="0" * 40)
    assert sentinel.read_bytes() == b"existing environment remains"
