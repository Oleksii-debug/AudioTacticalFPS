"""Prepare the hash-pinned optional FFmpeg runtime from an existing archive.

No download, package installer, arbitrary archive extraction or public upload
is performed. In Windows CI pass --verify-native before sealing the private
application package. Linux preparation verifies bytes only, never Windows use.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

# Support the source-tree build before the package is installed. This is a
# repository-owned path, not a path taken from an archive or user project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from carr_library.audio.runtime import (ARCHIVE_MEMBERS, FFMPEG_ARCHIVE_BYTES,
    FFMPEG_ARCHIVE_SHA256, FFMPEG_ARCHIVE_URL, FFMPEG_SOURCE_COMMIT,
    FFMPEG_VERSION, REQUIRED_FILTERS, file_sha256, regular_file,
    trusted_directory, verify_runtime)


def _native_check(directory: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("Native Windows FFmpeg verification must run on Windows.")
    binary = directory / "ffmpeg.exe"
    records = {}
    for option in ("-version", "-filters", "-L"):
        result = subprocess.run([str(binary), "-hide_banner", option],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        combined = result.stdout + result.stderr
        if len(combined) > 512 * 1024:
            raise ValueError("FFmpeg verification output exceeded its limit.")
        records[option] = combined.decode("utf-8", "replace")
    if f"ffmpeg version {FFMPEG_VERSION}" not in records["-version"]:
        raise ValueError("FFmpeg runtime version does not match the selected build.")
    present = {parts[1] for line in records["-filters"].splitlines() if len(parts := line.split()) >= 2}
    missing = set(REQUIRED_FILTERS) - present
    if missing:
        raise ValueError("FFmpeg lacks required audio filters: " + ", ".join(sorted(missing)))
    if "GNU General Public License" not in records["-L"]:
        raise ValueError("FFmpeg did not report its expected GPL license.")
    helpers = {}
    for name in ('ffplay', 'ffprobe'):
        probe = subprocess.run([str(directory / (name + '.exe')), '-version'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(probe.stdout) + len(probe.stderr) > 512 * 1024:
            raise ValueError('Audio helper verification exceeded its output budget.')
        report = (probe.stdout + probe.stderr).decode('utf-8', 'replace')
        if f'{name} version {FFMPEG_VERSION}' not in report:
            raise ValueError('Audio helper version differs from the selected build.')
        helpers[name] = report.splitlines()[0]
    return {"status": "PASS", "platform": "Windows", "version": records["-version"].splitlines()[0],
            "helpers": helpers,
            "required_filters": list(REQUIRED_FILTERS), "filter_count": len(present),
            "license_report": records["-L"]}


def prepare(archive, destination, *, verify_native: bool = False) -> dict:
    archive = Path(archive).absolute()
    regular_file(archive)
    if archive.stat().st_size != FFMPEG_ARCHIVE_BYTES or file_sha256(archive) != FFMPEG_ARCHIVE_SHA256:
        raise ValueError("FFmpeg archive size or SHA256 differs from the pinned reviewed release.")
    target = Path(destination).absolute()
    parent = trusted_directory(target.parent)
    if target.name in (".", "..") or target.name.rstrip(" .") != target.name:
        raise ValueError("Choose an unambiguous audio-runtime directory.")
    if target.exists() or target.is_symlink():
        manifest = verify_runtime(target)
        if verify_native:
            manifest["native_validation"] = _native_check(target)
            _manifest(target, manifest)
        return manifest
    manifest = {"schema": 1, "name": "FFmpeg Windows full", "version": FFMPEG_VERSION,
                "platform": "Windows x86-64", "distribution": "private personal-use package",
                "archive_url": FFMPEG_ARCHIVE_URL, "archive_sha256": FFMPEG_ARCHIVE_SHA256,
                "source_commit": FFMPEG_SOURCE_COMMIT,
                "source_url": f"https://github.com/FFmpeg/FFmpeg/tree/{FFMPEG_SOURCE_COMMIT}",
                "build_provider": "https://www.gyan.dev/ffmpeg/builds/",
                "build_license": "GPL v3 (upstream static build)",
                "network_at_runtime": "Application supplies file-only input protocol allowlist.",
                "files": {name: {"sha256": identity[2], "bytes": identity[1]}
                          for name, identity in ARCHIVE_MEMBERS.items()},
                "native_validation": {"status": "NOT_RUN"}}
    with tempfile.TemporaryDirectory(prefix=".ffmpeg-prepare-", dir=parent) as name:
        stage = Path(name)
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > 1000 or len({item.filename for item in members}) != len(members):
                raise ValueError("Unexpected or duplicate members in the pinned FFmpeg archive.")
            for output_name, (member, expected_size, expected_sha) in ARCHIVE_MEMBERS.items():
                entry = source.getinfo(member)
                if entry.file_size != expected_size or entry.is_dir():
                    raise ValueError("Pinned FFmpeg archive member differs from the expected file.")
                target_file = stage / output_name
                target_file.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as src, target_file.open("xb") as dst:
                    copied = 0
                    while block := src.read(1024 * 1024):
                        copied += len(block)
                        if copied > expected_size:
                            raise ValueError("FFmpeg archive member exceeded its fixed size.")
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                if copied != expected_size or file_sha256(target_file) != expected_sha:
                    raise ValueError("Extracted FFmpeg runtime failed its fixed hash check.")
        if verify_native:
            manifest["native_validation"] = _native_check(stage)
        _manifest(stage, manifest)
        # Reserve the destination atomically. A complete manifest is moved last;
        # incomplete/crashed preparations fail closed at application startup.
        target.mkdir(mode=0o700)
        created = []
        try:
            for filename in (*ARCHIVE_MEMBERS, "ffmpeg-runtime.json"):
                destination_file = target / filename
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                with (stage / filename).open("rb") as src, destination_file.open("xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                created.append(destination_file)
            verify_runtime(target)
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for directory in sorted((p for p in target.rglob('*') if p.is_dir()),
                                    key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                target.rmdir()
            except OSError:
                pass
            raise
    return manifest


def _manifest(directory: Path, value: dict):
    descriptor, name = tempfile.mkstemp(prefix=".ffmpeg-manifest-", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, directory / "ffmpeg-runtime.json")
    finally:
        path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, help=f"Existing pinned Gyan FFmpeg {FFMPEG_VERSION} full ZIP")
    parser.add_argument("--destination", required=True, help="New private package audio-runtime folder")
    parser.add_argument("--verify-native", action="store_true", help="Require actual Windows version/filter/license probes")
    args = parser.parse_args(argv)
    result = prepare(args.archive, args.destination, verify_native=args.verify_native)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
