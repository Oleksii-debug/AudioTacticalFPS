"""Seal a reviewed native launcher and its exact source/build evidence.

This checks bounded input bytes, the PE structure and current source identities.
A build report is provenance, not an Authenticode signature or proof of execution.
No compiler, download, installer or executable runs during this preflight.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import struct


SOURCE = Path(__file__).resolve().parent / "native-launcher"
MAX_PE_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
SOURCE_SETS = {
    "Windows x64 / MSVC": frozenset((
        "launcher.c", "launcher_core.h", "launcher.manifest", "launcher.rc",
        "windows_child_probe.c", "build_msvc.ps1", "probe_pe.py",
    )),
    "x86_64-windows-gnu": frozenset((
        "launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest",
        "toolchain.lock.json",
    )),
}


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _plain(info):
    return (stat.S_ISREG(info.st_mode) and
            not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _snapshot(path, limit):
    path = Path(path)
    before = path.lstat()
    if not _plain(before) or not 0 < before.st_size <= limit:
        raise ValueError("Native launcher input must be a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not _plain(opened) or _identity(opened) != _identity(before):
            raise ValueError("Native launcher input changed before capture")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    final = path.lstat()
    if (len(data) != before.st_size or len(data) > limit
            or not _plain(final) or _identity(before) != _identity(after)
            or _identity(before) != _identity(final)):
        raise ValueError("Native launcher input changed during capture")
    return data


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate native launcher evidence field")
        result[key] = value
    return result


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def capture_native_launcher(executable, evidence):
    """Return the two immutable byte snapshots admitted into a new bundle."""
    executable = Path(executable)
    if executable.name != "ResearchLibrary.exe":
        raise ValueError("Expected ResearchLibrary.exe")
    image = _snapshot(executable, MAX_PE_BYTES)
    report_bytes = _snapshot(evidence, MAX_REPORT_BYTES)
    try:
        report = json.loads(report_bytes.decode("utf-8-sig"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid native launcher build evidence") from error
    if (not isinstance(report, dict)
            or set(report) != {"schema", "target", "toolchain", "source_sha256", "pe", "release_status"}
            or type(report["schema"]) is not int or report["schema"] != 1
            or report["release_status"] != "unsigned-private-candidate"
            or not isinstance(report["target"], str) or report["target"] not in SOURCE_SETS):
        raise ValueError("Unsupported native launcher build evidence")
    sources = report["source_sha256"]
    if not isinstance(sources, dict) or set(sources) != SOURCE_SETS[report["target"]]:
        raise ValueError("Native launcher source inventory is incomplete or unexpected")
    for name, digest in sources.items():
        if (not _digest(digest)
                or hashlib.sha256(_snapshot(SOURCE / name, MAX_REPORT_BYTES)).hexdigest() != digest):
            raise ValueError("Native launcher was built from different source bytes")
    toolchain = report["toolchain"]
    if report["target"] == "Windows x64 / MSVC":
        if (not isinstance(toolchain, dict) or set(toolchain) != {"cl.exe", "link.exe", "rc.exe"}
                or any(not isinstance(row, dict) or set(row) != {"sha256", "version"}
                       or not _digest(row["sha256"]) or not isinstance(row["version"], str)
                       or not 1 <= len(row["version"]) <= 128 for row in toolchain.values())):
            raise ValueError("Invalid MSVC toolchain inventory")
    else:
        lock = json.loads(_snapshot(SOURCE / "toolchain.lock.json", MAX_REPORT_BYTES))
        if (not isinstance(toolchain, dict)
                or set(toolchain) != {"version", "archive_sha256", "archive_bytes", "binary_sha256", "primary_url"}
                or toolchain["version"] != lock["version"] or not _digest(toolchain["binary_sha256"])
                or not any(toolchain["archive_sha256"] == lock[host]["sha256"]
                           and type(toolchain["archive_bytes"]) is int
                           and toolchain["archive_bytes"] == lock[host]["bytes"]
                           and toolchain["primary_url"] == lock[host]["url"]
                           for host in ("x86_64-windows", "x86_64-linux"))):
            raise ValueError("Invalid pinned Zig toolchain inventory")
    spec = importlib.util.spec_from_file_location("carr_native_pe_preflight", SOURCE / "probe_pe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    try:
        actual = probe.inspect_pe_bytes(image, executable.name)
    except (ValueError, IndexError, struct.error) as error:
        raise ValueError("Invalid native launcher PE") from error
    if report["pe"] != actual:
        raise ValueError("Native launcher executable does not match its build evidence")
    return image, report_bytes
