"""Seal native launcher identity without treating synthetic PE bytes as execution.

The actual compiled C launcher is exercised separately on Windows. These tests
exercise retained source identities, receipt tampering and private bundle inputs.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


builder = module("build_windows_bundle")
installer = module("install_windows")


def synthetic_pe():
    # Structurally sufficient inert PE data; never execute or claim compilation.
    raw = bytearray(1536)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 0x3C, 0x80)
    raw[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", raw, 0x84, 0x8664, 1, 0, 0, 0, 0xF0, 0)
    optional = 0x98
    struct.pack_into("<H", raw, optional, 0x20B)
    struct.pack_into("<HH", raw, optional + 68, 2, 0x160)
    struct.pack_into("<II", raw, optional + 120, 0x1000, 40)
    struct.pack_into("<II", raw, optional + 128, 0x1100, 128)
    struct.pack_into("<IIII", raw, optional + 0xF0 + 8, 1024, 0x1000, 1024, 512)
    struct.pack_into("<IIIII", raw, 512, 0, 0, 0, 0x1060, 0)
    name = b"KERNEL32.dll\0"
    raw[608:608 + len(name)] = name
    manifest = b'<assembly><requestedExecutionLevel level="asInvoker" uiAccess="false"/><longPathAware>true</longPathAware></assembly>'
    raw[768:768 + len(manifest)] = manifest
    return bytes(raw)


@pytest.fixture
def native_build(tmp_path):
    root = tmp_path / "Private build Україна"
    root.mkdir()
    binary = synthetic_pe()
    (root / "ResearchLibrary.exe").write_bytes(binary)
    commit = subprocess.check_output(["git", "-C", str(ROOT.parent), "rev-parse", "HEAD"], text=True).strip()
    source = {}
    for name in ("launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest"):
        raw = subprocess.check_output(["git", "-C", str(ROOT.parent), "show",
                                       f"{commit}:carr-library/packaging/native-launcher/{name}"])
        source[name] = hashlib.sha256(raw).hexdigest()
    report = {"schema": 1, "release_status": "unsigned-private-candidate",
              "source_sha256": source, "pe": {"sha256": hashlib.sha256(binary).hexdigest()}}
    (root / "build-evidence.json").write_text(json.dumps(report), encoding="utf-8-sig")
    return root, commit, report, binary


def test_exact_committed_sources_and_bom_receipt_return_only_admitted_bytes(native_build):
    root, commit, _report, binary = native_build
    (root / "windows_child_probe.exe").write_bytes(b"Private CI fixture must never be copied")
    sealed, receipt = builder.verify_native_launcher(root, commit)
    assert sealed == binary
    assert receipt == (root / "build-evidence.json").read_bytes()
    assert b"Private CI fixture" not in sealed + receipt


@pytest.mark.parametrize("mutation", ["source", "missing_source", "unexpected_source", "pe_hash", "invalid_pe", "schema"])
def test_changed_or_incomplete_receipt_cannot_certify_a_native_launcher(native_build, mutation):
    root, commit, report, _binary = native_build
    if mutation == "source":
        report["source_sha256"]["launcher.c"] = "0" * 64
    elif mutation == "missing_source":
        del report["source_sha256"]["launcher.manifest"]
    elif mutation == "unexpected_source":
        report["source_sha256"]["../outside.c"] = "0" * 64
    elif mutation == "pe_hash":
        report["pe"]["sha256"] = "0" * 64
    elif mutation == "invalid_pe":
        report["pe"] = []
    else:
        report["schema"] = 999
    (root / "build-evidence.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        builder.verify_native_launcher(root, commit)


def test_binary_changed_after_build_receipt_is_rejected(native_build):
    root, commit, _report, binary = native_build
    (root / "ResearchLibrary.exe").write_bytes(binary + b"changed")
    with pytest.raises(ValueError, match="build receipt"):
        builder.verify_native_launcher(root, commit)


@pytest.mark.parametrize("commit", [None, "HEAD", "0" * 39, "F" * 40])
def test_unbound_source_is_not_a_releasable_native_input(native_build, commit):
    root, _commit, _report, _binary = native_build
    with pytest.raises(ValueError, match="exact source commit"):
        builder.verify_native_launcher(root, commit)


def test_native_bundle_preflight_requires_bundled_python_before_any_publication(native_build, tmp_path):
    root, commit, _report, _binary = native_build
    output = tmp_path / "Unpublished bundle"
    with pytest.raises(ValueError, match="complete bundled Python"):
        builder.build_bundle(tmp_path / "missing.whl", tmp_path / "missing wheelhouse",
                             tmp_path / "missing.lock", output, source_commit=commit, launcher_dir=root)
    assert not output.exists()


@pytest.mark.parametrize("flag", [True, False, "true"])
def test_installer_rejects_incomplete_or_unannounced_native_payload(tmp_path, flag):
    root = tmp_path / "Rejected installation"
    root.mkdir()
    required = ["install.lock", "install_windows.py", "Install.cmd", "ResearchLibrary.cmd",
                "CheckInstallation.cmd", "README.txt", "wheelhouse/fake.whl"]
    manifest = {"schema": 1, "files": {name: "0" * 64 for name in required}, "bundled_launcher": flag}
    (root / "ResearchLibrary.exe").write_bytes(b"Unannounced native file")
    (root / "bundle-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="launcher"):
        installer.verify(root)
    assert (root / "ResearchLibrary.exe").read_bytes() == b"Unannounced native file"


@pytest.mark.parametrize("changed", ["ResearchLibrary.exe", "native-launcher-build.json"])
def test_complete_native_inventory_verifies_and_detects_payload_changes(native_build, tmp_path, changed):
    native, commit, _report, binary = native_build
    root = tmp_path / "Sealed synthetic inventory"
    contents = {name: b"Synthetic packaging fixture" for name in
                ("install.lock", "install_windows.py", "Install.cmd", "ResearchLibrary.cmd",
                 "CheckInstallation.cmd", "README.txt", "wheelhouse/fake.whl", "runtime/python.exe")}
    contents.update({"ResearchLibrary.exe": binary,
                     "native-launcher-build.json": (native / "build-evidence.json").read_bytes()})
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"schema": 1, "bundled_launcher": True, "bundled_python": True,
                "source_commit": commit,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    (root / "bundle-manifest.json").write_text(json.dumps(manifest))
    installer.verify(root)
    (root / changed).write_bytes(b"Tampered after sealing")
    with pytest.raises(ValueError, match="checksum"):
        installer.verify(root)


def test_isolated_managed_launcher_bootstrap_uses_only_its_exact_source_siblings(tmp_path):
    """Execute the workflow's real bootstrap without a compiler or binary build."""
    import os
    import sys
    workflow = (ROOT.parent / '.github/workflows/carr-managed-update.yml').read_text(encoding='utf-8')
    start = "          $launcherBootstrap = @'\n"
    assert workflow.count(start) == 1
    block = workflow.split(start, 1)[1].split("\n          '@", 1)[0]
    bootstrap = '\n'.join(line[10:] for line in block.splitlines()) + '\n'
    hostile = tmp_path / 'Untrusted working directory Київ'
    hostile.mkdir()
    marker = hostile / 'ambient-helper-was-executed'
    payload = "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('must not run')\nraise RuntimeError('ambient import')\n"
    (hostile / 'probe_pe.py').write_text(payload, encoding='utf-8')
    (hostile / 'sitecustomize.py').write_text(payload, encoding='utf-8')
    commit = subprocess.check_output(['git', '-C', str(ROOT.parent), 'rev-parse', 'HEAD'], text=True).strip()
    environment = dict(os.environ, PYTHONPATH=str(hostile), PYTHONDONTWRITEBYTECODE='1')
    command = [sys.executable, '-I', '-B', '-', str(ROOT), commit, '--help']
    result = subprocess.run(command, input=bootstrap, text=True, encoding='utf-8',
                            cwd=hostile, env=environment, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert '--build-probe' in result.stdout
    assert not marker.exists()
    assert not list(hostile.rglob('*.pyc'))
    # A different claimed source cannot execute even the otherwise harmless CLI.
    command[-2] = '0' * 40
    refused = subprocess.run(command, input=bootstrap, text=True, encoding='utf-8',
                             cwd=hostile, env=environment, capture_output=True, timeout=20)
    assert refused.returncode != 0 and 'immutable launcher source changed' in refused.stderr
    assert not marker.exists()
