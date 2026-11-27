"""Probe Windows open-target rename semantics using only owned synthetic files.

Run with an ordinary Windows Python: python -I -B windows_json_replace_diagnostic.py
This is a diagnostic, not a replacement for production reader/writer tests.
Every mutation occurs inside one new TemporaryDirectory; child processes are
owned, isolated, bounded and never start descendants.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

OLD = b'{"done":0}'
NEW = b'{"done":1}'
READERS = ("closed", "ordinary", "raw-share7-reparse",
           "fd-share7-reparse", "fd-share7-normal")
WRITERS = ("os.replace", "MoveFileExW", "ReplaceFileW",
           "FileRenameInfo", "FileRenameInfoEx")
INVALID_HANDLE = ctypes.c_void_p(-1).value


class FileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME),
        ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class RenameInfo(ctypes.Structure):
    _fields_ = [
        ("flags", wintypes.DWORD),
        ("root", wintypes.HANDLE),
        ("length", wintypes.DWORD),
        ("name", wintypes.WCHAR * 1),
    ]


def api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    specifications = {
        "CreateFileW": (
            (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
             ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE),
            wintypes.HANDLE),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
        "GetHandleInformation": (
            (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "GetFileInformationByHandle": (
            (wintypes.HANDLE, ctypes.POINTER(FileInfo)), wintypes.BOOL),
        "ReadFile": (
            (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p), wintypes.BOOL),
        "MoveFileExW": (
            (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD), wintypes.BOOL),
        "ReplaceFileW": (
            (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
             wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p), wintypes.BOOL),
        "SetFileInformationByHandle": (
            (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
            wintypes.BOOL),
    }
    for name, (arguments, result) in specifications.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def open_native(kernel, path, *, access=0x80000000, reparse=True):
    handle = kernel.CreateFileW(
        str(path), access, 7, None, 3, 0x00200000 if reparse else 0, None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def identity(kernel, handle):
    info, flags = FileInfo(), wintypes.DWORD()
    checked(kernel.GetFileInformationByHandle(handle, ctypes.byref(info)))
    checked(kernel.GetHandleInformation(handle, ctypes.byref(flags)))
    return {
        "file_id": [info.volume, (info.index_high << 32) | info.index_low],
        "links": info.links,
        "bytes": (info.size_high << 32) | info.size_low,
        "reparse": bool(info.attributes & 0x400),
        "inheritable": bool(flags.value & 1),
    }


class Reader:
    def __init__(self, kernel, path, kind):
        import msvcrt
        self.kernel, self.stream, self.handle = kernel, None, None
        self.owned_handle = False
        if kind == "closed":
            return
        if kind == "ordinary":
            self.stream = path.open("rb")
            self.handle = msvcrt.get_osfhandle(self.stream.fileno())
            return
        handle = open_native(kernel, path, reparse=not kind.endswith("normal"))
        if kind.startswith("raw-"):
            self.handle, self.owned_handle = handle, True
            return
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        try:
            self.stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        self.handle = msvcrt.get_osfhandle(self.stream.fileno())

    def snapshot(self):
        if self.handle is None:
            return None
        return identity(self.kernel, self.handle)

    def read(self):
        if self.handle is None:
            return None
        if self.stream is not None:
            return self.stream.read(4096)
        buffer, count = ctypes.create_string_buffer(4096), wintypes.DWORD()
        checked(self.kernel.ReadFile(
            self.handle, buffer, len(buffer), ctypes.byref(count), None))
        return buffer.raw[:count.value]

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        elif self.owned_handle:
            checked(self.kernel.CloseHandle(self.handle))
        self.owned_handle = False
        self.handle = None


def rename_by_handle(kernel, staged, target, *, posix):
    # Flags=3 deliberately does not override read-only permissions.
    handle = open_native(kernel, staged, access=0x10000)
    try:
        encoded = str(target).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(encoded))
        info = RenameInfo.from_buffer(buffer)
        info.flags, info.root, info.length = 3 if posix else 1, None, len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.name.offset,
                       encoded, len(encoded))
        checked(kernel.SetFileInformationByHandle(
            handle, 22 if posix else 3, buffer, len(buffer)))
    finally:
        kernel.CloseHandle(handle)


def child(writer, target):
    kernel = api()
    staged = target.with_name("next.json")
    with staged.open("xb") as stream:
        stream.write(NEW)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if writer == "os.replace":
            os.replace(staged, target)
        elif writer == "MoveFileExW":
            checked(kernel.MoveFileExW(str(staged), str(target), 1))
        elif writer == "ReplaceFileW":
            checked(kernel.ReplaceFileW(str(target), str(staged), None, 0, None, None))
        else:
            rename_by_handle(kernel, staged, target, posix=writer == "FileRenameInfoEx")
    except OSError as error:
        print(json.dumps({"published": False, "winerror": error.winerror,
                          "errno": error.errno}, sort_keys=True), flush=True)
        return 2
    print(json.dumps({"published": True}, sort_keys=True), flush=True)
    return 0


def describe(path):
    try:
        raw = path.read_bytes()
        stat = path.stat()
        return {"bytes": raw.decode("ascii"), "file_id": [stat.st_dev, stat.st_ino]}
    except OSError as error:
        return {"error": type(error).__name__, "winerror": error.winerror}


def main():
    if os.name != "nt":
        print(json.dumps({"status": "NOT_RUN", "reason": "Windows is required"}))
        return 2
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        writer = sys.argv[2]
        if writer not in WRITERS:
            raise ValueError("Unknown diagnostic writer")
        return child(writer, Path(sys.argv[3]))
    if len(sys.argv) != 1:
        raise ValueError("This diagnostic takes no options")
    kernel = api()
    start = time.monotonic()
    rows, failures = [], []
    print(json.dumps({
        "platform": sys.getwindowsversion().platform_version,
        "python": sys.version,
        "pointer_bytes": ctypes.sizeof(ctypes.c_void_p),
        "rename_name_offset": RenameInfo.name.offset,
        "rename_structure_bytes": ctypes.sizeof(RenameInfo),
    }, sort_keys=True), flush=True)
    with tempfile.TemporaryDirectory(prefix="carr-json-replace-") as temporary:
        root = Path(temporary)
        for reader_kind in READERS:
            for writer in WRITERS:
                directory = root / (reader_kind + "-" + writer.replace(".", "-"))
                directory.mkdir()
                target = directory / "стан.json"
                target.write_bytes(OLD)
                reader = Reader(kernel, target, reader_kind)
                row = {"reader": reader_kind, "writer": writer}
                try:
                    row["handle_before"] = reader.snapshot()
                    remaining = 120.0 - (time.monotonic() - start)
                    if remaining <= 0:
                        raise TimeoutError("Overall diagnostic budget expired")
                    result = subprocess.run(
                        [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                         "--child", writer, str(target)],
                        capture_output=True, text=True, encoding="utf-8",
                        timeout=min(10.0, remaining), close_fds=True, check=False)
                    if result.returncode not in (0, 2):
                        raise RuntimeError("Owned child failed: " + result.stderr[:2000])
                    report = json.loads(result.stdout)
                    if report.get("published") != (result.returncode == 0):
                        raise RuntimeError("Child publication report contradicts its exit")
                    row["child"] = report
                    row["handle_after"] = reader.snapshot()
                    raw = reader.read()
                    row["reader_bytes"] = None if raw is None else raw.decode("ascii")
                    row["target_while_open"] = describe(target)
                    row["staged_while_open"] = describe(directory / "next.json")
                    if raw is not None and raw != OLD:
                        failures.append([reader_kind, writer, "reader snapshot changed"])
                    if row["handle_before"]:
                        if row["handle_before"]["inheritable"]:
                            failures.append([reader_kind, writer, "inheritable reader"])
                        if row["handle_before"]["file_id"] != row["handle_after"]["file_id"]:
                            failures.append([reader_kind, writer, "reader identity changed"])
                    expected = NEW if report["published"] else OLD
                    if row["target_while_open"].get("bytes") != expected.decode("ascii"):
                        failures.append([reader_kind, writer, "unexpected target bytes"])
                    if reader_kind == "closed" and not report["published"]:
                        failures.append([reader_kind, writer, "uncontended baseline failed"])
                    if reader_kind.startswith(("raw-", "fd-")) and writer == "FileRenameInfoEx":
                        if not report["published"]:
                            failures.append([reader_kind, writer, "POSIX rename candidate failed"])
                    if reader_kind == "ordinary" and writer == "os.replace":
                        if report["published"] or report.get("winerror") not in (5, 32, 33):
                            failures.append([reader_kind, writer, "negative control failed"])
                finally:
                    reader.close()
                row["target_after_close"] = describe(target)
                if row["target_after_close"].get("bytes") != expected.decode("ascii"):
                    failures.append([reader_kind, writer, "target changed after reader close"])
                if report["published"] and (directory / "next.json").exists():
                    failures.append([reader_kind, writer, "successful rename left staged source"])
                rows.append(row)
                print(json.dumps(row, sort_keys=True), flush=True)
    print(json.dumps({"status": "FAIL" if failures else "PASS",
                      "cases": len(rows), "failures": failures}, sort_keys=True), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
