"""Pinned optional Windows FFmpeg runtime identity; no downloads at startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
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
                    "sine", "adelay", "loudnorm", "asetnsamples")


def regular_file(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
        raise ValueError("The audio runtime requires regular files without links or reparse points.")


def trusted_directory(path: Path) -> Path:
    path = path.absolute()
    if str(path).startswith(("\\\\", "//")) or any(":" in part for part in path.parts[1:]):
        raise ValueError("The audio runtime requires a local directory, without network shares or alternate streams.")
    for parent in (path, *path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or bool(getattr(info, "st_file_attributes", 0) & 0x400)):
            raise ValueError("The audio runtime requires local directories without redirected ancestors.")
    return path.resolve(strict=True)


def file_sha256(path: Path) -> str:
    regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_runtime(directory: str | os.PathLike) -> dict:
    """Verify the manifest and exact reviewed bytes before executing a bundle."""
    root = trusted_directory(Path(directory))
    manifest_path = root / "ffmpeg-runtime.json"
    regular_file(manifest_path)
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError("Audio runtime manifest exceeds its read budget.")
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("archive_sha256") != FFMPEG_ARCHIVE_SHA256
            or manifest.get("source_commit") != FFMPEG_SOURCE_COMMIT
            or set(manifest.get("files", {})) != set(ARCHIVE_MEMBERS)):
        raise ValueError("The audio runtime manifest does not match the reviewed build.")
    for name, (_, size, expected) in ARCHIVE_MEMBERS.items():
        path = root / name
        if (manifest["files"][name] != {"sha256": expected, "bytes": size}
                or path.stat().st_size != size or file_sha256(path) != expected):
            raise ValueError(f"The bundled audio runtime failed its integrity check: {name}.")
    return manifest


def bundled_ffmpeg() -> Path | None:
    """Use the known install root, never the current working directory."""
    root = Path(sys.prefix).parent / "audio-runtime"
    binary = root / "ffmpeg.exe"
    if not root.exists() and not root.is_symlink():
        return None
    verify_runtime(root)
    return binary


def resolve_ffmpeg(explicit: str | os.PathLike | None = None) -> Path | None:
    """Find a local runtime without downloads or a current-directory search.

    An explicit selection or CARR_FFMPEG is authoritative. An invalid choice
    fails rather than silently selecting a different executable. A discovered
    private Windows bundle is verified before its path is returned.
    """
    selected = explicit if explicit is not None else os.environ.get("CARR_FFMPEG")
    if selected is not None:
        from .model_manifest import local_path
        candidate = Path(selected)
        if not candidate.is_absolute():
            raise ValueError("Select FFmpeg using an absolute local executable path.")
        candidate = local_path(candidate)
        if not candidate.is_file():
            raise ValueError("The selected FFmpeg executable is not a regular file.")
        return candidate
    if sys.platform == "win32":
        bundled = bundled_ffmpeg()
        if bundled is not None:
            return bundled
    # Passing only a name to shutil.which may search the current directory on
    # Windows. Inspect explicit absolute PATH entries instead; empty/relative
    # entries are not application runtime locations.
    selected_path = None
    filename = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for directory in os.get_exec_path():
        directory = Path(directory)
        if not directory.is_absolute():
            continue
        found = shutil.which(str(directory / filename))
        if found is not None:
            selected_path = Path(found)
            break
    if selected_path is None:
        return None
    # PATH is an installation decision. Canonicalize links before applying the
    # same local-file boundary used by the rest of the production pipeline.
    from .model_manifest import local_path
    candidate = local_path(selected_path.resolve(strict=True))
    if not candidate.is_file():
        raise ValueError("The installed FFmpeg executable is not a regular file.")
    return candidate
