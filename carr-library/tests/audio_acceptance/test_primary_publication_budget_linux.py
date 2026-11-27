"""Actual small-volume publication in an authorized private Linux namespace.

This separate capability test is not part of the strict Windows portable gate.
It retains the genuine mount, disk-space, PCM and resume assertions; unavailable
namespace authority is an explicit skip, never portable execution evidence.
"""
from pathlib import Path
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest


_PRIMARY_SCRIPT = Path(__file__).resolve().with_name("test_primary_publication_budget.py")
_spec = importlib.util.spec_from_file_location("_carr_primary_publication_worker", _PRIMARY_SCRIPT)
_primary = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _primary
_spec.loader.exec_module(_primary)
package_arguments = _primary.package_arguments


def test_actual_publication_preserves_destination_reserve_and_resumes_without_rewriting(tmp_path, request):
    if not sys.platform.startswith("linux"):
        pytest.skip("This real capacity test uses private Linux mount/user namespaces.")
    selected_package = package_arguments()
    ffmpeg, ffprobe = os.environ.get("CARR_FFMPEG") or shutil.which("ffmpeg"), os.environ.get("CARR_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe or not all(shutil.which(tool) for tool in ("unshare", "mount", "umount")):
        pytest.skip("Actual FFmpeg, ffprobe and private filesystem tools are required.")
    available = subprocess.run(["unshare", "-Urnm", "true"], capture_output=True, timeout=10)
    if available.returncode:
        pytest.skip("This host does not permit private user/mount namespaces.")
    evidence = tmp_path / "primary-reserve-evidence.json"
    command = ["unshare", "-Urnm", sys.executable, "-I", "-B", str(_PRIMARY_SCRIPT),
               *selected_package, "--work-parent", str(tmp_path), "--evidence", str(evidence),
               "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if evidence.is_file():
        request.node.user_properties.append(("actual_primary_disk_evidence", evidence.read_text(encoding="utf-8")))
    assert result.returncode == 0, result.stdout + result.stderr

