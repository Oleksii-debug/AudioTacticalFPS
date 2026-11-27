"""Isolated Windows x64 diagnostic; NOT an adopted production backend.

Uses NtQueryInformationByName(FileStatInformation=68), without a fallback.
Only normalized absolute DOS-drive and UNC paths are admitted. Native results,
including IO_STATUS_BLOCK.Information, are exposed for real-Windows measurement.
The optional dont_reparse=False mode is explicitly diagnostic: it must not be
treated as evidence that parent reparse points were rejected.
"""

from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
import errno
import json
import ntpath
import os
import stat
import sys


NTSTATUS = ctypes.c_int32
ULONG = ctypes.c_uint32
USHORT = ctypes.c_uint16
WCHAR = ctypes.c_uint16
HANDLE = ctypes.c_void_p

OBJ_CASE_INSENSITIVE = 0x00000040
OBJ_DONT_REPARSE = 0x00001000
FILE_STAT_INFORMATION_CLASS = 68
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_DEVICE = 0x00000040
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", USHORT),
        ("MaximumLength", USHORT),
        ("Buffer", ctypes.POINTER(WCHAR)),
    ]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ULONG),
        ("RootDirectory", HANDLE),
        ("ObjectName", ctypes.POINTER(UNICODE_STRING)),
        ("Attributes", ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_UNION(ctypes.Union):
    _fields_ = [("Status", NTSTATUS), ("Pointer", ctypes.c_void_p)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("u", _IO_STATUS_UNION), ("Information", ctypes.c_size_t)]


class FILE_STAT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("FileId", ctypes.c_int64),
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("AllocationSize", ctypes.c_int64),
        ("EndOfFile", ctypes.c_int64),
        ("FileAttributes", ULONG),
        ("ReparseTag", ULONG),
        ("NumberOfLinks", ULONG),
        ("EffectiveAccess", ULONG),
    ]


def abi_layout() -> dict:
    """Report and verify the Windows x64 layout without loading any DLL."""
    structures = (UNICODE_STRING, OBJECT_ATTRIBUTES, IO_STATUS_BLOCK,
                  FILE_STAT_INFORMATION)
    report = {
        "pointer_bytes": ctypes.sizeof(ctypes.c_void_p),
        "structures": {
            cls.__name__: {
                "size": ctypes.sizeof(cls),
                "alignment": ctypes.alignment(cls),
                "offsets": {name: getattr(cls, name).offset
                            for name, *_ in cls._fields_},
            }
            for cls in structures
        },
    }
    expected = {
        UNICODE_STRING: (16, {"Length": 0, "MaximumLength": 2, "Buffer": 8}),
        OBJECT_ATTRIBUTES: (48, {
            "Length": 0, "RootDirectory": 8, "ObjectName": 16,
            "Attributes": 24, "SecurityDescriptor": 32,
            "SecurityQualityOfService": 40,
        }),
        IO_STATUS_BLOCK: (16, {"u": 0, "Information": 8}),
        FILE_STAT_INFORMATION: (72, {
            "FileId": 0, "CreationTime": 8, "LastAccessTime": 16,
            "LastWriteTime": 24, "ChangeTime": 32, "AllocationSize": 40,
            "EndOfFile": 48, "FileAttributes": 56, "ReparseTag": 60,
            "NumberOfLinks": 64, "EffectiveAccess": 68,
        }),
    }
    if report["pointer_bytes"] != 8:
        raise RuntimeError("This isolated diagnostic supports Windows x64 only")
    for cls, (size, offsets) in expected.items():
        if ctypes.sizeof(cls) != size or any(
            getattr(cls, name).offset != offset for name, offset in offsets.items()
        ):
            raise RuntimeError(f"Unexpected native ABI for {cls.__name__}: {report}")
    if ctypes.sizeof(NTSTATUS) != 4 or ctypes.sizeof(ULONG) != 4:
        raise RuntimeError("NTSTATUS and ULONG must be exactly 32 bits")
    return report


