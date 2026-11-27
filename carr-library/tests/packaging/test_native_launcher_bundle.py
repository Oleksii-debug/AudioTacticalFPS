"""Seal source-bound native launch entries; synthetic PE is never executed."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import tarfile
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


builder = module("build_windows_bundle")
installer = module("install_windows")


def synthetic_pe():
    # Only the PE directory/manifest metadata under test. This has no executable
    # code and must never be represented as a compiled or native-tested launcher.
    data = bytearray(2048)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 132, 0x8664, 1, 0, 0, 0, 240, 0)
    optional = 152
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<HH", data, optional + 68, 2, 0x160)
    struct.pack_into("<II", data, optional + 112 + 8, 0x1100, 40)
    struct.pack_into("<II", data, optional + 112 + 16, 0x1000, 128)
    struct.pack_into("<IIII", data, optional + 240 + 8, 1536, 0x1000, 1536, 512)
    manifest = b'<assembly level="asInvoker" uiAccess="false"><longPathAware>true</longPathAware></assembly>'
    data[512:512 + len(manifest)] = manifest
    struct.pack_into("<IIIII", data, 768, 1, 0, 0, 0x1150, 1)
    data[848:861] = b"KERNEL32.dll\0"
    return bytes(data)


@pytest.fixture
def candidate(tmp_path):
    launcher = tmp_path / "ResearchLibrary.exe"
    launcher.write_bytes(synthetic_pe())
    sources = {name: builder.sha256(builder.HERE / "native-launcher" / name) for name in (
        "launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest", "toolchain.lock.json")}
    evidence = tmp_path / "build-evidence.json"
    data = {"schema": 1, "release_status": "unsigned-private-candidate", "source_sha256": sources,
            "pe": {"bytes": launcher.stat().st_size, "sha256": builder.sha256(launcher)}}
    evidence.write_text(json.dumps(data), encoding="utf-8")
    return launcher, evidence


def test_exact_source_and_pe_evidence_are_bound(candidate):
    launcher, evidence = candidate
    digest, raw = builder.verify_native_launcher(launcher, evidence)
    assert digest == hashlib.sha256(launcher.read_bytes()).hexdigest()
    assert raw == evidence.read_bytes()


@pytest.mark.parametrize("mutation", ["source", "pe", "version", "signed", "unknown_source", "unsafe_source"])
def test_changed_or_unsupported_build_evidence_is_rejected(candidate, mutation):
    launcher, evidence = candidate
    data = json.loads(evidence.read_text())
    if mutation == "source":
        data["source_sha256"]["launcher.c"] = "0" * 64
    elif mutation == "pe":
        data["pe"]["sha256"] = "0" * 64
    elif mutation == "version":
        data["schema"] = True
    elif mutation == "signed":
        data["release_status"] = "signed-production"
    else:
        data["source_sha256"]["../../outside" if mutation == "unsafe_source" else "extra.c"] = "0" * 64
    evidence.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        builder.verify_native_launcher(launcher, evidence)


def test_duplicate_evidence_key_is_rejected(candidate):
    launcher, evidence = candidate
    evidence.write_text('{"schema":1,"schema":1}')
    with pytest.raises(ValueError, match="duplicate"):
        builder.verify_native_launcher(launcher, evidence)


def test_windows_msvc_utf8_bom_evidence_is_supported(candidate):
    launcher, evidence = candidate
    data = json.loads(evidence.read_text())
    data["source_sha256"].pop("toolchain.lock.json")
    for name in ("windows_child_probe.c", "build_msvc.ps1", "probe_pe.py"):
        data["source_sha256"][name] = builder.sha256(builder.HERE / "native-launcher" / name)
    evidence.write_text(json.dumps(data), encoding="utf-8-sig")
    assert builder.verify_native_launcher(launcher, evidence)[1].startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("target", ["launcher", "evidence"])
def test_oversized_input_is_rejected_before_read(candidate, target):
    launcher, evidence = candidate
    path, limit = (launcher, 2 * 1024 * 1024) if target == "launcher" else (evidence, 256 * 1024)
    with path.open("wb") as stream:
        stream.truncate(limit + 1)
    with pytest.raises(ValueError, match="budget"):
        builder.verify_native_launcher(launcher, evidence)


@pytest.fixture
def bundle_inputs(tmp_path, monkeypatch):
    here = tmp_path / "product/packaging"
    here.mkdir(parents=True)
    for name in ("install_windows.py", "Install.cmd", "ResearchLibrary.cmd", "CheckInstallation.cmd"):
        shutil.copyfile(builder.HERE / name, here / name)
    for name in ("runtime-notices", "native-launcher"):
        shutil.copytree(builder.HERE / name, here / name)
    (here / "wheel-notices").mkdir()
    (here / "wheel-notices/SOURCES.json").write_text("[]")
    shutil.copytree(ROOT / "docs", here.parent / "docs")
    python_archive = tmp_path / "python.tar.gz"
    with tarfile.open(python_archive, "w:gz") as archive:
        member = tarfile.TarInfo("python/python.exe")
        member.size = 16
        archive.addfile(member, io.BytesIO(b"synthetic Python"))
    (here / "windows-python.json").write_text(json.dumps({"download": {"sha256": builder.sha256(python_archive)}}))
    wheels = tmp_path / "wheels"
    wheels.mkdir()

    def wheel(path, name, requires="", application=False):
        with ZipFile(path, "w") as packed:
            packed.writestr(name + "-1.0.dist-info/METADATA",
                            f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.0\n" + requires)
            if application:
                packed.writestr("carr_library/__main__.py", "# synthetic fixture\n")

    dependency = wheels / "fixture-1.0-py3-none-any.whl"
    wheel(dependency, "fixture")
    app = tmp_path / "universal_research_library-1.0-py3-none-any.whl"
    wheel(app, "universal_research_library", "Requires-Dist: fixture==1.0\n", True)
    lock = tmp_path / "requirements.lock"
    lock.write_text("fixture==1.0 --hash=sha256:" + builder.sha256(dependency) + "\n")
    monkeypatch.setattr(builder, "HERE", here)
    return dict(wheel=app, wheelhouse=wheels, lock=lock, output=tmp_path / "bundle", python_archive=python_archive)


def test_complete_bundle_seals_launcher_and_original_evidence(bundle_inputs, candidate):
    launcher, evidence = candidate
    builder.build_bundle(**bundle_inputs, launcher=launcher, launcher_evidence=evidence)
    root = bundle_inputs["output"]
    installer.verify(root)
    manifest = json.loads((root / "bundle-manifest.json").read_text())
    assert manifest["bundled_native_launcher"] is True
    assert (root / "ResearchLibrary.exe").read_bytes() == launcher.read_bytes()
    assert (root / "launcher-provenance.json").read_bytes() == evidence.read_bytes()
    (root / "ResearchLibrary.exe").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        installer.verify(root)


def test_replacement_during_copy_does_not_publish(bundle_inputs, candidate, monkeypatch):
    launcher, evidence = candidate
    original = builder.shutil.copyfile

    def replace(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source) == launcher:
            Path(destination).write_bytes(b"replacement")
        return result

    monkeypatch.setattr(builder.shutil, "copyfile", replace)
    with pytest.raises(ValueError, match="changed during"):
        builder.build_bundle(**bundle_inputs, launcher=launcher, launcher_evidence=evidence)
    assert not bundle_inputs["output"].exists()


def test_evidence_replacement_cannot_change_sealed_snapshot(bundle_inputs, candidate, monkeypatch):
    launcher, evidence = candidate
    expected = evidence.read_bytes()
    original = builder.shutil.copyfile

    def replace(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source) == launcher:
            evidence.write_text("changed after verification")
        return result

    monkeypatch.setattr(builder.shutil, "copyfile", replace)
    builder.build_bundle(**bundle_inputs, launcher=launcher, launcher_evidence=evidence)
    assert (bundle_inputs["output"] / "launcher-provenance.json").read_bytes() == expected
    installer.verify(bundle_inputs["output"])


@pytest.mark.parametrize("missing", ["launcher", "evidence", "python"])
def test_incomplete_launcher_inputs_never_publish(bundle_inputs, candidate, missing):
    launcher, evidence = candidate
    if missing == "launcher":
        launcher = None
    elif missing == "evidence":
        evidence = None
    else:
        bundle_inputs["python_archive"] = None
    with pytest.raises(ValueError, match="both native|requires bundled"):
        builder.build_bundle(**bundle_inputs, launcher=launcher, launcher_evidence=evidence)
    assert not bundle_inputs["output"].exists()


def test_unannounced_launcher_is_rejected(bundle_inputs):
    builder.build_bundle(**bundle_inputs)
    root = bundle_inputs["output"]
    (root / "ResearchLibrary.exe").write_bytes(synthetic_pe())
    with pytest.raises(ValueError, match="unannounced native launcher"):
        installer.verify(root)
