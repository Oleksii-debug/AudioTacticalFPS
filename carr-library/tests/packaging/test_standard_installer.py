"""Real sealed-tree preparation checks; native setup acceptance is separate."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy

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


@pytest.fixture
def native_bundle(bundle):
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    for name in ("runtime/python.exe", "ResearchLibrary.exe"):
        path = bundle / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"INERT SYNTHETIC FILE, NEVER EXECUTE\n")
        manifest["files"][name] = sha(path.read_bytes())
    manifest["bundled_python"] = True
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
    script = (output / "Resonance.iss").read_text(encoding="utf-8-sig")
    assert "@PAYLOAD" not in script and "@SOURCE" not in script
    assert 'Source: "payload\\ocr-corresponding-source.tar"' in script
    assert "AfterInstall: Bootstrap" not in script
    assert "function PrepareToInstall" in script
    assert "ExtractTemporaryFiles('{tmp}\\resonance-payload\\*')" in script
    assert "Result := 'Offline installation was not activated." in script
    assert "CloseApplications=no" in script and "PrivilegesRequired=lowest" in script
    assert "UninstallLogMode=append" in script
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


@pytest.mark.parametrize("mutation", ["source_commit", "missing_sources", "missing_python", "missing_launcher", "audio", "inno_expression"])
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