def nt_path(path: str | os.PathLike[str]) -> str:
    """Map a normalized absolute DOS/UNC spelling to an NT object name.

    This is a deliberately bounded diagnostic contract, not a Win32 path parser.
    No filesystem lookup, resolving of aliases, or QueryDosDevice call is made.
    Relative, dot-component, device, ADS, and ambiguous trailing-dot/space paths
    are rejected instead of silently changing their meaning.
    """
    text = os.fspath(path)
    if not isinstance(text, str):
        raise TypeError("A Unicode path is required")
    if not text or "\x00" in text:
        raise ValueError("An absolute path without NUL is required")
    text = text.replace("/", "\\")
    if text.startswith("\\\\?\\"):
        text = text[4:]
        if text[:4].upper() == "UNC\\":
            text = "\\\\" + text[4:]
        elif len(text) < 3 or text[1:3] != ":\\":
            raise ValueError("Unsupported extended device namespace")
    if text.startswith("\\\\.\\") or text.startswith("\\??\\"):
        raise ValueError("Device/native namespace input is not admitted")
    drive, rest = ntpath.splitdrive(text)
    if drive.startswith("\\\\"):
        server_share = drive[2:].split("\\")
        if len(server_share) != 2 or not all(server_share):
            raise ValueError("UNC path must include a server and share")
        prefix_parts = server_share
        native = "\\??\\UNC\\" + text[2:]
    elif (len(drive) == 2 and drive[0].isascii() and drive[0].isalpha()
          and drive[1] == ":" and rest.startswith("\\")):
        prefix_parts = []
        native = "\\??\\" + text
    else:
        raise ValueError("A fully qualified DOS-drive or UNC path is required")
    # Trailing separator is accepted for a directory. Interior empty components
    # and ambiguous Win32 spellings are not silently normalized.
    tail = rest[1:] if rest.startswith("\\") else rest
    if tail.endswith("\\"):
        tail = tail[:-1]
    parts = prefix_parts + (tail.split("\\") if tail else [])
    for part in parts:
        if (not part or part in (".", "..") or part[-1:] in (".", " ")
                or any(char in '<>:"|?*' or ord(char) < 32 for char in part)):
            raise ValueError("Unsupported or ambiguous path component")
        device_name = part.partition(".")[0].upper()
        if (device_name in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or (device_name[:3] in {"COM", "LPT"}
                    and device_name[3:] in "123456789\u00b9\u00b2\u00b3"
                    and len(device_name) == 4)):
            raise ValueError("Reserved DOS device component is not admitted")
    encoded = native.encode("utf-16-le", errors="strict")
    if len(encoded) > 65532:
        raise ValueError("NT path is too long for a terminated UNICODE_STRING")
    return native


