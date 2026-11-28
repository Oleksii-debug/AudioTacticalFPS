"""Prepare the hash-pinned optional FFmpeg runtime from an existing archive.

The existing prepare/CLI path stays offline. Explicit local setup may call
acquire_archive to fetch the one current fixed archive before using prepare.
No package installer, arbitrary extraction, engine selection or upload is used.
Windows --verify-native is required for native execution evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Support the source-tree build before the package is installed. This is a
# repository-owned path, not a path taken from an archive or user project.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from carr_library.audio.runtime import (ARCHIVE_MEMBERS, FFMPEG_ARCHIVE_BYTES,
    FFMPEG_ARCHIVE_SHA256, FFMPEG_ARCHIVE_URL, FFMPEG_SOURCE_COMMIT,
    FFMPEG_VERSION, REQUIRED_FILTERS, file_sha256, regular_file,
    trusted_directory, verify_runtime)



# The optional network boundary is used only by explicit local setup. These are
# the publisher and its release-asset host, not arbitrary runtime/manifest URLs.
DOWNLOAD_HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com'))
DOWNLOAD_FILENAME = 'ffmpeg-pinned.zip'
DOWNLOAD_SECONDS = 600
DOWNLOAD_SOCKET_SECONDS = 30
_DOWNLOAD_CHUNK = 1024 * 1024


def _download_url(value):
    if (type(value) is not str or not value or len(value) > 8192
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or '\\' in value):
        raise ValueError('Expected the fixed HTTPS audio archive URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError('Audio downloads require the approved HTTPS publisher or release-asset host.')
    return value


class _ApprovedRedirects(urllib.request.HTTPRedirectHandler):
    max_repeats = 2
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib calls this before opening the redirected connection.
        try:
            _download_url(newurl)
        except BaseException:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_opener():
    return urllib.request.build_opener(_ApprovedRedirects())


def _directory_owner(directory):
    from carr_library.audio.jobs import checked_directory
    info = checked_directory(directory).stat()
    return info.st_dev, info.st_ino


def acquire_archive(directory, *, cancelled=lambda: False):
    """Explicitly acquire the one fixed archive without adopting any old file.

    Stream the response into an exclusive owned partial, then use the existing
    receipt-bound no-replace publisher. No HTTP-provided name, path, command or
    executable is accepted. A completed download alone is not native evidence.
    """
    from carr_library.audio.jobs import checked_directory, publish_new
    selected = Path(directory)
    if not selected.is_absolute() or '..' in selected.parts:
        raise ValueError('Choose an absolute local audio download directory.')
    directory = checked_directory(selected)
    owner = _directory_owner(directory)
    destination = directory / DOWNLOAD_FILENAME
    if os.path.lexists(destination):
        raise FileExistsError('The fixed archive destination already exists; choose a new folder.')
    started = time.monotonic()

    def check():
        if cancelled():
            raise InterruptedError('Audio acquisition was cancelled.')
        if time.monotonic() - started >= DOWNLOAD_SECONDS:
            raise TimeoutError('The fixed audio download exceeded its elapsed-time budget.')

    check()
    request = urllib.request.Request(_download_url(FFMPEG_ARCHIVE_URL),
        headers={'User-Agent': 'CARR-explicit-local-audio/1',
                 'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'})
    descriptor, name = tempfile.mkstemp(prefix='.ffmpeg-download-', suffix='.partial', dir=directory)
    partial = Path(name)
    identity = None
    digest, count, final_host = hashlib.sha256(), 0, None
    try:
        with os.fdopen(descriptor, 'wb') as target:
            info = os.fstat(target.fileno())
            identity = info.st_dev, info.st_ino
            check()
            try:
                response = _download_opener().open(request, timeout=DOWNLOAD_SOCKET_SECONDS)
            except urllib.error.HTTPError as error:
                error.close()
                raise ValueError('The audio server refused the fixed archive request.') from None
            with response:
                final_url = _download_url(response.geturl())
                final_host = urllib.parse.urlsplit(final_url).hostname
                if response.status != 200 or response.headers.get('Content-Range') is not None:
                    raise ValueError('The audio server did not return one complete archive.')
                encodings = response.headers.get_all('Content-Encoding', [])
                if encodings and encodings != ['identity']:
                    raise ValueError('The audio server changed the pinned byte representation.')
                lengths = response.headers.get_all('Content-Length', [])
                if lengths and (len(lengths) != 1 or not lengths[0].isascii()
                        or not lengths[0].isdigit() or len(lengths[0]) > 20
                        or int(lengths[0]) != FFMPEG_ARCHIVE_BYTES):
                    raise ValueError('The audio response length differs from the fixed archive.')
                while True:
                    check()
                    chunk = response.read(min(_DOWNLOAD_CHUNK, FFMPEG_ARCHIVE_BYTES - count + 1))
                    check()
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > FFMPEG_ARCHIVE_BYTES:
                        raise ValueError('The audio download exceeded its fixed byte limit.')
                    digest.update(chunk)
                    target.write(chunk)
            if count != FFMPEG_ARCHIVE_BYTES or digest.hexdigest() != FFMPEG_ARCHIVE_SHA256:
                raise ValueError('The downloaded audio archive size or SHA256 differs from the fixed release.')
            target.flush()
            os.fsync(target.fileno())
        check()
        if _directory_owner(directory) != owner:
            raise ValueError('The selected audio download directory was replaced.')
        # publish_new verifies the bytes copied against this digest while its
        # immutable source reader is held, then commits without replacing a file.
        publish_new(partial, destination, expected_sha256=FFMPEG_ARCHIVE_SHA256,
                    cancelled=check)
        return {'schema': 1, 'status': 'downloaded', 'archive': str(destination),
                'archive_url': FFMPEG_ARCHIVE_URL, 'archive_bytes': count,
                'archive_sha256': digest.hexdigest(), 'final_host': final_host,
                'native_verification': 'NOT_RUN'}
    finally:
        # A concurrent replacement at the old partial path is not our file.
        try:
            info = partial.lstat()
        except FileNotFoundError:
            pass
        else:
            if identity is not None and (info.st_dev, info.st_ino) == identity:
                partial.unlink()


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
