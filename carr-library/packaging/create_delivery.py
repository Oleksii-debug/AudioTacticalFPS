"""Create a private two-archive delivery from an already sealed complete bundle.

Extract BOTH archives into the same destination before running Install.cmd.
The unchanged bundle manifest requires their union. No supplied Python code,
native executable or model is loaded. Use private local build directories.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import zlib


HERE = Path(__file__).resolve().parent
PREFIX = "Universal-Research-Library/"
MANIFEST = "bundle-manifest.json"
SOURCES = "ocr-corresponding-source.tar"
CHUNK = 1024 * 1024
MAX_MANIFEST = 8 * CHUNK
MAX_FILES = 50_000
MAX_FILE = 2 * 1024 ** 3
MAX_TOTAL = 2 * 1024 ** 3 - 1
INSTRUCTIONS = ("Extract both archives into the same destination so their shared "
                "Universal-Research-Library folder contains the complete manifest inventory. "
                "Only then run Install.cmd. The application ZIP alone is incomplete.")


def _check(cancelled):
    if cancelled():
        raise InterruptedError("Delivery creation cancelled.")


def _identity(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Expected an unlinked local " + ("directory: " if directory else "file: ") + str(path))
    return info


def _absolute(value):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not supported.")
    return path


def _directories(path):
    return [(p, _identity(_plain(p, directory=True))) for p in (*reversed(path.parents), path)]


def _unchanged_directories(records):
    if any(_identity(_plain(path, directory=True)) != before for path, before in records):
        raise ValueError("A build directory changed during delivery creation.")


@contextmanager
def _file(path):
    parents = _directories(path.parent)
    before = _plain(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Input file changed before reading.")
        yield stream
        if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(_plain(path)) != _stamp(before)):
            raise ValueError("Input file changed during reading.")
        _unchanged_directories(parents)


def _stream(stream, cancelled, *, destination=None, limit=MAX_TOTAL):
    value, total = hashlib.sha256(), 0
    while True:
        _check(cancelled)
        block = stream.read(CHUNK)
        if not block:
            return value.hexdigest(), total
        total += len(block)
        if total > limit:
            raise ValueError("Delivery input exceeds its size limit.")
        value.update(block)
        if destination is not None:
            destination.write(block)


def _read_manifest(bundle, cancelled):
    with _file(bundle / MANIFEST) as stream:
        _check(cancelled)
        raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise ValueError("Bundle manifest is too large.")
    return raw


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key.")
        result[key] = value
    return result


def _name(name):
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 2048:
        raise ValueError("Invalid delivery path.")
    parts = PurePosixPath(name).parts
    if (not parts or "/".join(parts) != name or name.startswith("/")
            or any(part in (".", "..") for part in parts)
            or any(c in '<>:"\\|?*' or ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError("Invalid delivery path.")
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if (part.endswith((".", " ")) or stem in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)):
            raise ValueError("Ambiguous Windows delivery path.")
    return unicodedata.normalize("NFC", name).casefold()


def _manifest(raw, expected_source_commit):
    if not isinstance(expected_source_commit, str) or not re.fullmatch("[0-9a-f]{40}", expected_source_commit):
        raise ValueError("Expected source commit must be a full lowercase Git SHA.")
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Bundle manifest is too large.")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1
            or data.get("source_commit") != expected_source_commit):
        raise ValueError("Bundle source commit or manifest schema does not match.")
    files = data.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid bundle inventory.")
    seen = {_name(MANIFEST)}
    paths = {}
    for name, value in files.items():
        folded = _name(name)
        if folded in seen or not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
            raise ValueError("Duplicate, case-colliding or invalid bundle inventory.")
        seen.add(folded)
    # A file/directory prefix or differently cased directory is also a Windows
    # collision, even when the complete file names differ.
    for name in (*files, MANIFEST):
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            key = _name(prefix)
            item = (prefix, index == len(parts))
            previous = paths.get(key)
            if previous is not None and (previous != item or item[1]):
                raise ValueError("Case-colliding or file/directory bundle paths.")
            paths[key] = item
    if SOURCES not in files or data.get("bundled_ocr") is not True:
        raise ValueError("A complete bundle including its OCR corresponding-source TAR is required.")
    return data


def _trusted_verify(bundle, manifest_bytes=None):
    # The supplied bundle's install_windows.py is payload, never imported.
    spec = importlib.util.spec_from_file_location("carr_delivery_trusted_installer", HERE / "install_windows.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.verify(bundle, manifest_bytes=manifest_bytes)


def _write_archive(path, bundle, names, files, raw, cancelled):
    with path.open("xb") as target:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in sorted(names):
                _check(cancelled)
                info = zipfile.ZipInfo(PREFIX + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info, "w", force_zip64=True) as destination:
                    if name == MANIFEST:
                        destination.write(raw)
                    else:
                        with _file(bundle / name) as source:
                            value, _size = _stream(source, cancelled, destination=destination, limit=MAX_FILE)
                        if value != files[name]:
                            raise ValueError("Bundle payload changed during archiving: " + name)
        target.flush()
        os.fsync(target.fileno())


def _zip_layout(stream, end, infos, cancelled):
    """Accept contiguous seekable Python ZIPs, with no unlisted envelope bytes.

    Local ZIP64 size fields are supported; global ZIP64 directories and data
    descriptors are unnecessary under this helper's 2 GiB / 50,000-file bound.
    """
    message = "Unsupported ZIP envelope; rebuild the source companion from the sealed TAR."
    stream.seek(0, os.SEEK_END)
    if (end[zipfile._ECD_SIGNATURE] != zipfile.stringEndArchive
            or end[zipfile._ECD_COMMENT_SIZE] or end[zipfile._ECD_COMMENT]
            or end[zipfile._ECD_LOCATION] + zipfile.sizeEndCentDir != stream.tell()
            or end[zipfile._ECD_OFFSET] + end[zipfile._ECD_SIZE] != end[zipfile._ECD_LOCATION]
            or end[zipfile._ECD_ENTRIES_TOTAL] != len(infos)
            or end[zipfile._ECD_ENTRIES_THIS_DISK] != len(infos)):
        raise ValueError(message)
    position, central_bytes, offsets = 0, 0, {}
    for info in sorted(infos, key=lambda item: item.header_offset):
        _check(cancelled)
        if (info.header_offset != position or info.comment or info.extra
                or info.flag_bits & ~0x800 or info.volume or info.reserved):
            raise ValueError(message)
        encoded_name = info.filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437")
        central_bytes += zipfile.sizeCentralDir + len(encoded_name)
        stream.seek(position)
        raw = stream.read(zipfile.sizeFileHeader)
        if len(raw) != zipfile.sizeFileHeader:
            raise ValueError(message)
        header = struct.unpack(zipfile.structFileHeader, raw)
        if (header[zipfile._FH_SIGNATURE] != zipfile.stringFileHeader
                or header[zipfile._FH_GENERAL_PURPOSE_FLAG_BITS] != info.flag_bits
                or header[zipfile._FH_COMPRESSION_METHOD] != info.compress_type
                or header[zipfile._FH_CRC] != info.CRC
                or header[zipfile._FH_FILENAME_LENGTH] != len(encoded_name)
                or stream.read(len(encoded_name)) != encoded_name):
            raise ValueError(message)
        sizes = (header[zipfile._FH_UNCOMPRESSED_SIZE], header[zipfile._FH_COMPRESSED_SIZE])
        expected_extra = (struct.pack("<HHQQ", 1, 16, info.file_size, info.compress_size)
                          if sizes == (0xffffffff, 0xffffffff) else b"")
        if (header[zipfile._FH_EXTRA_FIELD_LENGTH] != len(expected_extra)
                or stream.read(len(expected_extra)) != expected_extra
                or (not expected_extra and sizes != (info.file_size, info.compress_size))):
            raise ValueError(message)
        offsets[info.filename] = stream.tell()
        position = stream.tell() + info.compress_size
    if position != end[zipfile._ECD_OFFSET] or central_bytes != end[zipfile._ECD_SIZE]:
        raise ValueError(message)
    return offsets


def _compressed_frame(stream, info, offset, cancelled):
    """Reject bytes after the deflate end marker that ZipExtFile may ignore."""
    if info.compress_type == zipfile.ZIP_STORED:
        if info.compress_size != info.file_size:
            raise ValueError("Stored ZIP entry has unaccounted bytes.")
        return
    stream.seek(offset)
    decoder, remaining, total = zlib.decompressobj(-15), info.compress_size, 0
    while remaining:
        _check(cancelled)
        block = stream.read(min(CHUNK, remaining))
        if not block:
            raise ValueError("Truncated compressed ZIP payload.")
        remaining -= len(block)
        while True:
            _check(cancelled)
            payload = decoder.decompress(block, CHUNK)
            total += len(payload)
            if total > info.file_size or decoder.unused_data or (decoder.eof and remaining):
                raise ValueError("ZIP entry has unlisted compressed bytes or oversized data.")
            block = decoder.unconsumed_tail
            if decoder.eof or (not block and len(payload) < CHUNK):
                break
    if not decoder.eof or total != info.file_size:
        raise ValueError("Truncated or mismatched compressed ZIP payload.")


def _verify_archive(path, expected, cancelled):
    with _file(path) as stream:
        # Reuse stdlib EOCD/ZIP64 parsing to bound its central-directory allocation.
        end = zipfile._EndRecData(stream)
        if (end is None or end[zipfile._ECD_SIZE] > 32 * CHUNK
                or end[zipfile._ECD_ENTRIES_TOTAL] > MAX_FILES + 1
                or end[zipfile._ECD_DISK_NUMBER] or end[zipfile._ECD_DISK_START]):
            raise ValueError("Invalid or oversized delivery ZIP directory.")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            seen, total = set(), 0
            for info in infos:
                folded = _name(info.filename)
                kind = stat.S_IFMT(info.external_attr >> 16)
                if (info.orig_filename != info.filename or folded in seen
                        or info.filename not in expected or info.is_dir()
                        or kind not in (0, stat.S_IFREG)
                        or info.external_attr & (stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT)
                        or info.flag_bits & (1 | 0x40 | 0x2000)
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or info.file_size > MAX_FILE):
                    raise ValueError("Unexpected, duplicate, linked or encrypted delivery ZIP entry.")
                seen.add(folded)
                total += info.file_size
            if {info.filename for info in infos} != set(expected) or total > MAX_TOTAL:
                raise ValueError("Missing delivery ZIP entry or oversized archive.")
            offsets = _zip_layout(stream, end, infos, cancelled)
            for info in infos:
                _compressed_frame(stream, info, offsets[info.filename], cancelled)
                with archive.open(info) as member:
                    value, size = _stream(member, cancelled, limit=info.file_size)
                if value != expected[info.filename] or size != info.file_size:
                    raise ValueError("Delivery ZIP payload checksum failed: " + info.filename)
        stream.seek(0)
        sha, size = _stream(stream, cancelled)
        stamp = _stamp(os.fstat(stream.fileno()))
    return {"sha256": sha, "bytes": size, "entries": len(infos), "payload_bytes": total,
            "_verified_stamp": stamp}


def verify_archives(application, source_companion, manifest_bytes, expected_source_commit, *, cancelled=lambda: False):
    """Verify the exact two-archive union, without extracting or executing it."""
    data = _manifest(manifest_bytes, expected_source_commit)
    app = {PREFIX + n: h for n, h in data["files"].items() if n != SOURCES}
    app[PREFIX + MANIFEST] = hashlib.sha256(manifest_bytes).hexdigest()
    sources = {PREFIX + SOURCES: data["files"][SOURCES]}
    return {"application": _verify_archive(_absolute(application), app, cancelled),
            "sources": _verify_archive(_absolute(source_companion), sources, cancelled)}


def _tree(commit):
    try:
        value = subprocess.check_output(
            ["git", "--no-replace-objects", "-C", str(HERE.parent.parent), "rev-parse", "--verify", commit + "^{tree}"],
            stderr=subprocess.DEVNULL, text=True, timeout=5).strip()
        return value if re.fullmatch("[0-9a-f]{40}", value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _publish(stage, output, parent_fd):
    if os.name == "nt":
        # Unlike POSIX rename, Windows rename never replaces an existing target.
        os.rename(stage, output)
    elif sys.platform.startswith("linux"):
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "Atomic no-replace directory publication is unavailable.")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(parent_fd, os.fsencode(stage.name), parent_fd, os.fsencode(output.name), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(output))
    else:
        raise OSError(errno.ENOSYS, "Atomic delivery publication supports Windows and Linux.")


def _cleanup(stage, identity, parent_fd):
    try:
        info = (os.stat(stage.name, dir_fd=parent_fd, follow_symlinks=False)
                if parent_fd is not None else stage.lstat())
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or _identity(info) != identity:
        return  # Never remove a replacement path belonging to another operation.
    if parent_fd is not None:
        shutil.rmtree(stage.name, dir_fd=parent_fd)
    else:
        shutil.rmtree(stage)


def _start_here(application_name, source_name, reused):
    source_note = ("Архів джерел використано повторно: він залишається у попередньому місці. "
                   "Візьміть його разом з архівом програми.\n" if reused else "")
    return ("Universal Research Library — встановлення у Windows x64\n\n"
            "Якщо бібліотека вже існує, спочатку створіть резервну копію через меню "
            "Library, Back up all libraries та збережіть показану контрольну суму. "
            "Виберіть для копії папку поза папкою програми. Закрийте стару програму. "
            "Збережіть стару інсталяцію; не розпаковуйте новий пакет поверх неї.\n\n"
            "1. Потрібні обидва ZIP-архіви:\n"
            + application_name + "\n" + source_name + "\n" + source_note + "\n"
            "2. Розпакуйте ОБИДВА архіви в ОДНУ Й ТУ САМУ НОВУ папку призначення. "
            "Під час кожного розпакування вкажіть цю саму папку. Обидва архіви мають "
            "утворити одну спільну папку Universal-Research-Library.\n\n"
            "3. У спільній папці повинні бути Install.cmd та ocr-corresponding-source.tar. "
            "Лише після розпакування обох архівів запустіть Install.cmd і дочекайтеся "
            "успішного завершення встановлення. Самого архіву програми недостатньо.\n\n"
            "4. Запускайте програму через ResearchLibrary.cmd. Для перевірки встановлення "
            "використовуйте CheckInstallation.cmd.\n\n"
            "delivery-verification.json містить точну версію коду й контрольні суми архівів. "
            "Цей приватний пакет призначений для локального встановлення.\n")


def create_delivery(bundle, expected_source_commit, output, *, source_companion=None, cancelled=lambda: False):
    """Publish a new verified delivery directory; reuse a companion without copying it."""
    _check(cancelled)
    bundle, output = _absolute(bundle), _absolute(output)
    _directories(bundle)
    parents = _directories(output.parent)
    if os.path.lexists(output):
        raise FileExistsError("Use a new delivery output directory.")
    if output.is_relative_to(bundle) or bundle.is_relative_to(output):
        raise ValueError("Delivery output must be outside the input bundle.")
    raw = _read_manifest(bundle, cancelled)
    data = _manifest(raw, expected_source_commit)
    sizes = [_plain(bundle / name).st_size for name in data["files"]]
    if max(sizes) > MAX_FILE or sum(sizes) > MAX_TOTAL:
        raise ValueError("Bundle exceeds the delivery size limit.")
    _trusted_verify(bundle, manifest_bytes=raw)
    _check(cancelled)
    if _read_manifest(bundle, cancelled) != raw:
        raise ValueError("Bundle manifest changed during verification.")
    companion = _absolute(source_companion) if source_companion is not None else None
    original_companion = (_verify_archive(companion, {PREFIX + SOURCES: data["files"][SOURCES]}, cancelled)
                          if companion is not None else None)
    parent_fd = (os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if os.name != "nt" else None)
    stage, identity = None, None
    try:
        _unchanged_directories(parents)
        stage = Path(tempfile.mkdtemp(prefix=".carr-delivery-", dir=output.parent))
        identity = _identity(_plain(stage, directory=True))
        app_name = "Universal-Research-Library-Application-" + expected_source_commit[:12] + ".zip"
        source_name = "Universal-Research-Library-Sources-" + expected_source_commit[:12] + ".zip"
        _write_archive(stage / app_name, bundle, (set(data["files"]) - {SOURCES}) | {MANIFEST},
                       data["files"], raw, cancelled)
        if companion is None:
            companion = stage / source_name
            _write_archive(companion, bundle, {SOURCES}, data["files"], raw, cancelled)
        archives = verify_archives(stage / app_name, companion, raw, expected_source_commit, cancelled=cancelled)
        if original_companion is not None and archives["sources"] != original_companion:
            raise ValueError("Source companion changed during delivery creation.")
        app_stamp = archives["application"].pop("_verified_stamp")
        companion_stamp = archives["sources"].pop("_verified_stamp")
        archives["application"].update(path=app_name, reused=False)
        archives["sources"].update(path=str(companion) if source_companion is not None else source_name,
                                   reused=source_companion is not None)
        report = {"schema": 1, "kind": "private-local-delivery", "source_commit": expected_source_commit,
                  "source_tree": _tree(expected_source_commit), "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                  "manifest_bytes": len(raw), "manifest_payload_files": len(data["files"]),
                  "archive_prefix": PREFIX, "archives": archives, "instructions": INSTRUCTIONS,
                  "boundaries": {"manifest_unchanged": True, "both_archives_required": True,
                                 "inventory": "Only sealed files plus the exact bundle manifest",
                                 "source_claim": "Matches the expected commit; upstream canonical builder verifies wheel source",
                                 "native_execution": "NOT_RUN", "model_inference": "NOT_RUN", "nvda": "NOT_RUN"}}
        with (stage / "delivery-verification.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        report_stamp = _stamp(_plain(stage / "delivery-verification.json"))
        with (stage / "START_HERE_UK.txt").open("x", encoding="utf-8") as stream:
            stream.write(_start_here(app_name, json.dumps(companion.name, ensure_ascii=False),
                                     source_companion is not None))
            stream.flush()
            os.fsync(stream.fileno())
        instructions_stamp = _stamp(_plain(stage / "START_HERE_UK.txt"))
        _check(cancelled)
        if (_read_manifest(bundle, lambda: False) != raw or _stamp(_plain(companion)) != companion_stamp
                or _stamp(_plain(stage / app_name)) != app_stamp
                or _stamp(_plain(stage / "delivery-verification.json")) != report_stamp
                or _stamp(_plain(stage / "START_HERE_UK.txt")) != instructions_stamp):
            raise ValueError("Delivery inputs changed before publication.")
        _unchanged_directories(parents)
        if _identity(_plain(stage, directory=True)) != identity:
            raise ValueError("Delivery staging directory changed before publication.")
        expected_files = {app_name, "delivery-verification.json", "START_HERE_UK.txt"}
        if source_companion is None:
            expected_files.add(source_name)
        if {p.name for p in stage.iterdir()} != expected_files:
            raise ValueError("Unexpected delivery staging files.")
        _publish(stage, output, parent_fd)
        stage = None
        return {"output": str(output), **report}
    finally:
        try:
            if stage is not None and identity is not None:
                _cleanup(stage, identity, parent_fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-companion", type=Path, help="Reuse an existing verified single-entry Sources ZIP without copying it")
    args = parser.parse_args(argv)
    print(json.dumps(create_delivery(args.bundle, args.source_commit, args.output,
                                    source_companion=args.source_companion), indent=2))


if __name__ == "__main__":
    main()
