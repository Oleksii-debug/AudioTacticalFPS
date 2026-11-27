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


def verify(root, *, manifest_bytes=None):
    # Packaging callers can bind verification to the exact snapshot they seal,
    # without reopening a manifest that another operation might replace.
    manifest = json.loads(manifest_bytes if manifest_bytes is not None else
                          (root / "bundle-manifest.json").read_text(encoding="utf-8"))
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
    native = root / "native-runtime"
    included = manifest.get("bundled_native_runtime", False)
    expected_native = {p for p in files if p.startswith("native-runtime/")}
    if type(included) is not bool:
        raise ValueError("invalid native runtime installation flag")
    if included:
        if not manifest.get("bundled_python"):
            raise ValueError("native runtime requires bundled Python")
        for directory, dirs, _names in os.walk(native, followlinks=False):
            for name in dirs:
                info = (Path(directory) / name).lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError("linked native runtime directories are not supported")
        actual = {p.relative_to(root).as_posix() for p in native.rglob("*") if p.is_file()}
        required_native = {"native-runtime/native-manifest.json",
                           "native-runtime/bin/msvcp140.dll", "native-runtime/bin/msvcp140_1.dll"}
        if not required_native.issubset(actual) or actual != expected_native:
            raise ValueError("unexpected or missing native runtime file")
    elif os.path.lexists(native) or expected_native:
        raise ValueError("unannounced native runtime files")
    audio = root / "audio-runtime"
    included_audio = manifest.get("bundled_audio_runtime", False)
    expected_audio = {p for p in files if p.startswith("audio-runtime/")}
    if type(included_audio) is not bool:
        raise ValueError("invalid audio runtime installation flag")
    if included_audio:
        shared = any(os.path.lexists(audio / name) for name in
                     ("audio-manifest.json", "bin", "provenance/source-lock.json"))
        if shared:
            # Bootstrap uses only stdlib before the reviewed wheel is installed.
            # The installed audio_runtime verifier separately binds the exact
            # source lock, every native byte and rights-review profile.
            visited_audio = 0
            for directory, dirs, _names in os.walk(audio, followlinks=False):
                visited_audio += len(dirs) + len(_names)
                if visited_audio > 4096:
                    raise ValueError("audio runtime inventory exceeds its bounded size")
                for name in dirs:
                    info = (Path(directory) / name).lstat()
                    if (not stat.S_ISDIR(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        raise ValueError("linked audio runtime directories are not supported")
            actual_audio = {p.relative_to(root).as_posix() for p in audio.rglob("*") if p.is_file()}
            required_audio = {"audio-runtime/" + name for name in (
                "audio-manifest.json", "provenance/source-lock.json",
                "bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/ffplay.exe")}
            if not required_audio.issubset(actual_audio) or actual_audio != expected_audio:
                raise ValueError("unexpected or missing shared audio runtime file")
            if not 0 < (audio / "audio-manifest.json").stat().st_size <= 8 * 1024 * 1024:
                raise ValueError("audio runtime manifest exceeds its bounded size")
            inner = json.loads((audio / "audio-manifest.json").read_text(encoding="utf-8"))
            if (type(inner) is not dict or type(inner.get("schema")) is not int
                    or inner["schema"] != 1 or type(inner.get("files")) is not dict
                    or {"audio-runtime/" + name: digest for name, digest in inner["files"].items()}
                    != {name: files[name] for name in expected_audio
                        if name != "audio-runtime/audio-manifest.json"}):
                raise ValueError("shared audio runtime manifest differs from the installation inventory")
        else:
            required_audio = {"audio-runtime/" + name for name in (
                "ffmpeg.exe", "FFmpeg-LICENSE.txt", "FFmpeg-README.txt", "ffmpeg-runtime.json")}
            if expected_audio != required_audio or {"audio-runtime/" + p.name for p in audio.iterdir()} != required_audio:
                raise ValueError("unexpected or missing audio runtime file")
    elif os.path.lexists(audio) or expected_audio:
        raise ValueError("unannounced audio runtime files")
    included_launcher = manifest.get("bundled_native_launcher", False)
    launcher_files = {"ResearchLibrary.exe", "launcher-provenance.json"}
    if type(included_launcher) is not bool:
        raise ValueError("invalid native launcher installation flag")
    if included_launcher:
        if not manifest.get("bundled_python") or not launcher_files.issubset(files):
            raise ValueError("incomplete native launcher installation inventory")
    elif launcher_files.intersection(files) or any(os.path.lexists(root / name) for name in launcher_files):
        raise ValueError("unannounced native launcher files")


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
    subprocess.run([str(executable), "-B", "-X", "utf8", "-s", "-P", "-m", "ensurepip", "--upgrade", "--default-pip"],
                   check=True, env=bootstrap_environment, cwd=destination)
    subprocess.run([str(executable), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--no-index", "--only-binary=:all:", "--require-hashes",
                    "--no-cache-dir", "--find-links", str(root / "wheelhouse"),
                    "-r", str(root / "install.lock")], check=True, env=environment)
    subprocess.run([str(executable), "-I", "-B", "-X", "utf8", "-m",
                    "carr_library.native_checks", "--bundle-root", str(root)],
                   check=True, env=environment, cwd=root)
    subprocess.run([str(executable), "-I", "-B", "-X", "utf8", "-m", "carr_library", "--self-test"],
                   check=True, env=environment, cwd=root)
    # An incomplete or interrupted install must never be treated as ready.
    # Exclusive creation is the final lifecycle step; never delete an old environment.
    (destination / ".installation-complete").touch(exist_ok=False)
    entry = "ResearchLibrary.exe" if (root / "ResearchLibrary.exe").is_file() else "ResearchLibrary.cmd"
    print(f"Installation complete. Open {entry}. Research data has not been changed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Installation could not finish: {error}", file=sys.stderr)
        raise SystemExit(1)
