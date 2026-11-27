"""Admit only the exact private launcher already executed by native Windows CI.

This is a packaging check, not a signature check. It never executes the supplied
file. The approved unsigned bytes and their seven native build/probe source files are fixed
here; a supplied manifest cannot authorize another executable.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat


HERE = Path(__file__).resolve().parent
# Load the same stdlib-only contract that is packaged in the installed app.
# This source helper never imports the candidate bundle or executes its code.
_spec = importlib.util.spec_from_file_location(
    'carr_native_launcher_contract', HERE.parent.parent / 'src/carr_library/native_launcher.py')
_contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)
LAUNCHER_NAME = _contract.LAUNCHER_NAME
LAUNCHER_BYTES = _contract.LAUNCHER_BYTES
LAUNCHER_SHA256 = _contract.LAUNCHER_SHA256
SOURCE_COMMIT = _contract.SOURCE_COMMIT
SOURCE_SHA256 = _contract.SOURCE_SHA256
WINDOWS_RUN = _contract.WINDOWS_RUN
WINDOWS_JOB = _contract.WINDOWS_JOB


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _plain(path, *, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Native launcher inputs must be ordinary local files and directories.")
    return info


def _read(path, maximum):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("Native launcher inputs cannot contain parent traversal.")
    parents = [(parent, _plain(parent, directory=True)) for parent in path.parents]
    before = _plain(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError("Native launcher input exceeds its exact bounded size.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Native launcher input changed before reading.")
        raw = stream.read(maximum + 1)
        if (_stamp(os.fstat(stream.fileno())) != _stamp(before)
                or _stamp(_plain(path)) != _stamp(before) or len(raw) != before.st_size):
            raise ValueError("Native launcher input changed while reading.")
    for parent, previous in parents:
        current = _plain(parent, directory=True)
        if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
            raise ValueError("Native launcher input directory changed while reading.")
    return raw


def snapshot_launcher(path):
    """Return frozen approved bytes and narrowly scoped native-process evidence."""
    for name, expected in SOURCE_SHA256.items():
        if hashlib.sha256(_read(HERE / name, 128 * 1024)).hexdigest() != expected:
            raise ValueError("Native launcher source differs from the Windows-tested version: " + name)
    payload = _read(path, LAUNCHER_BYTES)
    if len(payload) != LAUNCHER_BYTES or hashlib.sha256(payload).hexdigest() != LAUNCHER_SHA256:
        raise ValueError("Native launcher does not match the exact Windows-tested private executable.")
    evidence = _contract.identity_record()
    evidence['bytes'], evidence['sha256'] = LAUNCHER_BYTES, LAUNCHER_SHA256
    return payload, evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher", type=Path)
    print(json.dumps(snapshot_launcher(parser.parse_args().launcher)[1], indent=2))
