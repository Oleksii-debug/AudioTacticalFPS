"""Admit one locally built native launcher before the existing bundle is sealed.

The owner's build and Windows-process reports bind bytes and source inputs.
They are consistency evidence, not signatures or third-party attestations.
No supplied executable is run by this module.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory

SOURCE = Path(__file__).resolve().parent
MAX_BINARY = 2 * 1024 * 1024
MAX_REPORT = 128 * 1024
CHECKS = (
    "unicode-spaces-fixed-argv-offline-optional-runtimes",
    "read-only-ready-probe",
    "fixed-installation-check",
    "child-exit-status-propagated",
    "extra-arguments-rejected",
    "incomplete-state-preserved-without-reinstall",
    "missing-runtime-does-not-search-path",
    "fresh-package-reuses-fixed-offline-installer",
    "research-data-sentinel-preserved",
)
COMMON_SOURCES = {"launcher.c", "launcher_core.h", "launcher.rc", "launcher.manifest"}
SOURCE_SETS = {
    "Windows x64 / MSVC": COMMON_SOURCES | {
        "windows_child_probe.c", "build_msvc.ps1", "probe_pe.py"},
    "x86_64-windows-gnu": COMMON_SOURCES | {"toolchain.lock.json"},
}


def _hash(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot(value, limit):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not supported for native launcher inputs.")
    for current in (path, *path.parents):
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Linked native launcher inputs are not supported.")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
        raise ValueError("Native launcher inputs must be bounded regular files.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _stamp(opened) != _stamp(before):
            raise ValueError("A native launcher input changed before reading.")
        raw = stream.read(limit + 1)
        if (len(raw) != before.st_size or _stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(path.lstat()) != _stamp(before)):
            raise ValueError("A native launcher input changed while reading.")
    return raw


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate native launcher evidence key.")
        result[key] = value
    return result


def _report(path):
    value = json.loads(_snapshot(path, MAX_REPORT).decode("utf-8-sig"),
                       object_pairs_hook=_unique)
    if type(value) is not dict or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Unsupported native launcher evidence schema.")
    return value


def _committed_sources(source_commit, declared, names):
    if not isinstance(source_commit, str) or not re.fullmatch("[0-9a-f]{40}", source_commit):
        raise ValueError("A full source commit is required for the optional native launcher.")
    if type(declared) is not dict or set(declared) != names or not all(map(_hash, declared.values())):
        raise ValueError("Native launcher source inventory does not match its build target.")
    repository = SOURCE.parents[2]
    command = ["git", "--no-replace-objects", "-C", str(repository)]
    environment = dict(os.environ, GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    try:
        kind = subprocess.check_output(
            [*command, "cat-file", "-t", source_commit], env=environment,
            stderr=subprocess.DEVNULL, timeout=15).strip()
        if kind != b"commit":
            raise ValueError("The native launcher source identity is not a commit.")
        for name in sorted(names):
            relative = (SOURCE / name).relative_to(repository).as_posix()
            committed = subprocess.check_output(
                [*command, "show", source_commit + ":" + relative], env=environment,
                stderr=subprocess.DEVNULL, timeout=15)
            current = _snapshot(SOURCE / name, 256 * 1024)
            if (current != committed or hashlib.sha256(committed).hexdigest() != declared[name]):
                raise ValueError("Native launcher source differs from the exact commit: " + name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("The exact committed native launcher source is unavailable.") from exc


def verify_launcher(executable, build_evidence, native_probe, source_commit):
    """Return captured PE bytes only after source, build and native evidence agree."""
    payload = _snapshot(executable, MAX_BINARY)
    digest = hashlib.sha256(payload).hexdigest()
    build, probe = _report(build_evidence), _report(native_probe)
    if (set(build) != {"schema", "target", "toolchain", "source_sha256", "pe", "release_status"}
            or not isinstance(build.get("target"), str) or build["target"] not in SOURCE_SETS
            or build.get("release_status") != "unsigned-private-candidate"
            or type(build.get("pe")) is not dict
            or build["pe"].get("sha256") != digest):
        raise ValueError("Native launcher build evidence does not identify the supplied unsigned PE.")
    _committed_sources(source_commit, build["source_sha256"], SOURCE_SETS[build["target"]])
    toolchain = build["toolchain"]
    if build["target"] == "Windows x64 / MSVC":
        if (type(toolchain) is not dict or set(toolchain) != {"cl.exe", "link.exe", "rc.exe"}
                or any(type(item) is not dict or set(item) != {"sha256", "version"}
                       or not _hash(item["sha256"]) or not isinstance(item["version"], str)
                       or not 0 < len(item["version"]) <= 256 for item in toolchain.values())):
            raise ValueError("Unsupported Microsoft native launcher toolchain evidence.")
    else:
        lock = json.loads(_snapshot(SOURCE / "toolchain.lock.json", MAX_REPORT))
        archives = [lock["x86_64-linux"], lock["x86_64-windows"]]
        if (type(toolchain) is not dict
                or set(toolchain) != {"version", "archive_sha256", "archive_bytes", "binary_sha256", "primary_url"}
                or toolchain["version"] != lock["version"]
                or not _hash(toolchain["binary_sha256"])
                or type(toolchain["archive_bytes"]) is not int
                or not any((toolchain["archive_sha256"], toolchain["archive_bytes"], toolchain["primary_url"])
                           == (entry["sha256"], entry["bytes"], entry["url"]) for entry in archives)):
            raise ValueError("Unsupported pinned Zig native launcher toolchain evidence.")
    if (set(probe) != {"schema", "status", "checks", "check_count", "launcher_sha256",
                      "synthetic_child_sha256", "native_launcher_execution_verified",
                      "actual_python_installation_verified", "nvda_verified", "authenticode_verified"}
            or probe.get("status") != "passed"
            or probe.get("checks") != list(CHECKS)
            or type(probe.get("check_count")) is not int or probe["check_count"] != len(CHECKS)
            or probe.get("launcher_sha256") != digest
            or not _hash(probe.get("synthetic_child_sha256"))
            or probe.get("native_launcher_execution_verified") is not True
            or any(probe.get(name) is not False for name in
                   ("actual_python_installation_verified", "nvda_verified", "authenticode_verified"))):
        raise ValueError("Matching actual native-process evidence is required for the launcher.")
    # Reuse the upstream PE inspector on the captured bytes; the mutable input
    # path is never reopened and this private validation copy is never executed.
    spec = importlib.util.spec_from_file_location("carr_native_launcher_pe", SOURCE / "probe_pe.py")
    inspector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspector)
    with TemporaryDirectory(prefix="carr-launcher-admission-") as temporary:
        captured = Path(temporary) / "ResearchLibrary.exe"
        captured.write_bytes(payload)
        try:
            actual_pe = inspector.inspect_pe(captured)
        except (ValueError, inspector.struct.error, IndexError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid unsigned native launcher PE: " + str(exc)) from exc
        if actual_pe != build["pe"]:
            raise ValueError("Native launcher PE structure differs from its build evidence.")
    return payload


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--build-evidence", required=True, type=Path)
    parser.add_argument("--native-probe", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    arguments = parser.parse_args()
    captured = verify_launcher(arguments.launcher, arguments.build_evidence,
                               arguments.native_probe, arguments.source_commit)
    print(json.dumps({
        "schema": 1, "status": "PASS", "source_commit": arguments.source_commit,
        "launcher_sha256": hashlib.sha256(captured).hexdigest(), "bytes": len(captured),
        "evidence_scope": "matching owner-supplied build/native-process reports and exact committed sources",
        "native_execution_performed_by_this_check": False,
        "authenticode_verified": False,
    }))
