"""One sealed audio layout through the existing local packaging tools.

The Windows files are synthetic PE-shaped bytes. No executable, DLL, model,
installer, download, or host activation runs in these tests.
"""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library import audio_runtime
from carr_library import native_runtime


ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _put(root, name, raw):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return target


def _pe():
    raw = bytearray(256)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 128)
    raw[128:134] = b"PE\0\0\x64\x86"
    return bytes(raw)


def _canonical_audio(tmp_path, monkeypatch):
    """Prepare synthetic bytes through the actual canonical package verifier."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    prepare = _load("audio_bundle_convergence_prepare", ROOT / "packaging/prepare_audio_runtime.py")
    sources = tmp_path / "supplied-sources"
    sources.mkdir()
    packaging = tmp_path / "setup-source"
    templates = packaging / "audio-runtime"
    templates.mkdir(parents=True)
    notice = b"Synthetic local audio fixture; no distribution or execution approval.\n"
    (templates / "README.txt").write_bytes(notice)
    payload = {name: _pe() for name in audio_runtime.TOOLS.values()}
    payload.update({"bin/avutil.dll": _pe(), "LICENSE.txt": notice})
    archive = tmp_path / "synthetic-runtime.zip"
    with ZipFile(archive, "w") as output:
        for name, raw in payload.items():
            output.writestr("upstream/" + name, raw)
    lock = {
        "schema": 1, "package_id": audio_runtime.PACKAGE_ID,
        "target": "Windows x64", "version": "synthetic-only",
        "executables": audio_runtime.TOOLS,
        "archive": {"root": "upstream", "size": archive.stat().st_size, "sha256": _sha(archive)},
        "archive_files": {name: {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in payload.items()},
        "extra_files": {"README.txt": {"size": len(notice), "sha256": hashlib.sha256(notice).hexdigest()}},
    }
    lock_raw = _bytes(lock)
    (templates / "source-lock.json").write_bytes(lock_raw)
    monkeypatch.setattr(prepare, "HERE", packaging)
    monkeypatch.setattr(audio_runtime, "SOURCE_LOCK_SHA256", hashlib.sha256(lock_raw).hexdigest())
    root = tmp_path / "program ž Україна"
    root.mkdir()
    runtime = root / audio_runtime.DIRECTORY
    report = prepare.prepare(archive, sources, runtime)
    return SimpleNamespace(root=root, runtime=runtime, report=report, templates=templates)


def _write_outer(root, **fields):
    value = {
        "schema": 1, "target": "Windows x64 / CPython 3.11",
        "bundled_python": False, "bundled_native_runtime": False,
        "bundled_audio": False, "bundled_audio_runtime": True,
        **fields,
        "files": {p.relative_to(root).as_posix(): _sha(p)
                  for p in root.rglob("*") if p.is_file() and p.name != "bundle-manifest.json"},
    }
    raw = _bytes(value)
    (root / "bundle-manifest.json").write_bytes(raw)
    return raw


@pytest.fixture
def audio_bundle(tmp_path, monkeypatch):
    fixture = _canonical_audio(tmp_path / "canonical-audio", monkeypatch)
    for name in ("install.lock", "install_windows.py", "Install.cmd", "ResearchLibrary.cmd",
                 "CheckInstallation.cmd", "README.txt", "wheelhouse/fixture.whl"):
        _put(fixture.root, name, b"Inert installation inventory only.\n")
    fixture.raw = _write_outer(fixture.root)
    fixture.installer = _load("audio_bundle_convergence_installer", ROOT / "packaging/install_windows.py")
    return fixture


def test_installer_and_canonical_resolver_admit_the_same_exact_local_layout(audio_bundle):
    fixture = audio_bundle
    assert fixture.installer.read_manifest(fixture.root) == fixture.raw
    fixture.installer.verify(fixture.root, manifest_bytes=fixture.raw)
    report = audio_runtime.inspect_bundled_audio(fixture.root)
    assert report["layout"] == "audio-runtime"
    assert report["tools"] == fixture.report["tools"]
    assert report["source_lock_sha256"] == fixture.report["source_lock_sha256"]
    assert report["manifest_sha256"] == fixture.report["manifest_sha256"]
    assert report["native_execution"] == "NOT_RUN"


@pytest.mark.parametrize("flag", [False, None, 1, "true"], ids=["false", "null", "integer", "text"])
def test_installer_rejects_unannounced_or_non_boolean_canonical_audio(audio_bundle, flag):
    fixture = audio_bundle
    value = json.loads(fixture.raw)
    value["bundled_audio_runtime"] = flag
    (fixture.root / "bundle-manifest.json").write_bytes(_bytes(value))
    with pytest.raises(ValueError, match="optional runtime flag|unannounced audio-runtime"):
        fixture.installer.verify(fixture.root)


@pytest.mark.parametrize("change", ["missing", "unlisted", "mixed", "wrong-case"],
                         ids=["missing-file", "unlisted-file", "both-layouts", "root-case"])
def test_installer_rejects_incomplete_unlisted_and_ambiguous_audio_layouts(audio_bundle, change):
    fixture = audio_bundle
    if change == "missing":
        (fixture.runtime / "bin/ffprobe.exe").unlink()
        _write_outer(fixture.root)
    elif change == "unlisted":
        (fixture.runtime / "bin/unknown.dll").write_bytes(_pe())
    elif change == "mixed":
        # A complete declared legacy inventory is still not an alternative
        # selection when the canonical runtime is already present.
        for name in ("bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE"):
            _put(fixture.root / "audio", name, b"Inert legacy inventory.\n")
        _write_outer(fixture.root, bundled_audio=True)
    else:
        moved = fixture.root / "audio-moving"
        fixture.runtime.rename(moved)
        moved.rename(fixture.root / "Audio-Runtime")
        _write_outer(fixture.root)
    with pytest.raises(ValueError, match="runtime file|layouts|ambiguous"):
        fixture.installer.verify(fixture.root)


def test_optional_inventory_read_error_is_not_treated_as_an_empty_directory(audio_bundle, monkeypatch):
    fixture = audio_bundle
    original = fixture.installer.os.scandir

    def denied(path):
        if Path(path) == fixture.runtime:
            raise PermissionError("Synthetic actual inventory denial.")
        return original(path)

    monkeypatch.setattr(fixture.installer.os, "scandir", denied)
    with pytest.raises(PermissionError, match="actual inventory denial"):
        fixture.installer.verify(fixture.root)



@pytest.mark.parametrize("error", [PermissionError, NotADirectoryError], ids=["unreadable-root", "non-directory-parent"])
def test_unannounced_optional_runtime_lstat_errors_cannot_be_downgraded_to_absence(audio_bundle, monkeypatch, error):
    fixture = audio_bundle
    value = json.loads(fixture.raw)
    value["bundled_audio_runtime"] = False
    value["files"] = {name: digest for name, digest in value["files"].items()
                      if not name.startswith("audio-runtime/")}
    (fixture.root / "bundle-manifest.json").write_bytes(_bytes(value))
    original_lstat, original_lexists = Path.lstat, fixture.installer.os.path.lexists
    visited = []

    def denied(path, *args, **kwargs):
        if path == fixture.runtime:
            visited.append(path)
            raise error("Synthetic declared absence cannot hide this root error.")
        return original_lstat(path, *args, **kwargs)

    def swallowed(path):
        # Model lexists' documented suppression of stat errors. The new
        # inventory must use the actual lstat boundary even for a false flag.
        if Path(path) == fixture.runtime:
            return False
        return original_lexists(path)

    monkeypatch.setattr(Path, "lstat", denied)
    monkeypatch.setattr(fixture.installer.os.path, "lexists", swallowed)
    with pytest.raises(error, match="cannot hide this root error"):
        fixture.installer.verify(fixture.root)
    assert visited == [fixture.runtime]


def test_empty_unannounced_optional_runtime_is_present_and_is_rejected(audio_bundle):
    fixture = audio_bundle
    shutil.rmtree(fixture.runtime)
    fixture.runtime.mkdir()
    _write_outer(fixture.root, bundled_audio_runtime=False)
    with pytest.raises(ValueError, match="unannounced audio-runtime"):
        fixture.installer.verify(fixture.root)

def test_optional_inventory_is_bounded_before_unlisted_tree_can_be_accepted(audio_bundle, monkeypatch):
    fixture = audio_bundle
    monkeypatch.setattr(fixture.installer, "MAX_OPTIONAL_ENTRIES", 1)
    with pytest.raises(ValueError, match="file limit"):
        fixture.installer.verify(fixture.root)


def test_duplicate_outer_identity_is_rejected_before_runtime_admission(audio_bundle):
    fixture = audio_bundle
    raw = b'{"schema":1,' + fixture.raw.lstrip()[1:]
    with pytest.raises(ValueError, match="Duplicate"):
        fixture.installer.verify(fixture.root, manifest_bytes=raw)


def _native_supplement(tmp_path, monkeypatch):
    root = tmp_path / "native-supplement"
    root.mkdir()
    binary = _pe()
    pins = {name: (len(binary), hashlib.sha256(binary).hexdigest()) for name in native_runtime.DLLS}
    monkeypatch.setattr(native_runtime, "DLLS", pins)
    for name in native_runtime.FILES:
        _put(root, name, binary if name.startswith("bin/") else b"Inert native supplement fixture.\n")
    source = {"selected_original_files": {
        "Library/bin/" + name: {"destination": "bin/" + name, "size": size, "sha256": digest}
        for name, (size, digest) in pins.items()}}
    lock_path = _put(root, "provenance/source-lock.json", _bytes(source))
    monkeypatch.setattr(native_runtime, "SOURCE_LOCK_SHA256", _sha(lock_path))
    inner = {
        "schema": 1, "target": "Windows x64", "use_scope": "private-local-installation",
        "source_lock_sha256": native_runtime.SOURCE_LOCK_SHA256,
        "files": {name: _sha(root / name) for name in native_runtime.FILES},
    }
    _put(root, native_runtime.MANIFEST, _bytes(inner))
    native_runtime.verify_native_runtime(root)
    return root


@pytest.fixture
def attach_bundle(audio_bundle, tmp_path, monkeypatch):
    fixture = audio_bundle
    (fixture.root / "wheelhouse/fixture.whl").unlink()
    wheel = fixture.root / "wheelhouse/universal_research_library-1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("carr_library/native_runtime.py", b'raise AssertionError("Never import fixture payload")\n')
        archive.writestr("carr_library/native_checks.py", b'raise AssertionError("Never import fixture payload")\n')
    _put(fixture.root, "runtime/python.exe", _pe())
    host = {}
    for name in native_runtime.HOST_DLLS:
        target = _put(fixture.root, "runtime/" + name, b"Inert host compatibility fixture: " + name.encode("ascii"))
        host[name] = _sha(target)
    monkeypatch.setattr(native_runtime, "HOST_DLLS", host)
    fixture.raw = _write_outer(fixture.root, bundled_python=True)
    fixture.supplement = _native_supplement(tmp_path, monkeypatch)
    fixture.adder = _load("audio_bundle_convergence_native_adder", ROOT / "packaging/add_native_runtime.py")
    fixture.output = tmp_path / "new-private-bundle"
    return fixture


def _observe_actual_installer(monkeypatch, after_verify):
    """Retain the real verifier; insert only an after-verification race."""
    original = importlib.machinery.SourceFileLoader.exec_module

    def execute(loader, module):
        original(loader, module)
        if module.__name__ == "carr_bundle_installer":
            verify = module.verify

            def checked(root, **kwargs):
                result = verify(root, **kwargs)
                after_verify(root, kwargs)
                return result

            module.verify = checked

    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", execute)


def test_native_attach_uses_captured_manifest_and_preserves_canonical_audio(attach_bundle, monkeypatch, tmp_path):
    fixture = attach_bundle
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_bytes(b"Do not collect or change these external bytes.")
    seen = []

    def replace(root, kwargs):
        if Path(root) == fixture.root:
            assert kwargs["manifest_bytes"] == fixture.raw
            changed = json.loads(fixture.raw)
            changed["files"]["../outside-sentinel"] = _sha(sentinel)
            (fixture.root / "bundle-manifest.json").write_bytes(_bytes(changed))
            seen.append(True)

    _observe_actual_installer(monkeypatch, replace)
    report = fixture.adder.attach_runtime(fixture.root, fixture.supplement, fixture.output)
    assert seen == [True]
    assert report["source_manifest_sha256"] == hashlib.sha256(fixture.raw).hexdigest()
    assert report["native_windows_execution"] == "NOT_RUN"
    assert sentinel.read_bytes() == b"Do not collect or change these external bytes."
    result = json.loads((fixture.output / "bundle-manifest.json").read_bytes())
    assert "../outside-sentinel" not in result["files"]
    assert result["bundled_native_runtime"] is True
    assert result["bundled_audio_runtime"] is True
    assert result["bundled_audio"] is False
    fixture.installer.verify(fixture.output)
    assert audio_runtime.inspect_bundled_audio(fixture.output)["manifest_sha256"] == fixture.report["manifest_sha256"]


def test_native_attach_never_publishes_acceptance_manifest_before_all_payload(attach_bundle, monkeypatch):
    fixture = attach_bundle
    original_iterdir = Path.iterdir
    original_move = shutil.move

    def manifest_first(path):
        items = list(original_iterdir(path))
        if path.name == "bundle" and path.parent.name.startswith("carr-native-bundle-"):
            items.sort(key=lambda item: item.name != "bundle-manifest.json")
        return iter(items)

    def fail_payload(source, destination, *args, **kwargs):
        if Path(destination).parent == fixture.output and Path(source).name != "bundle-manifest.json":
            raise OSError("Synthetic interrupted payload publication.")
        return original_move(source, destination, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", manifest_first)
    monkeypatch.setattr(shutil, "move", fail_payload)
    with pytest.raises(OSError, match="interrupted payload"):
        fixture.adder.attach_runtime(fixture.root, fixture.supplement, fixture.output)
    assert fixture.output.is_dir()
    assert not (fixture.output / "bundle-manifest.json").exists()
    assert (fixture.root / "bundle-manifest.json").read_bytes() == fixture.raw
