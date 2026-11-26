"""Install the reviewed wheel bundle offline in a new local environment."""
from __future__ import annotations

import sys

# Direct script launches must also leave the hash-verified runtime unchanged.
# Supplied launchers use -B from interpreter startup, before even this file runs.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import venv


def verify(root):
    manifest = json.loads((root / "bundle-manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files")
    if manifest.get("schema") != 1 or not isinstance(files, dict) or not files:
        raise ValueError("invalid bundle manifest")
    expected = {"install.lock", "install_windows.py", "Install.cmd", "ResearchLibrary.cmd",
                "CheckInstallation.cmd", "README.txt"}
    if not expected.issubset(files) or not any(p.startswith("wheelhouse/") for p in files):
        raise ValueError("incomplete installation bundle")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                or ":" in name or not relative.parts or "." in relative.parts):
            raise ValueError("invalid manifest path")
        path = root.joinpath(*relative.parts)
        for component in (path, *path.parents):
            info = component.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("linked bundle paths are not supported")
            if component == root:
                break
        if not path.is_file():
            raise ValueError("missing bundle file")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ValueError(f"bundle checksum failed: {name}")
    actual_wheels = {"wheelhouse/" + p.name for p in (root / "wheelhouse").iterdir()}
    if actual_wheels != {p for p in files if p.startswith("wheelhouse/")}:
        raise ValueError("unexpected or missing wheelhouse file")
    if manifest.get("bundled_python"):
        runtime = root / "runtime"
        actual = {p.relative_to(root).as_posix() for p in runtime.rglob("*") if p.is_file()}
        if ("runtime/python.exe" not in actual
                or actual != {p for p in files if p.startswith("runtime/")}):
            raise ValueError("unexpected or missing Python runtime file")
    if manifest.get("bundled_ocr"):
        ocr = root / "ocr"
        for directory, dirs, _names in os.walk(ocr, followlinks=False):
            for name in dirs:
                info = (Path(directory) / name).lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError("linked OCR runtime directories are not supported")
        actual = {p.relative_to(root).as_posix() for p in ocr.rglob("*") if p.is_file()}
        if ("ocr/ocr-manifest.json" not in actual or "ocr/tesseract.exe" not in actual
                or "ocr/tessdata/eng.traineddata" not in actual
                or actual != {p for p in files if p.startswith("ocr/")}):
            raise ValueError("unexpected or missing OCR runtime file")


def main():
    if os.name != "nt" or sys.version_info[:2] != (3, 11) or platform.architecture()[0] != "64bit":
        raise RuntimeError("Use Windows x64 with CPython 3.11 (py -3.11 install_windows.py).")
    root = Path(__file__).absolute().parent
    verify(root)
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise RuntimeError("Windows did not provide an absolute LOCALAPPDATA directory.")
    data = (Path(local) / "Universal Research Library").resolve()
    program = root.resolve()
    if data.is_relative_to(program) or program.is_relative_to(data):
        raise RuntimeError("Extract the program outside its research data directory.")
    destination = root / ".venv"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("This folder already has an environment. Extract a fresh package for a new installation.")
    print("Verifying complete. Creating the application environment.", flush=True)
    destination.mkdir(exist_ok=False)
    # Keep the hash-sealed base runtime unchanged. EnvBuilder's implicit pip
    # bootstrap does not carry the caller's -B flag into its child interpreter.
    # Launch the stdlib ensurepip entrypoint with a sanitized environment below.
    venv.EnvBuilder(with_pip=False, clear=False, symlinks=False).create(destination)
    executable = destination / "Scripts" / "python.exe"
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("PYTHON", "PIP_"))}
    environment["PIP_NO_INDEX"] = "1"
    if (root / "ocr" / "ocr-manifest.json").is_file():
        environment["CARR_OCR_DIR"] = str(root / "ocr")
    # Python 3.11 ensurepip forwards -I, but not -B, to its own pip subprocess.
    # Explicit -s/-P and these three controlled variables preserve no-user-site,
    # safe import paths and no-bytecode behavior across that nested process.
    bootstrap_environment = dict(environment, PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1",
                                 PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([str(executable), "-B", "-s", "-P", "-m", "ensurepip", "--upgrade", "--default-pip"],
                   check=True, env=bootstrap_environment, cwd=destination)
    subprocess.run([str(executable), "-I", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--no-index", "--only-binary=:all:", "--require-hashes",
                    "--no-cache-dir", "--find-links", str(root / "wheelhouse"),
                    "-r", str(root / "install.lock")], check=True, env=environment)
    subprocess.run([str(executable), "-I", "-B", "-m", "carr_library", "--self-test"],
                   check=True, env=environment, cwd=root)
    if (root / "ocr" / "ocr-manifest.json").is_file():
        subprocess.run([str(executable), "-I", "-B", "-m", "carr_library.extract.ocr_runtime", "--check"],
                       check=True, env=environment, cwd=root)
    print("Installation complete. Open ResearchLibrary.cmd. Research data has not been changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Installation could not finish: {error}", file=sys.stderr)
        raise SystemExit(1)
