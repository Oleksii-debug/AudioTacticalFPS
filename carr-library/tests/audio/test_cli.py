import json
from pathlib import Path
import shutil

import pytest

from carr_library.__main__ import main


def test_audio_help_does_not_open_or_create_a_library(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as result:
        main(["audio", "--help"])
    assert result.value.code == 0
    assert "No remote calls" in capsys.readouterr().out
    assert not tuple(tmp_path.iterdir())


def test_actual_cli_create_render_verify_and_backup_roundtrip(tmp_path, capsys):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Actual FFmpeg runtime is required for this integration test.")
    from carr_library.audio.voices import FFmpegFliteProvider
    try:
        voices = FFmpegFliteProvider(Path(shutil.which("ffmpeg"))).list_voices()
    except RuntimeError:
        pytest.skip("This local FFmpeg build has no Flite engine.")
    if not voices:
        pytest.skip("This local FFmpeg build has no Flite voice.")
    root = tmp_path / "author-project"
    assert main(["audio", "create", "--text", "Narrator: This is a local production.",
                 "--directory", str(root), "--voice", f"flite:{voices[0].id}", "--no-stems"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "saved"
    assert main(["audio", "render", "--project", str(root)]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["status"] == "complete" and rendered["frames"] > 0
    assert main(["audio", "verify", "--production", str(Path(rendered["manifest"]).parent)]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["local_only"] is True
    assert verified["quality"]["integrated_lufs"] is not None
    assert main(["audio", "backup", "--project", str(root), "--output", str(tmp_path / "backup")]) == 0
    backup = json.loads(capsys.readouterr().out)
    assert main(["audio", "restore", "--backup", str(tmp_path / "backup"),
                 "--directory", str(tmp_path / "restored"), "--manifest-sha256", backup["manifest_sha256"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_cli_malformed_project_fails_with_readable_error(tmp_path, capsys):
    (tmp_path / "project.json").write_text('{"shell":"run external commands"}')
    assert main(["audio", "show", "--project", str(tmp_path)]) == 1
    assert "unsupported fields" in capsys.readouterr().err
