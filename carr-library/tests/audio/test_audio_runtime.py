import importlib.util
import json
import hashlib
import os
from pathlib import Path

import pytest

from carr_library.audio.runtime import (ARCHIVE_MEMBERS, FFMPEG_ARCHIVE_SHA256, FFMPEG_SOURCE_COMMIT,
                                        bundled_ffmpeg, verify_runtime)


def small_runtime(root, monkeypatch):
    """Synthetic bytes exercise verification, never claim actual FFmpeg execution."""
    import carr_library.audio.runtime as runtime
    root.mkdir()
    inventory = {}
    for name in runtime.ARCHIVE_MEMBERS:
        raw = ("fixture: " + name).encode()
        (root / name).write_bytes(raw)
        inventory[name] = (name, len(raw), hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(runtime, "ARCHIVE_MEMBERS", inventory)
    manifest = {"schema": 1, "archive_sha256": FFMPEG_ARCHIVE_SHA256,
                "source_commit": FFMPEG_SOURCE_COMMIT,
                "files": {name: {"bytes": item[1], "sha256": item[2]} for name, item in inventory.items()}}
    (root / "ffmpeg-runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def seal(root, *, included=True):
    value = {"schema": 1, "bundled_audio_runtime": included,
             "files": {"audio-runtime/" + path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in (root / "audio-runtime").iterdir()}}
    (root / "bundle-manifest.json").write_text(json.dumps(value), encoding="utf-8")
    return value


def test_bundle_discovery_uses_install_root_not_working_directory(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    install = tmp_path / "installed"
    install.mkdir()
    (install / ".venv").mkdir()
    cwd = tmp_path / "other"
    cwd.mkdir()
    (cwd / "audio-runtime").mkdir()
    (cwd / "audio-runtime" / "ffmpeg.exe").write_bytes(b"untrusted")
    monkeypatch.setattr(runtime.sys, "prefix", str(install / ".venv"))
    monkeypatch.chdir(cwd)
    assert bundled_ffmpeg() is None


def test_invalid_bundle_fails_before_execution(tmp_path):
    root = tmp_path / "audio-runtime"
    root.mkdir()
    data = {"schema": 1, "archive_sha256": FFMPEG_ARCHIVE_SHA256,
            "source_commit": FFMPEG_SOURCE_COMMIT,
            "files": {name: {"bytes": values[1], "sha256": values[2]} for name, values in ARCHIVE_MEMBERS.items()}}
    (root / "ffmpeg-runtime.json").write_text(json.dumps(data), encoding="utf-8")
    (root / "ffmpeg.exe").write_bytes(b"MZ replacement")
    with pytest.raises(ValueError, match="integrity"):
        verify_runtime(root)


def test_package_preparer_rejects_wrong_archive_before_destination(tmp_path):
    script = Path(__file__).resolve().parents[2] / "packaging" / "prepare_ffmpeg_runtime.py"
    spec = importlib.util.spec_from_file_location("audio_prepare_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    archive = tmp_path / "ffmpeg.zip"
    archive.write_bytes(b"not the reviewed archive")
    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="SHA256"):
        module.prepare(archive, destination)
    assert not destination.exists()


def test_verified_runtime_rejects_unlisted_files_and_directories(tmp_path, monkeypatch):
    root = tmp_path / "audio-runtime"
    small_runtime(root, monkeypatch)
    verify_runtime(root)
    extra = root / "unexpected.dll"
    extra.write_bytes(b"payload")
    with pytest.raises(ValueError, match="unexpected"):
        verify_runtime(root)
    extra.unlink()
    extra.mkdir()
    with pytest.raises(ValueError, match="unexpected"):
        verify_runtime(root)


@pytest.mark.parametrize("replacement", [True, "1", 2])
def test_runtime_schema_is_an_exact_integer(tmp_path, monkeypatch, replacement):
    root = tmp_path / "audio-runtime"
    manifest = small_runtime(root, monkeypatch)
    manifest["schema"] = replacement
    (root / "ffmpeg-runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed"):
        verify_runtime(root)


def test_runtime_rejects_duplicate_manifest_fields(tmp_path, monkeypatch):
    root = tmp_path / "audio-runtime"
    small_runtime(root, monkeypatch)
    path = root / "ffmpeg-runtime.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"schema": 1', '"schema": 0, "schema": 1'), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        verify_runtime(root)


def test_enclosing_installation_required_and_tampering_rejected(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    root = tmp_path / "audio-runtime"
    small_runtime(root, monkeypatch)
    with pytest.raises(ValueError, match="enclosing"):
        runtime.verify_bundle_audio(tmp_path)
    seal(tmp_path, included=False)
    with pytest.raises(ValueError, match="unannounced"):
        runtime.verify_bundle_audio(tmp_path)
    outer = seal(tmp_path)
    assert runtime.verify_bundle_audio(tmp_path) == root / "ffmpeg.exe"
    outer['files']['audio-runtime/ffmpeg.exe'] = '0' * 64
    (tmp_path / "bundle-manifest.json").write_text(json.dumps(outer), encoding="utf-8")
    with pytest.raises(ValueError, match="enclosing"):
        runtime.verify_bundle_audio(tmp_path)
    seal(tmp_path)
    (root / "ffmpeg.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        runtime.verify_bundle_audio(tmp_path)


def test_default_lookup_ignores_working_directory(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr(runtime.sys, "prefix", str(install / ".venv"))
    monkeypatch.delenv("CARR_FFMPEG", raising=False)
    current = tmp_path / "working"
    current.mkdir()
    monkeypatch.chdir(current)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    (current / name).write_bytes(b"must not execute")
    (current / name).chmod(0o700)
    monkeypatch.setenv("PATH", os.pathsep.join((".", str(current))))
    assert runtime.find_ffmpeg() is None


def test_selected_or_bundled_failure_never_falls_back(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    monkeypatch.delenv("CARR_FFMPEG", raising=False)
    with pytest.raises(ValueError, match="absolute"):
        runtime.find_ffmpeg("ffmpeg")
    monkeypatch.setattr(runtime.sys, "prefix", str(tmp_path / ".venv"))
    root = tmp_path / "audio-runtime"
    small_runtime(root, monkeypatch)
    seal(tmp_path)
    (root / "ffmpeg.exe").write_bytes(b"bad")
    with pytest.raises(ValueError, match="integrity"):
        runtime.find_ffmpeg()


@pytest.mark.parametrize("value", ['//server/share/ffmpeg.exe', r'\\server\share\ffmpeg.exe',
                                    '/tmp/ffmpeg.exe:payload', '../ffmpeg'])
def test_remote_and_ambiguous_executable_paths_rejected_before_lookup(value):
    from carr_library.audio.runtime import trusted_executable
    with pytest.raises(ValueError, match="absolute local"):
        trusted_executable(value)


def test_redirected_executable_rejected_without_execution(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    selected = tmp_path / "ffmpeg"
    selected.write_bytes(b"do not execute")
    selected.chmod(0o700)
    # Simulate an observed native reparse attribute without needing the
    # runner to grant permission to create actual Windows symlinks.
    original = runtime.Path.lstat
    from types import SimpleNamespace
    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=1, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(runtime.Path, "lstat", reparse)
    with pytest.raises(ValueError, match="reparse"):
        runtime.find_ffmpeg(selected)
