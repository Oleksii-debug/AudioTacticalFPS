"""Pinned optional Windows FFmpeg runtime identity; no downloads at startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys


FFMPEG_VERSION = '9.1'
FFMPEG_SOURCE_COMMIT = '74e4393595cf2b5857f0da925c4e0dd74ea306e5'
FFMPEG_ARCHIVE_URL = 'https://github.com/GyanD/codexffmpeg/releases/download/9.1/ffmpeg-9.1-full_build.zip'
FFMPEG_ARCHIVE_SHA256 = 'f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48'
FFMPEG_ARCHIVE_BYTES = 264268459
FFMPEG_WINDOWS_SHA256 = '3ab4a9c404504ebff6662d6a48661db952f88ee7fb7d5949a711cad7a763b2cf'
# Exact original publisher inventory reused from PR112 source-lock.json.
# Executables remain at the established runtime root; original docs are retained.
ARCHIVE_MEMBERS = {
    'ffmpeg.exe': ('ffmpeg-9.1-full_build/bin/ffmpeg.exe', 231078400,
        '3ab4a9c404504ebff6662d6a48661db952f88ee7fb7d5949a711cad7a763b2cf'),
    'ffplay.exe': ('ffmpeg-9.1-full_build/bin/ffplay.exe', 232610304,
        '35fa7c57b0c9e7fe64991f70dff52c5a273fce47d0fc329e7820362108590700'),
    'ffprobe.exe': ('ffmpeg-9.1-full_build/bin/ffprobe.exe', 230862848,
        'ff181a5b52664aae7a9f673c3a718440706b33511e8a3e38064ff4f21f1a7bed'),
    'FFmpeg-LICENSE.txt': ('ffmpeg-9.1-full_build/LICENSE', 35147,
        '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'),
    'FFmpeg-README.txt': ('ffmpeg-9.1-full_build/README.txt', 48006,
        '25b75c3ab76c07ddabc1d2312543b1d1795b1f745b349db4c18e58a77e448ec8'),
    'doc/bootstrap.min.css': ('ffmpeg-9.1-full_build/doc/bootstrap.min.css', 109483,
        '3cedecf2b8064b4a56ba47bda04544e1b21d71c83a12e7b5709e7c7976ead70e'),
    'doc/community.html': ('ffmpeg-9.1-full_build/doc/community.html', 13505,
        'ed1a149ca1f5129e31b71f7bf6547a690bbefed1f94b24d3b6cf15b9b5e7cac6'),
    'doc/default.css': ('ffmpeg-9.1-full_build/doc/default.css', 2494,
        '504c4a0980e6ec809da02ce16b73151622a2fdfb4409098c7ce96c1cac9b3735'),
    'doc/developer.html': ('ffmpeg-9.1-full_build/doc/developer.html', 65770,
        '66a1bb540a7ed37077c8b58a6a0c2c8f23fe7fc27de5e051cf34d0bd115c5015'),
    'doc/drawvg-reference.html': ('ffmpeg-9.1-full_build/doc/drawvg-reference.html', 147341,
        'e63e3aa79935d162c5e29e8feac866ae899ae0073f45595b3a029b42a897db90'),
    'doc/faq.html': ('ffmpeg-9.1-full_build/doc/faq.html', 63714,
        '30c660c6163137c633ee1d949cc33c625699c50cd83eaa95bfe98a7e92f8af15'),
    'doc/fate.html': ('ffmpeg-9.1-full_build/doc/fate.html', 18768,
        '603ae75dc972d486d4901c222ec881d6bb903058311dfcc9cda9f6471c50e3aa'),
    'doc/ffmpeg-all.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-all.html', 3224525,
        '431caccf07e6423a9a4e5b9b8fa45e61f93f8ca325a417485056e6f81873fc84'),
    'doc/ffmpeg-bitstream-filters.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-bitstream-filters.html', 76435,
        '8fe083ccf77a34fe08a6b90959e32a0d44d67b5a61aae4071bfe31097c1b6fe2'),
    'doc/ffmpeg-codecs.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-codecs.html', 403895,
        '098d10db727b96cf3443b3aa5db766518c15bfcbf0190cbd38ff929ba85b3471'),
    'doc/ffmpeg-devices.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-devices.html', 114015,
        'f76ff7375ce43aa8c6ed078b1f69b8245257e4f9ece6b336fadd0cc8e72f947f'),
    'doc/ffmpeg-filters.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-filters.html', 1730368,
        '4ba35c618de8aa40d8b793c3e53bd966cb7a7b9ba1902ede2110c5eede885613'),
    'doc/ffmpeg-formats.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-formats.html', 348374,
        '712edb8efb5d870b3f39d353c9aa210a558d7534448953bab0cfe61a891ac997'),
    'doc/ffmpeg-protocols.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-protocols.html', 135480,
        '62feafab48d3094bb1541d07ceca415463b4225667b738d693d02571ba312f81'),
    'doc/ffmpeg-resampler.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-resampler.html', 13818,
        'ef33b5c00313e980fd8bef875e48ad49534f8d4da3acd62c2cb121cf2f767986'),
    'doc/ffmpeg-scaler.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-scaler.html', 12981,
        '77a322683a61362bf302e67fe62560f6c5a4ccb7ef944f5fb7e2e797685436ff'),
    'doc/ffmpeg-utils.html': ('ffmpeg-9.1-full_build/doc/ffmpeg-utils.html', 55775,
        'f0acb1ad146da71c65450b975f6e7e102eed03ee40eb9163e5d7b70df2e82146'),
    'doc/ffmpeg.html': ('ffmpeg-9.1-full_build/doc/ffmpeg.html', 222726,
        '7173ca2dc2054a899dc5d5537747bd533c274a0478d07c398b7de07461fe804c'),
    'doc/ffplay-all.html': ('ffmpeg-9.1-full_build/doc/ffplay-all.html', 2459520,
        'f19df677301e04ffd84d489af67af73b83777faecaf19cba9092d1540f99fd05'),
    'doc/ffplay.html': ('ffmpeg-9.1-full_build/doc/ffplay.html', 43483,
        '89f943be9d67ac48cb0426fa2c2eba73cc4a1b338af554351989352077750377'),
    'doc/ffprobe-all.html': ('ffmpeg-9.1-full_build/doc/ffprobe-all.html', 2475191,
        '3ae7589850cc8a00c25f256c5daf163ed071b77e757740de144ff7d0fabf323a'),
    'doc/ffprobe.html': ('ffmpeg-9.1-full_build/doc/ffprobe.html', 59074,
        '42c13f1a1a3a968c83ca41c889d4fa1c2d0de2e0a6bd6cc4a0f03200237bf887'),
    'doc/general.html': ('ffmpeg-9.1-full_build/doc/general.html', 133869,
        '2ced7095e846aa054e98ae80abb313c5045341001f799c9c335d74c7b3d0def7'),
    'doc/git-howto.html': ('ffmpeg-9.1-full_build/doc/git-howto.html', 27737,
        'e44ed6484fa73f819c59494c68be232728319250bd86c6e32a760ddca620e108'),
    'doc/libavcodec.html': ('ffmpeg-9.1-full_build/doc/libavcodec.html', 3147,
        'faf8dd1ae157374e92a685970fa28f21e0df8a2dfb8c3ea99e58d6d956bee879'),
    'doc/libavdevice.html': ('ffmpeg-9.1-full_build/doc/libavdevice.html', 3026,
        'cea441629a753d099d8b1032f0814a87cfa113d830f8fcdebd047738cb5481ac'),
    'doc/libavfilter.html': ('ffmpeg-9.1-full_build/doc/libavfilter.html', 3092,
        'd02ec4ed1dddb7cb09f9c4e9f2bf38810b3050f6db5f839f398bd6561cab4322'),
    'doc/libavformat.html': ('ffmpeg-9.1-full_build/doc/libavformat.html', 3122,
        '49f9fdced269207bfd3a6bcb63738b3f1dcf2b3c7641eba509fd46f37c5cebcc'),
    'doc/libavutil.html': ('ffmpeg-9.1-full_build/doc/libavutil.html', 3621,
        'ba59342959874520f152b8df5eedde47295929a43e29646392e03c6fcdc2d8e9'),
    'doc/libswresample.html': ('ffmpeg-9.1-full_build/doc/libswresample.html', 4005,
        'f66d2d2e5f29ba56f47d2855d5cd8f276bf207241d024a5a06d0564b96d52bf5'),
    'doc/libswscale.html': ('ffmpeg-9.1-full_build/doc/libswscale.html', 3621,
        '41859c7b95dd3d1089e2189c2dc81b1da920557e66ec181a1f8fe04dcc829118'),
    'doc/mailing-list-faq.html': ('ffmpeg-9.1-full_build/doc/mailing-list-faq.html', 31209,
        'e6a26273f248a5ca7f4354fc8b19e4d0bceeefc3c7aa492db58072fdc9e6a944'),
    'doc/nut.html': ('ffmpeg-9.1-full_build/doc/nut.html', 11279,
        '3c42c776be67cf21b69dd5a2fe3dd6b08cc196eb9bbb2a9886366b2efa139ce6'),
    'doc/platform.html': ('ffmpeg-9.1-full_build/doc/platform.html', 18861,
        'fd77fdd1ada6252bebb335c6523116b97b4226ddd2ff147b923d0b07ec148a44'),
    'doc/style.min.css': ('ffmpeg-9.1-full_build/doc/style.min.css', 6523,
        '02496d7bdbd48d0f332dc94b62668906cbeabc3a8592253d5dfe2e5196522660'),
    'presets/libvpx-1080p.ffpreset': ('ffmpeg-9.1-full_build/presets/libvpx-1080p.ffpreset', 227,
        'cf0dca9d029e0aa29c242e5a7ecfb202a55ae41c1acdf5e2e59bf281a0136251'),
    'presets/libvpx-1080p50_60.ffpreset': ('ffmpeg-9.1-full_build/presets/libvpx-1080p50_60.ffpreset', 227,
        '4d32bed9032caf8bf4ef8e4bcac427f482c6191396ef6b11f075a264da3a8def'),
    'presets/libvpx-360p.ffpreset': ('ffmpeg-9.1-full_build/presets/libvpx-360p.ffpreset', 219,
        'c2ccd70a941c9b7003e364d7cc0296e8f3dea78cc3a87ccdcaae7c39a1a05b79'),
    'presets/libvpx-720p.ffpreset': ('ffmpeg-9.1-full_build/presets/libvpx-720p.ffpreset', 227,
        '5bcac409c81ec0e11091801d5d1322b5ec6a9bb8617d7dda022acfea0d498d52'),
    'presets/libvpx-720p50_60.ffpreset': ('ffmpeg-9.1-full_build/presets/libvpx-720p50_60.ffpreset', 227,
        'b08a5ae448cb3cccd23faa6f42c8dbf61ead1a8381fce65636aa01a4a142cb8e'),
}
REQUIRED_FILTERS = ("aresample", "atrim", "asetpts", "volume", "highpass", "lowpass", "equalizer",
                    "acompressor", "agate", "afftdn", "aecho", "pan", "aformat", "afade",
                    "anullsrc", "concat", "apad", "amix", "asplit", "anull", "sidechaincompress", "alimiter",
                    "sine", "adelay", "loudnorm", "asetnsamples", "flite")


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
    actual = set()
    for count, entry in enumerate(root.rglob('*'), 1):
        if count > 1000:
            raise ValueError('The audio runtime exceeds its fixed inventory budget.')
        if entry.is_dir():
            trusted_directory(entry)
        else:
            regular_file(entry)
            actual.add(entry.relative_to(root).as_posix())
    if actual != set(ARCHIVE_MEMBERS) | {'ffmpeg-runtime.json'}:
        raise ValueError('The bundled audio runtime failed its integrity check: unexpected or missing file.')
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
