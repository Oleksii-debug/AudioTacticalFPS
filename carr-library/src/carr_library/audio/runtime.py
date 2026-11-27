"""Pinned optional Windows FFmpeg runtime identity; no downloads at startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


FFMPEG_VERSION = "9.0.2"
FFMPEG_SOURCE_COMMIT = "946fcce07b6dcd0331c8cc609192aeff5e1924f8"
FFMPEG_ARCHIVE_URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
FFMPEG_ARCHIVE_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
FFMPEG_ARCHIVE_BYTES = 114768076
FFMPEG_WINDOWS_SHA256 = "3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec"
ARCHIVE_MEMBERS = {
    "ffmpeg.exe": ("ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe", 105423872, FFMPEG_WINDOWS_SHA256),
    "FFmpeg-LICENSE.txt": ("ffmpeg-9.0.2-essentials_build/LICENSE", 35147,
                           "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"),
    "FFmpeg-README.txt": ("ffmpeg-9.0.2-essentials_build/README.txt", 41577,
                          "0342de6bb39dd421cbec2c00d89ad274b640d75ad3f2cab09ffd647ce2d8f949"),
}
REQUIRED_FILTERS = ("aresample", "atrim", "asetpts", "volume", "highpass", "lowpass", "equalizer",
                    "acompressor", "agate", "afftdn", "aecho", "pan", "aformat", "afade",
                    "anullsrc", "concat", "apad", "amix", "asplit", "anull", "sidechaincompress", "alimiter",
                    "loudnorm")


def _local_absolute(value) -> Path:
    raw = os.fspath(value)
    path = Path(raw)
    if (not isinstance(raw, str) or raw.startswith(("\\\\", "//")) or "://" in raw
            or not path.is_absolute() or ".." in path.parts
            or ":" in raw[len(path.drive):]
            or any(ord(char) < 32 for char in raw)):
        raise ValueError("Audio runtime paths must be absolute local paths without traversal or streams.")
    return path


def regular_file(path: Path) -> None:
    path = _local_absolute(path)
    trusted_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
        raise ValueError("The audio runtime requires regular files without links or reparse points.")


def trusted_directory(path: Path) -> Path:
    path = _local_absolute(path)
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
            raise ValueError("The audio runtime requires local directories without redirected ancestors.")
    return path.resolve(strict=True)


def trusted_executable(path) -> Path:
    candidate = _local_absolute(path)
    regular_file(candidate)
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise ValueError("The selected local FFmpeg file is not executable.")
    return candidate


def file_sha256(path: Path) -> str:
    regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate audio runtime manifest field.")
        value[key] = item
    return value


def _shared_layout(root: Path) -> bool:
    """Select the reviewed shared layout by its markers, never by user flags."""
    return any(os.path.lexists(root / name) for name in
               ("audio-manifest.json", "bin", "provenance/source-lock.json"))


def runtime_inventory(directory: str | os.PathLike) -> dict[str, str]:
    """Return a verified copy inventory for either supported installation layout."""
    root = trusted_directory(Path(directory))
    if _shared_layout(root):
        from ..audio_runtime import MANIFEST, _inspect_audio_runtime
        report, files = _inspect_audio_runtime(root)
        return dict(files, **{MANIFEST: report["manifest_sha256"]})
    verify_runtime(root)
    return {name: file_sha256(root / name)
            for name in (*ARCHIVE_MEMBERS, "ffmpeg-runtime.json")}


def verify_runtime(directory: str | os.PathLike) -> dict:
    """Verify the manifest and exact reviewed bytes before executing a bundle."""
    root = trusted_directory(Path(directory))
    if _shared_layout(root):
        from ..audio_runtime import verify_audio_runtime
        return verify_audio_runtime(root)
    manifest_path = root / "ffmpeg-runtime.json"
    regular_file(manifest_path)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("Audio runtime manifest exceeds its read budget.")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream, object_pairs_hook=_unique)
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest.get("schema") != 1
            or manifest.get("archive_sha256") != FFMPEG_ARCHIVE_SHA256
            or manifest.get("source_commit") != FFMPEG_SOURCE_COMMIT
            or set(manifest.get("files", {})) != set(ARCHIVE_MEMBERS)):
        raise ValueError("The audio runtime manifest does not match the reviewed build.")
    for name, (_, size, expected) in ARCHIVE_MEMBERS.items():
        path = root / name
        if (manifest["files"][name] != {"sha256": expected, "bytes": size}
                or path.stat().st_size != size or file_sha256(path) != expected):
            raise ValueError(f"The bundled audio runtime failed its integrity check: {name}.")
    if {path.name for path in root.iterdir()} != {*ARCHIVE_MEMBERS, "ffmpeg-runtime.json"}:
        raise ValueError("The audio runtime contains unexpected files or directories.")
    return manifest


def verify_bundle_audio(bundle_root: str | os.PathLike) -> Path | None:
    """Bind the optional runtime to its enclosing sealed installation inventory."""
    bundle = trusted_directory(Path(bundle_root))
    root = bundle / "audio-runtime"
    if _shared_layout(root):
        from ..audio_runtime import bundled_audio_tools
        tools = bundled_audio_tools(bundle)
        return Path(tools["ffmpeg"]) if tools is not None else None
    manifest_path = bundle / "bundle-manifest.json"
    if not os.path.lexists(manifest_path):
        if os.path.lexists(root):
            raise ValueError("The audio runtime has no enclosing installation manifest.")
        return None
    regular_file(manifest_path)
    if not 0 < manifest_path.stat().st_size <= 8 * 1024 * 1024:
        raise ValueError("The installation manifest exceeds its read budget.")
    with manifest_path.open("r", encoding="utf-8") as source:
        outer = json.load(source, object_pairs_hook=_unique)
    if (type(outer) is not dict or type(outer.get("schema")) is not int
            or outer.get("schema") != 1 or type(outer.get("files")) is not dict):
        raise ValueError("Invalid enclosing audio installation manifest.")
    included = outer.get("bundled_audio_runtime", False)
    if type(included) is not bool:
        raise ValueError("Invalid bundled audio installation flag.")
    inventory = {name: digest for name, digest in outer["files"].items()
                 if isinstance(name, str) and name.casefold().startswith("audio-runtime/")}
    if not included:
        if inventory or os.path.lexists(root):
            raise ValueError("The installation contains unannounced audio runtime files.")
        return None
    verify_runtime(root)
    expected = {"audio-runtime/" + name: file_sha256(root / name)
                for name in (*ARCHIVE_MEMBERS, "ffmpeg-runtime.json")}
    if inventory != expected:
        raise ValueError("The audio runtime differs from the enclosing installation inventory.")
    return root / "ffmpeg.exe"


def bundled_ffmpeg() -> Path | None:
    """Use the known install root, never the current working directory."""
    return verify_bundle_audio(Path(sys.prefix).parent)


def find_ffmpeg(explicit=None) -> Path | None:
    """Find a reviewed bundle or an explicitly installed local executable.

    An invalid selected or bundled runtime fails closed. A project file never
    supplies an executable path; callers pass only an explicit user setting.
    Relative PATH entries and the working directory are not searched.
    """
    selected = explicit if explicit is not None else os.environ.get("CARR_FFMPEG")
    if selected is not None:
        return trusted_executable(selected)
    bundled = bundled_ffmpeg()
    if bundled is not None:
        return bundled
    filename = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    current = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            directory = trusted_directory(_local_absolute(entry))
        except (OSError, ValueError):
            continue
        if directory == current:
            continue
        candidate = directory / filename
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return trusted_executable(candidate)
    return None
