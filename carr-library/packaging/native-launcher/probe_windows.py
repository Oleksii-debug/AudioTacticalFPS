"""Execute the exact launcher on Windows using an explicitly synthetic child.

No installed user application, private model, research library or signing key is
used. This establishes native process behavior, not full app/NVDA acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


def run_probe(launcher, child):
    if os.name != "nt":
        raise RuntimeError("The native launch probe must run on Windows; a cross build is not execution")
    results = []
    with TemporaryDirectory(prefix="CARR launcher Київ & spaces ") as temporary:
        home = Path(temporary)
        protected = home / "research-data"
        protected.mkdir()
        sentinel = protected / "preserve.txt"
        sentinel.write_bytes(b"existing research bytes stay untouched")
        environment = dict(os.environ, LOCALAPPDATA=str(protected))

        def fixture(name, ready=False, fresh=False):
            root = home / name
            root.mkdir()
            shutil.copyfile(launcher, root / "ResearchLibrary.exe")
            if ready:
                (root / ".venv/Scripts").mkdir(parents=True)
                shutil.copyfile(child, root / ".venv/Scripts/python.exe")
                (root / ".venv/.installation-complete").touch()
            if fresh:
                (root / "runtime").mkdir()
                shutil.copyfile(child, root / "runtime/python.exe")
                (root / "install_windows.py").write_text("# Synthetic CI installation marker only.\n")
            return root

        def execute(root, args=(), expected=0, extra_env=None):
            process = subprocess.run([str(root / "ResearchLibrary.exe"), *args], capture_output=True,
                                     env={**environment, **(extra_env or {})}, timeout=20)
            assert process.returncode == expected, (args, process.returncode, process.stderr.decode("utf-8", "replace"))
            return process

        def arguments(root):
            return [line[4:] for line in (root / "probe-app.txt").read_text(encoding="utf-16le").splitlines()
                    if line.startswith("ARG=")]

        ready = fixture("Готова бібліотека (A & B)", ready=True)
        for path in ("models/minilm-manifest.json", "ocr/ocr-manifest.json", "audio-runtime/ffmpeg.exe", "citation-runtime/node.exe"):
            target = ready / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic availability marker")
        execute(ready, ["--self-test"])
        args = arguments(ready)
        assert args[:7] == [str(ready / ".venv/Scripts/python.exe"), "-I", "-B", "-X", "utf8", "-m", "carr_library"]
        assert args[-1] == "--self-test" and args[7:11] == ["--model-dir", str(ready / "models/minilm"),
                                                          "--model-manifest", str(ready / "models/minilm-manifest.json")]
        report = (ready / "probe-app.txt").read_text(encoding="utf-16le")
        assert "HF_HUB_OFFLINE=\nVALUE=1" in report and "ORT_DISABLE_TELEMETRY=\nVALUE=1" in report
        assert str(ready / "audio-runtime") in report and str(ready / "citation-runtime") in report
        assert str(ready / "ocr") in report
        results.append("unicode-spaces-fixed-argv-offline-optional-runtimes")
        before = (ready / "probe-app.txt").read_bytes()
        assert json.loads(execute(ready, ["--launcher-check"]).stdout)["state"] == "ready"
        assert (ready / "probe-app.txt").read_bytes() == before
        results.append("read-only-ready-probe")
        execute(ready, ["--check-installation"])
        assert arguments(ready)[5:8] == ["-m", "carr_library.native_checks", "--bundle-root"]
        results.append("fixed-installation-check")
        execute(ready, ["--self-test"], expected=17, extra_env={"CARR_LAUNCHER_PROBE_EXIT": "17"})
        results.append("child-exit-status-propagated")
        execute(ready, ["--self-test", "--arbitrary-command"], expected=64)
        results.append("extra-arguments-rejected")

        partial = fixture("Partial environment", fresh=True)
        (partial / ".venv").mkdir()
        (partial / ".venv/keep.txt").write_bytes(b"must remain")
        execute(partial, ["--self-test"], expected=3)
        assert (partial / ".venv/keep.txt").read_bytes() == b"must remain"
        assert not (partial / "probe-install.txt").exists()
        results.append("incomplete-state-preserved-without-reinstall")

        missing = fixture("Missing runtime")
        execute(missing, ["--self-test"], expected=4)
        assert not (missing / ".venv").exists()
        results.append("missing-runtime-does-not-search-path")

        fresh = fixture("Fresh offline package", fresh=True)
        assert json.loads(execute(fresh, ["--launcher-check"], expected=10).stdout)["state"] == "needs-install"
        assert not (fresh / ".venv").exists()
        execute(fresh)
        assert (fresh / "probe-install.txt").is_file() and (fresh / "probe-app.txt").is_file()
        assert (fresh / ".venv/.installation-complete").is_file()
        results.append("fresh-package-reuses-fixed-offline-installer")

        assert sentinel.read_bytes() == b"existing research bytes stay untouched"
        results.append("research-data-sentinel-preserved")
    return {"schema": 1, "status": "passed", "checks": results, "check_count": len(results),
            "launcher_sha256": hashlib.sha256(Path(launcher).read_bytes()).hexdigest(),
            "synthetic_child_sha256": hashlib.sha256(Path(child).read_bytes()).hexdigest(),
            "native_launcher_execution_verified": True, "actual_python_installation_verified": False,
            "nvda_verified": False, "authenticode_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--child", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = run_probe(args.launcher.resolve(strict=True), args.child.resolve(strict=True))
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