def _unicode_buffer(text: str):
    encoded = text.encode("utf-16-le", errors="strict")
    if not text or "\x00" in text or len(encoded) > 65532:
        raise ValueError("Invalid UNICODE_STRING length")
    storage = (WCHAR * (len(encoded) // 2 + 1)).from_buffer_copy(encoded + b"\0\0")
    value = UNICODE_STRING(len(encoded), len(encoded) + 2,
                           ctypes.cast(storage, ctypes.POINTER(WCHAR)))
    return value, storage


@dataclass(frozen=True)
class FileObservation:
    st_mode: int
    st_size: int
    st_file_attributes: int
    allocation_size: int
    file_id: int
    reparse_tag: int
    number_of_links: int
    effective_access: int
    ntstatus: int
    io_status: int
    information: int
    dont_reparse: bool


class NativeMetadata:
    """Bind one explicit native policy; missing APIs/errors never fall back."""

    def __init__(self, *, dont_reparse: bool = True):
        abi_layout()
        if os.name != "nt":
            raise OSError(errno.ENOSYS, "Native metadata probe requires Windows")
        if type(dont_reparse) is not bool:
            raise TypeError("dont_reparse must be an explicit boolean")
        self.dont_reparse = dont_reparse
        self._dll = ctypes.WinDLL("ntdll", use_last_error=True)
        try:
            self._query = self._dll.NtQueryInformationByName
            self._to_dos_error = self._dll.RtlNtStatusToDosError
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "Required ntdll export is unavailable") from error
        self._query.argtypes = [ctypes.POINTER(OBJECT_ATTRIBUTES),
                                ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
                                ULONG, ctypes.c_int32]
        self._query.restype = NTSTATUS
        self._to_dos_error.argtypes = [NTSTATUS]
        self._to_dos_error.restype = ULONG

    def __call__(self, path: str | os.PathLike[str]) -> FileObservation:
        original = os.fspath(path)
        text = nt_path(path)
        name, storage = _unicode_buffer(text)
        flags = OBJ_CASE_INSENSITIVE | (OBJ_DONT_REPARSE if self.dont_reparse else 0)
        attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), None,
                                       ctypes.pointer(name), flags, None, None)
        io_status = IO_STATUS_BLOCK()
        result = FILE_STAT_INFORMATION()
        status = int(self._query(ctypes.byref(attributes), ctypes.byref(io_status),
                                 ctypes.byref(result), ctypes.sizeof(result),
                                 FILE_STAT_INFORMATION_CLASS))
        # Keep both the UTF-16 storage and its descriptor alive through the call.
        _ = storage
        if status != 0:
            error = ctypes.WinError(int(self._to_dos_error(status)))
            error.filename = original
            error.ntstatus = status & 0xFFFFFFFF
            error.io_status = int(io_status.Status) & 0xFFFFFFFF
            error.information = int(io_status.Information)
            error.nt_path = text
            error.dont_reparse = self.dont_reparse
            raise error
        # Microsoft specifies the non-PENDING return status as authoritative.
        # Information is request-dependent; record it, do not invent a requirement
        # that it equal sizeof(FILE_STAT_INFORMATION) before the native probe.
        attrs = int(result.FileAttributes)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT or result.ReparseTag:
            error = OSError(errno.ELOOP, "Reparse point is not admitted", original)
            error.reparse_tag = int(result.ReparseTag)
            error.information = int(io_status.Information)
            raise error
        if attrs & FILE_ATTRIBUTE_DEVICE or result.EndOfFile < 0 or result.AllocationSize < 0:
            raise OSError(errno.EIO, "Unsupported node or invalid native size", original)
        mode = stat.S_IFDIR if attrs & FILE_ATTRIBUTE_DIRECTORY else stat.S_IFREG
        return FileObservation(mode, int(result.EndOfFile), attrs,
                               int(result.AllocationSize), int(result.FileId),
                               int(result.ReparseTag), int(result.NumberOfLinks),
                               int(result.EffectiveAccess), status,
                               int(io_status.Status) & 0xFFFFFFFF,
                               int(io_status.Information), self.dont_reparse)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--allow-reparse-lookup", action="store_true",
                        help="Diagnostic comparison only; no parent-reparse guarantee")
    parser.add_argument("--abi-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps({"abi": abi_layout()}, ensure_ascii=True))
    if args.abi_only:
        return 0
    observe = NativeMetadata(dont_reparse=not args.allow_reparse_lookup)
    failed = False
    for path in args.paths:
        try:
            item = {"path": path, "nt_path": nt_path(path),
                    "result": asdict(observe(path))}
        except (OSError, ValueError, TypeError) as error:
            failed = True
            item = {"path": path, "error": type(error).__name__, "message": str(error),
                    **{key: getattr(error, key, None) for key in
                       ("winerror", "errno", "ntstatus", "io_status", "information",
                        "nt_path", "dont_reparse", "reparse_tag")}}
        print(json.dumps(item, ensure_ascii=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
