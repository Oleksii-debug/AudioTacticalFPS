import importlib.util
import json
from pathlib import Path

import pytest

from carr_library.audio.runtime import (ARCHIVE_MEMBERS, FFMPEG_ARCHIVE_SHA256, FFMPEG_SOURCE_COMMIT,
                                        bundled_ffmpeg, resolve_ffmpeg, verify_runtime)


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


def test_explicit_and_environment_discovery_do_not_execute_or_fall_back(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    explicit = tmp_path / "chosen.exe"
    environment = tmp_path / "environment.exe"
    explicit.write_bytes(b"explicitly selected, never executed during discovery")
    environment.write_bytes(b"environment selection")
    monkeypatch.setenv("CARR_FFMPEG", str(environment))
    monkeypatch.setattr(runtime, "bundled_ffmpeg", lambda: pytest.fail("unexpected bundled fallback"))
    assert resolve_ffmpeg(explicit) == explicit
    assert resolve_ffmpeg() == environment
    with pytest.raises(FileNotFoundError):
        resolve_ffmpeg(tmp_path / "missing.exe")
    monkeypatch.setenv("CARR_FFMPEG", "relative.exe")
    with pytest.raises(ValueError, match="absolute"):
        resolve_ffmpeg()


def test_discovery_refuses_working_directory_and_relative_path_entries(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    monkeypatch.delenv("CARR_FFMPEG", raising=False)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    monkeypatch.setattr(runtime, "bundled_ffmpeg", lambda: None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ffmpeg.exe").write_bytes(b"untrusted working-directory executable")
    monkeypatch.setenv("PATH", "." + runtime.os.pathsep + "")
    assert resolve_ffmpeg() is None


def test_invalid_windows_bundle_is_not_silently_replaced_by_path(tmp_path, monkeypatch):
    import carr_library.audio.runtime as runtime
    monkeypatch.delenv("CARR_FFMPEG", raising=False)
    monkeypatch.setattr(runtime.sys, "platform", "win32")
    install = tmp_path / "installed"
    (install / ".venv").mkdir(parents=True)
    (install / "audio-runtime").mkdir()
    monkeypatch.setattr(runtime.sys, "prefix", str(install / ".venv"))
    with pytest.raises(FileNotFoundError):
        resolve_ffmpeg()
